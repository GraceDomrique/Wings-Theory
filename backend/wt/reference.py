"""Configuration taken from the Wings Theory Inventory & Recipe Reference.

Why this module exists
----------------------
Migrations run against an EMPTY database on a fresh install — db.reset()
applies every .sql file before seed.py imports a single ingredient — so a
migration cannot configure rows that do not exist yet. Migration 0012 brings
the already-deployed database up to date; this module does the same job at
seed time, and both converge on the state asserted by TestReferenceAlignment.

Everything here is idempotent and safe to re-run.

What is NOT here
----------------
The fourteen flavour master ratios. Those live in gapfill.SUB_RECIPES, which
is the seed-time source for every sub-recipe, and a second copy would be a
second thing to keep in step. TestReferenceAlignment asserts the database
agrees with that table.
"""
from __future__ import annotations

import sqlite3

from .db import write_tx

# ---------------------------------------------------------------------------
# Flavour configuration — Section 3 "Type" column, plus initial premium prices
#
# The four premium prices are INITIAL SYSTEM RECOMMENDATIONS. The reference
# document classifies these four as Premium but states no amount anywhere, so
# these figures are a starting point for the owner, not a business-supplied
# price. They stay editable through the console (recipe.write), and the price
# actually charged is snapshotted onto each refill at serve time, so changing
# one never restates what a customer was already charged.
#
#   sauce ingredient name -> (customer-facing name, type, add-on price)
# ---------------------------------------------------------------------------
FLAVOR_CONFIG: dict[str, tuple[str, str, float]] = {
    "Classic Buffalo Sauce":  ("Classic Buffalo",              "included", 0.0),
    "Mango Habanero Sauce":   ("Mango Habanero",               "included", 0.0),
    "Salted Egg Sauce":       ("Salted Egg",                   "included", 0.0),
    "Lemon Glazed Sauce":     ("Lemon Glazed",                 "included", 0.0),
    "Original Dry Rub":       ("Original",                     "included", 0.0),
    "Korean BBQ Sauce":       ("Korean BBQ",                   "included", 0.0),
    "Garlic Parmesan Sauce":  ("Garlic Parmesan",              "included", 0.0),
    "Soy Garlic Sauce":       ("Soy Garlic",                   "included", 0.0),
    "Teriyaki Sauce":         ("Teriyaki",                     "included", 0.0),
    "Honey Sriracha Sauce":   ("Honey Sriracha",               "included", 0.0),
    "Snow Cheese Sauce":      ("Snow Cheese",                  "special",  30.0),
    "Orange Chicken Sauce":   ("Panda Express Orange Chicken", "special",  15.0),
    "Korean Yangneum Sauce":  ("Korean Yangnyeum",             "special",  20.0),
    "Cheese Wing Sauce":      ("Cheese",                       "special",  30.0),
}

# ---------------------------------------------------------------------------
# Benchmark prices for the six SKUs the document flags as "referenced in
# recipes but NOT currently in inventory".
#
# All six already exist — the 2026-08-11 spreadsheet revision added them, and
# all six are already wired into their recipes. These are re-priced against the
# supplied benchmarks, never re-created; a second "Onion" row would be exactly
# the SKU-name collision the validation pass exists to catch.
#
# Every one is an INITIAL BENCHMARK for the capstone system, not a confirmed
# Wings Theory supplier price, which is what price_status/verification_status
# record.
#
#   ingredient name -> (unit_cost, expected purchase unit or None to leave it)
#
# Blueberry is deliberately absent: the stocked SKU is "Blueberry Puree", a
# 750 ml bottle, and the benchmark is a 500 g frozen pack. Different products
# in different measurement families — attaching a per-pack price to a per-litre
# SKU, or rewriting base_unit while the row holds stock and an open FEFO batch,
# would both be silent falsifications. Reported instead.
# ---------------------------------------------------------------------------
SKU_BENCHMARKS: dict[str, tuple[float, str | None]] = {
    "Onion":            (140.0, None),   # already 140/kg — status only
    "Cucumber":         (135.0, None),
    "Shredded Cabbage": (105.0, None),
    "Fresh Milk":       (130.0, None),
    # purchase_unit read 'liters' against a gram base unit; the benchmark is
    # per kilo and pack_size is already 1000, so the label is corrected to
    # describe the number it carries.
    "Plain Yogurt":     (688.0, "kg"),
}

# Every SKU whose price provenance the document leaves open, including the one
# not re-priced, so the console shows the same honest state for all of them.
BENCHMARK_PENDING = tuple(SKU_BENCHMARKS) + ("Blueberry Puree", "White Vinegar")

# ---------------------------------------------------------------------------
# Recipe corrections the document directly contradicts
# ---------------------------------------------------------------------------
# Spicy Korean Wing Bowl consumed 200 g of KOREAN BBQ sauce. Section 4
# specifies "Korean Yangnyeum Sauce (Sec. 3 ratio, scaled to 40 g)". Both the
# flavour and the quantity were wrong, and once a flavour batch is 100 g rather
# than a fictional 1000 g, a 200 g line means two entire batches of sauce for
# one bowl.
KOREAN_BOWL_FIX = ("Spicy Korean Wing Bowl", "Korean BBQ Sauce",
                   "Korean Yangneum Sauce", 40.0)

# Taiwanese Porkchop is served with shredded cabbage (Section 4: 30 g) and the
# SKU is stocked, but the recipe carried no line for it.
CABBAGE_FIX = ("Taiwanese Porkchop", "Shredded Cabbage", 30.0)


def _pk(conn: sqlite3.Connection, table: str, name: str) -> int | None:
    row = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    return row["id"] if row else None


def apply_configuration(conn: sqlite3.Connection, user_id: str = "reference") -> dict:
    """Apply the document's configuration. Idempotent.

    Returns a per-section count so the seed can report what it actually did
    rather than claiming success for work that silently matched nothing.
    """
    out = {"flavors": 0, "skus": 0, "recipe_fixes": 0, "settings": 0}

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

        # --- benchmark prices ----------------------------------------------
        for name, (cost, purchase_unit) in SKU_BENCHMARKS.items():
            if purchase_unit:
                cur = conn.execute(
                    "UPDATE ingredient SET unit_cost=?, purchase_unit=?, "
                    "unit_cost_status='needs_verification', updated_at=datetime('now') "
                    "WHERE name=?", (cost, purchase_unit, name))
            else:
                cur = conn.execute(
                    "UPDATE ingredient SET unit_cost=?, "
                    "unit_cost_status='needs_verification', updated_at=datetime('now') "
                    "WHERE name=?", (cost, name))
            out["skus"] += cur.rowcount

        # price provenance: backfill everything from the older vocabulary,
        # then mark the benchmark SKUs explicitly.
        conn.execute(
            """
            UPDATE ingredient SET
              price_status = CASE WHEN unit_cost IS NULL THEN 'unset'
                                  WHEN unit_cost_status = 'verified' THEN 'verified'
                                  ELSE 'estimated' END,
              verification_status = CASE WHEN unit_cost_status = 'verified' THEN 'confirmed'
                                         ELSE 'pending_supplier_confirmation' END
            """)
        marks = ",".join("?" * len(BENCHMARK_PENDING))
        conn.execute(
            f"UPDATE ingredient SET price_status='estimated', "
            f"verification_status='pending_supplier_confirmation' WHERE name IN ({marks})",
            BENCHMARK_PENDING)

        # --- recipe corrections --------------------------------------------
        recipe_like, from_ing, to_ing, qty = KOREAN_BOWL_FIX
        to_pk, from_pk = _pk(conn, "ingredient", to_ing), _pk(conn, "ingredient", from_ing)
        if to_pk and from_pk:
            cur = conn.execute(
                """
                UPDATE recipe_line
                   SET ingredient_id=?, qty_base=?, display_label='Korean Yangnyeum Sauce',
                       qty_status='needs_verification',
                       note='corrected from the reference document: was Korean BBQ Sauce 200 g'
                 WHERE ingredient_id=?
                   AND recipe_id IN (SELECT id FROM recipe WHERE name LIKE ?)
                """,
                (to_pk, qty, from_pk, recipe_like + "%"))
            out["recipe_fixes"] += cur.rowcount

        recipe_like, ing_name, qty = CABBAGE_FIX
        ing_pk = _pk(conn, "ingredient", ing_name)
        if ing_pk:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO recipe_line
                  (recipe_id, ingredient_id, display_label, qty_base, qty_status,
                   is_optional, sort_order, note)
                SELECT r.id, ?, ?, ?, 'needs_verification', 0, 90,
                       'added from the reference document — the SKU was stocked but unreferenced'
                  FROM recipe r
                 WHERE r.name LIKE ?
                   AND NOT EXISTS (SELECT 1 FROM recipe_line rl
                                    WHERE rl.recipe_id = r.id AND rl.ingredient_id = ?)
                """,
                (ing_pk, ing_name, qty, recipe_like + "%", ing_pk))
            out["recipe_fixes"] += cur.rowcount

        # --- unlimited portion settings -------------------------------------
        # 3 wings per head per round is the shop's stated business rule.
        # 40 g of finished sauce per head per round comes from Section 4's
        # Sulit Deal 99, which portions 3 pieces at 35-40 g of sauce.
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
