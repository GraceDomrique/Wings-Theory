"""Fill the gaps the two spreadsheets still leave after import, and construct
the sub-recipes the 08-11 BOM sheet defines as ratios/percentages rather than
literal per-ingredient rows.

Two entry points, and the split matters — see the docstring on apply_pre()
for the bug it exists to prevent.

    apply_pre(conn, ids)   runs BEFORE import_recipes(). Anything that changes
                           an ingredient's base_unit, or that later recipe
                           lines will resolve to DIRECTLY (via ALIASES /
                           SUB_RECIPE_ALIASES / FLAVOUR_PLACEHOLDER, not the
                           unmapped queue), must exist and be in its final
                           form before recipe parsing starts.

    apply_post(conn)      runs AFTER import_recipes(). Resolves whatever
                           landed in the unmapped queue.
"""
from __future__ import annotations

NEEDS = "needs_verification"

# ---------------------------------------------------------------------------
# Ingredients consumed by recipes but still absent from the 08-11 stock
# sheet. The previous revision had eighteen of these; the owner's correction
# resolved fourteen of them into real, owner-counted inventory rows (see
# "Additional Ingredients (Recipe-Required)" — those now come through
# import_inventory() normally and are aliased directly in mapping.py). Panko
# is the one that is still genuinely not stocked.
#
# Starts at ZERO on hand with a needs_verification pack size — never an
# invented quantity. (name, category, base_unit, purchase_unit, pack_size,
# unit_cost)
# ---------------------------------------------------------------------------
ADDITIONS = [
    ("Panko Breadcrumbs", "dry_food", "g", "packs", 500, 145),
    # Required by two of the reference document's Section 3 flavour ratios —
    # Mango Habanero (5 g per 100 g batch) and Panda Express Orange Chicken
    # (10 g) — and absent from the stock sheet entirely. Section 2 prices it
    # at P45 for a 1000 ml bottle, marked [E] like every other pack size
    # there. Opens at zero stock: nobody has counted any.
    ("White Vinegar", "dry_food", "ml", "bottles", 1000, 45),
]

# ---------------------------------------------------------------------------
# Items the stock sheet counts in pieces while recipes ask for grams.
# Value = usable grams per piece, after peel/stone/trim.
# All needs_verification: these want a kitchen scale, not a reference table.
#
# Applied in apply_pre() — i.e. BEFORE import_recipes() converts any recipe
# quantity into base units. An earlier revision ran this step AFTER recipe
# import: White Eggs and Salted Eggs were seeded with base_unit='pc', recipe
# lines like "Eggs, 2 pcs (100 g)" resolved directly against that (writing
# recipe_line.qty_base = 2, meaning "2 pieces"), and only THEN did this step
# convert the ingredient to base_unit='g' — leaving every already-written
# recipe_line silently reinterpreted as "2 grams" instead of "100 grams", a
# 50x under-deduction on every recipe using eggs. Running this first means
# every recipe line — whether resolved immediately or via the queue later —
# always sees the ingredient's FINAL base unit.
# ---------------------------------------------------------------------------
PIECE_WEIGHTS = {
    "Mango":       180,   # flesh only, stone and skin discarded
    "Banana":      100,   # peeled
    "Lemon":        15,   # juice yield per fruit
    "Orange":        40,  # juice + zest
    "Salted Eggs":  20,   # mashed yolk only (see Salted Egg flavour sauce below
                           # for the separate whole-egg usage in Unlimited Wings)
    "White Eggs":   50,   # whole egg
}

# ---------------------------------------------------------------------------
# Sub-recipes: fourteen flavour sauces plus three house preparations, all
# defined by the 08-11 BOM sheet as ratios or percentages rather than literal
# ingredient rows (raw_import.py skips those blocks entirely — see its
# module docstring). Each entry is
#     produced_name: (base_unit, yield_qty, [(ingredient_name, qty), ...])
#
# The fourteen flavours are reconciled against the sheet's stated "~X g per
# kg wings" figures, starting from the FULLY-ITEMISED recipes the original
# manuscript specified (Wings_Theory_Inventory_Reference.md) and scaling
# proportionally to the new totals — the sheet gives a total, not a
# breakdown, so the itemisation is a documented estimate, not the owner's
# figure. Every quantity below is qty_status='needs_verification' in the
# database for exactly that reason.
#
# Three flavours needed judgment beyond simple proportional scaling, because
# the new sheet adds a named ingredient ON TOP of the base sauce rather than
# folding it in:
#   Salted Egg   "~100 g per kg wings + 1 Salted Egg (60-70 g) per kg wings"
#                — "incl crushed yolk" in the sheet's own wording, so the
#                mashed-yolk portion stays inside the scaled 100 g base, and
#                the whole extra egg (65 g, midpoint of the stated range) is
#                added on top. Both draw from the same stocked ingredient, so
#                they are summed into a single recipe line rather than two.
#   Lemon Glazed "~110 g per kg wings + zest/juice of 1 Lemon per kg wings"
#                — no "incl" here, so lemon is EXCLUDED from the base before
#                scaling to 110 g, then added back as one whole lemon (15 g,
#                matching the piece weight above) — not both, which an
#                earlier pass at this reconciliation got wrong by scaling the
#                old recipe's lemon component AND adding the new one,
#                double-counting it.
#   Orange Chicken  same pattern as Lemon Glazed: orange excluded from the
#                120 g base, one whole orange (40 g) added separately.
# ---------------------------------------------------------------------------
SUB_RECIPES: dict[str, tuple[str, float, list[tuple[str, float]]]] = {
    # -----------------------------------------------------------------------
    # The fourteen flavours, transcribed from Section 3 of the Inventory &
    # Recipe Reference: master ratios per 100 g of FINISHED SAUCE. Every one
    # sums to exactly 100 g, which is the point — the previous revision of
    # this table declared yield_qty=1000 while its lines summed to between
    # 14 g and 200 g, so `qty_base * portion / yield_qty` under-deducted every
    # flavour by a factor of 5 to 70. A 100 g batch that contains 100 g of
    # ingredients makes that arithmetic mean what it says, and lets any
    # portion scale off one ratio: a Sulit Deal at 35-40 g, a Wings Platter at
    # 60 g and an unlimited round at 40 g per head all multiply the same
    # numbers by (portion / 100).
    #
    # Still needs_verification, and the document says why: "None of the 14
    # flavors had a documented recipe, so these are proposed base ratios ...
    # kitchen should taste-test and adjust before locking in."
    #
    # Three ingredients are stocked in ml while the document states them in
    # grams (Evaporated Milk, Condense Milk, Soy Sauce). Their gram figures
    # are carried across 1:1 — units.to_base() correctly refuses a g<->ml
    # conversion without a per-ingredient density and none has been supplied,
    # so this is a stated ~3-10% approximation rather than a hidden one.
    # -----------------------------------------------------------------------
    "Classic Buffalo Sauce": ("g", 100, [
        ("Buffalo Hot Sauce", 70.0), ("Butter", 25.0),
        ("Garlic powder", 3.0), ("Honey", 2.0)]),
    # The document writes this as "Buffalo/Hot Sauce" — an either/or. It stays
    # on the generic Hot Sauce SKU, which is what the previous revision used;
    # the two remain distinct products.
    "Mango Habanero Sauce": ("g", 100, [
        ("Mango", 50.0), ("Hot Sauce", 20.0), ("Honey", 20.0),
        ("Chili Flakes Dehydrated", 5.0), ("White Vinegar", 5.0)]),
    "Salted Egg Sauce": ("g", 100, [
        ("Salted Eggs", 35.0), ("Evaporated Milk", 25.0), ("Butter", 20.0),
        ("Condense Milk", 10.0), ("Garlic Flakes", 10.0)]),
    "Lemon Glazed Sauce": ("g", 100, [
        ("Lemon", 25.0), ("Honey", 35.0), ("Butter", 20.0),
        ("Soy Sauce", 10.0), ("Cornstarch", 10.0)]),
    "Original Dry Rub": ("g", 100, [
        ("Iodized Salt", 25.0), ("Ground Pepper", 15.0), ("Garlic powder", 25.0),
        ("Paprika", 20.0), ("Cornstarch", 15.0)]),
    "Korean BBQ Sauce": ("g", 100, [
        ("Gochujang Big", 35.0), ("Soy Sauce", 20.0), ("Honey", 25.0),
        ("Fresh Garlic Peeled", 10.0), ("Sesame Seed", 10.0)]),
    "Garlic Parmesan Sauce": ("g", 100, [
        ("Butter", 40.0), ("Fresh Garlic Peeled", 25.0), ("Parmesan Cheese", 25.0),
        ("Parsley", 5.0), ("Iodized Salt", 5.0)]),
    "Soy Garlic Sauce": ("g", 100, [
        ("Soy Sauce", 40.0), ("Fresh Garlic Peeled", 25.0),
        ("Honey", 25.0), ("Butter", 10.0)]),
    "Teriyaki Sauce": ("g", 100, [
        ("Soy Sauce", 45.0), ("Honey", 30.0), ("Fresh Garlic Peeled", 10.0),
        ("Cornstarch", 10.0), ("Sesame Seed", 5.0)]),
    "Honey Sriracha Sauce": ("g", 100, [
        ("Honey", 50.0), ("Sriracha", 35.0), ("Butter", 15.0)]),
    "Snow Cheese Sauce": ("g", 100, [
        ("Cheese Powder", 35.0), ("Condense Milk", 25.0),
        ("Evaporated Milk", 20.0), ("Butter", 20.0)]),
    "Orange Chicken Sauce": ("g", 100, [
        ("Orange", 30.0), ("Soy Sauce", 20.0), ("Refined Sugar", 25.0),
        ("White Vinegar", 10.0), ("Cornstarch", 10.0), ("Fresh Garlic Peeled", 5.0)]),
    "Korean Yangneum Sauce": ("g", 100, [
        ("Gochujang Big", 40.0), ("Honey", 30.0), ("Soy Sauce", 15.0),
        ("Fresh Garlic Peeled", 10.0), ("Sesame Seed", 5.0)]),
    # Renamed from the sheet's bare "Cheese" to avoid colliding with the
    # unrelated "Cheese Sauce" house dip used by Cheesy Bacon Fries.
    "Cheese Wing Sauce": ("g", 100, [
        ("Cheese Bars", 45.0), ("Cheese Powder", 20.0),
        ("Evaporated Milk", 25.0), ("Butter", 10.0)]),

    # House preparations, defined as percentage-of-batch formulas
    # ("Cheese Sauce (Sub-Recipe Definition)" etc.) rather than a fixed
    # recipe. No batch size is stated, so 1000 g / 1000 ml is assumed —
    # consistent with the "per kg wings" convention used everywhere else —
    # and flagged needs_verification like every other assumption here. Water
    # and plain lemon-juice-as-a-percentage aren't stocked ingredients, so
    # they're simply omitted rather than guessed at; the sugar/mayo/relish
    # figures below are what actually gets deducted.
    "Cheese Sauce (house)": ("g", 1000, [
        ("Cheese Bars", 600.0), ("All purpose Cream", 400.0)]),
    "Tartar Sauce (house)": ("g", 1000, [
        ("Mayonaise", 700.0), ("Ram Pouch pickle relish", 200.0), ("Lemon", 100.0)]),
    "Simple Syrup (house)": ("ml", 1000, [
        ("Refined Sugar", 500.0)]),
}

# ---------------------------------------------------------------------------
# Queue entries that need a decision rather than a new ingredient.
# label (normalised) -> (stock item name, rationale recorded on the alias)
# ---------------------------------------------------------------------------
RESOLUTIONS = {
    "japanese breadcrumbs": ("Panko Breadcrumbs", "panko is a distinct product from Bread Crumbs"),
}

# Lines that are genuinely not inventory and are dismissed outright.
DISMISS = {"water"}


def _create_ingredient(conn, name, category, base_unit, purchase_unit,
                        pack_size, unit_cost, next_id) -> int:
    cur = conn.execute(
        """
        INSERT INTO ingredient
          (ingredient_id, name, category, kind, base_unit, purchase_unit,
           pack_size, pack_size_status, yield_factor, yield_factor_status,
           on_hand_base, unit_cost, unit_cost_status, lead_time_days)
        VALUES (?,?,?, 'raw', ?,?, ?,?, 1.0, ?, 0, ?, ?, ?)
        """,
        (next_id, name, category, base_unit, purchase_unit, pack_size, NEEDS,
         NEEDS, unit_cost, NEEDS, 2 if category == "wet_food" else 4),
    )
    return cur.lastrowid


def apply_pre(conn, ids: dict[str, int]) -> dict:
    """Everything that must be in its final form before import_recipes() runs.
    Mutates `ids` in place so the caller's dict includes the new entries."""
    added = repriced = subrecipes_created = 0
    next_seq = conn.execute("SELECT COUNT(*) AS n FROM ingredient").fetchone()["n"]

    # 1. new ingredients still genuinely missing, zero stock
    for name, cat, base_unit, pu, pack, cost in ADDITIONS:
        if name in ids:
            continue
        next_seq += 1
        pk = _create_ingredient(conn, name, cat, base_unit, pu, pack, cost,
                                f"ING-{next_seq:04d}")
        ids[name] = pk
        added += 1

    # 2. piece-weight base-unit fix, BEFORE any recipe line can be parsed
    #    against these ingredients. Piece count is read back from the source
    #    CSV: at this point on_hand_base was computed with pack_size=None
    #    (unset), so it's 0 and the original count only survives in the file.
    import csv as _csv
    from pathlib import Path as _Path
    _seed = _Path(__file__).resolve().parent.parent / "seed_data" / "inventory_tidy.csv"
    source_counts: dict[str, float] = {}
    with _seed.open(encoding="utf-8-sig") as fh:
        for r in _csv.DictReader(fh):
            source_counts[r["Product Name"].strip()] = float(r["Product Count"])

    for name, grams in PIECE_WEIGHTS.items():
        row = conn.execute(
            "SELECT id, base_unit, pack_size, on_hand_base FROM ingredient WHERE name=?",
            (name,)).fetchone()
        if not row:
            continue
        pieces = source_counts.get(name)
        if pieces is None:
            pieces = (row["on_hand_base"] / row["pack_size"]) if row["pack_size"] else 0
        on_hand = pieces * grams
        conn.execute(
            "UPDATE ingredient SET base_unit='g', pack_size=?, pack_size_status=?, "
            "on_hand_base=? WHERE id=?",
            (grams, NEEDS, on_hand, row["id"]),
        )
        conn.execute(
            "UPDATE fefo_batch SET qty_base=? WHERE ingredient_id=? AND state='open'",
            (on_hand, row["id"]),
        )
        repriced += 1

    # 3. sub-recipes: ingredient + producing recipe + recipe lines, all
    #    created together so SUB_RECIPE_ALIASES / FLAVOUR_PLACEHOLDER in
    #    mapping.py can resolve against them the moment import_recipes()
    #    reaches a line like "Buffalo Sauce".
    n_recipe = conn.execute("SELECT COUNT(*) AS n FROM recipe").fetchone()["n"]
    skipped_components = []
    for produced_name, (base_unit, yield_qty, components) in SUB_RECIPES.items():
        if produced_name in ids:
            continue
        next_seq += 1
        cur = conn.execute(
            """
            INSERT INTO ingredient
              (ingredient_id, name, category, kind, sub_recipe_mode, base_unit,
               purchase_unit, pack_size, pack_size_status,
               yield_factor, yield_factor_status, unit_cost_status)
            VALUES (?,?, 'dry_food', 'sub_recipe', 'exploded', ?,
                    'batch', NULL, 'unset', 1.0, 'verified', 'unset')
            """,
            (f"ING-{next_seq:04d}", produced_name, base_unit),
        )
        produces_pk = cur.lastrowid
        ids[produced_name] = produces_pk

        n_recipe += 1
        cur = conn.execute(
            "INSERT INTO recipe (recipe_id, name, kind, produces_ingredient_id, "
            "yield_qty, yield_unit) VALUES (?,?,'sub_recipe',?,?,?)",
            (f"REC-{n_recipe:04d}", produced_name, produces_pk, yield_qty, base_unit),
        )
        recipe_pk = cur.lastrowid

        for order, (component_name, qty) in enumerate(components):
            component_pk = ids.get(component_name)
            if not component_pk:
                skipped_components.append((produced_name, component_name))
                continue
            conn.execute(
                "INSERT INTO recipe_line (recipe_id, ingredient_id, display_label, "
                "qty_base, qty_status, sort_order, note) VALUES (?,?,?,?,?,?,?)",
                (recipe_pk, component_pk, component_name, qty, NEEDS, order,
                 f"reconciled from the 08-11 sheet's per-kg-wings ratio; "
                 f"nominal {int(yield_qty)} {base_unit} batch is an assumption"),
            )
        subrecipes_created += 1

    if skipped_components:
        # Would indicate a component name typo against the ingredient master —
        # surfaced loudly rather than silently dropping part of a sauce.
        raise RuntimeError(f"sub-recipe components not found in ids: {skipped_components}")

    return {"ingredients_added": added, "piece_weights_set": repriced,
            "subrecipes_created": subrecipes_created}


def apply_post(conn) -> dict:
    """Resolve whatever import_recipes() could not map directly."""
    from .units import normalise_label, recipe_line_to_base

    resolved = dismissed = 0
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM ingredient")}

    for q in conn.execute(
        "SELECT * FROM unmapped_recipe_line WHERE state='open'"
    ).fetchall():
        norm = normalise_label(q["source_ingredient_label"])

        if norm in DISMISS:
            conn.execute(
                "UPDATE unmapped_recipe_line SET state='dismissed', resolved_by='gapfill', "
                "resolved_at=datetime('now'), note='not inventory' WHERE id=?", (q["id"],))
            dismissed += 1
            continue

        target, note = RESOLUTIONS.get(norm, (None, ""))
        pk = ids.get(target) if target else None
        if not pk:
            continue

        row = conn.execute(
            "SELECT base_unit, pack_size, yield_factor FROM ingredient WHERE id=?", (pk,)
        ).fetchone()
        try:
            qty_base = recipe_line_to_base(
                q["parsed_qty"] or 0, q["parsed_unit"] or row["base_unit"], row["base_unit"],
                row["pack_size"], row["yield_factor"],
            )
        except Exception:
            continue
        if qty_base <= 0:
            continue

        if q["recipe_id"]:
            conn.execute(
                "INSERT OR IGNORE INTO recipe_line (recipe_id, ingredient_id, display_label, "
                "qty_base, qty_status, sort_order, note) VALUES (?,?,?,?,?,900,?)",
                (q["recipe_id"], pk, q["source_ingredient_label"], qty_base, NEEDS, note or None),
            )
        conn.execute(
            "INSERT OR IGNORE INTO ingredient_alias (ingredient_id, alias_norm, alias_raw, "
            "source, created_by) VALUES (?,?,?, 'gapfill', 'gapfill')",
            (pk, norm, q["source_ingredient_label"]),
        )
        conn.execute(
            "UPDATE unmapped_recipe_line SET state='resolved', resolved_ingredient_id=?, "
            "resolved_by='gapfill', resolved_at=datetime('now'), note=? WHERE id=?",
            (pk, note or None, q["id"]),
        )
        resolved += 1

    return {"queue_resolved": resolved, "queue_dismissed": dismissed}
