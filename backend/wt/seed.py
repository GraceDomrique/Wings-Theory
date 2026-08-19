"""Import the shop's real spreadsheets into the typed master.

Run:  python -m wt.seed [--fresh]

Everything this importer writes that was not directly measured by a human is
flagged 'needs_verification'. Nothing is silently defaulted into looking real.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from . import db, gapfill
from .mapping import (
    ALIASES, DEFERRED, FLAVOUR_PLACEHOLDER, FLAVOUR_PLACEHOLDER_DEFAULT_QTY,
    SUB_RECIPE_ALIASES,
)
from .units import normalise_label, parse_quantity, recipe_line_to_base

SEED_DIR = Path(__file__).resolve().parent.parent / "seed_data"
NEEDS = "needs_verification"

# Which base unit each stock item is consumed in. Everything not listed is
# inferred from its purchase unit (see _infer_base_unit).
BASE_UNIT_OVERRIDE = {
    "Palm Oil": "ml", "Soy Sauce": "ml", "Worchestershire": "ml", "White Vinergar": "ml",
    "Knorr Liquid Seasoning Big": "ml", "Evaporated Milk": "ml", "Condense Milk": "ml",
    "All purpose Cream": "ml", "Alcohol": "ml",
    "White Eggs": "pc", "Salted Eggs": "pc", "Parchment Paper": "pc", "Shake Straw": "pc",
    "Takeout Box": "pc", "Takeout Cups": "pc", "Takeout Plastic Bags": "pc",
    "Takeout Paper Bags": "pc", "Thermal Paper": "pc", "Trash Bags": "pc",
    "Toothpicks": "pc", "Tissue Interfold": "pc", "Tissue Rolls": "pc",
    "Plastic Gloves": "pc", "Coke in Can": "pc", "Coke Zero in Can": "pc",
    "Sprite in Can": "pc", "Royal in Can": "pc",
    "Mango": "g", "Banana": "g", "Lemon": "g", "Orange": "g", "Fish Fillet": "g",
    # Bought by the sack, consumed by weight in every shake recipe.
    "Tube Ice": "g",
    # "Additional Ingredients (Recipe-Required)" — real stock rows added in
    # the 08-11 sheet. Most default correctly via _infer_base_unit (kg./Jars/
    # bottles-of-something-weighed -> g, "liters" -> ml); these three don't:
    "Olive Oil": "ml", "Blueberry Puree": "ml",
    # Sold by the liter but the recipe measures it in grams (Plain Yogurt,
    # 100 g) — treating 1 L as ~1000 g is a placeholder density assumption,
    # not a measured conversion, hence needs_verification like every pack
    # size below.
    "Plain Yogurt": "g",
}

# Pack sizes: base units per purchase unit. Every one of these is a planning
# estimate read off a typical retail pack, NOT a measured value, so all are
# seeded 'needs_verification'. Items absent from this dict get pack_size NULL
# and status 'unset', which blocks deduction outright.
PACK_SIZES = {
    "Wings": 1000, "Fries": 1000, "Hugarian Sausage": 500, "Bacon": 500, "Tocino": 500,
    "Beef Tapa": 500, "Tube Ice": 20000, "Pork Cutlets (Tontatsu)": 600,
    "Parchment Paper": 1, "Rice": 1000, "Palm Oil": 1000, "Pasta": 1000,
    "Plastic Gloves": 100, "Flour": 1000, "Evaporated Milk": 370, "Condense Milk": 300,
    "Mushroom Pieces and stems": 184, "Canned Tuna Big Can": 420, "Shitake Mushroom": 184,
    "Spam": 340, "Pesto Jar": 190, "Sriracha": 435, "Knorr Liquid Seasoning Big": 250,
    "Mama Sitas Oyster Sauce Big": 405, "Worchestershire": 290, "White Vinergar": 1000,
    "Soy Sauce": 1000, "All purpose Cream": 250, "Gochujang Big": 500, "Cheese Bars": 450,
    "Refined Sugar": 1000, "Linguine Pasta": 500, "Balducci Penne Pasta": 500,
    "Bread Crumbs": 1000, "Iodized Salt": 1000, "Mayonaise": 470,
    "Ram Pouch pickle relish": 250, "Garlic Flakes": 250, "Chili Flakes Dehydrated": 100,
    "Garlic powder": 250, "Ground Pepper": 250, "Mixed Veggies 500g": 500, "Paprika": 100,
    "Hot Sauce": 340, "Coke in Can": 1, "Coke Zero in Can": 1, "Sprite in Can": 1,
    "Royal in Can": 1, "Red Tea Powder": 500, "Shake Straw": 1, "Parsley": 50,
    "Sesame Seed": 100, "White Eggs": 1, "Takeout Box": 1, "Takeout Cups": 1,
    "Takeout Plastic Bags": 1, "Takeout Paper Bags": 1, "Thermal Paper": 1,
    "Alcohol": 500, "Trash Bags": 1, "Milk Powder": 300, "Cheese Powder": 250,
    "Tissue Interfold": 300, "Tissue Rolls": 1, "Toothpicks": 500,
    "Parmesan Cheese": 200, "Fish Fillet": 150, "Fresh Garlic Peeled": 1000,
    "Kimchi": 500, "Butter": 225, "Cornstarch": 1000, "Salted Eggs": 1,
    "Buffalo Hot Sauce": 340, "Honey": 500,
    # "Additional Ingredients (Recipe-Required)" — real stock as of the 08-11
    # sheet, but the size of the actual purchased container is still a
    # planning estimate (kg./liters -> g/ml conversions are exact; bottle,
    # jar, and yogurt-liter-to-gram figures are not), hence needs_verification
    # like every other entry in this table.
    "Truffle Oil": 100, "Olive Oil": 500, "Fresh Mushrooms": 1000,
    "Fresh Milk": 1000, "Cucumber": 1000, "Onion": 1000,
    "Blueberry Puree": 750, "Plain Yogurt": 1000,
    "Chicken Breast/Thigh (raw, boneless)": 1000,
    "Pork Chop (Taiwanese cut, boneless)": 1000,
    "Shredded Cabbage": 1000, "Pickled Papaya (Atchara)": 300,
    "Tonkatsu Sauce": 300, "Spring Onion": 1000,
    # Deliberately absent: Mango, Banana, Lemon, Orange. Recipes consume these
    # by weight of usable flesh, and grams-per-piece varies enough that a guess
    # would be a real error. They stay unset until gapfill.PIECE_WEIGHTS runs
    # (before recipe import — see gapfill.apply_pre).
}

# Preparation yields. Only the ones that genuinely differ from 1.0 are listed.
# Rice is the important one: recipes are written in cooked grams, stock is
# bought raw, so base units for Rice are "grams of cooked rice".
YIELDS = {
    "Rice": 2.5,                      # raw -> steamed weight gain
    "Canned Tuna Big Can": 0.70,      # drained
    "Mushroom Pieces and stems": 0.65,
    "Shitake Mushroom": 0.65,
    "Fresh Garlic Peeled": 0.95,
}

# Explicit expiry-mode overrides — same names, same reasoning as migration
# 0005: fresh produce and herbs never carry a manufacturer date, so they're
# tracked by an estimated shelf life instead; eggs default to not tracked at
# all unless a real date shows up on a specific batch. Every other ingredient
# keeps the schema default (fixed_date). Duplicated here rather than only in
# the migration because a fresh install seeds straight from these CSVs —
# the ingredient rows the migration's UPDATE targets don't exist yet the
# first time this database is ever built.
EXPIRY_MODE_OVERRIDE = {
    "Mango": ("shelf_life_estimate", 7), "Banana": ("shelf_life_estimate", 7),
    "Spring Onion": ("shelf_life_estimate", 7), "Cucumber": ("shelf_life_estimate", 7),
    "Parsley": ("shelf_life_estimate", 5),
    "White Eggs": ("none", None),
}

# Explicit inventory-type overrides. Everything defaults to food_ingredient,
# which is right for anything actually consumed through a recipe — that
# includes some of the packaging bucket in _category() below: Parchment
# Paper, Shake Straw and Tissue Interfold are all real recipe_line
# ingredients today, so they already get real POS-driven consumption data
# and reclassifying them would make ingredient_depletion() look at the wrong
# ledger reason and read them as never consumed. The items below have no
# recipe_line anywhere — nothing has ever deducted them, which is exactly
# what "operational supply, tracked by manual usage" describes. Coke/Sprite/
# Royal are the reverse case: sold directly rather than consumed inside a
# recipe, so they're sellable_product rather than food_ingredient (this
# doesn't change how they're deducted, only how the console labels them).
OPERATIONAL_SUPPLIES = {
    "Takeout Box", "Takeout Cups", "Takeout Plastic Bags", "Takeout Paper Bags",
    "Thermal Paper", "Tissue Rolls", "Toothpicks", "Plastic Gloves", "Trash Bags", "Alcohol",
}
SELLABLE_PRODUCTS = {"Coke in Can", "Coke Zero in Can", "Sprite in Can", "Royal in Can"}

# Menu, priced from the banner photographs and the 08-11 BOM sheet's own
# per-item price suffixes (which agree with the banner throughout). The
# recipe_name in each row is the exact "Product / Recipe" title as it comes
# out of raw_import.py — several titles changed in the 08-11 revision (price
# suffixes added, "(2 Flavors)" -> "(2 flavors)", "Blueberry Yogurt Shake" ->
# "Blueberry Yogurt (Shake)"), so these are matched against the current sheet,
# not carried over from the previous one.
MENU_SEED = [
    ("Unlimited Wings — 14 Flavors", "Unlimited", 399.0, None, 1),
    ("Wing Rack 12 pcs (2 Flavors)", "Ala Carte", 339.0,
     "Wing Rack 12 pcs (2 flavors) (P339)", 0),
    ("Wings Platter 6 pcs w/ Fries", "Ala Carte", 219.0, "Wings Platter w/ Fries on Side", 0),
    ("Sulit Deal — 3 pcs Wings + Rice", "Ala Carte", 99.0, "Sulit Deals 99: (3 pcs Wings + Rice)", 0),
    ("White Truffle Pasta", "Pasta", 220.0, "White Truffle Pasta", 0),
    ("Spicy Tuna Linguine Pesto", "Pasta", 220.0, "Spicy Tuna Linguine Pesto (P220)", 0),
    ("Creamy Sausage Pasta", "Pasta", 190.0, "Creamy Sausage Pasta (P190)", 0),
    ("Spicy Korean Wing Bowl", "Rice Meals", 149.0,
     "Spicy Korean Wing Bowl (P149) - Korean Flavored Wings Kimchi Cucumber & Egg", 0),
    ("Chicken Salpicao", "Rice Meals", 139.0, "Chicken Salpicao (P139)", 0),
    ("Creamy Chicken & Mushroom Alfredo", "Rice Meals", 149.0,
     "Creamy Chicken & Mushroom Alfredo (P149)", 0),
    ("Pork Tonkatsu", "Rice Meals", 159.0, "Pork Tonkatsu", 0),
    ("Taiwanese Porkchop", "Rice Meals", 179.0, "Taiwanese Porkchop (P179)", 0),
    ("Beef Tapa Silog", "Breakfast", 159.0, "Breakfast Meals: (Beef Tapa)", 0),
    ("Hungarian Sausage Silog", "Breakfast", 149.0, "Hungarian Sausage (Breakfast) (P149)", 0),
    ("Pork Tocino Silog", "Breakfast", 149.0, "Pork Tocino (Breakfast) (P149)", 0),
    ("Spam & Egg", "Breakfast", 139.0, "Spam & Egg (P139)", 0),
    ("Bacon & Egg Silog", "Breakfast", 139.0, "Bacon & Egg (P139)", 0),
    ("Cheesy Bacon Fries", "Starters", 109.0, "Cheesy Bacon Fries", 0),
    ("Onion Rings", "Starters", 99.0, "Onion Rings (P99)", 0),
    ("Fish & Chips with Tartar", "Starters", 199.0,
     "Fish & Chips with Tartar Sauce (P199)", 0),
    ("Mango Shake", "Drinks", 89.0, "Mango FruitShakes", 0),
    ("Banana MilkShake", "Drinks", 89.0, "Banana MilkShake (P89)", 0),
    ("Blueberry Yogurt Shake", "Drinks", 89.0, "Blueberry Yogurt (Shake) (P89)", 0),
    ("1 Pitcher Iced Tea", "Drinks", 89.0, "1 Pitcher Iced Tea (Add-On) (P89)", 0),
    ("Coke in Can", "Drinks", 60.0, "Coke in Can (P60)", 0),
    ("Sprite in Can", "Drinks", 60.0, "Sprite in Can (P60)", 0),
    ("Royal in Can", "Drinks", 60.0, "Royal in Can (P60)", 0),
    ("1 Cup Rice", "Add-ons", 39.0, "1 Cup Rice (Add-On) (P39)", 0),
    # Ranch Dip is sold on its own; the 08-11 sheet no longer bundles it into
    # the Wing Rack recipe (that now only lists wings + the two flavour
    # selections), so this is its only consumer.
    ("Ranch Dip", "Add-ons", 20.0, "Ranch Dip (Add-On) (P20)", 0),
]

# The unli vector. λ values from the manuscript where one exists, otherwise
# unmeasured — all four seeded needs_verification.
UNLI_SEED = [
    ("wings", "Unlimited Wings", "Wings", 845.0, "manuscript p.24: 612.50 kg / 725 covers"),
    ("rice", "Unlimited Rice", "Rice", 380.0, "no measurement on record — placeholder"),
    ("fries", "Unlimited Fries", "Fries", 210.0, "no measurement on record — placeholder"),
    ("drinks", "Unlimited Drinks", "Red Tea Powder", 18.0, "no measurement on record — placeholder"),
]


def _infer_base_unit(name: str, unit: str) -> str:
    if name in BASE_UNIT_OVERRIDE:
        return BASE_UNIT_OVERRIDE[name]
    u = unit.lower()
    if u in ("liters", "liter", "l"):
        return "ml"
    if u in ("pcs.", "pcs", "pc", "rolls", "box", "sack"):
        return "pc"
    return "g"


def _category(cat: str, name: str) -> str:
    packaging = {"Parchment Paper", "Takeout Box", "Takeout Cups", "Takeout Plastic Bags",
                 "Takeout Paper Bags", "Thermal Paper", "Shake Straw", "Tissue Interfold",
                 "Tissue Rolls", "Toothpicks"}
    consumable = {"Plastic Gloves", "Trash Bags", "Alcohol"}
    resale = {"Coke in Can", "Coke Zero in Can", "Sprite in Can", "Royal in Can"}
    if name in packaging:
        return "packaging"
    if name in consumable:
        return "consumable"
    if name in resale:
        return "resale"
    return "wet_food" if cat.startswith("Wet") else "dry_food"


def _next_id(conn, table, col, prefix) -> str:
    n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    return f"{prefix}-{n + 1:04d}"


# ---------------------------------------------------------------------------
def import_inventory(conn) -> dict[str, int]:
    """Stock list -> ingredient master. Returns name -> ingredient pk."""
    ids: dict[str, int] = {}
    with (SEED_DIR / "inventory_tidy.csv").open(encoding="utf-8-sig") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=1):
            name = row["Product Name"].strip()
            unit = row["Unit"].strip()
            base_unit = _infer_base_unit(name, unit)
            pack = PACK_SIZES.get(name)
            yield_f = YIELDS.get(name, 1.0)
            qty = float(row["Product Count"])
            on_hand = qty * pack * yield_f if pack else 0.0
            expiry_mode, shelf_life_days = EXPIRY_MODE_OVERRIDE.get(name, ("fixed_date", None))
            inventory_type = (
                "operational_supply" if name in OPERATIONAL_SUPPLIES else
                "sellable_product" if name in SELLABLE_PRODUCTS else
                "food_ingredient"
            )

            cur = conn.execute(
                """
                INSERT INTO ingredient
                  (ingredient_id, name, category, kind, base_unit, purchase_unit,
                   pack_size, pack_size_status, yield_factor, yield_factor_status,
                   on_hand_base, unit_cost, unit_cost_status, lead_time_days,
                   expiry_mode, shelf_life_days, inventory_type)
                VALUES (?,?,?, 'raw', ?,?, ?,?, ?,?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"ING-{i:04d}", name, _category(row["Category"], name),
                    base_unit, unit,
                    pack, NEEDS if pack else "unset",
                    yield_f, NEEDS,
                    on_hand,
                    float(row["Price (PHP)"]), NEEDS,
                    2 if row["Category"].startswith("Wet") else 4,
                    expiry_mode, shelf_life_days, inventory_type,
                ),
            )
            pk = cur.lastrowid
            ids[name] = pk

            # A shelf_life_estimate ingredient is tracked by estimate, full
            # stop — the sheet's own guess at a date (nobody prints one on a
            # mango) is exactly the fixed-date noise this mode exists to
            # replace, so it's dropped here rather than stored to be shown
            # or "verified" later.
            expiry = "" if expiry_mode == "shelf_life_estimate" else row["Expiry Date"].strip()
            conn.execute(
                """
                INSERT INTO fefo_batch
                  (ingredient_id, lot_no, qty_base, received_at, expires_at,
                   expiry_status, state)
                VALUES (?,?,?,?,?,?, 'open')
                """,
                (
                    pk, f"SEED-{i:04d}", on_hand, "2026-08-03",
                    None if expiry in ("N/A", "") else expiry,
                    "unset" if expiry in ("N/A", "") else NEEDS,
                ),
            )
    return ids


def import_recipes(conn, ids: dict[str, int]) -> tuple[int, int, int]:
    """BOM sheet -> recipes. Lines that cannot be mapped go to the queue."""
    rows: dict[str, list[dict]] = {}
    with (SEED_DIR / "product_item_breakdown_tidy.csv").open(encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            rows.setdefault(r["Product / Recipe"].strip(), []).append(r)

    n_recipes = n_lines = n_unmapped = 0
    # gapfill.apply_pre() has already created the sub-recipes and claimed the
    # low REC-#### numbers; continue from the current count rather than
    # restarting at 1 and colliding with them.
    start = conn.execute("SELECT COUNT(*) AS n FROM recipe").fetchone()["n"]

    for offset, (source_name, lines) in enumerate(rows.items(), start=1):
        # Every recipe reaching this point is a menu/add-on item: the 08-11
        # sheet's sub-recipe and flavour-ratio blocks are filtered out by
        # raw_import.py and constructed instead by gapfill.apply_pre(), which
        # has already run by the time this function is called.
        cur = conn.execute(
            "INSERT INTO recipe (recipe_id, name, kind, yield_qty, yield_unit) "
            "VALUES (?,?, 'menu', 1.0, 'serving')",
            (f"REC-{start + offset:04d}", source_name),
        )
        recipe_pk = cur.lastrowid
        n_recipes += 1

        for order, r in enumerate(lines):
            label = r["Ingredient"].strip()
            norm = normalise_label(label)
            parsed = parse_quantity(r["Quantity"])

            target_name = (SUB_RECIPE_ALIASES.get(norm) or ALIASES.get(norm)
                           or FLAVOUR_PLACEHOLDER.get(norm))
            reason = DEFERRED.get(norm)

            if reason or not target_name or target_name not in ids:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO unmapped_recipe_line
                      (recipe_id, source_recipe_name, source_ingredient_label,
                       source_qty_text, parsed_qty, parsed_unit, reason, state)
                    VALUES (?,?,?,?,?,?,?, 'open')
                    """,
                    (recipe_pk, source_name, label, r["Quantity"], parsed.qty,
                     parsed.unit, reason or "no_stock_record"),
                )
                n_unmapped += 1
                continue

            ingredient_pk = ids[target_name]
            ing_row = conn.execute(
                "SELECT base_unit, pack_size, yield_factor FROM ingredient WHERE id=?",
                (ingredient_pk,),
            ).fetchone()
            base_unit = ing_row["base_unit"]

            note = None
            if parsed.qty <= 0 and norm in FLAVOUR_PLACEHOLDER_DEFAULT_QTY:
                # The source text is pure prose ("per the selected flavor's
                # ratio") with no number for parse_quantity to find at all —
                # NOT the same as a genuinely zero/optional quantity. Without
                # this, the line silently vanishes (qty_base <= 0 below skips
                # it) and the menu item still reports fully deductable,
                # because "zero lines needed" and "one line needing zero" are
                # indistinguishable to the completeness gate. Wing Rack sold
                # with no flavour-sauce deduction at all until this was
                # caught by inspecting its recipe_line rows directly.
                qty_base = FLAVOUR_PLACEHOLDER_DEFAULT_QTY[norm]
                note = "no quantity in source text; using the documented house default"
            else:
                try:
                    qty_base = recipe_line_to_base(
                        parsed.qty, parsed.unit or base_unit, base_unit,
                        ing_row["pack_size"], ing_row["yield_factor"],
                    )
                except Exception:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO unmapped_recipe_line
                          (recipe_id, source_recipe_name, source_ingredient_label,
                           source_qty_text, parsed_qty, parsed_unit, reason, state)
                        VALUES (?,?,?,?,?,?, 'ambiguous', 'open')
                        """,
                        (recipe_pk, source_name, label, r["Quantity"], parsed.qty, parsed.unit),
                    )
                    n_unmapped += 1
                    continue

            if qty_base <= 0:
                continue

            conn.execute(
                """
                INSERT OR IGNORE INTO recipe_line
                  (recipe_id, ingredient_id, display_label, qty_base, qty_status,
                   is_optional, sort_order, note)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (recipe_pk, ingredient_pk, label, qty_base, NEEDS,
                 1 if "optional" in label.lower() else 0, order,
                 note or ("range in source — needs a decision" if parsed.ambiguous else None)),
            )
            n_lines += 1

            conn.execute(
                "INSERT OR IGNORE INTO ingredient_alias "
                "(ingredient_id, alias_norm, alias_raw, source) VALUES (?,?,?,'bom_csv')",
                (ingredient_pk, norm, label),
            )

    return n_recipes, n_lines, n_unmapped


def import_menu(conn) -> int:
    n = 0
    for i, (name, cat, price, recipe_name, is_unli) in enumerate(MENU_SEED, start=1):
        recipe_pk = None
        if recipe_name:
            row = conn.execute("SELECT id FROM recipe WHERE name=?", (recipe_name,)).fetchone()
            recipe_pk = row["id"] if row else None
        conn.execute(
            """
            INSERT INTO menu_item (menu_item_id, name, category, price, recipe_id, is_unli)
            VALUES (?,?,?,?,?,?)
            """,
            (f"MEN-{i:04d}", name, cat, price, recipe_pk, is_unli),
        )
        n += 1
    return n


def import_unli(conn, ids: dict[str, int]) -> int:
    n = 0
    for key, label, ingredient_name, lam, note in UNLI_SEED:
        pk = ids.get(ingredient_name)
        if not pk:
            continue
        cur = conn.execute(
            "INSERT INTO unli_item (item_key, label, ingredient_id, window_days) "
            "VALUES (?,?,?,28)",
            (key, label, pk),
        )
        item_pk = cur.lastrowid
        for role in ("baseline", "rolling"):
            conn.execute(
                """
                INSERT INTO unli_coefficient
                  (unli_item_id, lambda_value, role, total_actual_base, total_heads, status)
                VALUES (?,?,?,?,?,?)
                """,
                (item_pk, lam, role,
                 612500.0 if key == "wings" else None,
                 725 if key == "wings" else None,
                 NEEDS),
            )
        n += 1
    return n


def seed_users(conn) -> None:
    """Every seeded account shares the same placeholder password
    ('wingstheory') and is flagged must_change_password=1 for exactly that
    reason -- see migration 0014's docstring for why the same flag is set
    there too (that UPDATE only reaches rows that already exist, so a fresh
    install needs it set here at INSERT time instead).

    password_changed_at is set explicitly here rather than left to a
    column default -- the column has none (see migration 0014's docstring
    for why a NOT NULL DEFAULT CURRENT_TIMESTAMP column is not safe to add
    via ALTER TABLE against a populated table), so every INSERT path,
    including this one, is responsible for supplying a real value itself.
    """
    from .auth import hash_password
    for username, display, role in [
        ("owner", "R. Alonzo", "admin"),
        ("manager", "M. Santos", "manager"),
        ("cashier1", "J. Cruz", "cashier"),
        ("kitchen1", "D. Lim", "kitchen"),
    ]:
        h, s = hash_password("wingstheory")
        conn.execute(
            "INSERT OR IGNORE INTO app_user "
            "(username, display_name, role, password_hash, password_salt, "
            "password_changed_at, must_change_password) "
            "VALUES (?,?,?,?,?, datetime('now', 'subsec'), 1)",
            (username, display, role, h, s),
        )


def run(fresh: bool = False) -> dict:
    conn = db.reset() if fresh else db.get_conn()
    if not fresh:
        db.migrate(conn)
    if conn.execute("SELECT COUNT(*) AS n FROM ingredient").fetchone()["n"]:
        print("database already seeded — pass --fresh to rebuild")
        return {}

    with db.write_tx(conn):
        ids = import_inventory(conn)
        # Piece-weight base-unit fixes and all 17 sub-recipes (14 flavours +
        # 3 house preps) MUST exist, in their final form, before
        # import_recipes() runs — see gapfill.apply_pre()'s docstring for the
        # under-deduction bug this ordering prevents.
        gap_pre = gapfill.apply_pre(conn, ids)
        n_rec, n_lines, n_unmapped = import_recipes(conn, ids)
        gap_post = gapfill.apply_post(conn)
        n_menu = import_menu(conn)
        n_unli = import_unli(conn, ids)
        seed_users(conn)

    # Register the fourteen sauces as selectable unlimited flavours. Not done
    # in migration 0011 for a fresh install: migrations run against an empty
    # database, so the ingredients its backfill selects from do not exist
    # until the block above has run. sync_flavors is idempotent, so the
    # migration's backfill (which does the job for an already-populated
    # database) and this call cannot produce duplicates.
    from .refills import sync_flavors
    n_flavors = sync_flavors(conn)

    # Configuration from the Inventory & Recipe Reference: flavour types and
    # premium prices, benchmark SKU costs, the two recipe corrections the
    # document contradicts outright, and the unlimited portion settings.
    # Migration 0012 does the same for an already-deployed database; a fresh
    # one has none of these rows yet when migrations run, so it is applied
    # here instead. Both converge on the state TestReferenceAlignment asserts.
    from .reference import apply_configuration
    ref = apply_configuration(conn)

    gap = {**gap_pre, **gap_post}
    n_rec += gap_pre["subrecipes_created"]

    sellable = conn.execute(
        "SELECT COUNT(*) AS n FROM v_menu_item_availability WHERE sellable=1"
    ).fetchone()["n"]
    needs = conn.execute("SELECT COUNT(*) AS n FROM v_needs_verification").fetchone()["n"]

    stats = {
        "ingredients": conn.execute(
            "SELECT COUNT(*) AS n FROM ingredient").fetchone()["n"],
        "recipes": n_rec,
        "recipe_lines": n_lines,
        "unmapped_lines": n_unmapped,
        "menu_items": n_menu,
        "unli_streams": n_unli,
        "unli_flavors": n_flavors,
        "reference_flavors_configured": ref["flavors"],
        "reference_skus_repriced": ref["skus"],
        "reference_recipe_fixes": ref["recipe_fixes"],
        "sellable_menu_items": sellable,
        "needs_verification": needs,
        **{f"gapfill_{k}": v for k, v in gap.items()},
    }
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Seed the Wings Theory database")
    ap.add_argument("--fresh", action="store_true", help="drop and rebuild")
    args = ap.parse_args()

    print(f"database: {db.db_path()}")
    # Guarded only here, not inside run() — run(fresh=True) is also the
    # in-process entrypoint six test classes call against their own tempfile
    # WT_DB in setUpClass, and forcing them through an interactive
    # confirmation would hang the suite. This CLI block is the only path an
    # operator actually invokes by hand.
    if args.fresh:
        db.require_destructive_confirmation("drop and rebuild the database")
    stats = run(fresh=args.fresh)
    if stats:
        print("\nimported")
        for k, v in stats.items():
            print(f"  {k:24} {v}")
        blocked = stats["menu_items"] - stats["sellable_menu_items"]
        if blocked:
            print(f"\n  {blocked} menu item(s) are blocked from sale pending "
                  f"ingredient mapping — this is expected, not a failure.")
        sys.exit(0)
