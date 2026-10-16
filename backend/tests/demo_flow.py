"""End-to-end walk of the deduction path against the seeded database.

Run:  python -m tests.demo_flow

Exercises the HTTP layer, not the domain functions, so route wiring, auth and
the completeness gate are all in the path. Writes real orders into whatever
database WT_DB/WT_ENV currently resolve to -- refuses to run at all under
WT_ENV=production, the same way wt/demo_seed.py and scripts/generate_demo_
history.py refuse, since this is exactly the kind of synthetic write that
must never land in the shop's real data.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wt import db                                   # noqa: E402


def line(ch="-", n=74):
    print(ch * n)


def main() -> int:
    if db.environment() == "production":
        print("REFUSING TO PROCEED: this writes real orders and must never run "
              "under WT_ENV=production.", file=sys.stderr)
        return 1

    from fastapi.testclient import TestClient
    from wt.api import app
    client = TestClient(app)
    conn = db.get_conn()

    print("\nWINGS THEORY — Phase 2 end-to-end\n")
    line("=")

    # --- auth -------------------------------------------------------------
    cashier = client.post("/auth/login", json={"username": "cashier1",
                                               "password": "wingstheory"}).json()
    kitchen = client.post("/auth/login", json={"username": "kitchen1",
                                               "password": "wingstheory"}).json()
    owner = client.post("/auth/login", json={"username": "owner",
                                             "password": "wingstheory"}).json()
    H_CASH = {"Authorization": f"Bearer {cashier['token']}"}
    H_KIT = {"Authorization": f"Bearer {kitchen['token']}"}
    H_OWN = {"Authorization": f"Bearer {owner['token']}"}
    print(f"signed in: {cashier['display_name']} ({cashier['role']}), "
          f"{kitchen['display_name']} ({kitchen['role']}), "
          f"{owner['display_name']} ({owner['role']})")

    # --- role enforcement -------------------------------------------------
    line()
    r = client.get("/console/ingredients", headers=H_CASH)
    print(f"cashier -> /console/ingredients   {r.status_code} {r.json().get('message','')}")
    r = client.get("/console/queues/unmapped", headers=H_KIT)
    print(f"kitchen -> /console/queues/unmapped {r.status_code} {r.json().get('message','')}")
    r = client.get("/console/ingredients", headers=H_OWN)
    print(f"owner   -> /console/ingredients   {r.status_code} "
          f"({r.json()['count']} ingredients)")

    # --- the completeness gate -------------------------------------------
    line()
    menu = client.get("/pos/menu", headers=H_CASH).json()
    print(f"POS menu: {menu['sellable']} sellable, {menu['blocked']} blocked")
    blocked = [m for m in menu["items"] if not m["sellable"]][:3]
    for m in blocked:
        print(f"   blocked  {m['name'][:34]:<34} {m['block_reason']}")

    sellable = next(m for m in menu["items"]
                    if m["sellable"] and not m["is_unli"] and m["price"] > 50)
    pk = sellable["menu_item_pk"]
    avail = client.get(f"/pos/menu/{pk}/availability", headers=H_CASH).json()
    print(f"\n   {sellable['name']}: max {avail['max_qty']} servings, "
          f"limited by {avail['limiting_ingredient']}")

    # --- blocked item is refused at the door ------------------------------
    line()
    bad = client.post("/pos/orders", headers=H_CASH,
                      json={"table_label": "T-01",
                            "lines": [{"menu_item_pk": blocked[0]["menu_item_pk"], "qty": 1}]})
    print(f"punching a blocked item -> {bad.status_code}")
    print(f"   {bad.json()['detail']}")

    # --- happy path -------------------------------------------------------
    line()
    leaf = conn.execute(
        "SELECT i.id, i.name, i.on_hand_base, i.reserved_base, i.base_unit "
        "FROM v_recipe_leaf l JOIN ingredient i ON i.id=l.ingredient_id "
        "WHERE l.recipe_id=? ORDER BY l.qty_base DESC LIMIT 1",
        (sellable["recipe_id"],)).fetchone()

    def snapshot(tag: str) -> None:
        row = conn.execute(
            "SELECT on_hand_base, reserved_base FROM ingredient WHERE id=?",
            (leaf["id"],)).fetchone()
        n = conn.execute("SELECT COUNT(*) AS n FROM stock_ledger").fetchone()["n"]
        print(f"   {tag:<22} on_hand {row['on_hand_base']:>10.1f}  "
              f"reserved {row['reserved_base']:>8.1f}  ledger rows {n}")

    print(f"tracking '{leaf['name']}' through one order\n")
    snapshot("before")

    order = client.post("/pos/orders", headers=H_CASH,
                        json={"table_label": "T-06", "head_count": 2,
                              "lines": [{"menu_item_pk": pk, "qty": 2}]}).json()
    snapshot("after punch (reserve)")

    client.post(f"/kds/orders/{order['order_id']}/start", headers=H_KIT)
    snapshot("after kitchen start")

    # --- void after commit ------------------------------------------------
    line()
    order2 = client.post("/pos/orders", headers=H_CASH,
                         json={"table_label": "T-08",
                               "lines": [{"menu_item_pk": pk, "qty": 1}]}).json()
    client.post(f"/kds/orders/{order2['order_id']}/start", headers=H_KIT)
    snapshot("second order cooked")
    v = client.post(f"/pos/orders/{order2['order_id']}/void", headers=H_CASH,
                    json={"reason": "customer left", "restock": False}).json()
    snapshot("after void")
    print(f"   -> {v['ledger_entries']} compensating entries, restocked={v['restocked']}")
    trail = conn.execute(
        "SELECT reason, COUNT(*) AS n, ROUND(SUM(delta_base),1) AS net FROM stock_ledger "
        "WHERE ref_id=? GROUP BY reason ORDER BY MIN(id)", (order2["order_no"],)).fetchall()
    n_ing = conn.execute(
        "SELECT COUNT(*) AS n FROM v_recipe_leaf WHERE recipe_id=?",
        (sellable["recipe_id"],)).fetchone()["n"]
    print(f"   -> ledger trail for {order2['order_no']} ({n_ing} ingredients):")
    for t in trail:
        print(f"        {t['reason']:<14} x{t['n']:<3} net {t['net']:>9.1f}")
    print("      credit and wastage cancel out: the food is gone, but the ledger says why")

    # --- void before commit ----------------------------------------------
    line()
    order3 = client.post("/pos/orders", headers=H_CASH,
                         json={"table_label": "T-09",
                               "lines": [{"menu_item_pk": pk, "qty": 1}]}).json()
    snapshot("third order reserved")
    v = client.post(f"/pos/orders/{order3['order_id']}/void", headers=H_CASH,
                    json={"reason": "changed mind"}).json()
    snapshot("after void before cook")
    print(f"   -> {v['reservations_released']} holds released, "
          f"{v['ledger_entries']} ledger entries (no phantom deduction)")

    # --- admin queues -----------------------------------------------------
    line()
    q = client.get("/console/queues/unmapped", headers=H_OWN).json()
    print(f"Unmapped Ingredients queue: {q['count']} open")
    for reason, n in sorted(q["by_reason"].items(), key=lambda x: -x[1]):
        print(f"   {reason:<18} {n}")
    print("   sample:")
    for it in q["items"][:4]:
        print(f"     {it['source_ingredient_label'][:32]:<32} "
              f"from {it['source_recipe_name'][:26]:<26} blocks {it['blocks_menu_items']}")

    v = client.get("/console/queues/verification", headers=H_OWN).json()
    print(f"\nNeeds Verification queue: {v['count']} constants")
    for field, n in sorted(v["by_field"].items(), key=lambda x: -x[1]):
        print(f"   {field:<28} {n}")

    rd = client.get("/console/readiness", headers=H_OWN).json()
    line()
    print("readiness:", {k: rd[k] for k in
                         ("menu_items_sellable", "menu_items_blocked",
                          "unmapped_open", "needs_verification", "production_ready")})
    line("=")
    return 0


if __name__ == "__main__":
    sys.exit(main())
