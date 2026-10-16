"""Import the owner's Menu & Ingredient Audit workbook into the typed master.

Run:  python -m wt.seed [--fresh]

Everything this importer writes that was not directly measured by a human is
flagged 'needs_verification'. Nothing is silently defaulted into looking real.

2026-09 note — this module used to read two hand-tidied CSVs
(seed_data/inventory_tidy.csv, seed_data/product_item_breakdown_tidy.csv)
through a curated alias table (wt/mapping.py) built for a spreadsheet that
was full of gaps and inconsistent labels. The owner has since supplied
data/Wings_Theory_Menu_Ingredient_Audit.xlsx — a purpose-built audit
workbook that already carries the full ingredient master, one already-
standardized recipe per menu item, and menu pricing, with every ingredient
name used consistently across its sheets. wt/xlsx_menu_audit_import.py reads
it directly; this module is now just the orchestration seed.run() has always
provided (ordering, gapfill, reference configuration, users, stats), not a
second copy of the import logic. See that module's docstring for exactly
what changed and why an alias table is barely needed any more.
"""
from __future__ import annotations

import argparse
import sys

from . import db, gapfill, xlsx_menu_audit_import as menu_audit

NEEDS = "needs_verification"

# ---------------------------------------------------------------------------
# The unli vector. λ values from the manuscript where one exists, otherwise
# unmeasured — all four seeded needs_verification.
#
# "wings" used to point at a gram-tracked "Wings" ingredient (612.50 kg /
# 725 covers = 844.8 g/head, from the manuscript). The workbook's
# "Chicken Wings" is piece-tracked instead (an explicit owner decision — see
# its own sheet 4 description: "use whole PIECE as the unit throughout...
# Stock on hand = actual piece count"), so the same manuscript figure is
# restated in pieces using the workbook's own stated average raw weight for
# this SKU (~60 g/pc): 612500 g / 60 g/pc ≈ 10208.33 pieces / 725 covers ≈
# 14.08 pieces/head. This is a unit restatement of a real measurement, not a
# new one — flagged needs_verification like the other three, same as before.
#
# "drinks" used to point at "Red Tea Powder" (a bulk powder SKU); the
# workbook's beverage ingredient for iced tea is "Black Tea Bag" (a bagged,
# piece-counted SKU) — that ingredient no longer exists, so the stream now
# tracks bag count instead of grams. Still an unmeasured placeholder either
# way.
# ---------------------------------------------------------------------------
_WINGS_LAMBDA_GRAMS_PER_HEAD = 844.8
_WINGS_AVG_GRAMS_PER_PIECE = 60.0  # sheet4's own stated average for Chicken Wings (ING-021)

UNLI_SEED = [
    ("wings", "Unlimited Wings", "Chicken Wings",
     round(_WINGS_LAMBDA_GRAMS_PER_HEAD / _WINGS_AVG_GRAMS_PER_PIECE, 4),
     "manuscript p.24: 612.50 kg / 725 covers, restated in pieces at ~60 g/pc "
     "(the workbook's own stated average raw weight) since Chicken Wings is "
     "now piece-tracked, not gram-tracked"),
    ("rice", "Unlimited Rice", "Cooked White Rice", 380.0,
     "no measurement on record — placeholder"),
    ("fries", "Unlimited Fries", "Frozen French Fries", 210.0,
     "no measurement on record — placeholder"),
    ("drinks", "Unlimited Drinks", "Black Tea Bag", 18.0,
     "no measurement on record — placeholder; bag-count based (Black Tea Bag) "
     "rather than the old gram-based Red Tea Powder, which the workbook does "
     "not carry"),
]

_WINGS_TOTAL_ACTUAL_BASE = round(612500.0 / _WINGS_AVG_GRAMS_PER_PIECE, 4)
_WINGS_TOTAL_HEADS = 725


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
            # 'baseline' is seeded already 'verified', never NEEDS: it is the
            # frozen anomaly-detection reference, not a pending estimate, and
            # v_needs_verification excludes role='baseline' outright (see
            # migrations/0020) -- but a row seeded with the wrong status here
            # would still misrepresent its own state to anything that reads
            # unli_coefficient.status directly. 'rolling' keeps starting as
            # NEEDS: it genuinely is an unconfirmed estimate until an owner
            # reviews it.
            conn.execute(
                """
                INSERT INTO unli_coefficient
                  (unli_item_id, lambda_value, role, total_actual_base, total_heads, status)
                VALUES (?,?,?,?,?,?)
                """,
                (item_pk, lam, role,
                 _WINGS_TOTAL_ACTUAL_BASE if key == "wings" else None,
                 _WINGS_TOTAL_HEADS if key == "wings" else None,
                 "verified" if role == "baseline" else NEEDS),
            )
        n += 1
    return n


def seed_users(conn) -> None:
    """Each seeded account gets its own default password (not one placeholder
    shared across every role) and is flagged must_change_password=1 so an
    admin is still prompted to pick something private -- see migration
    0014's docstring for why the same flag is set there too (that UPDATE
    only reaches rows that already exist, so a fresh install needs it set
    here at INSERT time instead). Username and display name are both
    editable afterward from Users & Access (routes/admin/UsersAccess.js);
    these are starting values, not fixed identities.

    password_changed_at is set explicitly here rather than left to a
    column default -- the column has none (see migration 0014's docstring
    for why a NOT NULL DEFAULT CURRENT_TIMESTAMP column is not safe to add
    via ALTER TABLE against a populated table), so every INSERT path,
    including this one, is responsible for supplying a real value itself.
    """
    from .auth import hash_password
    # 'manager' seeds is_active=0 — the role is retired (migration 0034 flips
    # the same flag for a database that was already seeded before that
    # migration existed; this is what makes a brand-new install match that
    # same end state from the start, instead of a fresh install and an
    # upgraded one landing on different manager account states).
    for username, display, role, password, is_active in [
        ("Administrator", "R. Alonzo", "admin", "wingstheoryadd", 1),
        ("business_owner", "Business Owner", "owner", "ownerwingstheory", 1),
        ("manager", "M. Santos", "manager", "wingstheory", 0),
        ("cashier1", "J. Cruz", "cashier", "wingstheorycash", 1),
        ("kitchen1", "D. Lim", "kitchen", "wingstheorykds", 1),
    ]:
        h, s = hash_password(password)
        conn.execute(
            "INSERT OR IGNORE INTO app_user "
            "(username, display_name, role, password_hash, password_salt, "
            "password_changed_at, must_change_password, is_active) "
            "VALUES (?,?,?,?,?, datetime('now', 'subsec'), 1, ?)",
            (username, display, role, h, s, is_active),
        )


def run(fresh: bool = False) -> dict:
    conn = db.reset() if fresh else db.get_conn()
    if not fresh:
        db.migrate(conn)
    if conn.execute("SELECT COUNT(*) AS n FROM ingredient").fetchone()["n"]:
        print("database already seeded — pass --fresh to rebuild")
        return {}

    with db.write_tx(conn):
        ids = menu_audit.import_inventory(conn)
        # Flavour sub-recipes (13 workbook-derived + the empty "Original Dry
        # Rub" placeholder) MUST exist, in their final form, before
        # import_recipes() runs — see gapfill.apply_pre()'s docstring for the
        # ordering reason.
        gap_pre = gapfill.apply_pre(conn, ids)
        recipe_stats = menu_audit.import_recipes(conn, ids)
        gap_post = gapfill.apply_post(conn)
        n_menu = menu_audit.import_menu(conn)
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

    # Configuration from reference.py: the flavour display names/premium
    # prices and the unlimited portion settings. See that module's docstring
    # for what it does and does NOT do any more — most of its old recipe
    # correction tables were retired once the workbook itself became the
    # source of the corrected data.
    from .reference import apply_configuration
    ref = apply_configuration(conn)

    with db.write_tx(conn):
        dependent_rescaled = gapfill.rescale_dependent_recipe_lines(conn)

    gap = {**gap_pre, **gap_post, "dependent_lines_rescaled": dependent_rescaled}
    n_rec = recipe_stats["recipes"] + gap_pre["subrecipes_created"]

    sellable = conn.execute(
        "SELECT COUNT(*) AS n FROM v_menu_item_availability WHERE sellable=1"
    ).fetchone()["n"]
    needs = conn.execute("SELECT COUNT(*) AS n FROM v_needs_verification").fetchone()["n"]

    stats = {
        "ingredients": conn.execute(
            "SELECT COUNT(*) AS n FROM ingredient").fetchone()["n"],
        "recipes": n_rec,
        "recipe_lines": recipe_stats["recipe_lines"],
        "unmapped_lines": recipe_stats["unmapped_lines"],
        "workbook_assumption_problems": recipe_stats["workbook_assumption_problems"],
        "menu_items": n_menu,
        "unli_streams": n_unli,
        "unli_flavors": n_flavors,
        "reference_flavors_configured": ref["flavors"],
        "reference_settings_configured": ref["settings"],
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
    # in-process entrypoint several test classes call against their own
    # tempfile WT_DB in setUpClass, and forcing them through an interactive
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
