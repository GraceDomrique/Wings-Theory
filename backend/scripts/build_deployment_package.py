"""Assembles a clean, deployable copy of the app: production runtime files
only (backend/wt/, backend/migrations/, backend/requirements.txt, the
production launcher, the production database, only the CURRENTLY ACTIVE
model artifacts, and the frontend/ static assets wt/api.py mounts and
serves directly), excluding tests/, venv/, backups/, dev/, and every
superseded/failed/rolled_back training artifact.

The target mirrors the real repo layout -- <target>/backend/... and
<target>/frontend/... as siblings -- because wt/api.py locates its static
assets as a fixed relative hop from its own file
(_STATIC_DIR = .../wt/api.py's parent.parent.parent), not via any
configurable path; packaging backend/ alone without frontend/ next to it
leaves the app unable to start at all (RuntimeError: StaticFiles directory
does not exist) -- found by actually booting uvicorn against the first
version of this script's output, not by inspection.

Nothing at the source is deleted or modified -- this only COPIES into a new
target directory, so the full training history (every artifact under
backend/ml_artifacts/, every row in ml_training_run, every backup under
backend/backups/) stays exactly where it already is, untouched. This is a
packaging step, not a cleanup step.

Usage:
    python scripts/build_deployment_package.py <target_dir>

Writes <target_dir>/DEPLOYMENT_MANIFEST.json (machine-readable: every file
copied, its size, and the active model registry) and
<target_dir>/DEPLOYMENT_MANIFEST.md (human-readable version of the same).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
FRONTEND_DIR = REPO_ROOT / "frontend"

# Whole directories copied verbatim (minus __pycache__), relative to
# BACKEND_DIR, landing under <target>/backend/.
RUNTIME_DIRS = ["wt", "migrations"]

# Individual files copied verbatim, relative to BACKEND_DIR, landing under
# <target>/backend/.
RUNTIME_FILES = ["requirements.txt", "start_server.ps1"]

# Explicitly NOT copied, and why -- kept here so the exclusion list is a
# reviewable decision, not an accident of what the glob happened to miss.
EXCLUDED = {
    "tests/": "test-only; never imported by wt/api.py at runtime",
    "venv/": "dev-only virtualenv; production rebuilds from requirements.txt",
    "backups/": "historical DB backups; kept at the source, not redistributed",
    "dev/": "dev-only databases (wings_theory_dev.db, preview.db)",
    "wings_theory_demo.db": "demo-only database, never read by production code",
    ".secret": "JWT signing secret; production generates/keeps its own, never copied",
    "start_server_dev.ps1": "dev-only launcher (binds loopback, passes --reload)",
    "scripts/": "packaging/ops tooling, not imported by the running app",
}


def _copytree_no_pycache(src: Path, dst: Path) -> list[Path]:
    copied = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel_root = Path(root).relative_to(src)
        (dst / rel_root).mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            s = Path(root) / f
            d = dst / rel_root / f
            shutil.copy2(s, d)
            copied.append(d)
    return copied


def build(target: Path) -> dict:
    """Lays down <target>/backend/... and <target>/frontend/... as
    siblings, mirroring the real repo so wt/api.py's relative _STATIC_DIR
    hop finds frontend/ in the same place it always does."""
    backend_target = target / "backend"
    backend_target.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "runtime_dirs": {}, "runtime_files": [], "frontend": None, "database": None, "ml_artifacts": [],
    }

    for d in RUNTIME_DIRS:
        copied = _copytree_no_pycache(BACKEND_DIR / d, backend_target / d)
        manifest["runtime_dirs"][d] = {
            "file_count": len(copied),
            "total_bytes": sum(p.stat().st_size for p in copied),
        }

    for f in RUNTIME_FILES:
        src = BACKEND_DIR / f
        shutil.copy2(src, backend_target / f)
        manifest["runtime_files"].append({"path": f, "bytes": src.stat().st_size})

    # frontend/ -- static POS/KDS/console/landing HTML + assets wt/api.py
    # mounts and serves directly. Lives one level above backend/ in the
    # real repo; the app will not even start without it next to backend/.
    frontend_copied = _copytree_no_pycache(FRONTEND_DIR, target / "frontend")
    manifest["frontend"] = {
        "file_count": len(frontend_copied),
        "total_bytes": sum(p.stat().st_size for p in frontend_copied),
    }

    # Production database -- the live, real file, not a fixture. SQLite is
    # in WAL mode, so recent commits can sit in wings_theory.db-wal, not yet
    # merged into the main file; copying wings_theory.db alone would ship a
    # STALE snapshot missing whatever hasn't been checkpointed yet (this
    # bit the first version of this script -- the freshly trained/active
    # runs went missing from the packaged copy because the training commits
    # were still only in -wal). Checkpointing with TRUNCATE first forces
    # every committed write back into the main file and empties -wal, so a
    # single-file copy is always complete and self-contained.
    db_src = BACKEND_DIR / "wings_theory.db"
    checkpoint_conn = sqlite3.connect(db_src)
    checkpoint_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint_conn.close()
    shutil.copy2(db_src, backend_target / "wings_theory.db")
    manifest["database"] = {"path": "backend/wings_theory.db", "bytes": db_src.stat().st_size}

    # Only the artifacts the registry currently calls active -- the ones a
    # fresh deployment actually needs to serve predictions. Resolved via
    # wt.ml_artifacts_io so a relative OR legacy absolute artifact_path in
    # the DB is handled identically to how the running app resolves it.
    sys.path.insert(0, str(BACKEND_DIR))
    from wt import ml_artifacts_io as artifacts  # noqa: E402

    conn = sqlite3.connect(db_src)
    conn.row_factory = sqlite3.Row
    (backend_target / "ml_artifacts").mkdir(exist_ok=True)
    active_rows = conn.execute(
        "SELECT r.* FROM ml_training_run r JOIN ml_model_registry m "
        "ON m.active_training_run_id = r.id ORDER BY r.model_key"
    ).fetchall()
    for row in active_rows:
        resolved = artifacts.resolve_path(row["artifact_path"])
        dest = backend_target / "ml_artifacts" / resolved.name
        shutil.copy2(resolved, dest)
        manifest["ml_artifacts"].append({
            "model_key": row["model_key"], "run_id": row["id"], "mode": row["mode"],
            "trained_at": row["trained_at"], "schema_version": row["schema_version"],
            "real_count": row["real_count"], "synthetic_count": row["synthetic_count"],
            "artifact_path": row["artifact_path"], "bytes": resolved.stat().st_size,
        })
    conn.close()

    manifest["excluded"] = EXCLUDED
    return manifest


def _write_markdown(manifest: dict, target: Path) -> None:
    lines = ["# Wings Theory — deployment package manifest", ""]
    lines.append(f"Built from `{BACKEND_DIR}` into `{target}`.")
    lines.append("")
    lines.append("## Runtime directories (under backend/)")
    for d, info in manifest["runtime_dirs"].items():
        lines.append(f"- `backend/{d}/` — {info['file_count']} files, {info['total_bytes']:,} bytes")
    lines.append("")
    lines.append("## Runtime files (under backend/)")
    for f in manifest["runtime_files"]:
        lines.append(f"- `backend/{f['path']}` — {f['bytes']:,} bytes")
    lines.append("")
    fe = manifest["frontend"]
    lines.append(f"## Frontend static assets\n- `frontend/` — {fe['file_count']} files, {fe['total_bytes']:,} bytes "
                 f"(mounted by backend/wt/api.py; must stay a sibling of backend/)")
    lines.append("")
    db = manifest["database"]
    lines.append(f"## Database\n- `{db['path']}` — {db['bytes']:,} bytes")
    lines.append("")
    lines.append("## Active model artifacts (only these ship — full training history stays at the source)")
    lines.append("")
    lines.append("| model_key | run_id | mode | trained_at | real | synthetic | bytes | file |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for a in manifest["ml_artifacts"]:
        fname = Path(a["artifact_path"]).name
        lines.append(f"| {a['model_key']} | {a['run_id']} | {a['mode']} | {a['trained_at']} | "
                     f"{a['real_count']} | {a['synthetic_count']} | {a['bytes']:,} | `{fname}` |")
    lines.append("")
    lines.append("## Deliberately excluded (reason)")
    for path, reason in manifest["excluded"].items():
        lines.append(f"- `{path}` — {reason}")
    (target / "DEPLOYMENT_MANIFEST.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python scripts/build_deployment_package.py <target_dir>")
        sys.exit(1)
    target = Path(sys.argv[1]).resolve()
    manifest = build(target)
    (target / "DEPLOYMENT_MANIFEST.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    _write_markdown(manifest, target)
    print(f"Deployment package built at {target}")
    print(f"  runtime dirs: {list(manifest['runtime_dirs'])}")
    print(f"  database: {manifest['database']}")
    print(f"  active artifacts: {len(manifest['ml_artifacts'])}")


if __name__ == "__main__":
    main()
