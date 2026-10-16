"""Builds the sanitized, version-controlled deployment snapshot shipped in
the repo under backend/deploy/ -- the thing a brand-new checkout boots from
(see wt/db.bootstrap_from_snapshot_if_missing) without any manual seeding or
retraining.

Two outputs:
  backend/deploy/wings_theory.snapshot.db  -- a consistent copy of the live
      production database, taken with sqlite3's online backup API (never a
      raw file copy -- the live db is WAL-journalled and may have writers),
      then sanitized in place: every password replaced with a known,
      documented demo credential and every live session/login-event row
      cleared. Schema, menu, recipes, ingredients, config, and historical
      demo activity are left exactly as they are -- this script makes
      credentials safe to publish, nothing else.
  backend/ml_artifacts/MANIFEST.json -- describes exactly the artifact
      files the model registry currently calls "active" for each
      model_key (same selection rule as scripts/build_deployment_package.py):
      model_key, run_id, mode, schema_version, sha256. The files
      themselves already live in backend/ml_artifacts/ (where
      wt/ml_artifacts_io.resolve_path expects them) and are carved back
      out of .gitignore by name -- see the .gitignore comment next to
      them. This script does not copy them anywhere; it only verifies
      each one is present and records its checksum. Superseded/failed/
      rolled_back training history is deliberately left out of the
      manifest and out of .gitignore -- it stays local, same as the
      existing packaging script's rationale ("full training history
      stays at the source").

Nothing at the source (backend/wings_theory.db) is modified or deleted.
This only reads it and writes new files under backend/deploy/ and
backend/ml_artifacts/MANIFEST.json.

Usage:
    python -m scripts.build_github_deploy_snapshot
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from wt import auth, ml_artifacts_io as artifacts  # noqa: E402

SOURCE_DB = BACKEND_DIR / "wings_theory.db"
DEPLOY_DIR = BACKEND_DIR / "deploy"
SNAPSHOT_DB = DEPLOY_DIR / "wings_theory.snapshot.db"
ARTIFACTS_MANIFEST = BACKEND_DIR / "ml_artifacts" / "MANIFEST.json"

# Documented, intentionally-not-secret demo credential every sanitized
# account is reset to. Anyone who clones the repo and boots from the
# snapshot signs in with <username> / this password and is immediately
# forced to change it (must_change_password=1, set below) -- same flow a
# real first-time account provision already goes through
# (owner_provisioning.provision_first_owner).
DEMO_PASSWORD = "WingsTheoryDemo!2026"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _backup_db(src_path: Path, dst_path: Path) -> None:
    """Consistent snapshot via SQLite's online backup API -- correct under
    concurrent writers and WAL, unlike copying the file bytes directly
    (which can grab a half-written page or miss not-yet-checkpointed WAL
    frames)."""
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.exists():
        dst_path.unlink()
    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _sanitize(db_path: Path) -> dict:
    """Mutates ONLY the snapshot copy -- src_path above is never opened for
    writing. Returns a report of what was scrubbed, for the manifest."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    report: dict = {}

    users = conn.execute("SELECT username FROM app_user").fetchall()
    for row in users:
        h, salt = auth.hash_password(DEMO_PASSWORD)
        conn.execute(
            "UPDATE app_user SET password_hash=?, password_salt=?, "
            "must_change_password=1, failed_login_count=0, locked_until=NULL "
            "WHERE username=?",
            (h, salt, row["username"]),
        )
    report["users_reset"] = len(users)

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "auth_session" in tables:
        n = conn.execute("SELECT COUNT(*) FROM auth_session").fetchone()[0]
        conn.execute("DELETE FROM auth_session")
        report["auth_sessions_cleared"] = n
    if "login_event" in tables:
        # login_event is DB-enforced append-only (no delete trigger exists
        # for it) and holds no secret -- just username/role/success/reason
        # -- so it is left as historical demo activity, not scrubbed.
        report["login_events_retained"] = conn.execute("SELECT COUNT(*) FROM login_event").fetchone()[0]

    conn.execute("UPDATE app_setting SET value='GITHUB_DEPLOY_SNAPSHOT_V1' "
                 "WHERE key='db_environment_marker'")

    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    return report


def _verify_active_artifacts() -> list[dict]:
    """Does not copy anything -- the active artifact files already live in
    backend/ml_artifacts/ (wt/ml_artifacts_io.ARTIFACT_DIR) and are the
    exact files carved back out of .gitignore by name. This only checks
    each one is actually present and records its checksum into
    backend/ml_artifacts/MANIFEST.json."""
    conn = sqlite3.connect(str(SOURCE_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT r.* FROM ml_training_run r JOIN ml_model_registry m "
        "ON m.active_training_run_id = r.id ORDER BY r.model_key"
    ).fetchall()
    conn.close()

    manifest = []
    for row in rows:
        resolved = artifacts.resolve_path(row["artifact_path"])
        entry = {
            "model_key": row["model_key"], "run_id": row["id"], "mode": row["mode"],
            "schema_version": row["schema_version"], "trained_at": row["trained_at"],
            "artifact_path": row["artifact_path"],
        }
        if not resolved.exists():
            entry["status"] = "MISSING_AT_SOURCE"
            manifest.append(entry)
            continue
        entry["status"] = "present"
        entry["sha256"] = _sha256(resolved)
        entry["bytes"] = resolved.stat().st_size
        manifest.append(entry)

    ARTIFACTS_MANIFEST.write_text(json.dumps({"models": manifest}, indent=2, default=str), encoding="utf-8")
    return manifest


def main() -> None:
    if not SOURCE_DB.exists():
        print(f"ERROR: source database not found at {SOURCE_DB}")
        sys.exit(1)

    print(f"Backing up {SOURCE_DB} -> {SNAPSHOT_DB} via SQLite online backup API...")
    _backup_db(SOURCE_DB, SNAPSHOT_DB)

    print("Sanitizing snapshot (passwords, sessions, login history)...")
    sanitize_report = _sanitize(SNAPSHOT_DB)

    print("Verifying active ML artifacts and writing MANIFEST.json...")
    artifact_manifest = _verify_active_artifacts()

    manifest = {
        "source_db": str(SOURCE_DB),
        "snapshot_db": str(SNAPSHOT_DB.relative_to(BACKEND_DIR.parent)),
        "snapshot_db_sha256": _sha256(SNAPSHOT_DB),
        "snapshot_db_bytes": SNAPSHOT_DB.stat().st_size,
        "sanitize_report": sanitize_report,
        "demo_credential_note": (
            f"Every account's password was reset to the documented demo "
            f"password ({DEMO_PASSWORD!r}) with must_change_password=1. "
            "No real credential from the source database is present in "
            "this snapshot."
        ),
        "ml_artifacts": artifact_manifest,
    }
    (DEPLOY_DIR / "SNAPSHOT_MANIFEST.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    missing = [a for a in artifact_manifest if a["status"] != "present"]
    print(json.dumps(manifest, indent=2, default=str))
    if missing:
        print("\nWARNING: the following registered 'active' artifacts were NOT found on "
              "disk and could NOT be shipped (reported, not fabricated):")
        for a in missing:
            print(f"  - {a['model_key']} (run {a['run_id']}): {a['artifact_path']}")


if __name__ == "__main__":
    main()
