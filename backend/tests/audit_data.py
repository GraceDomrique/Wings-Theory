"""Data completeness audit: is anything the owner provided actually missing?

Compares the source spreadsheets row-by-row against the seeded database and
reports every discrepancy in both directions.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wt import db                                    # noqa: E402
from wt.units import normalise_label                 # noqa: E402

SEED = Path(__file__).resolve().parent.parent / "seed_data"


def main() -> int:
    conn = db.get_conn()
    print("\nDATA COMPLETENESS AUDIT\n" + "=" * 74)

    # ---- 1. every inventory row landed --------------------------------
    src_inv = []
    with (SEED / "inventory_tidy.csv").open(encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            src_inv.append((r["Product Name"].strip(), float(r["Product Count"])))

    db_inv = {r["name"]: r for r in conn.execute(
        "SELECT name, purchase_unit, pack_size, on_hand_base, base_unit FROM ingredient "
        "WHERE kind='raw'")}

    missing = [n for n, _ in src_inv if n not in db_inv]
    print(f"\n1. STOCK ITEMS  source={len(src_inv)}  in database={len(db_inv)}")
    print(f"   missing from database: {missing or 'NONE'}")
    extra = [n for n in db_inv if n not in {x[0] for x in src_inv}]
    print(f"   invented (not in source): {extra or 'NONE'}")

    # quantity fidelity
    bad_qty = []
    for name, qty in src_inv:
        row = db_inv.get(name)
        if not row:
            continue
        if row["pack_size"] is None:
            continue
        expected = qty * row["pack_size"]
        # yield factor is applied on top; recompute from the row
        yf = conn.execute("SELECT yield_factor FROM ingredient WHERE name=?",
                          (name,)).fetchone()["yield_factor"]
        expected *= yf
        if abs(expected - row["on_hand_base"]) > 0.01:
            bad_qty.append((name, qty, row["on_hand_base"], expected))
    print(f"   quantity mismatches: {len(bad_qty)}")
    for b in bad_qty[:5]:
        print(f"      {b}")

    # ---- 2. items with no pack size (cannot deduct) --------------------
    nopack = conn.execute(
        "SELECT name, purchase_unit, base_unit FROM ingredient "
        "WHERE kind='raw' AND pack_size IS NULL ORDER BY name").fetchall()
    print(f"\n2. STOCK ITEMS THAT CANNOT DEDUCT (no pack size): {len(nopack)}")
    for r in nopack:
        print(f"   {r['name']:<24} {r['purchase_unit']} -> {r['base_unit']}")

    # ---- 3. every BOM row landed --------------------------------------
    src_bom = {}
    with (SEED / "product_item_breakdown_tidy.csv").open(encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            src_bom.setdefault(r["Product / Recipe"].strip(), []).append(
                r["Ingredient"].strip())
    n_src_lines = sum(len(v) for v in src_bom.values())

    n_mapped = conn.execute("SELECT COUNT(*) AS n FROM recipe_line").fetchone()["n"]
    n_queue = conn.execute(
        "SELECT COUNT(*) AS n FROM unmapped_recipe_line").fetchone()["n"]
    print(f"\n3. RECIPE LINES  source={n_src_lines}  "
          f"mapped={n_mapped}  queued={n_queue}  accounted={n_mapped + n_queue}")
    print(f"   recipes: source={len(src_bom)}  database="
          f"{conn.execute('SELECT COUNT(*) AS n FROM recipe').fetchone()['n']}")

    # which source lines are in neither place?
    lost = []
    for recipe, labels in src_bom.items():
        for lab in labels:
            hit = conn.execute(
                """
                SELECT 1 FROM recipe_line rl JOIN recipe r ON r.id=rl.recipe_id
                 WHERE rl.display_label=? LIMIT 1
                """, (lab,)).fetchone()
            if hit:
                continue
            hit = conn.execute(
                "SELECT 1 FROM unmapped_recipe_line WHERE source_ingredient_label=? LIMIT 1",
                (lab,)).fetchone()
            if not hit:
                lost.append((recipe, lab))
    print(f"   source lines in NEITHER table: {len(lost)}")
    for l in lost[:12]:
        print(f"      {l[0][:34]:<34} {l[1]}")

    # ---- 4. distinct BOM ingredients never resolved --------------------
    src_ings = sorted({lab for v in src_bom.values() for lab in v})
    unresolved = sorted({r["source_ingredient_label"] for r in conn.execute(
        "SELECT DISTINCT source_ingredient_label FROM unmapped_recipe_line WHERE state='open'")})
    print(f"\n4. DISTINCT BOM INGREDIENTS  source={len(src_ings)}  "
          f"unresolved={len(unresolved)}")

    # do any of these exist in the stock list under a different name?
    stock_norm = {normalise_label(n): n for n in db_inv}
    could_map = []
    for lab in unresolved:
        n = normalise_label(lab)
        for sk, sn in stock_norm.items():
            if sk and (sk in n or n in sk) and len(sk) > 3:
                could_map.append((lab, sn))
                break
    print(f"   of those, plausibly present in stock under another name: {len(could_map)}")
    for c in could_map:
        print(f"      {c[0][:38]:<38} ~ {c[1]}")

    # ---- 5. blocked menu items ----------------------------------------
    print("\n5. MENU GATE")
    for r in conn.execute(
        "SELECT name, sellable, block_reason FROM v_menu_item_availability "
        "ORDER BY sellable, name"
    ):
        print(f"   {'OK ' if r['sellable'] else 'BLK'} {r['name'][:34]:<34} "
              f"{r['block_reason'] or ''}")

    print("\n" + "=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
