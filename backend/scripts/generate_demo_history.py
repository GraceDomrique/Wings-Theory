"""Orchestrates a full synthetic-history generation run against the
isolated development database, and only that database.

This is the one entrypoint that should be used for phase-05-style
generation — not wt/demo_seed.py's own CLI, and not calling wt.seed or
wt.demo_seed by hand — because the safety checks below (path assertion,
backup, coefficient-date adjustment, before/after verification) are what
make the run safe, not the generator itself.

Run:  python scripts/generate_demo_history.py [--days 90] [--seed 42]

Refuses to run unless WT_DB resolves to backend/dev/wings_theory_dev.db.
Never opens, reads, or writes backend/wings_theory.db (production) at any
point — that file's path is never even constructed here.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEV_DB = BACKEND_DIR / "dev" / "wings_theory_dev.db"
PROD_DB = BACKEND_DIR / "wings_theory.db"


def _fail(msg: str) -> None:
    print(f"REFUSING TO PROCEED: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    # ---- 1. Target verification, before importing anything that could
    # touch a database ------------------------------------------------
    os.environ["WT_DB"] = str(DEV_DB)
    resolved = DEV_DB.resolve()
    print(f"Target database (explicit): {resolved}")
    if resolved == PROD_DB.resolve():
        _fail("WT_DB resolved to the production database path. Aborting before any write.")
    if "dev" not in resolved.parts or resolved.name != "wings_theory_dev.db":
        _fail(f"WT_DB does not look like the development database: {resolved}")
    print(f"Production database (never opened this run): {PROD_DB.resolve()}")

    sys.path.insert(0, str(BACKEND_DIR))
    from wt import db, seed, demo_seed   # noqa: E402  (import after WT_DB is set)

    if db.db_path().resolve() != resolved:
        _fail(f"db.db_path() resolved to {db.db_path()}, not the expected dev path.")

    # ---- 2. Backup before touching anything ---------------------------
    DEV_DB.parent.mkdir(parents=True, exist_ok=True)
    backup_dir = BACKEND_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"wings_theory_dev.pre_demo_history.{stamp}.db"
    if DEV_DB.exists():
        shutil.copy2(DEV_DB, backup_path)
        print(f"Backed up existing dev db to: {backup_path}")
    else:
        backup_path.write_bytes(b"")   # empty marker — nothing existed to back up
        print(f"No existing dev db — wrote an empty marker at: {backup_path}")

    # ---- 3. Catalog: real importer, not a hand-built one --------------
    conn = db.get_conn()
    db.migrate(conn)
    pre_report = _integrity_report(conn, label="before catalog import")

    n_before = conn.execute("SELECT COUNT(*) AS n FROM ingredient").fetchone()["n"]
    if n_before == 0:
        print("\nImporting catalog via wt.seed.run() (real importer, real spreadsheets)...")
        catalog_stats = seed.run(fresh=False)
        print(f"Catalog imported: {json.dumps(catalog_stats, default=str)}")
    else:
        print(f"\nCatalog already present ({n_before} ingredients) — skipping import, "
              "generating history on top of what's already there.")
        catalog_stats = {}

    # ---- 4. Coefficient effective_from — approved adjustment -----------
    # The only unlimited-order coefficients that exist are placeholder rows
    # (status='needs_verification') dated whenever the catalog was seeded —
    # after this run's synthetic history window entirely. Reconciliation
    # sizes its provisional figure per-order against the coefficient that
    # was effective at that order's own timestamp, so without this
    # adjustment every historical day would look like it had no provisional
    # deduction to reconcile against. This updates only the placeholder
    # rows' own effective_from column — not their lambda_value, not any
    # algorithm, not any other row.
    end = _dt.date.today() - _dt.timedelta(days=1)
    start = end - _dt.timedelta(days=args.days - 1)
    new_effective = f"{start.isoformat()} 00:00:00"
    coeff_rows = conn.execute(
        "SELECT id, unli_item_id, role, effective_from FROM unli_coefficient WHERE status='needs_verification'"
    ).fetchall()
    print(f"\nMoving effective_from back to {new_effective} on {len(coeff_rows)} "
          f"placeholder unli_coefficient row(s)...")
    with db.write_tx(conn):
        conn.execute(
            "UPDATE unli_coefficient SET effective_from=? WHERE status='needs_verification'",
            (new_effective,),
        )

    # ---- 5. Generate --------------------------------------------------
    print(f"\nGenerating {args.days} days of synthetic history, seed={args.seed}...")
    t0 = _dt.datetime.now()
    stats = demo_seed.run(conn, days=args.days, seed=args.seed, verbose=True)
    elapsed = (_dt.datetime.now() - t0).total_seconds()
    print(f"\nGeneration finished in {elapsed:.1f}s")
    print(json.dumps(stats, indent=2, default=str))

    # ---- 6. Verify ------------------------------------------------------
    post_report = _integrity_report(conn, label="after generation")

    result = {
        "database_path": str(resolved),
        "backup_path": str(backup_path),
        "seed": args.seed,
        "days": args.days,
        "generation_seconds": round(elapsed, 1),
        "catalog_import": catalog_stats,
        "generation_stats": stats,
        "integrity_before": pre_report,
        "integrity_after": post_report,
    }
    out_path = BACKEND_DIR / "scripts" / f"generation_report_{stamp}.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nFull report written to: {out_path}")
    return 0


def _integrity_report(conn, label: str) -> dict:
    print(f"\n--- integrity check: {label} ---")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())

    fefo_drift = 0
    for r in conn.execute(
        """
        SELECT i.id, i.on_hand_base,
               COALESCE((SELECT SUM(qty_base) FROM fefo_batch b
                          WHERE b.ingredient_id=i.id AND b.state='open'), 0) AS bs
          FROM ingredient i WHERE i.is_active=1
        """
    ):
        if abs(r["on_hand_base"] - r["bs"]) > 0.01:
            fefo_drift += 1

    ledger_drift = 0
    for r in conn.execute(
        """
        SELECT i.id, i.on_hand_base,
               (SELECT balance_after_base FROM stock_ledger l
                 WHERE l.ingredient_id=i.id ORDER BY l.id DESC LIMIT 1) AS last_bal
          FROM ingredient i WHERE i.is_active=1
        """
    ):
        if r["last_bal"] is not None and abs(r["on_hand_base"] - r["last_bal"]) > 0.01:
            ledger_drift += 1

    stale_reservations = conn.execute(
        "SELECT COUNT(*) AS n FROM reservation WHERE state='held'"
    ).fetchone()["n"]
    reserved_nonzero = conn.execute(
        "SELECT COUNT(*) AS n FROM ingredient WHERE reserved_base > 0.01"
    ).fetchone()["n"]
    orphan_recipe_lines = conn.execute(
        "SELECT COUNT(*) AS n FROM recipe_line rl LEFT JOIN ingredient i ON i.id=rl.ingredient_id WHERE i.id IS NULL"
    ).fetchone()["n"]
    orphan_batches = conn.execute(
        "SELECT COUNT(*) AS n FROM fefo_batch b LEFT JOIN ingredient i ON i.id=b.ingredient_id WHERE i.id IS NULL"
    ).fetchone()["n"]
    dup_skus = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT ingredient_id FROM ingredient GROUP BY 1 HAVING COUNT(*)>1)"
    ).fetchone()["n"]
    refill_state_mismatch = conn.execute(
        """
        SELECT COUNT(*) AS n FROM unli_refill_line rl JOIN unli_refill r ON r.id=rl.refill_id
         WHERE (r.status='served' AND rl.state!='committed')
            OR (r.status='voided' AND rl.state='held')
        """
    ).fetchone()["n"]

    report = {
        "integrity_check": integrity,
        "foreign_key_violations": fk_violations,
        "fefo_reconciliation_drift": fefo_drift,
        "ledger_reconciliation_drift": ledger_drift,
        "stale_reservations": stale_reservations,
        "ingredients_with_reserved_stock": reserved_nonzero,
        "orphan_recipe_lines": orphan_recipe_lines,
        "orphan_fefo_batches": orphan_batches,
        "duplicate_skus": dup_skus,
        "refill_line_state_mismatch": refill_state_mismatch,
    }
    for k, v in report.items():
        print(f"  {k}: {v}")
    return report


if __name__ == "__main__":
    sys.exit(main())
