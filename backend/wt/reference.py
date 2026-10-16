"""Configuration for the flavour catalogue and the unlimited-wings portion
settings — the two things no spreadsheet ("4. Master Ingredient Inventory",
"6. Menu-to-Ingredient Mapping") actually specifies for itself.

Why this module exists
----------------------
Migrations run against an EMPTY database on a fresh install — db.reset()
applies every .sql file before seed.py imports a single ingredient — so a
migration cannot configure rows that do not exist yet. This module does that
configuration at seed time; both converge on the state asserted by
TestReferenceAlignment.

Everything here is idempotent and safe to re-run.

2026-09 note — retired since the switch to the Menu & Ingredient Audit
workbook (see xlsx_menu_audit_import.py)
------------------------------------------------------------------------
This module used to carry a second, large layer of hand-transcribed
corrections on top of the old CSV import: SKU_BENCHMARKS (prices for SKUs
the old stock sheet didn't price), RECIPE_QTY_FIXES / RECIPE_INGREDIENT_
SWAPS / RECIPE_LINE_ADDITIONS / RECIPE_LINE_REMOVALS (line-by-line
corrections against a separate PDF reference document), and two named fixes
(KOREAN_BOWL_FIX, CABBAGE_FIX) for specific wrong ingredients.

All of it is gone, not merely renamed — verified against the real, current
workbook, not assumed:

  * Every ingredient this database uses is priced directly by
    '4. Master Ingredient Inventory' (Purchase Price (₱)); there is no
    "unpriced SKU" gap left for SKU_BENCHMARKS to fill.
  * Every recipe's composition and quantities come directly from
    '6. Menu-to-Ingredient Mapping', already reconciled by the owner's own
    audit — there is no separate, less-trusted source left to correct it
    against. Concretely: Classic Buffalo now uses a generic "Hot Sauce" 45 g
    line (not a "Buffalo Hot Sauce" SKU); Spicy Korean Wing Bowl no longer
    references any flavour sub-recipe at all (20 raw ingredients — Gochujang,
    Soy Sauce, Kimchi, etc. — directly on its own recipe), so KOREAN_BOWL_FIX
    would now corrupt that recipe rather than correct it; Taiwanese Pork Chop
    uses Pickled Mustard Greens + a Stir-Fry Vegetable Mix, not a Shredded
    Cabbage swap, so CABBAGE_FIX no longer applies either.

What is still here
----------------------
FLAVOR_CONFIG — the customer-facing flavour name, included/special tier, and
premium add-on price for each of the 14 flavour sub-recipes. Nothing in
either workbook sheet specifies these three things (sheet 6 only lists
ingredient quantities), so they remain this module's own configuration, plus
the two unlimited-portion app_settings (pieces/sauce grams per head), which
likewise have no workbook source and are left at their previous, still-live
values rather than being silently changed on a hunch — the workbook's
"5 g per wing" per-order sauce basis (Sulit Deal 99: 3 pcs / 15 g) is a
different, informational figure, not a stated replacement for what
refills.py actually applies per unlimited-wings round; changing it is a
business decision for the owner, not something this import makes for them.
"""
from __future__ import annotations

import sqlite3

from .db import write_tx

# ---------------------------------------------------------------------------
# Flavour configuration — customer-facing name, included/special tier, and
# premium add-on price. Not sourced from either workbook sheet (see module
# docstring) — this is this module's own configuration, unchanged from the
# CSV-era pipeline since the flavour catalogue itself (which 14 sauces, and
# which of them are "special") hasn't changed.
#
# The four premium prices are INITIAL SYSTEM RECOMMENDATIONS, not a
# business-supplied price. They stay editable through the console
# (recipe.write), and the price actually charged is snapshotted onto each
# refill at serve time, so changing one never restates what a customer was
# already charged.
#
#   sauce ingredient name -> (customer-facing name, type, add-on price)
# ---------------------------------------------------------------------------
FLAVOR_CONFIG: dict[str, tuple[str, str, float]] = {
    "Classic Buffalo Sauce":  ("Classic Buffalo",              "included", 0.0),
    "Mango Habanero Sauce":   ("Mango Habanero",               "included", 0.0),
    "Salted Egg Sauce":       ("Salted Egg",                   "included", 0.0),
    "Lemon Glazed Sauce":     ("Lemon Glazed",                 "included", 0.0),
    "Original Dry Rub":       ("Original",                     "included", 0.0),
    "Korean BBQ Wing Sauce":  ("Korean BBQ",                   "included", 0.0),
    "Garlic Parmesan Sauce":  ("Garlic Parmesan",              "included", 0.0),
    "Soy Garlic Sauce":       ("Soy Garlic",                   "included", 0.0),
    "Teriyaki Sauce":         ("Teriyaki",                     "included", 0.0),
    "Honey Sriracha Sauce":   ("Honey Sriracha",               "included", 0.0),
    "Snow Cheese Sauce":      ("Snow Cheese",                  "special",  30.0),
    "Orange Chicken Sauce":   ("Panda Express Orange Chicken", "special",  15.0),
    "Korean Yangneum Sauce":  ("Korean Yangnyeum",             "special",  20.0),
    "Cheese Wing Sauce":      ("Cheese",                       "special",  30.0),
}


def _pk(conn: sqlite3.Connection, table: str, name: str) -> int | None:
    row = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    return row["id"] if row else None


def apply_configuration(conn: sqlite3.Connection, user_id: str = "reference") -> dict:
    """Apply the flavour catalogue configuration and unlimited portion
    settings. Idempotent.

    Returns a per-section count so the seed can report what it actually did
    rather than claiming success for work that silently matched nothing.
    """
    out = {"flavors": 0, "settings": 0}

    with write_tx(conn):
        # --- flavour type, price and customer-facing name ------------------
        for sauce_name, (display, ftype, price) in FLAVOR_CONFIG.items():
            ing_pk = _pk(conn, "ingredient", sauce_name)
            if ing_pk is None:
                continue
            cur = conn.execute(
                "UPDATE unli_flavor SET display_name=?, flavor_type=?, addon_price=?, "
                "addon_menu_item_id=NULL, updated_by=?, updated_at=datetime('now') "
                "WHERE ingredient_id=?",
                (display, ftype, price, user_id, ing_pk),
            )
            out["flavors"] += cur.rowcount
        conn.execute(
            "UPDATE unli_flavor SET sort_order = ("
            "  SELECT COUNT(*) FROM unli_flavor f2 "
            "   WHERE f2.display_name <= unli_flavor.display_name)")

        # --- price provenance -------------------------------------------------
        # Every ingredient's price now comes straight from the workbook's own
        # Purchase Price column (see xlsx_menu_audit_import.import_inventory),
        # so every row is 'estimated'/'pending_supplier_confirmation' — an
        # owner-editable workbook default, not a confirmed supplier quote —
        # rather than some rows being a separately-sourced "benchmark".
        conn.execute(
            """
            UPDATE ingredient SET
              price_status = CASE WHEN unit_cost IS NULL THEN 'unset' ELSE 'estimated' END,
              verification_status = CASE WHEN unit_cost IS NULL THEN 'not_required'
                                         ELSE 'pending_supplier_confirmation' END
            """)

        # --- unlimited portion settings -------------------------------------
        # 3 wings per head per round and 40 g of finished sauce per head per
        # round are the shop's stated, still-live business rules — unchanged
        # by this workbook (see module docstring for why they aren't derived
        # from it).
        conn.execute("DELETE FROM app_setting WHERE key='unli_sauce_g_per_piece'")
        for key, value in (("unli_pieces_per_head", "3"), ("unli_sauce_g_per_head", "40")):
            conn.execute(
                "INSERT INTO app_setting (key, value, updated_by) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_by=excluded.updated_by, updated_at=datetime('now')",
                (key, value, user_id))
            out["settings"] += 1

    return out


if __name__ == "__main__":   # pragma: no cover - operator entry point
    from . import db as _db
    _conn = _db.get_conn()
    _db.migrate(_conn)
    print("applied:", apply_configuration(_conn))
