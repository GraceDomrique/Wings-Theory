"""Normalise the owner's raw spreadsheet exports into the tidy CSVs the
importer consumes.

Run:  python -m wt.raw_import

The raw files are section-header exports (a "Wet Foods,,,," row starts a
section, a blank row separates records, a leading BOM sits on the first
cell) rather than one-record-per-row CSVs. This module is the one place that
understands that shape; everything downstream (seed.py, mapping.py,
gapfill.py) keeps consuming the same tidy format as before.

Two blocks in the breakdown sheet are deliberately NOT emitted as generic
recipe rows, because they are not recipes in the usual sense:

  "<Name> (Sub-Recipe Definition)"   percentage-of-batch formulas, not fixed
                                      quantities — handled by hand-authored
                                      tables in gapfill.py (SUB_RECIPE_DEFS)
  "Unlimited Wings - 14 Flavors"     per-kg-of-wings ratios used to
                                      reconcile the existing flavour
                                      sub-recipes (mapping.py), not a BOM for
                                      a menu item — Unlimited Wings has no
                                      recipe_id and never did; sauce for an
                                      unli order was always out of scope for
                                      Phase 2's per-head lambda vector.

Both are logged, not silently dropped, so a future re-run of this script
makes it obvious if the source file's structure changes again.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parent.parent / "seed_data"
RAW_INVENTORY = SEED_DIR / "Inventory_List_Tracker - Inventory.csv"
RAW_BREAKDOWN = SEED_DIR / "Inventory_List_Tracker - Product Item Breakdown.csv"
OUT_INVENTORY = SEED_DIR / "inventory_tidy.csv"
OUT_BREAKDOWN = SEED_DIR / "product_item_breakdown_tidy.csv"

SPECIAL_BLOCK_RE = re.compile(r"\(Sub-Recipe Definition\)$|^Unlimited Wings", re.I)

# "Additional Ingredients (Recipe-Required)" is a real inventory section, but
# it mixes perishables and pantry goods that need different wet/dry
# classification (drives category, lead time, and the wet/dry KPI split) —
# same distinction the original Wet Foods / Dry Foods sections already make.
ADDITIONAL_WET = {
    "Chicken Breast/Thigh (raw, boneless)", "Pork Chop (Taiwanese cut, boneless)",
    "Fresh Mushrooms", "Fresh Milk", "Cucumber", "Onion", "Plain Yogurt",
    "Shredded Cabbage", "Spring Onion",
}


def _fix_mojibake(s: str) -> str:
    """Best-effort repair of UTF-8-decoded-as-Latin-1 double encoding
    ('sautÃ©ing' -> 'sautéing'). Falls back to the original string if the
    round-trip doesn't produce clean text, since guessing wrong here would
    corrupt a name silently."""
    if not s or "Ã" not in s and "â" not in s and "Â" not in s:
        return s
    try:
        fixed = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return fixed if "�" not in fixed else s


def _clean(s: str) -> str:
    return _fix_mojibake((s or "").strip())


def parse_inventory(path: Path = RAW_INVENTORY) -> list[dict]:
    rows: list[dict] = []
    section = None
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for raw in csv.reader(fh):
            if not raw or all(not c.strip() for c in raw):
                continue
            name = _clean(raw[0])
            count = raw[1].strip() if len(raw) > 1 else ""
            if not count:
                # section header ("Wet Foods,,,,") — no count means no product
                section = name
                continue
            if section is None:
                continue  # stray row before the first section header
            unit = _clean(raw[2]) if len(raw) > 2 else ""
            price = raw[3].strip() if len(raw) > 3 else "0"
            expiry = _clean(raw[4]) if len(raw) > 4 else "N/A"
            if expiry.upper().startswith("N/A"):
                expiry = "N/A"
            category = section
            if section.startswith("Additional"):
                category = "Wet Foods" if name in ADDITIONAL_WET else "Dry Foods"
            rows.append({
                "Category": category, "Product Name": name, "Product Count": count,
                "Unit": unit, "Price (PHP)": price, "Expiry Date": expiry,
            })
    return rows


def parse_breakdown(path: Path = RAW_BREAKDOWN) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    skipped: list[str] = []
    current = None
    current_is_special = False
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # header row: "Product Name:,Items/Ingredients Needed:,Qty.,,"
        for raw in reader:
            if not raw or all(not c.strip() for c in raw):
                continue
            name_cell = _clean(raw[0])
            ing_cell = _clean(raw[1]) if len(raw) > 1 else ""
            if name_cell and not ing_cell:
                # product/recipe header row
                current = name_cell
                current_is_special = bool(SPECIAL_BLOCK_RE.search(current))
                if current_is_special:
                    skipped.append(current)
                continue
            if current is None or current_is_special or not ing_cell:
                continue
            qty = _clean(raw[2]) if len(raw) > 2 else ""
            note = _clean(raw[4]) if len(raw) > 4 else ""
            rows.append({
                "Product / Recipe": current, "Ingredient": ing_cell,
                "Quantity": qty, "Note": note,
            })
    return rows, skipped


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    inv_rows = parse_inventory()
    _write_csv(OUT_INVENTORY, inv_rows,
               ["Category", "Product Name", "Product Count", "Unit", "Price (PHP)", "Expiry Date"])
    print(f"inventory:  {len(inv_rows)} rows -> {OUT_INVENTORY.name}")

    bom_rows, skipped = parse_breakdown()
    _write_csv(OUT_BREAKDOWN, bom_rows, ["Product / Recipe", "Ingredient", "Quantity", "Note"])
    n_recipes = len({r["Product / Recipe"] for r in bom_rows})
    print(f"breakdown:  {len(bom_rows)} lines across {n_recipes} recipes -> {OUT_BREAKDOWN.name}")
    print(f"skipped as special blocks (handled by hand-authored tables instead): {len(skipped)}")
    for s in skipped:
        print(f"  - {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
