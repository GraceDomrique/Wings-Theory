"""Startup-time artifact validation (called once from api.py's FastAPI
startup event, right after db.migrate()/db.validate_production_startup()).

For every registered model with an active training run: load its artifact,
confirm it actually opens and that its schema_version/feature_names still
match what the running code expects (a stale artifact trained before a
feature-list change must never be fed silently). On failure, roll back once
to the previous active/superseded run and re-validate that one; if nothing
valid is found, leave the registry's active_training_run_id as-is (serving
code already falls back safely per-call -- anomaly.py's
_score_with_isolation_forest refits fresh, associations.active_combo_
recommendations re-mines, depletion's Prophet stores simply stay empty and
build_demand() already falls back to EWMA for any item with no fresh fit)
and write an admin_audit_log entry so the degradation is visible, not
silent.

For demand_prophet specifically, a validated artifact is also used to WARM
the in-memory _prophet_store/_hybrid_store immediately (those dicts are
always empty on process start otherwise, since they are plain module-level
state) -- this is the real value of persisting Prophet's fitted state:
forecasts are available immediately on restart instead of only after the
next ~12h scheduled refresh completes.
"""
from __future__ import annotations

import sqlite3

from . import anomaly, audit, depletion, model_registry
from . import ml_artifacts_io as artifacts
from .db import write_tx
from .errors import InvalidStateError, NotFoundError, RegistryUnavailableError
from .ml_training import FPGROWTH_SCHEMA_VERSION, ISO_SCHEMA_VERSION, PROPHET_SCHEMA_VERSION

_EXPECTED_FEATURE_NAMES = {
    "stock_loss_isoforest": ("relative_mag", "fraction"),
    "unli_consumption_isoforest": anomaly.FEATURE_NAMES,
    "flavor_consumption_isoforest": anomaly.FLAVOR_FEATURE_NAMES,
}
_EXPECTED_SCHEMA_VERSION = {
    "stock_loss_isoforest": ISO_SCHEMA_VERSION,
    "unli_consumption_isoforest": ISO_SCHEMA_VERSION,
    "flavor_consumption_isoforest": ISO_SCHEMA_VERSION,
    "demand_prophet": PROPHET_SCHEMA_VERSION,
    "fpgrowth_combos": FPGROWTH_SCHEMA_VERSION,
}


def _validate_payload(model_key: str, payload: dict) -> tuple[bool, str]:
    expected_schema = _EXPECTED_SCHEMA_VERSION.get(model_key)
    if payload.get("schema_version") != expected_schema:
        return False, (
            f"schema_version mismatch: artifact has {payload.get('schema_version')!r}, "
            f"code expects {expected_schema!r}"
        )
    expected_features = _EXPECTED_FEATURE_NAMES.get(model_key)
    if expected_features is not None and list(payload.get("feature_names") or []) != list(expected_features):
        return False, (
            f"feature_names mismatch: artifact has {payload.get('feature_names')!r}, "
            f"code expects {list(expected_features)!r}"
        )
    return True, "ok"


def validate_artifacts_on_startup(conn: sqlite3.Connection) -> dict:
    """DatabaseIntegrityError (a damaged ml_model_registry/ml_training_run
    file, as opposed to a merely-missing or merely-empty one) is
    deliberately NOT caught here -- it must propagate out of the FastAPI
    startup hook and stop the process, not be folded into a per-model
    'degraded' report the operator could miss."""
    report: dict[str, dict] = {}
    for model_key in model_registry.MODEL_KEYS:
        try:
            active = model_registry.active_run(conn, model_key)
        except RegistryUnavailableError as exc:
            report[model_key] = {
                "status": "registry_unavailable",
                "detail": f"{exc.message}; serving falls back live until migrations run",
            }
            continue
        if active is None:
            report[model_key] = {"status": "no_active_run", "detail": "nothing trained yet; serving falls back live"}
            continue

        try:
            payload = artifacts.load_any(active["artifact_path"])
            ok, reason = _validate_payload(model_key, payload)
        except Exception as exc:  # noqa: BLE001
            payload, ok, reason = None, False, f"artifact_load_failed: {exc}"

        if ok:
            report[model_key] = {"status": "valid", "artifact_path": active["artifact_path"]}
            if model_key == "demand_prophet":
                _warm_prophet(payload)
            continue

        # Fall back one step: the run that was active immediately before
        # this one (model_registry.rollback reuses the atomic
        # activate/supersede path rather than a bare UPDATE).
        try:
            restored = model_registry.rollback(
                conn, model_key, triggered_by="startup_validation",
                reason=f"active artifact failed validation: {reason}",
            )
            try:
                payload2 = artifacts.load_any(restored["artifact_path"])
                ok2, reason2 = _validate_payload(model_key, payload2)
            except Exception as exc:  # noqa: BLE001
                payload2, ok2, reason2 = None, False, f"artifact_load_failed: {exc}"
        except (NotFoundError, InvalidStateError) as exc:
            # No active run, or no prior (superseded) run to fall back to --
            # an ordinary "nothing else to serve" outcome, not a failure.
            ok2, reason2, payload2 = False, str(exc), None
        # DatabaseIntegrityError propagates: a database that went from
        # readable (active_run succeeded above) to corrupt mid-request is
        # not a "no fallback available" case to paper over.

        if ok2:
            report[model_key] = {
                "status": "rolled_back", "original_failure": reason, "artifact_path": restored["artifact_path"],
            }
            if model_key == "demand_prophet":
                _warm_prophet(payload2)
        else:
            report[model_key] = {
                "status": "degraded_serving_live_fallback",
                "original_failure": reason, "rollback_failure": reason2,
            }
            with write_tx(conn):
                audit.log(
                    conn, "ml_model_registry", model_key, "startup_validation_failed", "system",
                    field="active_artifact", previous_value=active["artifact_path"],
                    new_value=None, note=f"{reason}; rollback also failed: {reason2}",
                )
    return report


def _warm_prophet(payload: dict | None) -> None:
    if not payload:
        return
    depletion.restore_prophet_stores((payload.get("prophet_store") or {}, payload.get("hybrid_store") or {}))
