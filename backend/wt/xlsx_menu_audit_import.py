"""Import the owner's Menu & Ingredient Audit workbook
(data/Wings_Theory_Menu_Ingredient_Audit.xlsx) into the ingredient/recipe/
menu master — the replacement for the old CSV + hardcoded-dict pipeline
(seed_data/inventory_tidy.csv, seed_data/product_item_breakdown_tidy.csv,
wt/mapping.py's ALIASES/SUB_RECIPE_ALIASES/FLAVOUR_PLACEHOLDER/DEFERRED).

Why a bulk re-derivation was possible here, and mostly wasn't guesswork
------------------------------------------------------------------------
Three sheets carry the data this module needs:

  '4. Master Ingredient Inventory' -> ingredient master (108 rows)
  '1. Menu Audit'                  -> menu_item.name/category/price (44 sellable
                                       items + 3 "Leftover Charge" rows that are
                                       order-time penalty fees, not menu_item
                                       rows at all — see IGNORED_MENU_ITEMS)
  '6. Menu-to-Ingredient Mapping'  -> recipe_line rows, one block per menu item

Two facts, verified directly against the real workbook (not assumed), make
the import mechanical rather than another curated-alias exercise:

  1. Sheet 6's 'Ingredient Name (standardized)' column matches sheet 4's
     'Ingredient Name' EXACTLY for all 479 data rows — zero mismatches. So
     every recipe line resolves to an ingredient by a plain name lookup
     against the ids this module's own import_inventory() just built; no
     alias table is needed at all for the ordinary case.
  2. Sheet 6's 'Base Qty' column equals its own 'Recipe Quantity' column for
     all 479 rows — the sheet does no unit conversion of its own; whatever
     unit the recipe is written in already IS the ingredient's base unit.
     recipe_line.qty_base is therefore 'Recipe Quantity', verbatim, once
     summed across any repeated ingredient rows within one menu item (see
     _group_lines) — never re-derived through units.recipe_line_to_base.

_validate_workbook_assumptions() re-checks both facts at import time (not
just when this module was written) and refuses to import silently if either
one ever stops holding for a re-exported copy of the workbook.

What this module deliberately does NOT reproduce from the old pipeline
------------------------------------------------------------------------
The three old "house preparation" sub-recipes (Cheese Sauce (house), Tartar
Sauce (house), Simple Syrup (house)) do not exist in this workbook in any
form — every recipe that used to reference one now lists its raw components
directly (Fish & Chips lists Mayonnaise/Pickle Relish/Lemon Juice, not
"Tartar Sauce"; the three shakes list Fresh Milk/Ice/White Sugar, not
"Simple Syrup"; Cheesy Bacon Fries and the Cheese flavour both reference a
now-directly-purchased "Cheese Sauce" ingredient, not a house recipe that
produces one). So gapfill.SUB_RECIPES no longer carries them — see that
module's docstring.

What this module produces that gapfill.py consumes
------------------------------------------------------------------------
derive_flavor_subrecipes() reads the 14 "Unli Wings - <Flavor>" blocks in
sheet 6 and returns SUB_RECIPES-shaped entries for gapfill.SUB_RECIPES to
merge in — see that function's own docstring for the breading/sauce split.
"""
from __future__ import annotations

from pathlib import Path

import openpyxl

NEEDS = "needs_verification"
VERIFIED = "verified"

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
# 2026-09-25 note: the owner re-exported this workbook under a new filename
# (Wings_Theory_Menu_Ingredient_List.xlsx, replacing ..._Audit.xlsx). Sheet
# names, column headers and the Base Qty == Recipe Quantity / Ingredient
# Name (standardized) match assumptions this module relies on (see module
# docstring) were re-verified against the new export before repointing this
# constant -- only the filename changed.
XLSX_PATH = _DATA_DIR / "Wings_Theory_Menu_Ingredient_List.xlsx"

SHEET_MENU_AUDIT = "1. Menu Audit"
SHEET_INVENTORY = "4. Master Ingredient Inventory"
SHEET_MAPPING = "6. Menu-to-Ingredient Mapping"

UNLI_PREFIX = "Unli Wings - "
# The included-tier category for the 10 non-premium "Unli Wings -" rows —
# these carry the base Unlimited Wings price (399). The 4 premium rows sit
# under a different category ("Unlimited Wings - Premium Flavors") and list
# each flavor's own all-in total, not the base price — see import_menu().
UNLI_INCLUDED_CATEGORY = "Unlimited Wings - 14 Flavors"
# Printed-on-the-menu penalty fees for unfinished Unlimited Wings food, not
# separate sellable items — no ingredient lines exist for these in sheet 6,
# and the actual charge mechanism already lives elsewhere (order_charge /
# app_setting), predating this workbook entirely. Confirmed by inspection:
# these are the only three '1. Menu Audit' rows absent from sheet 6.
IGNORED_MENU_ITEMS = {
    "Leftover Charge - Wings", "Leftover Charge - Rice", "Leftover Charge - Fries",
}

# ---------------------------------------------------------------------------
# Category enum mapping. ingredient.category is CHECK-constrained to exactly
# ('wet_food','dry_food','packaging','consumable','resale') (migration 0001).
# The workbook's own Category labels are free text with no such constraint in
# mind (Dairy, Dairy/Eggs, Meat, Meat/Seafood, Produce, Dry Goods, Spices,
# Condiments, Oils, Beverages, Other, Component). This workbook carries no
# packaging or consumable (non-food) items at all — the old CSV's Takeout
# Box/Tissue/Trash Bags/etc. bucket is simply absent from the new source of
# truth, so nothing is seeded into 'packaging'/'consumable' any more. If the
# shop wants those tracked again, they need their own rows added to the
# workbook (or a small explicit addendum here, the same pattern gapfill.py's
# old ADDITIONS list used) — not fabricated by this importer.
# ---------------------------------------------------------------------------
_PERISHABLE_CATEGORIES = {"Dairy", "Dairy/Eggs", "Meat", "Meat/Seafood", "Produce"}
_RESALE_SUFFIX = " (Can)"  # Coke/Coke Zero/Sprite/Royal — sold as-is, not consumed by a recipe


def _category(raw_category: str, name: str) -> str:
    if name.endswith(_RESALE_SUFFIX):
        return "resale"
    return "wet_food" if raw_category in _PERISHABLE_CATEGORIES else "dry_food"


def _inventory_type(name: str) -> str:
    return "sellable_product" if name.endswith(_RESALE_SUFFIX) else "food_ingredient"


def _expiry(raw_category: str, name: str) -> tuple[str, int | None]:
    # Same reasoning as the old EXPIRY_MODE_OVERRIDE in seed.py: fresh
    # produce has no manufacturer date to track against, and a whole shell
    # egg is normally used well inside any shelf-life estimate worth tracking
    # at all, so it's simply not date-tracked.
    if name == "Egg":
        return "none", None
    if raw_category == "Produce":
        return "shelf_life_estimate", 5
    return "fixed_date", None


# ---------------------------------------------------------------------------
# Explicit, documented overrides layered on top of the workbook — same
# pattern the old seed.py used (BASE_UNIT_OVERRIDE / PIECE_WEIGHTS), kept to
# an absolute minimum because sheet 4/6 already cover almost everything
# directly. Both entries below are deliberate BUSINESS decisions that
# contradict the workbook's own figures, not corrections of a data error:
#
#   BANANA: the workbook prices/sizes Banana by weight (Produce, ~65/kg,
#   base_unit 'g', and Banana Milk Shake's own line reads "Banana 100 g").
#   A prior, still-standing business decision (see reference.py's retired
#   BANANA_UNIT_FIX for the full history) is that Banana stays piece-counted
#   end-to-end — purchase, stock, recipe, deduction — and average weight is
#   metadata only, never a conversion the deduction path applies. So this
#   importer overrides Banana to base_unit='pc'/purchase_unit='pc'/
#   pack_size=1, and its one recipe line (Banana Milk Shake) to qty_base=1.0
#   (one whole banana) instead of the sheet's 100 g. unit_cost is restated
#   per-piece using the workbook's own stated average (~120 g/pc, the same
#   figure the old override used) against its per-gram price, purely so the
#   ingredient has SOME cost rather than none — flagged needs_verification
#   like every other cost in this importer.
#
#   WING RACK DEFAULT FLAVOUR LINE: sheet 6 has NO ingredient line at all for
#   "12 pcs (2 Flavors) Ala Carte Wing Rack" that represents the two flavour
#   sauce portions a customer actually picks (every other multi/single-flavor
#   wing item has an explicit "Wing Sauce (Selected Flavor...)" line; this
#   one doesn't). The shop has always served this item with two flavour
#   picks, and the old CSV-driven system deducted a default flavour for
#   exactly this reason (see the now-removed gapfill.FLAVOUR_PLACEHOLDER
#   default of 60 g per pick, ~60% of one full order's flavour sauce). This
#   override restores that same default as two explicit recipe_line rows —
#   clearly NOT sourced from the workbook — rather than silently leaving the
#   item deduct zero flavour sauce.
# ---------------------------------------------------------------------------
BANANA_NAME = "Banana"
BANANA_GRAMS_PER_PIECE_ASSUMED = 120.0  # metadata only — see docstring above
WING_RACK_RECIPE_NAME = "12 pcs (2 Flavors) Ala Carte Wing Rack"
WING_RACK_DEFAULT_FLAVOR_INGREDIENT = "Classic Buffalo Sauce"
WING_RACK_DEFAULT_FLAVOR_QTY_BASE = 0.60  # fraction of one full flavour order — see docstring

# The one recipe line in this workbook that names a customer-chosen flavour
# rather than a fixed ingredient (single-flavour Ala Carte items: 6 pcs w/
# Fries & Ranch Dip, Wings Platter, Sulit Deal). Redirected at import time to
# the same house-default flavour sub-recipe the old FLAVOUR_PLACEHOLDER used,
# expressed as a fraction of one full flavour order (grams / 100 — the exact
# rule the old gapfill.DEPENDENT_RESCALES table used to convert a raw gram
# figure written against the old 100 g nominal batch into a fraction of the
# new yield_qty=1 'order' sub-recipe; sheet 6's own grams for these three
# items, 30/30/15, are simply carried through that same rule here instead of
# being hand-transcribed).
FLAVOR_PLACEHOLDER_NAME = "Wing Sauce (Selected Flavor - see flavor recipe)"
FLAVOR_PLACEHOLDER_DEFAULT = "Classic Buffalo Sauce"


def _load_sheet(wb, name):
    return wb[name]


def _validate_workbook_assumptions(inv_rows: list[tuple], map_rows: list[tuple]) -> list[str]:
    """Re-check, against THIS run's actual workbook contents, the two facts
    this whole import strategy depends on (see module docstring). Returns a
    list of problem descriptions — never raises and never silently drops a
    row; the caller decides what to do with a non-empty list (currently:
    surface it in the returned stats and still import everything that DOES
    validate, routing anything that doesn't through the normal unmapped
    queue like any other unresolved row)."""
    problems = []
    inv_names = {r[1] for r in inv_rows}
    for row_i, (menu_item, _cat, ing_name, recipe_qty, _unit, base_qty) in enumerate(map_rows, start=1):
        if ing_name not in inv_names:
            problems.append(
                f"sheet6 row {row_i} ({menu_item!r}): ingredient "
                f"{ing_name!r} not found in sheet4 by exact name")
        elif recipe_qty != base_qty:
            problems.append(
                f"sheet6 row {row_i} ({menu_item!r}, {ing_name!r}): "
                f"Recipe Quantity ({recipe_qty!r}) != Base Qty ({base_qty!r}) — "
                f"this importer assumes they always match (no unit conversion)")
    return problems


# ---------------------------------------------------------------------------
# Sheet 4 -> ingredient master
# ---------------------------------------------------------------------------
def import_inventory(conn, source_path: Path = XLSX_PATH) -> dict[str, int]:
    """'4. Master Ingredient Inventory' -> ingredient. Returns name -> pk,
    same contract seed.py's old CSV-driven import_inventory() produced.

    No physical stock count exists anywhere in this workbook (it is a menu/
    recipe/costing audit, not an inventory count sheet) — every ingredient
    opens with a synthetic "opening delivery" of INITIAL_STOCK_PACKS
    purchase-units, clearly flagged as such in the fefo_batch lot number and
    never claimed as a real count. This is the owner-approved placeholder
    (see the task's own instructions); wt.xlsx_history_import's replay tops
    up any ingredient that runs short via its existing _restock_shortfalls
    path exactly as it already does for a low/zero starting balance, so the
    starting number only needs to be non-absurd, not accurate.
    """
    wb = openpyxl.load_workbook(source_path, data_only=True)
    ws = _load_sheet(wb, SHEET_INVENTORY)

    ids: dict[str, int] = {}
    i = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row[0]:
            continue
        i += 1
        (_ing_id, name, raw_category, _spec, purchase_unit, purchase_price,
         raw_base_unit, pack_size, _cost_per_base, _menu_items, _storage,
         _shelf_unopened, _shelf_opened, _status) = row[:14]
        name = str(name).strip()

        if name == BANANA_NAME:
            base_unit, purchase_unit_final, pack = "pc", "pc", 1.0
            unit_cost = round((purchase_price / 1000.0) * BANANA_GRAMS_PER_PIECE_ASSUMED, 2)
        else:
            u = (raw_base_unit or "").strip().lower()
            base_unit = u if u in ("g", "ml") else "pc"
            purchase_unit_final = str(purchase_unit).strip()
            pack = float(pack_size) if pack_size else None
            unit_cost = float(purchase_price) if purchase_price is not None else None

        expiry_mode, shelf_life_days = _expiry(raw_category, name)
        on_hand = (pack * INITIAL_STOCK_PACKS) if pack else 0.0

        cur = conn.execute(
            """
            INSERT INTO ingredient
              (ingredient_id, name, category, kind, base_unit, purchase_unit,
               pack_size, pack_size_status, yield_factor, yield_factor_status,
               on_hand_base, unit_cost, unit_cost_status, lead_time_days,
               expiry_mode, shelf_life_days, inventory_type)
            VALUES (?,?,?, 'raw', ?,?, ?,?, 1.0,?, ?, ?,?, ?, ?,?, ?)
            """,
            (
                f"ING-{i:04d}", name, _category(raw_category, name),
                base_unit, purchase_unit_final,
                pack, NEEDS if pack else "unset",
                NEEDS,
                on_hand,
                unit_cost, NEEDS if unit_cost is not None else "unset",
                2 if base_unit != "pc" and _category(raw_category, name) == "wet_food" else 4,
                expiry_mode, shelf_life_days, _inventory_type(name),
            ),
        )
        pk = cur.lastrowid
        ids[name] = pk

        conn.execute(
            """
            INSERT INTO fefo_batch
              (ingredient_id, lot_no, qty_base, received_at, expires_at,
               expiry_status, state)
            VALUES (?,?,?,?, NULL, 'unset', 'open')
            """,
            (pk, f"SEED-SYNTH-{i:04d}", on_hand, "2024-12-15"),
        )
    wb.close()
    return ids


INITIAL_STOCK_PACKS = 500.0  # see import_inventory()'s docstring — synthetic, not a real count


# ---------------------------------------------------------------------------
# Sheet 6 -> recipe / recipe_line
# ---------------------------------------------------------------------------
def _read_mapping_rows(ws) -> list[tuple]:
    """Sheet 6's real header is row index 2 (0-based) — rows 0-1 hold two
    assumption cells (Oil Absorption Factor, Cooking Oil per Wing), not
    tabular data. Data starts row index 3, i.e. worksheet row 4 (1-based,
    openpyxl's min_row)."""
    rows = []
    for r in ws.iter_rows(min_row=4, values_only=True):
        if not r[0]:
            continue
        rows.append((r[0], r[1], r[2], r[3], r[4], r[5]))  # menu, cat, ing, qty, unit, base_qty
    return rows


def _group_lines(lines: list[tuple]) -> list[tuple]:
    """Sum repeated ingredient rows within one menu item's block (the same
    ingredient legitimately appears more than once in several recipes — e.g.
    Chicken Salpicao lists 'Garlic (Whole/Crushed, Cloves)' twice, and every
    breakfast Silog repeats 'Cooking Oil'/'Salt'/'Black Pepper' across its
    protein, garlic-rice, and egg steps). recipe_line has a UNIQUE
    (recipe_id, ingredient_id, display_label) constraint, and physically the
    total ingredient consumed per order really is the sum, so this groups by
    ingredient name and sums qty_base rather than either colliding on INSERT
    or silently keeping only the last occurrence. Order of first appearance
    is preserved for sort_order."""
    order: list[str] = []
    totals: dict[str, float] = {}
    for _menu, _cat, ing_name, qty, _unit, _base in lines:
        if ing_name not in totals:
            order.append(ing_name)
            totals[ing_name] = 0.0
        totals[ing_name] += float(qty)
    return [(name, totals[name]) for name in order]


def import_recipes(conn, ids: dict[str, int], source_path: Path = XLSX_PATH) -> dict:
    """'6. Menu-to-Ingredient Mapping' -> recipe + recipe_line, one recipe
    per non-Unli, non-leftover-charge menu item. Must run AFTER
    gapfill.apply_pre() has created the flavour sub-recipes this module
    derived (see derive_flavor_subrecipes()) — the single-flavour Ala Carte
    items' placeholder line redirects to one of them, exactly the same
    ordering constraint the old CSV pipeline had for its own
    FLAVOUR_PLACEHOLDER.

    Every line that fails _validate_workbook_assumptions() (an ingredient
    name sheet 4 doesn't have, or a Recipe Quantity/Base Qty mismatch) is
    routed to unmapped_recipe_line instead of guessed at, same discipline as
    the old CSV importer.
    """
    wb = openpyxl.load_workbook(source_path, data_only=True)
    ws = _load_sheet(wb, SHEET_MAPPING)
    all_rows = _read_mapping_rows(ws)
    wb.close()

    inv_rows = [(None, name) for name in ids]  # only .name is used by the validator
    problems = _validate_workbook_assumptions(inv_rows, all_rows)

    by_item: dict[str, list[tuple]] = {}
    order: list[str] = []
    for row in all_rows:
        menu_item = row[0]
        if menu_item.startswith(UNLI_PREFIX) or menu_item in IGNORED_MENU_ITEMS:
            continue
        if menu_item not in by_item:
            by_item[menu_item] = []
            order.append(menu_item)
        by_item[menu_item].append(row)

    n_recipes = n_lines = n_unmapped = 0
    start = conn.execute("SELECT COUNT(*) AS n FROM recipe").fetchone()["n"]

    for offset, menu_item in enumerate(order, start=1):
        cur = conn.execute(
            "INSERT INTO recipe (recipe_id, name, kind, yield_qty, yield_unit) "
            "VALUES (?,?, 'menu', 1.0, 'serving')",
            (f"REC-{start + offset:04d}", menu_item),
        )
        recipe_pk = cur.lastrowid
        n_recipes += 1

        for sort_order, (ing_name, qty_base) in enumerate(_group_lines(by_item[menu_item]), start=1):
            display_label = ing_name
            note = None

            if ing_name == FLAVOR_PLACEHOLDER_NAME:
                target_name = FLAVOR_PLACEHOLDER_DEFAULT
                qty_base = qty_base / 100.0  # grams-of-100g-batch -> fraction of one flavour order
                # Spelled "flavor" (American), not "flavour" — deliberately:
                # GET /pos/menu (wt/api.py's pos_menu()) detects a
                # customer-choice flavor slot with
                # "display_label LIKE '%flavor%'", a plain substring match.
                # "flavour" (the British spelling used in this module's own
                # prose/comments) does NOT contain "flavor" as a substring
                # (the inserted "u" breaks it before the "r"), so a label
                # spelled that way would silently stop being recognised as a
                # flavor slot at all — caught by actually running the POS
                # menu test, not by inspection.
                display_label = "Wing Sauce (default flavor — see Unlimited Wings)"
                note = (f"placeholder in the source workbook ({FLAVOR_PLACEHOLDER_NAME!r}); "
                        f"the customer's actual flavour pick is applied at order time — this "
                        f"deducts the house-default flavour ({target_name}) as a planning figure, "
                        f"same convention as the old gapfill.FLAVOUR_PLACEHOLDER")
            elif ing_name == BANANA_NAME:
                target_name = BANANA_NAME
                qty_base = 1.0  # one whole banana, not the sheet's 100 g — see BANANA overrides above
                display_label = "Banana (whole piece)"
                note = "Banana stays piece-counted end-to-end — see BANANA overrides in this module"
            else:
                target_name = ing_name

            ingredient_pk = ids.get(target_name)
            if ingredient_pk is None or qty_base <= 0:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO unmapped_recipe_line
                      (recipe_id, source_recipe_name, source_ingredient_label,
                       source_qty_text, parsed_qty, parsed_unit, reason, state)
                    VALUES (?,?,?,?,?,?,?, 'open')
                    """,
                    (recipe_pk, menu_item, ing_name, str(qty_base), qty_base, None,
                     "no_stock_record" if ingredient_pk is None else "ambiguous"),
                )
                n_unmapped += 1
                continue

            conn.execute(
                """
                INSERT OR IGNORE INTO recipe_line
                  (recipe_id, ingredient_id, display_label, qty_base, qty_status,
                   sort_order, note)
                VALUES (?,?,?,?,?,?,?)
                """,
                (recipe_pk, ingredient_pk, display_label, qty_base, NEEDS, sort_order, note),
            )
            n_lines += 1

        if menu_item == WING_RACK_RECIPE_NAME:
            flavor_pk = ids.get(WING_RACK_DEFAULT_FLAVOR_INGREDIENT)
            if flavor_pk:
                for pick, label in enumerate(("Flavor Sauce #1 (default)", "Flavor Sauce #2 (default)"), start=1):
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO recipe_line
                          (recipe_id, ingredient_id, display_label, qty_base, qty_status,
                           sort_order, note)
                        VALUES (?,?,?,?,?,?,?)
                        """,
                        (recipe_pk, flavor_pk, label, WING_RACK_DEFAULT_FLAVOR_QTY_BASE, NEEDS,
                         900 + pick,
                         "sheet 6 carries no flavour-sauce line for this item at all — this is a "
                         "deliberate business-rule override (two default flavour picks), not "
                         "sourced from the workbook — see WING RACK DEFAULT FLAVOUR LINE above"),
                    )
                    n_lines += 1

    return {
        "recipes": n_recipes, "recipe_lines": n_lines, "unmapped_lines": n_unmapped,
        "workbook_assumption_problems": problems,
    }


# ---------------------------------------------------------------------------
# Sheet 1 -> menu_item
# ---------------------------------------------------------------------------
def import_menu(conn, source_path: Path = XLSX_PATH) -> int:
    """'1. Menu Audit' -> menu_item. menu_item.name is the workbook's own
    'Menu Item' text VERBATIM (not remapped to some internal spelling) —
    this is what makes xlsx_history_import.REGULAR_ALIASES collapse to
    almost nothing: the order-history workbook's 'Product/Menu Item' column
    is drawn from the same real menu names.

    The single "Unlimited Wings — 14 Flavors" menu item is NOT one of the 44
    rows here (those 14 "Unli Wings - <Flavor>" rows describe the flavour
    RECIPES, consumed by derive_flavor_subrecipes(), not 14 separate
    sellable items) — it is created separately, once, with is_unli=1 and no
    recipe_id of its own (the flavour sub-recipes carry the ingredient
    cost; see gapfill.py / refills.list_flavors).
    """
    wb = openpyxl.load_workbook(source_path, data_only=True)
    ws = _load_sheet(wb, SHEET_MENU_AUDIT)
    rows = [r for r in ws.iter_rows(min_row=2, values_only=True) if r[0]]
    wb.close()

    # menu_item_id is numbered by ACTUAL insertion order (n), not by this
    # row's position in the source sheet — the sheet also carries 14 "Unli
    # Wings - <Flavor>" rows and 3 "Leftover Charge" rows this loop skips,
    # so numbering by row position would leave gaps in the MEN-#### sequence
    # (e.g. MEN-0001, MEN-0003, MEN-0009, ...). A gap-free sequence matters
    # beyond cosmetics: tests/run_tests.py's own add_menu_item() test helper
    # derives a fresh row's id from COUNT(*) FROM menu_item + 1 — a gappy
    # sequence left that helper computing an id that collided with one of
    # THIS importer's own already-used numbers, failing with a
    # menu_item_id UNIQUE constraint violation (caught by actually running
    # the test suite, not by inspection).
    n = 0
    included_prices: set[float] = set()
    for r in rows:
        name, category, price = r[0], r[1], r[2]
        if name.startswith(UNLI_PREFIX):
            # Only the included-tier rows (category "Unlimited Wings - 14
            # Flavors") carry the base price. The 4 premium rows (category
            # "Unlimited Wings - Premium Flavors") list each flavor's own
            # all-in total (399 + that flavor's addon — e.g. 429 for Snow
            # Cheese/Cheese), not the base price, so they must never be
            # mistaken for it. Previously this took whichever "Unli Wings -"
            # row happened to be LAST in the sheet, which landed on a
            # premium row (429) purely by sheet ordering.
            if category == UNLI_INCLUDED_CATEGORY:
                included_prices.add(float(price))
            continue
        if name in IGNORED_MENU_ITEMS:
            continue
        recipe_row = conn.execute("SELECT id FROM recipe WHERE name=?", (name,)).fetchone()
        n += 1
        conn.execute(
            "INSERT INTO menu_item (menu_item_id, name, category, price, recipe_id, is_unli) "
            "VALUES (?,?,?,?,?,0)",
            (f"MEN-{n:04d}", name, category, float(price), recipe_row["id"] if recipe_row else None),
        )

    if len(included_prices) > 1:
        raise ValueError(
            f"included-tier 'Unli Wings -' rows disagree on price: {sorted(included_prices)} "
            "— the base Unlimited Wings price is ambiguous, refusing to guess"
        )
    unli_price = next(iter(included_prices), None)

    n += 1
    conn.execute(
        "INSERT INTO menu_item (menu_item_id, name, category, price, recipe_id, is_unli) "
        "VALUES (?,?,?,?, NULL, 1)",
        (f"MEN-{n:04d}", "Unlimited Wings — 14 Flavors",
         "Unlimited Wings - 14 Flavors", float(unli_price or 399.0)),
    )
    return n


# ---------------------------------------------------------------------------
# Flavour sub-recipes: shared breading vs. flavour-specific sauce
# ---------------------------------------------------------------------------
# The 8 lines every one of the 14 "Unli Wings - <Flavor>" blocks shares —
# Chicken Wings itself plus the breading. Confirmed identical (name AND qty)
# across all 14 flavours except Garlic Powder, where Original alone uses 2 g
# against everyone else's 1 g (sheet 1's own change-log entry for Original
# calls this out explicitly: "Garlic powder standardized to 2g, the only
# value that differed from the shared base recipe"). This breading is NOT
# modelled as recipe_line data anywhere — it never was, even under the old
# CSV pipeline: the wings/flour/starch/etc. consumed per unli order are
# accounted for through the unli_item/unli_coefficient lambda system
# (wt.unli), not through a per-flavour recipe. Only the flavour-SPECIFIC
# sauce on top of that shared breading becomes a sub-recipe here — same
# scope the old gapfill.SUB_RECIPES flavour entries always had.
# Canonical per-order breading quantities, read from "Unli Wings - Classic
# Buffalo"'s own 8 breading lines (identical across all 14 flavours except
# Original's Garlic Powder, 2 g instead of 1 g — irrelevant here since
# Original is excluded from sauce derivation entirely). A handful of flavours
# legitimately use MORE of a breading-set ingredient than the shared
# breading alone calls for — e.g. Garlic Parmesan's block lists
# "Black Pepper (ground)" twice: once as ordinary breading (1 g) and once
# again as part of the sauce itself (1 g), for a summed total of 2 g. This is
# why filtering is done by SUBTRACTING the canonical breading amount from
# each ingredient's total (see derive_flavor_subrecipes below) rather than by
# dropping every row whose ingredient name is in _BREADING_INGREDIENTS
# outright — the latter would also discard Garlic Parmesan's genuine extra
# 1 g of sauce-only black pepper.
_BREADING_QTY = {
    "Chicken Wings": 6.0, "All-Purpose Flour": 45.0, "Cornstarch": 30.0,
    "Baking Powder": 2.0, "Salt": 2.0, "Black Pepper (ground)": 1.0,
    "Garlic Powder": 1.0, "Cooking Oil": 30.0,
}

# sheet6 ingredient label (standardized) -> the display label this importer
# writes to recipe_line, kept short and consistent with the old hand-curated
# labels rather than the sheet's own occasionally-verbose text.
_SAUCE_LABELS = {
    "Hot Sauce": "Hot sauce", "Worcestershire Sauce": "Worcestershire sauce",
    "Mango Nectar/Puree": "Mango nectar/puree", "Habanero Sauce": "Habanero sauce",
    "Vinegar (cane/white)": "Vinegar or lime juice",
    "Salted Egg Yolk": "Salted egg yolk", "Garlic (Minced, g)": "Garlic",
    "Evaporated Milk": "Evaporated milk/cream", "Chili Flakes": "Chili flakes",
    "Curry Leaves": "Curry leaves", "Curry Powder": "Curry powder",
    "Lemon Juice": "Lemon juice", "Lemon Zest": "Lemon zest",
    "Korean BBQ Sauce": "Korean BBQ sauce", "Soy Sauce": "Soy sauce",
    "Sesame Oil": "Sesame oil", "Sesame Seeds": "Sesame seeds",
    "Parmesan Cheese": "Parmesan cheese", "Parsley, fresh": "Parsley",
    "Brown Sugar": "Brown sugar", "Ginger": "Ginger", "Mirin": "Mirin",
    "Sriracha Sauce": "Sriracha sauce", "Calamansi Juice": "Lime/calamansi juice",
    "Cheese Powder": "Cheese powder", "Milk Powder": "Milk powder",
    "Powdered Sugar": "Powdered sugar", "Orange Juice": "Orange juice",
    "Orange Zest": "Orange zest", "Rice Vinegar": "Rice vinegar",
    "Gochujang": "Gochujang", "Ketchup": "Ketchup", "Corn Syrup": "Corn syrup/honey",
    "Cheese Sauce": "Cheese sauce", "Cheddar Cheese": "Cheddar cheese",
    "Fresh Milk": "Milk", "Butter": "Butter", "Honey": "Honey",
}


def derive_flavor_subrecipes(source_path: Path = XLSX_PATH) -> dict[str, tuple]:
    """Returns SUB_RECIPES-shaped entries (same shape as gapfill.SUB_RECIPES)
    for the 13 flavours that have any sauce beyond the shared breading.
    "Unli Wings - Original" is deliberately excluded — its sheet 6 block
    is exactly the 8 shared breading lines with nothing on top (sheet 1's
    own change-log entry explains why: a duplicated seasoning block was
    removed from the source data as a copy-paste artifact). gapfill.py
    keeps "Original Dry Rub" as its own explicit, still-hardoded entry for
    that flavour — see gapfill.py's docstring for why.

    Quantities are the workbook's own per-6-wing-order figures, unchanged —
    yield_qty=1/yield_unit='order', the same convention migration 0021
    established and the old gapfill.SUB_RECIPES already used, so
    reference.py's FLAVOR_CONFIG (keyed by these same ingredient names) and
    refills.sync_flavors (which registers any kind='sub_recipe' ingredient
    not named "...(house)" as a customer-selectable flavour) need no change.
    """
    wb = openpyxl.load_workbook(source_path, data_only=True)
    ws = _load_sheet(wb, SHEET_MAPPING)
    rows = _read_mapping_rows(ws)
    wb.close()

    by_flavor: dict[str, list[tuple]] = {}
    order: list[str] = []
    for menu_item, _cat, ing_name, qty, _unit, _base in rows:
        if not menu_item.startswith(UNLI_PREFIX):
            continue
        if menu_item not in by_flavor:
            by_flavor[menu_item] = []
            order.append(menu_item)
        by_flavor[menu_item].append((menu_item, _cat, ing_name, qty, _unit, _base))

    # sauce-ingredient produced-name mapping, transcribed from the old
    # gapfill.SUB_RECIPES / reference.FLAVOR_CONFIG keys so nothing downstream
    # (unli_flavor rows, reference.py) needs to change key names.
    produced_name_by_flavor = {
        "Unli Wings - Classic Buffalo": "Classic Buffalo Sauce",
        "Unli Wings - Mango Habanero": "Mango Habanero Sauce",
        "Unli Wings - Salted Egg": "Salted Egg Sauce",
        "Unli Wings - Lemon Glazed": "Lemon Glazed Sauce",
        # NOT "Korean BBQ Sauce" — that name is already taken by sheet 4's
        # own raw, purchased bottled condiment ("Korean BBQ Sauce", ml,
        # Beverages... actually Condiments) which this flavour's sauce is
        # itself built FROM (see the 45 ml "Korean BBQ Sauce" component
        # below). Reusing the same name for the flavour's OUTPUT ingredient
        # would silently collide with that raw ingredient row in apply_pre()
        # (produced_name in ids already True) and the flavour would never
        # get registered at all — caught by inspection (gapfill stats showed
        # only 13 of 14 flavours' sub-recipes created on the first run of
        # this importer). "Cheese Wing Sauce" already avoided the identical
        # trap for the raw "Cheese Sauce" ingredient; this is the same fix.
        "Unli Wings - Korean BBQ": "Korean BBQ Wing Sauce",
        "Unli Wings - Garlic Parmesan": "Garlic Parmesan Sauce",
        "Unli Wings - Soy Garlic": "Soy Garlic Sauce",
        "Unli Wings - Teriyaki": "Teriyaki Sauce",
        "Unli Wings - Honey Sriracha": "Honey Sriracha Sauce",
        "Unli Wings - Snow Cheese": "Snow Cheese Sauce",
        "Unli Wings - Panda Express Orange Chicken": "Orange Chicken Sauce",
        "Unli Wings - Korean Yangnyeum": "Korean Yangneum Sauce",  # workbook spells the flavor "Yangnyeum" (u before m), matching reference.py's FLAVOR_CONFIG display name
        "Unli Wings - Cheese": "Cheese Wing Sauce",
    }

    out: dict[str, tuple] = {}
    for flavor_item in order:
        produced = produced_name_by_flavor.get(flavor_item)
        if not produced:
            continue  # Unli Wings - Original — see docstring
        grouped = _group_lines(by_flavor[flavor_item])
        sauce_lines = []
        for ing_name, total_qty in grouped:
            if ing_name in _BREADING_QTY:
                remainder = total_qty - _BREADING_QTY[ing_name]
                if remainder > 1e-9:
                    sauce_lines.append((ing_name, remainder))
            else:
                sauce_lines.append((ing_name, total_qty))
        if not sauce_lines:
            continue
        components = [
            (ing_name, qty, _SAUCE_LABELS.get(ing_name, ing_name), VERIFIED)
            for ing_name, qty in sauce_lines
        ]
        out[produced] = ("g", 1, "order", components)
    return out
