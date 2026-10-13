"""Shared artifact storage conventions for every trained model: where files
live, how they're named, and the one load/dump wrapper every train_*()
function (ml_training.py) and serving function (anomaly.py, depletion.py,
associations.py) goes through.

Two formats: joblib for a real fitted scikit-learn/Prophet object
(IsolationForest x3, Prophet), plain JSON for a model whose "state" is
already a small serializable structure (FP-Growth's mined, holdout-
validated rule set). Every payload, either format, is a dict with at least
`schema_version` and `feature_names`/`rule_shape` so startup validation
(wt/ml_startup.py) can check compatibility without trusting the pickle
blindly.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import joblib

ARTIFACT_DIR = Path(__file__).resolve().parent.parent / "ml_artifacts"


def fresh_fit_fallback_allowed() -> bool:
    """Whether a serving path may fit/mine a model fresh, in-request, when
    no trained artifact is available yet.

    Deployment requirement: a production request must never implicitly
    train a model. WT_ENV=production disables this fallback by default
    (same gate db.validate_production_startup already uses) -- each
    serving path's own rule-based/statistical layer, or an explicit
    'unavailable'/'served_from: fresh_mine_fallback' marker, takes over
    instead of silently fabricating a verdict. WT_ML_ALLOW_FRESH_FIT_
    FALLBACK=1 overrides this explicitly, for an operator who has
    deliberately chosen to accept it in production."""
    override = os.environ.get("WT_ML_ALLOW_FRESH_FIT_FALLBACK")
    if override is not None:
        return override == "1"
    return os.environ.get("WT_ENV") != "production"


def new_run_token() -> str:
    return uuid.uuid4().hex[:12]


def _filename(model_key: str, run_token: str, ext: str) -> str:
    return f"{model_key}__{run_token}.{ext}"


def resolve_path(path: str) -> Path:
    """Deployment requirement: ml_training_run.artifact_path must be
    portable across machines, not a machine-specific absolute path baked
    into the database. dump_joblib()/dump_json() below now return a path
    relative to ARTIFACT_DIR ('ml_artifacts/<file>', forward slashes,
    stored verbatim in the registry) -- moving the whole backend/
    directory (database + ml_artifacts/ together) to a different drive,
    user, or OS therefore keeps every artifact_path resolvable, because it
    is always re-anchored to THIS process's own ARTIFACT_DIR rather than
    read as a literal filesystem path.

    Historical rows written before this change recorded a full absolute
    path; those are honored as-is here (an absolute path is returned
    unchanged) rather than migrated by force, so an already-deployed
    database with pre-existing training history keeps loading without a
    separate backfill step being mandatory. A bare filename with no
    directory component is also accepted, for callers that only ever
    stored the filename."""
    p = Path(path)
    if p.is_absolute():
        return p
    if p.parent == Path("."):
        return ARTIFACT_DIR / p.name
    return (ARTIFACT_DIR.parent / p).resolve()


def dump_joblib(payload: dict, model_key: str, run_token: str) -> str:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    filename = _filename(model_key, run_token, "joblib")
    joblib.dump(payload, ARTIFACT_DIR / filename)
    return f"ml_artifacts/{filename}"


def load_joblib(path: str) -> dict:
    return joblib.load(resolve_path(path))


def dump_json(payload: dict, model_key: str, run_token: str) -> str:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    filename = _filename(model_key, run_token, "json")
    with open(ARTIFACT_DIR / filename, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return f"ml_artifacts/{filename}"


def load_json(path: str) -> dict:
    with open(resolve_path(path), "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_any(path: str) -> dict:
    return load_json(path) if path.endswith(".json") else load_joblib(path)
