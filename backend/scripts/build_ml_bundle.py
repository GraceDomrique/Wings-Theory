"""Builds a portable ML deployment bundle: the currently-ACTIVE artifact
file for every model, the manifest/metadata/metrics export
(scripts/export_ml_manifest.py), and a registry_rows.json snapshot of the
exact ml_training_run rows a fresh database would need to re-register them
-- everything a new environment needs to serve predictions without
retraining, with nothing invented (every value here is read straight out
of ml_model_registry/ml_training_run).

This bundles ARTIFACTS, never the database itself -- no customer/business
data, no app_user rows, no secrets. Safe to hand to a fresh deployment or
attach to a release; nothing here is gitignored-for-a-reason data.

Usage (from backend/), with WT_ENV/WT_DB set as usual:
    WT_ENV=development WT_DB=dev/wings_theory_dev.db python scripts/build_ml_bundle.py <out_dir>
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

from wt import db, model_registry as mr  # noqa: E402
from wt import ml_artifacts_io as artifacts  # noqa: E402
import export_ml_manifest  # noqa: E402

RESTORE_MD = """# ML deployment bundle -- restore instructions

This bundle contains the currently-ACTIVE artifact for every model in
`ml_model_registry`, plus the metadata/metrics/checksum manifest that
describes them (`manifest/manifest.json`) and the exact registry rows
needed to re-register them in a fresh database (`registry_rows.json`).

## What's in here

```
ml_artifacts/<model_key>__<token>.joblib|.json    -- the 5 active artifacts
manifest/manifest.json                            -- SHA-256 checksums, provenance, dependency versions
manifest/<model_key>/metadata.json                -- per-model metadata
manifest/<model_key>/metrics.json                 -- per-model evaluation metrics
registry_rows.json                                -- the ml_training_run row for each active model
```

No database file, no customer data, no secrets are included.

## Restoring into a fresh deployment

1. Copy `ml_artifacts/` (the files directly under it, not the `manifest/`
   subfolder) into the target's `backend/ml_artifacts/` directory.
2. Run the target database through `wt.db.migrate()` as normal (this
   creates `ml_model_registry`/`ml_training_run` if they don't exist yet).
3. For each entry in `registry_rows.json`, insert a row into
   `ml_training_run` with the given fields, then call
   `model_registry.activate_training_run()` with those same values (NOT
   a bare INSERT into `ml_model_registry` -- that function is the only
   write path that updates `mode`/`active_training_run_id` together; see
   wt/model_registry.py's own module docstring for why).
4. Verify BEFORE serving any traffic: run
   `wt.ml_startup.validate_artifacts_on_startup(conn)` and confirm every
   model reports `"status": "valid"`. If any reports
   `"artifact_load_failed"` or a schema/feature mismatch, do not activate
   it -- the checksums in `manifest/manifest.json` are there to verify the
   copied files are byte-identical to what was exported.
5. Start the application normally. Startup loads these artifacts; it does
   NOT retrain. Confirm `ml_training_run` row counts and artifact file
   checksums are unchanged after restart.

## Provenance -- read this before trusting any number

Every model in this bundle was trained under `mode=SIMULATED`:
dev-only, generated synthetic history
(`wt/demo_seed.py`/`wt/simulated_workbook_import.py`/
`scripts/seed_recent_demo_orders.py`), never real business data. See
`manifest/manifest.json`'s `mode` field per model and each model's own
`metadata.json`. None of the evaluation metrics in `manifest/<model_key>/
metrics.json` are real-world accuracy figures -- they are capstone/
pre-launch benchmarks against simulated data, reported as such.

A model trained under `mode=SIMULATED` is never silently promoted to
`REAL` by any code in this codebase -- promotion only happens through an
explicit `wt.ml_training.train_one(..., mode="REAL")` (or `promote_mode()`
for the models that ladder automatically) call, run deliberately once
enough real data exists, never a side effect of time passing or a
restart.
"""


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ml_bundle_out")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ml_artifacts").mkdir(exist_ok=True)

    conn = db.get_conn()
    manifest_dir = out_dir / "manifest"
    manifest = export_ml_manifest.export(conn, manifest_dir)

    registry_rows = []
    for model_key in mr.MODEL_KEYS:
        run = mr.active_run(conn, model_key)
        if run is None:
            continue
        resolved = artifacts.resolve_path(run["artifact_path"])
        if not resolved.exists():
            continue
        shutil.copy2(resolved, out_dir / "ml_artifacts" / resolved.name)
        registry_rows.append({
            "model_key": model_key,
            "mode": run["mode"],
            "dataset_version": run["dataset_version"],
            "real_count": run["real_count"],
            "synthetic_count": run["synthetic_count"],
            "schema_version": run["schema_version"],
            "metrics": run["metrics"],
            "artifact_path": run["artifact_path"],
            "status": run["status"],
            "triggered_by": run["triggered_by"],
            "trained_at": run["trained_at"],
        })

    (out_dir / "registry_rows.json").write_text(
        json.dumps(registry_rows, indent=2, default=str), encoding="utf-8"
    )
    (out_dir / "RESTORE.md").write_text(RESTORE_MD, encoding="utf-8")

    print(json.dumps({
        "out_dir": str(out_dir),
        "artifacts_bundled": [r["model_key"] for r in registry_rows],
        "manifest_models": len(manifest["models"]),
    }, indent=2))


if __name__ == "__main__":
    main()
