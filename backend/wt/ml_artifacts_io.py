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
import uuid
from pathlib import Path

import joblib

ARTIFACT_DIR = Path(__file__).resolve().parent.parent / "ml_artifacts"


def new_run_token() -> str:
    return uuid.uuid4().hex[:12]


def _path(model_key: str, run_token: str, ext: str) -> str:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    return str(ARTIFACT_DIR / f"{model_key}__{run_token}.{ext}")


def dump_joblib(payload: dict, model_key: str, run_token: str) -> str:
    path = _path(model_key, run_token, "joblib")
    joblib.dump(payload, path)
    return path


def load_joblib(path: str) -> dict:
    return joblib.load(path)


def dump_json(payload: dict, model_key: str, run_token: str) -> str:
    path = _path(model_key, run_token, "json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_any(path: str) -> dict:
    return load_json(path) if path.endswith(".json") else load_joblib(path)
