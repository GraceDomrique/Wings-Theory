"""Fill the gaps the Menu & Ingredient Audit workbook still leaves after
import, and construct the flavour sub-recipes it defines as per-order
"Unli Wings - <Flavor>" recipe blocks rather than a single sauce-only table.

Two entry points, and the split matters — see the docstring on apply_pre()
for the bug it exists to prevent.

    apply_pre(conn, ids)   runs BEFORE xlsx_menu_audit_import.import_recipes().
                           Anything later recipe lines resolve to DIRECTLY —
                           today, only the flavour sub-recipes below, which
                           the single-flavour Ala Carte items' "Wing Sauce
                           (Selected Flavor...)" placeholder line redirects
                           to — must exist and be in its final form first.

    apply_post(conn)      runs AFTER import_recipes(). Resolves whatever
                           landed in the unmapped queue (expected to be at
                           or near empty now — see that module's docstring
                           for why the workbook needs almost no alias table).

2026-09 note — retired since the switch to the Menu & Ingredient Audit
workbook (see xlsx_menu_audit_import.py): the old ADDITIONS / RECONCILED_
INGREDIENTS / PIECE_WEIGHTS tables existed to patch gaps in the CSV+
hardcoded-dict pipeline (ingredients the old stock sheet didn't carry at
all, or counted in pieces while recipes wanted grams). The new workbook's
'4. Master Ingredient Inventory' sheet already carries all 108 ingredients
this database uses, already in the unit each recipe consumes them in
(verified: sheet 6's 'Base Qty' equals its own 'Recipe Quantity' for every
row — see xlsx_menu_audit_import.py's docstring) — so none of those three
gap-filling tables have anything left to do, and are deleted rather than
kept as dead code.
"""
from __future__ import annotations

from .xlsx_menu_audit_import import derive_flavor_subrecipes as _derive_flavor_subrecipes

NEEDS = "needs_verification"
VERIFIED = "verified"
UNSET = "unset"

# ---------------------------------------------------------------------------
# Sub-recipes: fourteen flavour sauces plus three house preparations, all
# defined by the 08-11 BOM sheet as ratios or percentages rather than literal
# ingredient rows (raw_import.py skips those blocks entirely — see its
# module docstring). Each entry is
#     produced_name: (base_unit, yield_qty, yield_unit, [component, ...])
# where a component is either
#     (ingredient_name, qty)                                   -- old style
#     (ingredient_name, qty, display_label, qty_status)        -- verified
# apply_pre() tells them apart by tuple length: the 4-tuple form writes the
# given display_label/qty_status straight to recipe_line and skips the
# "reconciled ratio" note, matching how migration 0021 writes these same
# rows for an already-seeded database.
#
# Thirteen of the fourteen flavours (all but Original Dry Rub, excluded by
# explicit business decision — see migration 0021's header) are transcribed
# 1:1 from that migration's Section 2/3: the workbook's verified per-one-
# 6-pc-order recipe, yield_qty=1/yield_unit='order', in place of the
# earlier "100 g nominal batch" placeholder this table used to declare (that
# batch was never itself confirmed against a real kitchen recipe — see the
# migration's header for the full history). Keeping this table in sync with
# the migration is exactly the point: a brand-new database must reach the
# same recipe state as one that started on the old model and was migrated
# through 0021, not silently keep serving the old placeholder forever.
#
# House preparations are unaffected by migration 0021 and keep the same
# 1000 g/ml assumed-batch model, still needs_verification, as before. They
# are listed FIRST because Cheese Wing Sauce's component list below
# references "Cheese Sauce (house)" as an ingredient, and apply_pre() walks
# this dict in order — the producing sub-recipe must already exist (and be
# in `ids`) before a later sub-recipe can consume it.
# ---------------------------------------------------------------------------
SUB_RECIPES: dict[str, tuple[str, float, str, list[tuple]]] = {
    # House preparations, defined as percentage-of-batch formulas
    # ("Cheese Sauce (Sub-Recipe Definition)" etc.) rather than a fixed
    # recipe. No batch size is stated, so 1000 g / 1000 ml is assumed —
    # consistent with the "per kg wings" convention used everywhere else —
    # and flagged needs_verification like every other assumption here. Water
    # and plain lemon-juice-as-a-percentage aren't stocked ingredients, so
    # they're simply omitted rather than guessed at; the sugar/mayo/relish
    # figures below are what actually gets deducted.
    # Original Dry Rub: verified against the real workbook, not assumed —
    # '1. Menu Audit''s own change-log entry for "Unli Wings - Original"
    # says outright: "Removed a duplicated Salt/Black pepper/Garlic powder
    # block (identical to the base breading) that appeared to be a
    # copy-paste artifact in the source file." '6. Menu-to-Ingredient
    # Mapping''s Original block is now EXACTLY the 8 shared breading lines
    # (see xlsx_menu_audit_import._BREADING_QTY) with nothing on top — i.e.
    # the current source of truth says Original has no flavour-specific
    # seasoning distinguishable from the shared breading at all.
    #
    # This table's first attempt at reflecting that literally — an EMPTY
    # sub-recipe (zero components) — is NOT viable and was caught by actually
    # replaying the order-history workbook through the live service layer,
    # not just by seeding: refills._record_refill_tx() hard-refuses to
    # record a refill for a flavour that "resolves to no deductable
    # ingredients" (a real guard against a silently-broken flavour, not a
    # bug), so every historical "Unli Wings - Original" order fails outright
    # the moment a customer picks Original. So this keeps the OLD hardcoded
    # 100 g nominal-batch recipe (Salt/Black Pepper/Garlic Powder/Paprika/
    # Cornstarch — restated here against the new ingredient names, all of
    # which the workbook does stock directly) as the one deliberate exception
    # this table still hardcodes rather than derives — needs_verification,
    # exactly as unverified as it always was. Flagged for the owner: the
    # workbook's own audit says this seasoning is redundant with the shared
    # breading; the system's own refill guard says the flavour needs a
    # non-empty recipe regardless of what the workbook says. Both are real;
    # reconciling them (e.g. by deducting a token amount vs. revisiting the
    # refill guard) is a product decision, not an import decision.
    "Original Dry Rub": ("g", 100, "g", [
        ("Salt", 25.0), ("Black Pepper (ground)", 15.0), ("Garlic Powder", 25.0),
        ("Paprika", 20.0), ("Cornstarch", 15.0)]),

    # The 13 reconciled flavours' sub-recipes (Classic Buffalo Sauce, Mango
    # Habanero Sauce, ... Cheese Wing Sauce) are no longer hardcoded here at
    # all — merged in below, computed fresh from the workbook at IMPORT TIME
    # by xlsx_menu_audit_import.derive_flavor_subrecipes(), so this table
    # never drifts from '6. Menu-to-Ingredient Mapping' and both this
    # module-level constant AND apply_pre() below see the identical, already-
    # merged dict (tests that read gapfill.SUB_RECIPES directly, e.g.
    # TestReferenceAlignment, need no special-casing for where a given
    # flavour's entry came from). See that function's docstring for the
    # shared-breading/sauce-only split and the ingredient-name mapping (kept
    # identical to the names this table used before, so reference.FLAVOR_CONFIG
    # and refills.sync_flavors need no change).
    **_derive_flavor_subrecipes(),
}

#
# The three old "house preparation" sub-recipes (Cheese Sauce (house),
# Tartar Sauce (house), Simple Syrup (house)) are gone, not merely renamed:
# verified against the real workbook, every recipe that used to reference
# one now lists its raw components directly instead (Fish & Chips lists
# Mayonnaise/Pickle Relish/Lemon Juice; the three shakes list Fresh Milk/
# Ice/White Sugar; Cheesy Bacon Fries and "Unli Wings - Cheese" both
# reference a now-directly-purchased "Cheese Sauce" ingredient rather than
# a house recipe that produces one) — so nothing in this table replaces
# them; they simply have no successor.

# ---------------------------------------------------------------------------
# Queue entries that need a decision rather than a new ingredient. Retired
# along with mapping.py's old normalise_label()-keyed alias tables — the
# workbook resolves essentially everything directly (see
# xlsx_menu_audit_import.py's docstring), so RESOLUTIONS/DISMISS are left
# empty rather than deleted: apply_post() below is still a useful safety net
# for the rare row that genuinely can't resolve, and an owner reviewing the
# unmapped queue may still want to wire a decision back in here.
# ---------------------------------------------------------------------------
RESOLUTIONS: dict[str, tuple[str, str]] = {}

# Lines that are genuinely not inventory and are dismissed outright.
DISMISS = {"water"}


def _create_ingredient(conn, name, category, base_unit, purchase_unit,
                        pack_size, unit_cost, next_id,
                        pack_size_status=NEEDS, unit_cost_status=NEEDS) -> int:
    cur = conn.execute(
        """
        INSERT INTO ingredient
          (ingredient_id, name, category, kind, base_unit, purchase_unit,
           pack_size, pack_size_status, yield_factor, yield_factor_status,
           on_hand_base, unit_cost, unit_cost_status, lead_time_days)
        VALUES (?,?,?, 'raw', ?,?, ?,?, 1.0, ?, 0, ?, ?, ?)
        """,
        (next_id, name, category, base_unit, purchase_unit, pack_size, pack_size_status,
         NEEDS, unit_cost, unit_cost_status, 2 if category == "wet_food" else 4),
    )
    return cur.lastrowid


def apply_pre(conn, ids: dict[str, int]) -> dict:
    """Everything that must be in its final form before
    xlsx_menu_audit_import.import_recipes() runs. Mutates `ids` in place so
    the caller's dict includes the new entries.

    The old steps 1/1b (ADDITIONS / RECONCILED_INGREDIENTS — new ingredients
    the CSV pipeline's stock sheet didn't carry) and 2 (PIECE_WEIGHTS — a
    piece-to-gram base-unit fix) are gone: the Menu & Ingredient Audit
    workbook's '4. Master Ingredient Inventory' sheet already carries all
    108 ingredients this database uses, in their final base unit, via
    xlsx_menu_audit_import.import_inventory() — see that module's docstring.
    """
    added = repriced = 0
    next_seq = conn.execute("SELECT COUNT(*) AS n FROM ingredient").fetchone()["n"]

    # sub-recipes: ingredient + producing recipe + recipe lines, all created
    # together so the single-flavour Ala Carte items' "Wing Sauce (Selected
    # Flavor...)" placeholder line (see xlsx_menu_audit_import.py) can
    # resolve against "Classic Buffalo Sauce" the moment import_recipes()
    # reaches it. SUB_RECIPES already includes the workbook-derived flavours
    # (merged at module import time — see the comment above that dict).
    n_recipe = conn.execute("SELECT COUNT(*) AS n FROM recipe").fetchone()["n"]
    skipped_components = []
    subrecipes_created = 0
    for produced_name, (base_unit, yield_qty, yield_unit, components) in SUB_RECIPES.items():
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
            (f"REC-{n_recipe:04d}", produced_name, produces_pk, yield_qty, yield_unit),
        )
        recipe_pk = cur.lastrowid

        for order, component in enumerate(components, start=1):
            if len(component) == 4:
                component_name, qty, display_label, qty_status = component
                note = None
            else:
                component_name, qty = component
                display_label = component_name
                qty_status = NEEDS
                note = (f"reconciled from the 08-11 sheet's per-kg-wings ratio; "
                        f"nominal {int(yield_qty)} {base_unit} batch is an assumption")
            component_pk = ids.get(component_name)
            if not component_pk:
                skipped_components.append((produced_name, component_name))
                continue
            conn.execute(
                "INSERT INTO recipe_line (recipe_id, ingredient_id, display_label, "
                "qty_base, qty_status, sort_order, note) VALUES (?,?,?,?,?,?,?)",
                (recipe_pk, component_pk, display_label, qty, qty_status, order, note),
            )
        subrecipes_created += 1

    if skipped_components:
        # Would indicate a component name typo against the ingredient master —
        # surfaced loudly rather than silently dropping part of a sauce.
        raise RuntimeError(f"sub-recipe components not found in ids: {skipped_components}")

    return {"ingredients_added": added, "piece_weights_set": repriced,
            "subrecipes_created": subrecipes_created}


def rescale_dependent_recipe_lines(conn) -> int:
    """2026-09 note: retired as a no-op, kept as a function (rather than
    deleted) only because seed.run() still calls it in sequence and the call
    site is harmless to leave in place.

    Migration 0021 Section 4 rescaled a handful of OTHER menu recipes' flat
    sauce quantities (e.g. Wing Rack's flavour line) from "percent of a
    fictional 100 g batch" to "fraction of one real flavour order" — a
    correction needed only because the OLD CSV pipeline wrote those flat
    numbers straight from source text with no notion of the new yield model.
    The Menu & Ingredient Audit workbook needs no equivalent pass: every
    quantity xlsx_menu_audit_import.import_recipes() writes is already
    final — including the Wing Rack default-flavour override, written
    directly as 0.60 (see WING_RACK_DEFAULT_FLAVOR_QTY_BASE in that module)
    — and Spicy Korean Wing Bowl no longer references a flavour sub-recipe
    ingredient at all (verified: its sheet 6 block is 20 raw ingredients,
    Gochujang/Ketchup/Soy Sauce/etc., not "Korean BBQ Sauce" or "Korean
    Yangneum Sauce"), so there is nothing left for this function to do."""
    return 0


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
