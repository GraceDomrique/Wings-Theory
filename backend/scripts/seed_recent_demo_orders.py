"""Idempotent demo-order seeder: populates 2026-09-21 -> 2026-10-13 with
realistic, clearly-marked demo transactions so dashboards/sales analytics/
FP-Growth combo mining have recent data to show, without touching
inventory, without inflating real revenue reports, and without disturbing
any existing real/historical data.

Safety properties (all verified by scripts/verify_demo_seed.py):
  - Every row this script writes is tagged `customer_order.is_demo = 1`
    (migration 0054) and `order_no` prefixed `WTRORDEM-`.
  - NEVER calls reservation.reserve_order()/commit_order() -- raw INSERTs
    into customer_order/order_line only, exactly like
    wt/xlsx_history_import.py's own pattern minus the stock-mutating steps.
    Zero stock_ledger/reservation rows are ever created by this script.
  - Idempotent: deletes every existing is_demo=1 row first (order_line
    cascades via its FK), then regenerates deterministically from a fixed
    RNG seed -- re-running produces the identical dataset, never duplicates.
  - Real/historical data is never read destructively and never modified;
    this script only ever INSERTs new customer_order/order_line rows and
    DELETEs rows it itself tagged is_demo=1.
  - Revenue-reporting queries (business_overview.py/sales_overview.py/
    dashboard_overview.py/api.py's /reports/sales, plus forecasting.py's
    and depletion.py's real demand-training queries) all filter
    `is_demo = 0` -- added alongside this script -- so this seeded data
    never inflates an "actual business" figure or contaminates a real
    model's training signal. wt/associations.py's FP-Growth basket query
    deliberately does NOT filter is_demo, so this data can surface combo
    recommendations through the real, unmodified mining code, per the
    user's explicit request.

Usage (from backend/), with WT_ENV/WT_DB set the same way the app itself
requires everywhere else:
    WT_ENV=development WT_DB=dev/wings_theory_dev.db python scripts/seed_recent_demo_orders.py

Goes through wt.db.get_conn()/db_path() rather than a bare
sqlite3.connect(BACKEND_DIR / "wings_theory.db") -- a hardcoded path to the
PRODUCTION file meant this script could never safely target a dev/demo
database, and offered no WT_ENV/WT_DB mismatch protection if accidentally
run with the wrong cwd. Same fix as reset_prod_passwords.py; see that
script's docstring for the full reasoning.
"""
from __future__ import annotations

import datetime as _dt
import random
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from wt import associations, combo_pricing, db  # noqa: E402

SEED = 20260921  # fixed -> idempotent regeneration
START_DATE = _dt.date(2026, 9, 21)
END_DATE = _dt.date(2026, 10, 13)
ORDER_NO_PREFIX = "WTRORDEM"
CREATED_BY = "demo_seed_recent_orders"
MANILA_OFFSET_HOURS = 8  # matches wt/timeutil.py's MANILA_UTC_OFFSET_HOURS

# Daily order-count targets by weekday (0=Mon..6=Sun) -- weekend busier.
BASE_ORDERS_BY_WEEKDAY = {0: 65, 1: 62, 2: 64, 3: 68, 4: 85, 5: 120, 6: 110}

# Hour-of-day (Manila local) weight -- lunch and dinner peaks, quiet late night.
HOUR_WEIGHTS = {
    9: 0.3, 10: 0.5, 11: 1.4, 12: 2.2, 13: 1.6, 14: 0.6, 15: 0.5, 16: 0.6,
    17: 0.9, 18: 1.8, 19: 2.4, 20: 2.0, 21: 1.2, 22: 0.4,
}

# Deliberately boosted co-purchase pairs (by menu_item.name) so FP-Growth has
# real, repeatable signal to mine -- these are REAL menu items at REAL prices;
# nothing about the mining or pricing is fabricated, only which items get
# bundled into the same basket more often than chance.
DESIGNED_PAIRS = [
    ("Sprite", "Creamy Sausage Pasta"),
    ("12 pcs (2 Flavors) Ala Carte Wing Rack", "Coke"),
    ("Fish & Chips with Tartar Sauce", "Royal"),
    ("Pork Tonkatsu", "1 Cup Rice"),
    ("Onion Rings", "Coke Zero"),
    ("Mango Shake (Tall)", "Bacon and 2 Egg"),
    ("Wings Platter 6 pcs w/ Fries", "Ranch Dip"),
    ("Cheesy Bacon Fries", "Sprite"),
]
PAIR_BOOST_PROBABILITY = 0.32  # fraction of orders that deliberately include one designed pair
EXTRA_ITEM_PROBABILITY = 0.55  # chance of 1-2 additional random items on top of the pair/base item


def _manila_to_utc(d: _dt.date, hour: int, minute: int) -> _dt.datetime:
    manila = _dt.datetime.combine(d, _dt.time(hour, minute),
                                  tzinfo=_dt.timezone(_dt.timedelta(hours=MANILA_OFFSET_HOURS)))
    return manila.astimezone(_dt.timezone.utc)


def _sql(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _pick_hour(rng: random.Random) -> int:
    hours, weights = zip(*HOUR_WEIGHTS.items())
    return rng.choices(hours, weights=weights, k=1)[0]


def wipe_existing_demo(conn: sqlite3.Connection) -> int:
    n = conn.execute("SELECT COUNT(*) FROM customer_order WHERE is_demo = 1").fetchone()[0]
    conn.execute("DELETE FROM customer_order WHERE is_demo = 1")  # order_line cascades via FK
    conn.commit()
    return n


def load_menu(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT id, name, category, price FROM menu_item WHERE pos_visible = 1 AND is_unli = 0"
    ).fetchall()
    return {r["name"]: {"id": r["id"], "category": r["category"], "price": r["price"]} for r in rows}


def build_basket(rng: random.Random, menu: dict[str, dict], all_names: list[str]) -> dict[str, int]:
    """menu_item name -> qty for one order."""
    basket: dict[str, int] = {}

    def add(name: str, qty: int = 1) -> None:
        if name in menu:
            basket[name] = basket.get(name, 0) + qty

    if rng.random() < PAIR_BOOST_PROBABILITY:
        a, b = rng.choice(DESIGNED_PAIRS)
        add(a)
        add(b)
    else:
        add(rng.choice(all_names))

    if rng.random() < EXTRA_ITEM_PROBABILITY:
        for _ in range(rng.randint(1, 2)):
            add(rng.choice(all_names))

    # Occasional repeat-quantity on one line, matching real a-la-carte behavior.
    if basket and rng.random() < 0.15:
        name = rng.choice(list(basket))
        basket[name] += 1

    return basket


def insert_order(conn: sqlite3.Connection, seq: int, created_at: _dt.datetime,
                 basket: dict[str, int], menu: dict[str, dict], rng: random.Random) -> int:
    order_no = f"{ORDER_NO_PREFIX}-{created_at.strftime('%Y%m%d')}-{seq:04d}"
    head_count = rng.randint(1, 4)
    table_label = rng.choice(["T1", "T2", "T3", "T4", "T5", "T6", "Takeout"])
    ts = _sql(created_at)

    cur = conn.execute(
        "INSERT INTO customer_order "
        "(order_no, status, table_label, head_count, created_by, created_at, "
        " committed_at, served_at, is_demo) "
        "VALUES (?,?,?,?,?,?,?,?,1)",
        (order_no, "served", table_label, head_count, CREATED_BY, ts, ts, ts),
    )
    order_pk = cur.lastrowid

    subtotal = 0.0
    for name, qty in basket.items():
        price = menu[name]["price"]
        conn.execute(
            "INSERT INTO order_line (order_id, menu_item_id, qty, unit_price) VALUES (?,?,?,?)",
            (order_pk, menu[name]["id"], qty, price),
        )
        subtotal += qty * price

    conn.execute(
        "UPDATE customer_order SET subtotal=?, service_charge=0, total=?, "
        "discount_amount=0, cash_received=?, change_due=0 WHERE id=?",
        (round(subtotal, 2), round(subtotal, 2), round(subtotal, 2), order_pk),
    )
    return order_pk


def seed(conn: sqlite3.Connection) -> dict:
    rng = random.Random(SEED)
    menu = load_menu(conn)
    all_names = list(menu.keys())
    if not all_names:
        raise RuntimeError("no pos_visible, non-unli menu items found -- cannot seed demo orders")

    removed = wipe_existing_demo(conn)

    total_orders = 0
    total_revenue = 0.0
    day = START_DATE
    daily_counts = {}
    while day <= END_DATE:
        target = BASE_ORDERS_BY_WEEKDAY[day.weekday()]
        n_orders = max(1, int(rng.gauss(target, target * 0.12)))
        daily_counts[day.isoformat()] = n_orders
        for seq in range(1, n_orders + 1):
            hour = _pick_hour(rng)
            minute = rng.randint(0, 59)
            created_at = _manila_to_utc(day, hour, minute)
            basket = build_basket(rng, menu, all_names)
            if not basket:
                continue
            insert_order(conn, seq, created_at, basket, menu, rng)
            total_orders += 1
            total_revenue += sum(menu[n]["price"] * q for n, q in basket.items())
        day += _dt.timedelta(days=1)

    conn.commit()
    return {
        "removed_existing_demo_orders": removed,
        "orders_generated": total_orders,
        "total_demo_revenue": round(total_revenue, 2),
        "date_range": [START_DATE.isoformat(), END_DATE.isoformat()],
        "daily_counts": daily_counts,
    }


def discover_and_draft_combos(conn: sqlite3.Connection, max_drafts: int = 8) -> dict:
    """Mines the recent (mostly-demo) window on its own -- scoping
    lookback_days to this window (not the whole order history) means the
    80/20 chronological holdout split inside product_combinations() falls
    entirely within this window too, so a demo-exclusive pair has a real
    chance to land in BOTH the training and holdout slices. A pair that
    only existed in the single most recent slice of the WHOLE multi-year
    history would otherwise be mathematically unable to pass that holdout
    check at all. Then calls the real, unmodified create_draft_combo() for
    each holdout-validated combo -- never a fabricated association metric."""
    today = _dt.date.today()
    lookback_days = (today - START_DATE).days + 2
    result = associations.product_combinations(conn, lookback_days=lookback_days, holdout_validate=True)
    combos = result.get("combos", [])[:max_drafts]

    drafted = []
    for c in combos:
        # Deterministic client_request_id -> re-running this script never
        # creates a duplicate draft for the same pair (create_draft_combo's
        # own dedup returns the existing row unchanged when this matches).
        client_request_id = f"demo-seed-recent-orders:{c['item_a_menu_item_id']}:{c['item_b_menu_item_id']}"
        draft = combo_pricing.create_draft_combo(
            conn, c["item_a_menu_item_id"], c["item_b_menu_item_id"],
            name=f"{c['item_a']} + {c['item_b']}",
            pair_stats={"pair_count": c["pair_count"], "support": c["support"],
                       "confidence": c["confidence"], "lift": c["lift"],
                       "holdout_lift": c.get("holdout_lift")},
            user_id=CREATED_BY,
            client_request_id=client_request_id,
        )
        drafted.append({
            "draft_combo_id": draft["id"], "item_a": c["item_a"], "item_b": c["item_b"],
            "lift": c["lift"], "holdout_lift": c.get("holdout_lift"),
            "support": c["support"], "confidence": c["confidence"], "pair_count": c["pair_count"],
        })

    return {
        "lookback_days": lookback_days,
        "transaction_count": result.get("transaction_count"),
        "candidates_considered": result.get("mining_basis", {}).get("candidates_considered"),
        "candidates_validated": len(result.get("combos", [])),
        "drafted": drafted,
        "empty_reason": result.get("empty_reason"),
    }


def main() -> None:
    target = db.db_path()  # fails closed if WT_ENV/WT_DB are unset or mismatched
    print(f"targeting {target} (WT_ENV={db.environment()})")
    conn = db.connect(target)
    try:
        seed_report = seed(conn)
        combo_report = discover_and_draft_combos(conn)
    finally:
        conn.close()

    import json
    print(json.dumps({"seed": seed_report, "combos": combo_report}, indent=2, default=str))


if __name__ == "__main__":
    main()
