"""Phase 09A P1-1 — backup, verify, and restore, wired together as one
documented procedure: BACKUP -> VERIFY -> RESTORE -> VERIFY.

Restoring a database is one of the few operations in this codebase that can
destroy the current one if it goes wrong partway through, so every step in
restore() is ordered to make that specific failure mode structurally
impossible: nothing is ever deleted, only overwritten, and only after the
current database has already been safely copied aside and the incoming
backup has already been verified as a standalone file.

Run:
    python -m wt.backup_restore backup
    python -m wt.backup_restore verify <path>
    python -m wt.backup_restore restore <backup_path> --target development
    python -m wt.backup_restore restore <backup_path> --target production --confirm-production

See docs/BACKUP_RESTORE.md for the full procedure this implements and the
disposable-copy drill it was proven against. This module never opens,
reads, or writes the real production database as part of its own test
suite -- restore() against WT_ENV=production is exercised only far enough
to prove it refuses (see TestBackupRestore), never far enough to touch a
file.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

from . import db

VALID_TARGETS = ("development", "production", "test")


class RestoreError(RuntimeError):
    """A backup/restore precondition failed and the operation was refused
    before touching any file. If this is ever raised partway through
    restore(), that is itself a bug -- every guard runs before the first
    write; see restore()'s own comments for exactly where the point of no
    return is (there isn't one: the current file is only ever overwritten
    with something already proven restorable, never deleted outright)."""


def verify(path: str | Path) -> dict:
    """Opens `path` as its own independent connection -- never through
    db.db_path()/db.get_conn(), because this has to be able to inspect a
    backup file sitting in backend/backups/, which is never "the current
    database" -- and runs every check the go-live procedure calls for:
    SQLite's own integrity_check and foreign_key_check, schema version
    against the head this codebase currently ships, key row counts, the
    FEFO invariant (SUM of open batch quantities == on_hand_base), and the
    ledger invariant (each ingredient's most recent stock_ledger row's
    balance_after_base == on_hand_base).

    "No orphan records" is not a separate query here: every table in this
    schema is created with foreign keys enforced (PRAGMA foreign_keys=ON,
    set on every connection this project opens), so an orphan row IS a
    foreign-key violation by construction -- foreign_key_check already
    covers it exhaustively. A hand-rolled per-table orphan scan alongside
    it would just be a second, weaker copy of the same check with its own
    chance to drift from the real one.
    """
    path = Path(path)
    if not path.exists():
        raise RestoreError(f"no such backup file: {path}")

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        version = db.applied_version(conn)
        head = max((v for v, _, _ in db._discover()), default=0)

        counts: dict[str, int | None] = {}
        for table in ("ingredient", "customer_order", "stock_ledger", "fefo_batch", "app_user"):
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            except sqlite3.OperationalError:
                counts[table] = None

        fefo_drift = [r["name"] for r in conn.execute(
            """SELECT i.name FROM ingredient i
                LEFT JOIN fefo_batch b ON b.ingredient_id = i.id AND b.state='open'
                GROUP BY i.id
               HAVING ABS(i.on_hand_base - COALESCE(SUM(b.qty_base), 0)) > 0.001"""
        ).fetchall()]

        ledger_drift = [r["name"] for r in conn.execute(
            """SELECT i.name FROM ingredient i
                JOIN stock_ledger l ON l.id = (
                    SELECT MAX(id) FROM stock_ledger WHERE ingredient_id = i.id
                )
               WHERE ABS(i.on_hand_base - l.balance_after_base) > 0.001"""
        ).fetchall()]

        schema_up_to_date = version == head
        ok = (
            integrity == "ok" and fk_violations == 0 and schema_up_to_date
            and not fefo_drift and not ledger_drift
        )
        return {
            "path": str(path),
            "ok": ok,
            "integrity_check": integrity,
            "foreign_key_violations": fk_violations,
            "orphan_records": fk_violations,  # same check -- see docstring
            "schema_version": version,
            "schema_head": head,
            "schema_up_to_date": schema_up_to_date,
            "row_counts": counts,
            "fefo_drift": fefo_drift,
            "ledger_drift": ledger_drift,
        }
    finally:
        conn.close()


def backup(reason: str = "manual") -> dict:
    """The BACKUP half. Backs up whatever db.db_path() currently resolves
    to -- there is no separate "which database" argument, so this always
    runs under the same WT_ENV/WT_DB gate as everything else in this
    codebase -- then immediately verifies the file it just wrote."""
    conn = db.get_conn()
    dest = db.backup_now(reason, conn=conn)
    if dest is None:
        raise RestoreError(
            "nothing to back up -- no database file exists yet at "
            f"{db.db_path()}"
        )
    return {"backup_path": str(dest), "verify": verify(dest)}


def restore(backup_path: str | Path, target: str, *, confirm_production: bool = False) -> dict:
    """The RESTORE half. Every guard below runs before anything on disk is
    touched -- if any of them raises, the current database is
    byte-for-byte untouched.

      1. backup_path must exist and must itself pass verify() -- a broken
         backup is refused outright rather than restored over a working
         database.
      2. target must be given explicitly and must equal db.environment():
         a caller passing target="development" while WT_ENV=production is
         actually configured is refused as an ambiguous target, not
         silently resolved by trusting one side over the other.
      3. target=="production" additionally requires confirm_production=
         True. WT_ENV=production and target="production" agreeing is not
         enough on its own -- this is the separate "explicit production
         restore operation" the go-live checklist calls for, kept as its
         own boolean specifically so environment configuration alone can
         never satisfy it.

    Once past the guards: the live connection is checkpointed and closed,
    the current file is preserved as an emergency pre-restore backup, its
    stale WAL/SHM sidecars are cleared (they belong to the database being
    replaced, not the one about to take its place), the verified backup is
    copied into place, the database is reopened, and it is verified again
    -- now as the live database, not just a standalone file.
    """
    backup_path = Path(backup_path)
    if target not in VALID_TARGETS:
        raise RestoreError(f"target must be one of {VALID_TARGETS}, got {target!r}")

    env = db.environment()
    if env != target:
        raise RestoreError(
            f"ambiguous target: restore() was called with target={target!r} but "
            f"WT_ENV={env!r} is currently configured. These must agree — refusing "
            "rather than guessing which one is right."
        )
    if target == "production" and not confirm_production:
        raise RestoreError(
            "restoring the production database requires confirm_production=True "
            "in addition to WT_ENV=production and target='production'. This is "
            "deliberately not satisfiable by environment configuration alone."
        )

    pre_check = verify(backup_path)
    if not pre_check["ok"]:
        raise RestoreError(
            f"refusing to restore {backup_path}: it fails its own verification "
            f"({pre_check}). A broken backup must not be restored over a working "
            "database."
        )

    current_path = db.db_path()

    # Checkpoint while still connected, so nothing recently committed is
    # left sitting only in the -wal sidecar when the emergency backup below
    # copies the plain file.
    conn = db.get_conn()
    if current_path.exists():
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # 1. stop/close the database before replacement.
    db.close_conn()

    # 2. preserve the CURRENT database as an emergency pre-restore backup
    #    -- nothing has been deleted or overwritten yet at this point.
    emergency_backup = None
    if current_path.exists():
        emergency_backup = db.backup_now("pre_restore_emergency", path=current_path)

    # 3. clear stale WAL/SHM sidecars for the CURRENT file. They belong to
    #    the database being replaced; leaving them in place would make
    #    SQLite try to replay old WAL frames against unrelated restored
    #    content the next time this file is opened.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(current_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()

    # 4. copy the verified backup into place. copy2 does not remove
    #    backup_path -- it stays exactly where it was, as its own file.
    current_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, current_path)

    # 5. reopen and register, the same way db.reset() does, so a later
    #    get_conn() in this thread does not open a second handle on the
    #    same file.
    db.open_and_register(current_path)

    # 6. verify the RESTORED, now-live database.
    post_check = verify(current_path)

    return {
        "ok": post_check["ok"],
        "restored_from": str(backup_path),
        "restored_to": str(current_path),
        "emergency_backup": str(emergency_backup) if emergency_backup else None,
        "pre_restore_verify": pre_check,
        "post_restore_verify": post_check,
    }


def _print_report(report: dict) -> None:
    print(f"  ok:                    {report['ok']}")
    print(f"  integrity_check:       {report['integrity_check']}")
    print(f"  foreign_key_violations:{report['foreign_key_violations']}")
    print(f"  schema:                v{report['schema_version']} (head v{report['schema_head']}, "
          f"up to date: {report['schema_up_to_date']})")
    print(f"  row_counts:            {report['row_counts']}")
    print(f"  fefo_drift:            {report['fefo_drift'] or 'none'}")
    print(f"  ledger_drift:          {report['ledger_drift'] or 'none'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("backup", help="back up the currently configured database and verify it")

    p_verify = sub.add_parser("verify", help="verify an arbitrary database file, live or backup")
    p_verify.add_argument("path")

    p_restore = sub.add_parser("restore", help="restore a backup over the currently configured database")
    p_restore.add_argument("backup_path")
    p_restore.add_argument("--target", required=True, choices=VALID_TARGETS)
    p_restore.add_argument("--confirm-production", action="store_true", dest="confirm_production")

    args = ap.parse_args()

    if args.command == "backup":
        result = backup()
        print(f"database: {db.db_path()}")
        print(f"backup written to: {result['backup_path']}")
        _print_report(result["verify"])
        return 0 if result["verify"]["ok"] else 1

    if args.command == "verify":
        report = verify(args.path)
        _print_report(report)
        return 0 if report["ok"] else 1

    if args.command == "restore":
        if args.target == "production":
            if not args.confirm_production:
                print("refusing: --target production also requires --confirm-production",
                      file=sys.stderr)
                return 1
            # Extra, human-facing layer on top of restore()'s own guards --
            # the same typed-filename confirmation every other destructive
            # operator command in this codebase requires.
            db.require_destructive_confirmation(
                f"restore {args.backup_path} over the production database"
            )
        result = restore(args.backup_path, args.target, confirm_production=args.confirm_production)
        print(f"restored {result['restored_from']} -> {result['restored_to']}")
        print(f"emergency pre-restore backup: {result['emergency_backup']}")
        print("post-restore verification:")
        _print_report(result["post_restore_verify"])
        return 0 if result["ok"] else 1

    return 1  # pragma: no cover -- argparse's `required=True` makes this unreachable


if __name__ == "__main__":
    sys.exit(main())
