"""Exports a versioned, checksummed deployment manifest for every currently
ACTIVE model in ml_model_registry: one metadata.json + one metrics.json per
model, plus one consolidated manifest.json with a SHA-256 checksum for every
artifact file.

This is a read-only EXPORT, not a second source of truth -- ml_training_run
(migration 0050) stays the one place training provenance actually lives.
Nothing here invents a metric, a date, or a count that isn't already in the
registry; this only serializes what's there into the stand-alone JSON files
a deployment step can ship or inspect without a database connection.

Usage (from backend/), with WT_ENV/WT_DB set the same way every other entry
point into this database requires:
    WT_ENV=development WT_DB=dev/wings_theory_dev.db python scripts/export_ml_manifest.py [out_dir]

out_dir defaults to backend/ml_artifacts/manifest/ (gitignored, same as
every other training artifact).
"""
from __future__ import annotations

import hashlib
import json
import sys
from importlib import metadata as _ilmd
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from wt import db, model_registry as mr  # noqa: E402
from wt import ml_artifacts_io as artifacts  # noqa: E402

DEPENDENCY_NAMES = ("scikit-learn", "prophet", "mlxtend", "joblib", "pandas", "numpy", "cmdstanpy")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dependency_versions() -> dict[str, str | None]:
    out = {}
    for name in DEPENDENCY_NAMES:
        try:
            out[name] = _ilmd.version(name)
        except _ilmd.PackageNotFoundError:
            out[name] = None
    return out


def export(conn, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    dep_versions = _dependency_versions()
    manifest: dict = {
        "database_path": str(db.db_path()), "environment": db.environment(),
        "dependency_versions": dep_versions, "models": [],
    }

    for model_key in mr.MODEL_KEYS:
        run = mr.active_run(conn, model_key)
        model_dir = out_dir / model_key
        model_dir.mkdir(parents=True, exist_ok=True)
        if run is None:
            manifest["models"].append({
                "model_key": model_key, "status": "no_active_run",
                "detail": "nothing trained yet in this database",
            })
            continue

        resolved = artifacts.resolve_path(run["artifact_path"])
        if not resolved.exists():
            manifest["models"].append({
                "model_key": model_key, "status": "artifact_missing",
                "artifact_path": run["artifact_path"], "run_id": run["id"],
            })
            continue

        checksum = _sha256(resolved)
        metadata = {
            "model_key": model_key,
            "algorithm": {
                "stock_loss_isoforest": "IsolationForest (scikit-learn)",
                "unli_consumption_isoforest": "IsolationForest (scikit-learn)",
                "flavor_consumption_isoforest": "IsolationForest (scikit-learn)",
                "demand_prophet": "Prophet + naive/trailing-average/day-of-week baselines (hybrid selection)",
                "fpgrowth_combos": "FP-Growth (mlxtend)",
            }[model_key],
            "training_run_id": run["id"],
            "mode": run["mode"],
            "provenance": "SIMULATED" if run["mode"] == "SIMULATED"
                         else ("MIXED (real + approved synthetic batch)" if run["mode"] == "MIXED" else "REAL"),
            "trained_at": run["trained_at"],
            "dataset_version": run["dataset_version"],
            "real_count": run["real_count"],
            "synthetic_count": run["synthetic_count"],
            "schema_version": run["schema_version"],
            "status": run["status"],
            "triggered_by": run["triggered_by"],
            "artifact_path": run["artifact_path"],
            "artifact_bytes": resolved.stat().st_size,
            "artifact_sha256": checksum,
            "dependency_versions": dep_versions,
        }
        (model_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
        (model_dir / "metrics.json").write_text(json.dumps(run["metrics"], indent=2, default=str), encoding="utf-8")

        manifest["models"].append({
            "model_key": model_key, "status": "active", "mode": run["mode"],
            "trained_at": run["trained_at"], "artifact_path": run["artifact_path"],
            "artifact_sha256": checksum, "artifact_bytes": resolved.stat().st_size,
            "real_count": run["real_count"], "synthetic_count": run["synthetic_count"],
            "metadata_file": str((model_dir / "metadata.json").relative_to(out_dir.parent)),
            "metrics_file": str((model_dir / "metrics.json").relative_to(out_dir.parent)),
        })

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return manifest


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else artifacts.ARTIFACT_DIR / "manifest"
    conn = db.get_conn()
    manifest = export(conn, out_dir)
    print(json.dumps(manifest, indent=2, default=str))


if __name__ == "__main__":
    main()
