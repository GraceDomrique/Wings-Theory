"""ML model registry (migration 0050): one row per logical model in
ml_model_registry (current mode, lock, auto-promote flag, pointer to the
currently active ml_training_run), and the append-ish history of every
training attempt in ml_training_run.

The one invariant every write path here preserves: a model's `mode` and its
`active_training_run_id` change together, in one transaction, or not at all.
There is no code path that updates ml_model_registry.mode without a
validated, already-fitted run backing it — a "mode promotion" IS a training
run that happened to be trained under the new mode and passed validation,
not a bare flag flip. This is what "train, validate, then activate the new
model and mode together; failed training must preserve the previous active
model" (see the plan) means in code: activate_training_run() is the only way
mode moves forward, record_failed_run() never touches ml_model_registry at
all, and rollback() reuses the exact same atomic path to go backward.

wt/ml_training.py is the only caller of activate_training_run/
record_failed_run (one call per train_*() function, after that function's
own validation decides pass/fail). wt/api.py's manual "promote mode" action
triggers a real retrain under the requested mode rather than calling
anything in this module directly — see ml_training.retrain_model().
"""
from __future__ import annotations

import json
import sqlite3

from .db import write_tx
from .errors import InvalidStateError, NotFoundError, ValidationError

MODES = ("SIMULATED", "MIXED", "REAL")
MODEL_KEYS = (
    "stock_loss_isoforest",
    "unli_consumption_isoforest",
    "flavor_consumption_isoforest",
    "demand_prophet",
    "fpgrowth_combos",
)
# SIMULATED -> MIXED -> REAL -- a promotion always steps exactly one notch,
# never skips. Used by ml_training.py's readiness-driven auto-promote.
_NEXT_MODE = {"SIMULATED": "MIXED", "MIXED": "REAL", "REAL": "REAL"}


def next_mode(current: str) -> str:
    return _NEXT_MODE[current]


def _registry_row(conn: sqlite3.Connection, model_key: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM ml_model_registry WHERE model_key=?", (model_key,)
    ).fetchone()
    if not row:
        raise NotFoundError(f"unknown model_key '{model_key}'")
    return row


def get_mode(conn: sqlite3.Connection, model_key: str) -> str:
    return _registry_row(conn, model_key)["mode"]


def is_mode_locked(conn: sqlite3.Connection, model_key: str) -> bool:
    return bool(_registry_row(conn, model_key)["mode_locked"])


def _hydrate(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["metrics"] = json.loads(d.pop("metrics_json"))
    return d


def _run_row(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM ml_training_run WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise NotFoundError(f"training run {run_id} not found")
    return row


def active_run(conn: sqlite3.Connection, model_key: str) -> dict | None:
    row = conn.execute(
        "SELECT r.* FROM ml_training_run r "
        "JOIN ml_model_registry m ON m.active_training_run_id = r.id "
        "WHERE m.model_key=?",
        (model_key,),
    ).fetchone()
    return _hydrate(row) if row else None


def active_artifact_path(conn: sqlite3.Connection, model_key: str) -> str | None:
    run = active_run(conn, model_key)
    return run["artifact_path"] if run else None


def training_history(conn: sqlite3.Connection, model_key: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM ml_training_run WHERE model_key=? ORDER BY trained_at DESC, id DESC LIMIT ?",
        (model_key, limit),
    ).fetchall()
    return [_hydrate(r) for r in rows]


def list_models(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for row in conn.execute("SELECT * FROM ml_model_registry ORDER BY model_key"):
        out.append({
            "model_key": row["model_key"],
            "display_name": row["display_name"],
            "mode": row["mode"],
            "mode_locked": bool(row["mode_locked"]),
            "auto_promote": bool(row["auto_promote"]),
            "active_run": active_run(conn, row["model_key"]),
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        })
    return out


def _insert_run_row(
    conn: sqlite3.Connection, model_key: str, *, mode: str, dataset_version: str,
    real_count: int, synthetic_count: int, schema_version: str, metrics: dict,
    artifact_path: str, status: str, triggered_by: str, notes: str | None,
) -> int:
    if mode not in MODES:
        raise ValidationError(f"unknown mode '{mode}'")
    cur = conn.execute(
        "INSERT INTO ml_training_run "
        "(model_key, mode, dataset_version, real_count, synthetic_count, schema_version, "
        " metrics_json, artifact_path, status, triggered_by, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (model_key, mode, dataset_version, real_count, synthetic_count, schema_version,
         json.dumps(metrics), artifact_path, status, triggered_by, notes),
    )
    return cur.lastrowid


def activate_training_run(
    conn: sqlite3.Connection, model_key: str, *, mode: str, dataset_version: str,
    real_count: int, synthetic_count: int, schema_version: str, metrics: dict,
    artifact_path: str, triggered_by: str, notes: str | None = None,
) -> dict:
    """Record a run that the caller has already validated, and promote it to
    'active' in the same transaction that updates ml_model_registry.mode —
    the only write path that ever changes mode. Demotes whatever was active
    before to 'superseded'."""
    _registry_row(conn, model_key)  # 404s on an unknown model_key before opening the tx
    with write_tx(conn):
        prior = active_run(conn, model_key)
        run_id = _insert_run_row(
            conn, model_key, mode=mode, dataset_version=dataset_version,
            real_count=real_count, synthetic_count=synthetic_count,
            schema_version=schema_version, metrics=metrics, artifact_path=artifact_path,
            status="active", triggered_by=triggered_by, notes=notes,
        )
        if prior:
            conn.execute("UPDATE ml_training_run SET status='superseded' WHERE id=?", (prior["id"],))
        conn.execute(
            "UPDATE ml_model_registry SET mode=?, active_training_run_id=?, "
            " updated_by=?, updated_at=datetime('now') WHERE model_key=?",
            (mode, run_id, triggered_by, model_key),
        )
        return _hydrate(_run_row(conn, run_id))


def record_failed_run(
    conn: sqlite3.Connection, model_key: str, *, mode: str, dataset_version: str,
    real_count: int, synthetic_count: int, schema_version: str, metrics: dict,
    artifact_path: str, triggered_by: str, notes: str | None = None,
) -> dict:
    """Record a run that failed validation. Never touches ml_model_registry
    — the previous active run/mode is left exactly as it was, by
    construction (there is no UPDATE ml_model_registry statement in this
    function at all)."""
    _registry_row(conn, model_key)
    with write_tx(conn):
        run_id = _insert_run_row(
            conn, model_key, mode=mode, dataset_version=dataset_version,
            real_count=real_count, synthetic_count=synthetic_count,
            schema_version=schema_version, metrics=metrics, artifact_path=artifact_path,
            status="failed", triggered_by=triggered_by, notes=notes,
        )
        return _hydrate(_run_row(conn, run_id))


def rollback(conn: sqlite3.Connection, model_key: str, *, triggered_by: str, reason: str | None = None) -> dict:
    """Reactivate the run that was active immediately before the current
    one, through the same atomic mode+model path activate_training_run uses
    — never a bare UPDATE. The current active run becomes 'rolled_back'
    (distinct from the ordinary 'superseded' an activation produces) so
    training_history shows a rollback happened, not just a routine
    promotion."""
    with write_tx(conn):
        current = active_run(conn, model_key)
        if current is None:
            raise NotFoundError(f"no active run for '{model_key}'")
        prior_row = conn.execute(
            "SELECT * FROM ml_training_run WHERE model_key=? AND status='superseded' "
            "ORDER BY trained_at DESC, id DESC LIMIT 1",
            (model_key,),
        ).fetchone()
        if prior_row is None:
            raise InvalidStateError(f"no prior training run to roll back to for '{model_key}'")
        conn.execute("UPDATE ml_training_run SET status='rolled_back' WHERE id=?", (current["id"],))
        conn.execute("UPDATE ml_training_run SET status='active' WHERE id=?", (prior_row["id"],))
        conn.execute(
            "UPDATE ml_model_registry SET mode=?, active_training_run_id=?, "
            " updated_by=?, updated_at=datetime('now') WHERE model_key=?",
            (prior_row["mode"], prior_row["id"], triggered_by, model_key),
        )
        if reason:
            conn.execute(
                "UPDATE ml_training_run SET notes = COALESCE(notes,'') || ? WHERE id=?",
                (f" [rolled back: {reason}]", current["id"]),
            )
        return _hydrate(_run_row(conn, prior_row["id"]))


def approved_batch_ids(conn: sqlite3.Connection) -> list[str]:
    return [r["batch_id"] for r in conn.execute("SELECT batch_id FROM ml_approved_synthetic_batch")]
