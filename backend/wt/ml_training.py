"""Trains, validates, persists, and activates every model in
ml_model_registry (migration 0050). One train_*() per model_key, each
following the same sequence: pull mode-filtered records by calling the
owning module's own existing record-builder (no duplicated feature
engineering), fit, validate, and either model_registry.activate_training_run()
or model_registry.record_failed_run() -- never a bare mode/artifact change.
wt/ml_scheduler.py (the background loop) and the manual
POST /console/ml/models/{key}/train and .../mode API actions are the only
callers.

Every metrics_json always separates "simulated_metrics" from "real_metrics"
(and a "structural" bucket that applies regardless of provenance) -- never
blended into one number, so a dashboard reader can never mistake a
synthetic-fixture result for a real-world accuracy claim.
"""
from __future__ import annotations

import sqlite3
import statistics

import numpy as np
from sklearn.ensemble import IsolationForest

from . import anomaly, associations, depletion, forecasting, model_registry
from . import ml_artifacts_io as artifacts

ISO_SCHEMA_VERSION = anomaly.ANOMALY_ARTIFACT_SCHEMA_VERSION
PROPHET_SCHEMA_VERSION = "demand_prophet_v1"
FPGROWTH_SCHEMA_VERSION = "fpgrowth_combos_v1"

# The one readiness function per unlocked model -- shared by promote_mode()
# below (manual promotion) and ml_scheduler.py's auto-promote cycle, so a
# manual click can never promote a model past what the scheduler itself
# would ever allow. Prophet/FP-Growth are absent on purpose: they are
# mode_locked and promote_mode() refuses them before this dict is even
# consulted.
READINESS_FUNCS = {
    "stock_loss_isoforest": anomaly.stock_loss_real_data_readiness,
    "unli_consumption_isoforest": anomaly.consumption_real_data_readiness,
    "flavor_consumption_isoforest": anomaly.flavor_real_data_readiness,
}

STOCK_LOSS_FEATURE_NAMES = ("relative_mag", "fraction")

# Prior-active-run fleet WAPE may regress by at most this fraction before a
# refresh is rejected and the previous Prophet artifact keeps serving --
# see train_demand_prophet().
PROPHET_WAPE_REGRESSION_TOLERANCE = 0.15


def _fingerprint(conn: sqlite3.Connection, table: str, id_col: str = "id") -> str:
    row = conn.execute(f"SELECT MAX({id_col}) AS m FROM {table}").fetchone()
    return f"{table}:max_{id_col}={row['m'] or 0}"


def _provenance_counts(records: list[dict]) -> tuple[int, int]:
    real = sum(1 for r in records if r["provenance"] == "real")
    return real, len(records) - real


def _fit_and_score(X: np.ndarray, max_fraction: float) -> tuple[IsolationForest, np.ndarray, float]:
    """Fits a fresh IsolationForest with this module's fixed hyperparameters
    (same as every live anomaly.py pass) and computes the flagged fraction
    the SAME capped-worst-scoring rule those passes already use, so the
    structural validation below measures the identical quantity a live
    call would report."""
    model = IsolationForest(n_estimators=anomaly.N_ESTIMATORS, contamination="auto",
                            random_state=anomaly.RANDOM_STATE)
    model.fit(X)
    raw_scores = model.decision_function(X)
    n = len(X)
    n_candidates = int((model.predict(X) == -1).sum())
    cap = max(1, int(n * max_fraction)) if n_candidates else 0
    flagged_fraction = min(cap, n_candidates) / n if n else 0.0
    return model, raw_scores, flagged_fraction


def _structural_ok(flagged_fraction: float, score_std: float, max_fraction: float,
                   min_score_std: float = 1e-6) -> tuple[bool, str]:
    if score_std < min_score_std:
        return False, "degenerate_score_distribution"
    if flagged_fraction > max_fraction + 1e-6:
        return False, "flagged_fraction_exceeds_historical_band"
    return True, "ok"


def _finish(
    conn: sqlite3.Connection, *, model_key: str, mode: str, dataset_version: str,
    real_count: int, synthetic_count: int, schema_version: str, metrics: dict,
    artifact_path: str, triggered_by: str, ok: bool, reason: str,
) -> dict:
    kwargs = dict(
        model_key=model_key, mode=mode, dataset_version=dataset_version,
        real_count=real_count, synthetic_count=synthetic_count, schema_version=schema_version,
        metrics=metrics, artifact_path=artifact_path, triggered_by=triggered_by,
    )
    if ok:
        run = model_registry.activate_training_run(conn, **kwargs)
    else:
        run = model_registry.record_failed_run(conn, **kwargs, notes=reason)
    return {
        "status": "active" if ok else "failed", "model_key": model_key, "mode": mode,
        "real_count": real_count, "synthetic_count": synthetic_count,
        "artifact_path": artifact_path, "metrics": metrics, "reason": reason, "run": run,
    }


def train_stock_loss_model(
    conn: sqlite3.Connection, *, triggered_by: str, lookback_days: int = 90, mode: str | None = None,
) -> dict:
    model_key = "stock_loss_isoforest"
    mode = mode or model_registry.get_mode(conn, model_key)
    records, _ = anomaly._evaluate_stock_loss(conn, lookback_days, mode=mode, use_artifact=False)
    real_count, synthetic_count = _provenance_counts(records)
    if len(records) < anomaly.MIN_POOLED_SAMPLES:
        return {"status": "skipped", "model_key": model_key, "mode": mode,
               "reason": "insufficient_pooled_samples", "real_count": real_count,
               "synthetic_count": synthetic_count, "n_samples": len(records)}
    X = np.array([[r["relative_mag"], r["fraction"]] for r in records])
    model, raw_scores, flagged_fraction = _fit_and_score(X, anomaly.MAX_STOCK_LOSS_FLAG_FRACTION)
    structural = {"n_samples": len(records), "flagged_fraction": round(flagged_fraction, 4),
                 "score_std": round(float(np.std(raw_scores)), 6)}
    ok, reason = _structural_ok(flagged_fraction, structural["score_std"], anomaly.MAX_STOCK_LOSS_FLAG_FRACTION)
    metrics = {"simulated_metrics": None, "real_metrics": None, "structural": structural}
    path = artifacts.dump_joblib(
        {"model": model, "feature_names": list(STOCK_LOSS_FEATURE_NAMES),
         "schema_version": ISO_SCHEMA_VERSION, "model_key": model_key},
        model_key, artifacts.new_run_token(),
    )
    return _finish(
        conn, model_key=model_key, mode=mode, dataset_version=_fingerprint(conn, "stock_ledger"),
        real_count=real_count, synthetic_count=synthetic_count, schema_version=ISO_SCHEMA_VERSION,
        metrics=metrics, artifact_path=path, triggered_by=triggered_by, ok=ok, reason=reason,
    )


def train_consumption_model(
    conn: sqlite3.Connection, *, triggered_by: str, lookback_days: int = 365, mode: str | None = None,
) -> dict:
    model_key = "unli_consumption_isoforest"
    mode = mode or model_registry.get_mode(conn, model_key)
    records = anomaly._consumption_records(conn, lookback_days, mode=mode)
    real_count, synthetic_count = _provenance_counts(records)
    if len(records) < anomaly.MIN_RECONCILED_DAYS:
        return {"status": "skipped", "model_key": model_key, "mode": mode,
               "reason": "insufficient_reconciled_days", "real_count": real_count,
               "synthetic_count": synthetic_count, "n_samples": len(records)}
    X = np.array([[r["features"][name] for name in anomaly.FEATURE_NAMES] for r in records])
    model, raw_scores, flagged_fraction = _fit_and_score(X, anomaly.MAX_FLAG_FRACTION)
    structural = {"n_samples": len(records), "flagged_fraction": round(flagged_fraction, 4),
                 "score_std": round(float(np.std(raw_scores)), 6)}
    ok, reason = _structural_ok(flagged_fraction, structural["score_std"], anomaly.MAX_FLAG_FRACTION)
    metrics = {"simulated_metrics": None, "real_metrics": None, "structural": structural}
    path = artifacts.dump_joblib(
        {"model": model, "feature_names": list(anomaly.FEATURE_NAMES),
         "schema_version": ISO_SCHEMA_VERSION, "model_key": model_key},
        model_key, artifacts.new_run_token(),
    )
    dataset_version = (
        f"{_fingerprint(conn, 'unli_reconciliation')}+{_fingerprint(conn, 'unli_flavor_reconciliation')}"
    )
    return _finish(
        conn, model_key=model_key, mode=mode, dataset_version=dataset_version,
        real_count=real_count, synthetic_count=synthetic_count, schema_version=ISO_SCHEMA_VERSION,
        metrics=metrics, artifact_path=path, triggered_by=triggered_by, ok=ok, reason=reason,
    )


def train_flavor_consumption_model(
    conn: sqlite3.Connection, *, triggered_by: str, lookback_days: int = 365, mode: str | None = None,
) -> dict:
    model_key = "flavor_consumption_isoforest"
    mode = mode or model_registry.get_mode(conn, model_key)
    records = anomaly._flavor_consumption_records(conn, lookback_days, mode=mode)
    real_count, synthetic_count = _provenance_counts(records)
    if len(records) < anomaly.FLAVOR_MIN_RECONCILED_DAYS:
        return {"status": "skipped", "model_key": model_key, "mode": mode,
               "reason": "insufficient_reconciled_days", "real_count": real_count,
               "synthetic_count": synthetic_count, "n_samples": len(records)}
    X = np.array([[r["features"][name] for name in anomaly.FLAVOR_FEATURE_NAMES] for r in records])
    model, raw_scores, flagged_fraction = _fit_and_score(X, anomaly.FLAVOR_MAX_FLAG_FRACTION)
    structural = {"n_samples": len(records), "flagged_fraction": round(flagged_fraction, 4),
                 "score_std": round(float(np.std(raw_scores)), 6)}
    ok, reason = _structural_ok(flagged_fraction, structural["score_std"], anomaly.FLAVOR_MAX_FLAG_FRACTION)
    metrics = {"simulated_metrics": None, "real_metrics": None, "structural": structural}
    path = artifacts.dump_joblib(
        {"model": model, "feature_names": list(anomaly.FLAVOR_FEATURE_NAMES),
         "schema_version": ISO_SCHEMA_VERSION, "model_key": model_key},
        model_key, artifacts.new_run_token(),
    )
    return _finish(
        conn, model_key=model_key, mode=mode, dataset_version=_fingerprint(conn, "unli_flavor_reconciliation"),
        real_count=real_count, synthetic_count=synthetic_count, schema_version=ISO_SCHEMA_VERSION,
        metrics=metrics, artifact_path=path, triggered_by=triggered_by, ok=ok, reason=reason,
    )


def train_demand_prophet(conn: sqlite3.Connection, *, triggered_by: str, mode: str | None = None) -> dict:
    """Wraps the existing, already-working refresh_prophet_baselines()/
    run_prophet_refresh_job() (depletion.py) with the registry/artifact/
    rollback machinery those never had: snapshots the live stores first,
    lets the refresh run exactly as it always has, then only keeps the new
    fit if the STORE-LEVEL fleet WAPE did not regress beyond
    PROPHET_WAPE_REGRESSION_TOLERANCE against the last ACTIVE run's own
    recorded figure for the SAME mode -- otherwise restores the snapshot
    (the previous, still-good forecast keeps serving) and records the
    attempt as 'failed'.

    mode (SIMULATED/MIXED/REAL, same ladder as the IsolationForest models --
    see model_registry.MODES) decides which customer_order rows are
    eligible, via forecasting._order_provenance_clause(): REAL reads real
    orders only (today's exact default, unchanged for every existing
    caller); SIMULATED reads ONLY wt/demo_seed.py's sentinel-tagged rows, a
    capstone/pre-launch path for a restaurant with too little real history
    yet to fit anything. Mirrors how wt/anomaly.py's IsolationForest passes
    already take an explicit mode rather than only ever reading real data.

    Reports two genuinely different numbers, never conflated into one:
      store_level_prophet_wape  mean WAPE across the 3 store-aggregate
                                series (orders/items/sales) -- what
                                evaluate_store_demand_model() backtests, and
                                what the regression guard above compares.
      item_level_prophet_wape   fleet-mean WAPE across individual menu
                                items -- what evaluate_demand_model()
                                backtests. Typically much higher than the
                                store-level figure (small per-item daily
                                volume inflates WAPE; this is the same
                                finding the 2026-10-12 audit already
                                recorded: ~50% item-level vs ~10% store-
                                level), so reporting only one of them next
                                to the other's historical context would be
                                misleading.

    Both numbers, and every other metric below, land in metrics["simulated_
    metrics"] when mode='SIMULATED' and metrics["real_metrics"] when
    mode='REAL' -- NEVER both at once, so a dashboard reader can never
    mistake a synthetic-fixture result for a real-world accuracy claim (the
    exact rule this module's own docstring states for every other model)."""
    model_key = "demand_prophet"
    mode = mode or model_registry.get_mode(conn, model_key)
    metrics_bucket = "simulated_metrics" if mode == "SIMULATED" else "real_metrics"
    dataset_version = _fingerprint(conn, "customer_order")
    snapshot = depletion.snapshot_prophet_stores()
    prior_run = model_registry.active_run(conn, model_key)
    prior_store_wape = prior_item_wape = None
    # Only compare against a prior run trained under the SAME mode -- a
    # SIMULATED run's WAPE regressing against a REAL run's (or vice versa)
    # would compare two different datasets, not two fits of the same one.
    if prior_run and prior_run["mode"] == mode and prior_run["metrics"].get(metrics_bucket):
        prior_bucket = prior_run["metrics"][metrics_bucket]
        prior_store_wape = prior_bucket.get("store_level_prophet_wape")
        prior_item_wape = prior_bucket.get("item_level_prophet_wape")

    def _artifact_from(store_snapshot: tuple[dict, dict]) -> str:
        return artifacts.dump_joblib(
            {"prophet_store": store_snapshot[0], "hybrid_store": store_snapshot[1],
             "schema_version": PROPHET_SCHEMA_VERSION, "feature_names": None, "model_key": model_key,
             "mode": mode},
            model_key, artifacts.new_run_token(),
        )

    summary = depletion.run_prophet_refresh_job(conn, triggered_by=triggered_by, mode=mode)
    if not summary.get("lastRunOk", False):
        depletion.restore_prophet_stores(snapshot)
        metrics = {"simulated_metrics": None, "real_metrics": None,
                  "structural": {"error": summary.get("error", "refresh_prophet_baselines failed")}}
        path = _artifact_from(snapshot)
        return _finish(
            conn, model_key=model_key, mode=mode, dataset_version=dataset_version,
            real_count=0, synthetic_count=0, schema_version=PROPHET_SCHEMA_VERSION,
            metrics=metrics, artifact_path=path, triggered_by=triggered_by,
            ok=False, reason="refresh_failed",
        )

    store_eval = forecasting.evaluate_store_demand_model(conn, mode=mode)
    store_wapes = []
    if store_eval.get("available"):
        for r in store_eval["metrics"].values():
            if r.get("insufficient_history"):
                continue
            m = r.get("methods", {}).get("prophet")
            if m and m.get("folds_scored", 0) > 0 and m.get("wape") is not None:
                store_wapes.append(m["wape"])
    new_store_wape = round(statistics.mean(store_wapes), 4) if store_wapes else None

    item_eval = forecasting.evaluate_demand_model(conn, mode=mode)
    new_item_wape = (item_eval["fleet_summary"]["prophet"]["mean_wape"]
                     if item_eval.get("available") else None)

    fitted_count = summary.get("fitted", 0)
    real_count = fitted_count if mode == "REAL" else 0
    synthetic_count = fitted_count if mode == "SIMULATED" else 0
    metrics = {
        "simulated_metrics": None,
        "real_metrics": None,
        metrics_bucket: {
            "mode": mode,
            "fitted_series_count": fitted_count,
            "store_level_prophet_wape": new_store_wape, "prior_store_level_prophet_wape": prior_store_wape,
            "item_level_prophet_wape": new_item_wape, "prior_item_level_prophet_wape": prior_item_wape,
            "items_backtested": item_eval.get("items_evaluated") if item_eval.get("available") else None,
            "refresh_summary": summary,
        },
    }
    # The regression guard compares STORE-level WAPE only -- that is the
    # figure Prophet actually, measurably beats the best baseline on (see
    # the module docstring); item-level WAPE is reported for transparency
    # but is not a meaningful swap-or-keep signal on its own (both Prophet
    # and every baseline score similarly poorly there by construction, per
    # forecasting.py's own module docstring on small per-item daily volume).
    regressed = (prior_store_wape is not None and new_store_wape is not None
                and new_store_wape > prior_store_wape * (1 + PROPHET_WAPE_REGRESSION_TOLERANCE))
    if regressed:
        depletion.restore_prophet_stores(snapshot)
        path = _artifact_from(snapshot)
        return _finish(
            conn, model_key=model_key, mode=mode, dataset_version=dataset_version,
            real_count=real_count, synthetic_count=synthetic_count, schema_version=PROPHET_SCHEMA_VERSION,
            metrics=metrics, artifact_path=path, triggered_by=triggered_by,
            ok=False, reason=f"store_level_wape_regressed_{prior_store_wape}_to_{new_store_wape}",
        )

    # fitted_count == 0 means Prophet fit NOTHING -- every item/metric was
    # insufficient_history. Accepting that as an 'active' run would let a
    # zero-fitted registry entry be read as "Prophet is trained" (exactly
    # what the capstone brief forbids); record it as a failed attempt
    # instead so the registry/UI can tell "trained" apart from "ran, fit
    # nothing" at a glance, without needing to parse refresh_summary.
    if fitted_count == 0:
        path = _artifact_from(snapshot)
        return _finish(
            conn, model_key=model_key, mode=mode, dataset_version=dataset_version,
            real_count=real_count, synthetic_count=synthetic_count, schema_version=PROPHET_SCHEMA_VERSION,
            metrics=metrics, artifact_path=path, triggered_by=triggered_by,
            ok=False, reason=f"zero_series_fitted_insufficient_{mode.lower()}_history",
        )

    path = _artifact_from(depletion.snapshot_prophet_stores())
    return _finish(
        conn, model_key=model_key, mode=mode, dataset_version=dataset_version,
        real_count=real_count, synthetic_count=synthetic_count, schema_version=PROPHET_SCHEMA_VERSION,
        metrics=metrics, artifact_path=path, triggered_by=triggered_by, ok=True, reason="ok",
    )


def train_fpgrowth_combos(conn: sqlite3.Connection, *, triggered_by: str, lookback_days: int = 365) -> dict:
    """Mines + holdout-validates exactly as product_combinations() always
    has (unchanged mining/validation code), then persists the result as a
    JSON artifact (the mined ruleset already IS a small JSON-serializable
    structure, no sklearn object to pickle) so
    associations.active_combo_recommendations() can serve it without
    re-mining on every call."""
    model_key = "fpgrowth_combos"
    mode = model_registry.get_mode(conn, model_key)
    result = associations.product_combinations(conn, lookback_days=lookback_days, holdout_validate=True)
    ok = not result.get("insufficient_data", True)
    real_count = result.get("transaction_count", 0)
    metrics = {
        "simulated_metrics": None,
        "real_metrics": {
            "transaction_count": result.get("transaction_count"),
            "validated_combos": result.get("number_of_final_recommendations"),
            "mining_basis": result.get("mining_basis"),
        },
        "structural": {"insufficient_data": result.get("insufficient_data")},
    }
    path = artifacts.dump_json(
        {"result": result, "schema_version": FPGROWTH_SCHEMA_VERSION, "feature_names": None,
         "model_key": model_key},
        model_key, artifacts.new_run_token(),
    )
    return _finish(
        conn, model_key=model_key, mode=mode, dataset_version=_fingerprint(conn, "customer_order"),
        real_count=real_count, synthetic_count=0, schema_version=FPGROWTH_SCHEMA_VERSION,
        metrics=metrics, artifact_path=path, triggered_by=triggered_by,
        ok=ok, reason="ok" if ok else "insufficient_data",
    )


TRAIN_FUNCS = {
    "stock_loss_isoforest": train_stock_loss_model,
    "unli_consumption_isoforest": train_consumption_model,
    "flavor_consumption_isoforest": train_flavor_consumption_model,
    "demand_prophet": train_demand_prophet,
    "fpgrowth_combos": train_fpgrowth_combos,
}


def train_one(conn: sqlite3.Connection, model_key: str, *, triggered_by: str, **kwargs) -> dict:
    try:
        fn = TRAIN_FUNCS[model_key]
    except KeyError:
        raise ValueError(f"unknown model_key '{model_key}'") from None
    return fn(conn, triggered_by=triggered_by, **kwargs)


def train_all(conn: sqlite3.Connection, *, triggered_by: str) -> dict:
    return {key: train_one(conn, key, triggered_by=triggered_by) for key in model_registry.MODEL_KEYS}


def promote_mode(conn: sqlite3.Connection, model_key: str, *, triggered_by: str) -> dict:
    """The only path that moves a model's mode forward: retrains under the
    NEXT mode (model_registry.next_mode) and activates only if that
    candidate training run passes validation -- mode and model artifact
    always change together (see model_registry.activate_training_run),
    never a bare registry UPDATE. Refuses for a mode_locked model.

    Gated on real-data readiness (READINESS_FUNCS) -- a manual click (the
    API's POST .../promote) must never be able to move a model to MIXED/
    REAL with zero or insufficient real observations backing it; that is
    exactly what READY_REAL_DATA already verifies, and reusing the same
    function the scheduler's auto-promote uses means the manual and
    automatic paths can never disagree about what "ready" means."""
    if model_registry.is_mode_locked(conn, model_key):
        from .errors import PermissionError_
        raise PermissionError_(f"'{model_key}' mode is locked (no synthetic data source exists)")
    current = model_registry.get_mode(conn, model_key)
    target = model_registry.next_mode(current)
    if target == current:
        return {"status": "no_op", "model_key": model_key, "mode": current, "reason": "already_at_REAL"}
    readiness_fn = READINESS_FUNCS.get(model_key)
    readiness = readiness_fn(conn) if readiness_fn else None
    if readiness is not None and readiness["state"] != "READY_REAL_DATA":
        return {
            "status": "not_ready", "model_key": model_key, "mode": current,
            "reason": f"readiness is {readiness['state']}, not READY_REAL_DATA: {readiness['reason']}",
            "readiness": readiness,
        }
    return train_one(conn, model_key, triggered_by=triggered_by, mode=target)
