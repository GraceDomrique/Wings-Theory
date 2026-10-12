"""Generalizes depletion.py's existing run_prophet_refresh_job()/
start_prophet_refresh_scheduler() pattern (anti-overlap lock, durable
app_setting-persisted status) into one background thread that keeps every
registered model trained and, where eligible, promoted -- not just Prophet.

Daily tick, per model_key: retrain under its current mode (ml_training.
train_one), then -- for a model that is not already REAL and has
auto_promote set -- check its readiness function and step mode exactly one
notch forward (model_registry.next_mode) if READY_REAL_DATA, posting an
owner_instruction so the promotion is visible rather than silent. A manual
"run once now" is exposed via API for the verification step (run_cycle()
is exactly what the scheduler thread calls, so a manual trigger and the
background tick are never two different code paths).
"""
from __future__ import annotations

import json
import threading
import datetime as _dt

from . import instructions, model_registry
from . import ml_training as training
from .db import write_tx

TICK_SECONDS = 24 * 3600

# Single source of truth for "which readiness function backs which model" --
# see ml_training.READINESS_FUNCS's own comment: promote_mode() (manual) and
# _maybe_promote() (automatic) below must never be able to disagree about
# what "ready" means.
_READINESS_FUNCS = training.READINESS_FUNCS

_run_lock = threading.Lock()
_running = False


def _save_status(conn, status: dict) -> None:
    with write_tx(conn):
        conn.execute(
            "INSERT INTO app_setting (key, value, updated_by, updated_at) "
            "VALUES ('ml_scheduler_status', ?, 'system', datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (json.dumps(status),),
        )


def get_scheduler_status(conn) -> dict | None:
    row = conn.execute("SELECT value FROM app_setting WHERE key='ml_scheduler_status'").fetchone()
    if not row or not row["value"]:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return None


def _maybe_promote(conn, model_key: str, triggered_by: str) -> dict | None:
    row = next(m for m in model_registry.list_models(conn) if m["model_key"] == model_key)
    if row["mode_locked"] or not row["auto_promote"] or row["mode"] == "REAL":
        return None
    readiness_fn = _READINESS_FUNCS.get(model_key)
    if readiness_fn is None:
        return None
    readiness = readiness_fn(conn)
    if readiness["state"] != "READY_REAL_DATA":
        return None
    result = training.promote_mode(conn, model_key, triggered_by=triggered_by)
    if result.get("status") == "active":
        instructions.create(
            conn,
            title=f"ML model '{model_key}' promoted to {result['mode']}",
            body=(
                f"Automatic promotion: real-data readiness reached READY_REAL_DATA "
                f"({readiness['reason']}). New training run is now active."
            ),
            priority="normal", assigned_to=None, due_date=None, created_by="system",
        )
    return result


def run_cycle(conn, *, triggered_by: str) -> dict:
    """Train every registered model, then evaluate auto-promotion for the
    three anomaly models (Prophet/FP-Growth are mode_locked, never
    promoted). Never raises -- a single model's failure is recorded on its
    own training run and does not stop the rest of the cycle."""
    results: dict[str, dict] = {}
    for model_key in model_registry.MODEL_KEYS:
        try:
            results[model_key] = training.train_one(conn, model_key, triggered_by=triggered_by)
        except Exception as exc:  # noqa: BLE001 -- one bad model must never abort the cycle
            results[model_key] = {"status": "error", "model_key": model_key, "error": str(exc)}
    promotions: dict[str, dict] = {}
    for model_key in _READINESS_FUNCS:
        try:
            promoted = _maybe_promote(conn, model_key, triggered_by)
            if promoted is not None:
                promotions[model_key] = promoted
        except Exception as exc:  # noqa: BLE001
            promotions[model_key] = {"status": "error", "error": str(exc)}
    status = {
        "lastRunAt": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "triggeredBy": triggered_by, "results": results, "promotions": promotions,
    }
    _save_status(conn, status)
    return status


def run_scheduled_cycle(conn, *, triggered_by: str) -> dict:
    """Guards run_cycle() against overlap -- same shape as depletion.
    run_prophet_refresh_job()'s _refresh_running lock."""
    global _running
    with _run_lock:
        if _running:
            return {"skipped": True, "reason": "a training cycle is already running"}
        _running = True
    try:
        return run_cycle(conn, triggered_by=triggered_by)
    finally:
        with _run_lock:
            _running = False


def start_ml_scheduler(get_conn) -> None:
    """Spawns the one background thread, daily tick, non-blocking at
    startup -- mirrors depletion.start_prophet_refresh_scheduler() exactly."""
    def _loop():
        while True:
            try:
                run_scheduled_cycle(get_conn(), triggered_by="scheduler")
            except Exception:  # noqa: BLE001 -- the loop must never die
                pass
            threading.Event().wait(TICK_SECONDS)
    t = threading.Thread(target=_loop, name="ml-scheduler", daemon=True)
    t.start()
