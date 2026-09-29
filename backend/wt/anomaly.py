"""Phase 3 — anomaly detection via scikit-learn's IsolationForest.

Two passes, two datasets, and the distinction matters:

  detect_consumption_anomalies()  the specified model. Trains on the
    RECONCILED DAILY CONSUMPTION dataset (v_unli_daily_consumption) — one
    vector per reconciled service day per unlimited stream, built from head
    counts that came off real orders and consumption figures that came off a
    scale. Seven features, listed in FEATURE_NAMES and explained where they
    are built. This is the pass the manuscript describes; it could not exist
    before wt/unli.py because until then nothing measured actual consumption.

  detect_anomalies()  the stock-loss pass. Trains on wastage / spoilage /
    count_adjustment ledger entries. Kept, unchanged, because it answers a
    different and still-useful question ("was that write-off unusual for this
    ingredient") that the consumption pass cannot: a dropped tray of garlic
    has no head count and no unlimited stream. It is reported separately
    rather than blended in, so neither pass is mistaken for the other.

Both are descriptive only: they flag, they explain, and nothing downstream
acts on a flag. No accusation, no automatic adjustment, no punishment.

An earlier version of this module used a hand-rolled z-score instead,
specifically to avoid training a model on what was then a completely empty
ledger. The owner has since explicitly requested IsolationForest by name
(a common capstone/thesis specification), and the ledger now has real
history to train on — a 90-day seeded operating history plus whatever real
wastage/spoilage/count-adjustment entries have been logged since. So this
now really is scikit-learn's IsolationForest, not a lookalike.

Two layers each, same shape:
  - IsolationForest, trained fresh on every call across all loss-reason
    ledger entries in the lookback window (pooled across ingredients, with
    each entry's magnitude normalized against that specific ingredient's
    own average first, so a naturally large-quantity ingredient doesn't
    dominate the model). Only runs once there's enough pooled volume to
    train on at all — see MIN_POOLED_SAMPLES.
  - a no-history rule that needs no training data: any single entry
    removing >=25% of what was on hand right before it. An ingredient's
    first-ever large loss has nothing to compare against yet; this catches
    it anyway.

Runs on demand (GET /console/anomalies), not as a background job — there is
no scheduler in this deployment.
"""
from __future__ import annotations

import sqlite3

import numpy as np
from sklearn.ensemble import IsolationForest

from . import timeutil
from .db import write_tx
from .errors import InvalidStateError

REVIEW_STATUSES = ("flagged", "under_review", "confirmed_anomaly", "false_positive", "resolved")

ANOMALY_REASONS = ("wastage", "spoilage", "count_adjustment")
MIN_POOLED_SAMPLES = 15          # entries needed before IsolationForest is trained at all
RULE_FRACTION_THRESHOLD = 0.25   # a single entry removing >=25% of prior on-hand stock
N_ESTIMATORS = 200
RANDOM_STATE = 42                # deterministic output for the same ledger

# 2026-09 audit: detect_anomalies() trained IsolationForest with
# contamination="auto" and NO cap, unlike the consumption pass below (see
# MAX_FLAG_FRACTION's own comment, which explains why an uncapped 'auto'
# cutoff is unsafe on this kind of pooled, mostly-ordinary data). Against
# the real production ledger this flagged 496 of 2,534 entries (19.6%) —
# one in five wastage/spoilage/count-adjustment rows called "unusual" is not
# a usable signal for anyone; it is the model describing the shape of the
# whole distribution, not outliers within it. Same fix as the consumption
# pass: keep only the worst-scoring MAX_STOCK_LOSS_FLAG_FRACTION before the
# rule-based pass (which needs no cap — it already requires a >=25%-of-
# on-hand single event, which is rare by construction) adds any more.
MAX_STOCK_LOSS_FLAG_FRACTION = 0.10

# Ground-truth threshold for detect_anomalies()'s Precision/Recall/F1 — see
# anomaly_model_details()'s validation block. Chosen the same way
# MIN_RECONCILED_DAYS/MIN_POOLED_SAMPLES were: small enough to be reachable,
# large enough that one or two lucky/unlucky reviews cannot swing a reported
# metric wildly. Never computed below this regardless of how the labels
# split between confirmed/false-positive.
MIN_VALIDATED_LABELS = 20

# --- consumption pass (the specified model) --------------------------------
MIN_RECONCILED_DAYS = 12         # reconciled service-days before the forest is trained
RULE_VARIANCE_MULTIPLE = 2.0     # |variance| >= this many x the configured tolerance

# Ceiling on how much of the dataset the forest is allowed to call unusual.
#
# contamination='auto' sets its cutoff from the score distribution, not from
# any notion of how many outliers there ought to be. On a well-run shop —
# every reconciled day landing within a few percent of the coefficient, which
# is exactly the state the system is trying to reach — the scores bunch so
# tightly that the cutoff falls above nearly all of them and the forest
# returns -1 for the entire history. A run of consistent days is the opposite
# of an anomaly, so past this fraction the model is describing the whole
# distribution rather than outliers in it, and only the worst-scoring days are
# kept. Found by a test that seeded sixteen ordinary days and got sixteen
# flags back.
MAX_FLAG_FRACTION = 0.25

FEATURE_NAMES = (
    "actual_per_head",
    "variance_ratio",
    "baseline_deviation_ratio",
    "peer_rate_ratio",
    "leftover_ratio",
    "heads",
    "consumption_scale_ratio",
)


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def detect_anomalies(conn: sqlite3.Connection, lookback_days: int = 90) -> list[dict]:
    placeholders = ",".join("?" * len(ANOMALY_REASONS))
    rows = conn.execute(
        f"""
        SELECT l.id, l.ingredient_id, i.name AS ingredient_name, i.base_unit,
               l.delta_base, l.balance_after_base, l.reason, l.wastage_reason,
               l.note, l.user_id, l.created_at
          FROM stock_ledger l JOIN ingredient i ON i.id = l.ingredient_id
         WHERE l.reason IN ({placeholders})
           AND l.created_at >= ?
         ORDER BY l.ingredient_id, l.created_at
        """,
        (*ANOMALY_REASONS, timeutil.utc_bound_n_manila_days_ago(lookback_days)),
    ).fetchall()
    if not rows:
        return []

    by_ingredient: dict[int, list] = {}
    for r in rows:
        by_ingredient.setdefault(r["ingredient_id"], []).append(r)

    records = []
    for r in rows:
        mag = abs(r["delta_base"])
        balance_before = r["balance_after_base"] - r["delta_base"]
        fraction = (mag / balance_before) if balance_before > 0 else 0.0
        peer_mags = [abs(x["delta_base"]) for x in by_ingredient[r["ingredient_id"]]]
        peer_mean = sum(peer_mags) / len(peer_mags)
        relative_mag = (mag / peer_mean) if peer_mean > 0 else 0.0
        records.append({"row": r, "mag": mag, "balance_before": balance_before,
                        "fraction": fraction, "relative_mag": relative_mag})

    flagged_by_id: dict[int, dict] = {}

    if len(records) >= MIN_POOLED_SAMPLES:
        X = np.array([[rec["relative_mag"], rec["fraction"]] for rec in records])
        model = IsolationForest(n_estimators=N_ESTIMATORS, contamination="auto",
                                random_state=RANDOM_STATE)
        preds = model.fit_predict(X)
        scores = model.decision_function(X)   # lower (more negative) = more anomalous
        # Cap to the worst-scoring MAX_STOCK_LOSS_FLAG_FRACTION of the pool —
        # see that constant's comment for why 'auto' alone over-flags this
        # data. Sorted first so a cap that bites keeps the genuinely worst
        # entries, not an arbitrary slice of an over-broad verdict.
        candidates = sorted(
            ((rec, score) for rec, pred, score in zip(records, preds, scores) if pred == -1),
            key=lambda pair: pair[1],
        )
        cap = max(1, int(len(records) * MAX_STOCK_LOSS_FLAG_FRACTION)) if candidates else 0
        for rec, score in candidates[:cap]:
            r = rec["row"]
            flagged_by_id[r["id"]] = {
                "ledger_id": r["id"], "ingredient_id": r["ingredient_id"],
                "ingredient_name": r["ingredient_name"], "base_unit": r["base_unit"],
                "reason": r["reason"], "wastage_reason": r["wastage_reason"],
                "delta_base": round(r["delta_base"], 3), "created_at": r["created_at"],
                "note": r["note"], "user_id": r["user_id"],
                "method": "isolation_forest", "score": round(-float(score), 3),
                "why": (f"{rec['mag']:.2f} {r['base_unit']} is {rec['relative_mag']:.1f}x this "
                        f"ingredient's typical {r['reason']}, and {rec['fraction'] * 100:.0f}% of "
                        f"what was on hand right before it — flagged against the full pattern of "
                        f"recent losses, not one number in isolation"),
                "severity": "high" if score < -0.15 else "medium",
            }

    # Always runs, even when IsolationForest didn't (too little pooled
    # volume) or didn't flag a given entry — an ingredient's first big loss
    # has no peers to be unusual *relative to* yet.
    for rec in records:
        r = rec["row"]
        if r["id"] in flagged_by_id:
            continue
        if rec["balance_before"] > 0 and rec["fraction"] >= RULE_FRACTION_THRESHOLD:
            flagged_by_id[r["id"]] = {
                "ledger_id": r["id"], "ingredient_id": r["ingredient_id"],
                "ingredient_name": r["ingredient_name"], "base_unit": r["base_unit"],
                "reason": r["reason"], "wastage_reason": r["wastage_reason"],
                "delta_base": round(r["delta_base"], 3), "created_at": r["created_at"],
                "note": r["note"], "user_id": r["user_id"],
                "method": "large_fraction", "score": round(rec["fraction"], 3),
                "why": (f"{rec['mag']:.2f} {r['base_unit']} is {rec['fraction'] * 100:.0f}% of the "
                        f"{rec['balance_before']:.2f} {r['base_unit']} on hand right before this entry"),
                "severity": "high" if rec["fraction"] >= 0.5 else "medium",
            }

    flagged = list(flagged_by_id.values())
    flagged.sort(key=lambda f: f["created_at"], reverse=True)
    return flagged


# ===========================================================================
# Consumption anomalies — the specified model, over reconciled service days
# ===========================================================================
def _consumption_records(conn: sqlite3.Connection, lookback_days: int) -> list[dict]:
    """The seven-feature vectors, one per reconciled service day per stream.

    Every value below is derived from a real unli_reconciliation row — a head
    count that came off actual orders and a consumption figure that came off a
    scale. None of it is a renamed ledger statistic: a stock-loss entry has no
    head count, no coefficient and no leftover, so four of these seven
    features have no ledger analogue at all and the other three mean something
    different here.

      1 actual_per_head           Au / H. The quantity lambda is expressed in,
                                  so a day and a coefficient are comparable.
      2 variance_ratio            (Au - D) / D. How far the measurement landed
                                  from what the coefficient predicted.
      3 baseline_deviation_ratio  per-head rate against the FROZEN baseline
                                  lambda. Survives recalibration, so a slow
                                  drift away from the original measurement
                                  stays visible instead of being absorbed.
      4 peer_rate_ratio           per-head rate against this stream's own mean
                                  per-head rate in the window. Normalising per
                                  stream is what stops wings (hundreds of
                                  grams a head) from swamping drinks (tens).
      5 leftover_ratio            closing / (opening + added). Purely a
                                  measurement property: how much of what was
                                  put out came back.
      6 heads                     covers served, scaled against the window's
                                  mean. Volume is context — the same variance
                                  on 12 covers and 120 is not the same event.
      7 consumption_scale_ratio   total Au against the stream's mean total.
                                  Catches a day that is odd in absolute terms
                                  even when its per-head rate looks ordinary.
    """
    rows = conn.execute(
        """
        SELECT d.*, b.lambda_value AS baseline_lambda
          FROM v_unli_daily_consumption d
          LEFT JOIN (
                SELECT unli_item_id, lambda_value FROM unli_coefficient c1
                 WHERE role = 'baseline'
                   AND id = (SELECT MAX(c2.id) FROM unli_coefficient c2
                              WHERE c2.unli_item_id = c1.unli_item_id AND c2.role = 'baseline')
               ) b ON b.unli_item_id = d.unli_item_id
         WHERE d.service_date >= ?
           AND d.heads > 0
           AND d.validation_state <> 'rejected'
         ORDER BY d.unli_item_id, d.service_date
        """,
        (timeutil.business_date_n_days_ago(lookback_days),),
    ).fetchall()
    if not rows:
        return []

    by_stream: dict[int, list] = {}
    for r in rows:
        by_stream.setdefault(r["unli_item_id"], []).append(r)

    records = []
    for r in rows:
        peers = by_stream[r["unli_item_id"]]
        rate = r["actual_per_head"] or 0.0
        peer_rates = [x["actual_per_head"] or 0.0 for x in peers]
        peer_rate_mean = sum(peer_rates) / len(peer_rates)
        peer_totals = [x["actual_base"] or 0.0 for x in peers]
        peer_total_mean = sum(peer_totals) / len(peer_totals)
        peer_heads = [x["heads"] or 0 for x in peers]
        peer_head_mean = sum(peer_heads) / len(peer_heads)
        baseline = r["baseline_lambda"]

        records.append({
            "row": r,
            "features": {
                "actual_per_head": rate,
                "variance_ratio": r["variance_ratio"] or 0.0,
                "baseline_deviation_ratio": (rate / baseline) if baseline else 1.0,
                "peer_rate_ratio": (rate / peer_rate_mean) if peer_rate_mean > 0 else 1.0,
                "leftover_ratio": r["leftover_ratio"] if r["leftover_ratio"] is not None else 0.0,
                "heads": (r["heads"] / peer_head_mean) if peer_head_mean > 0 else 1.0,
                "consumption_scale_ratio": ((r["actual_base"] or 0.0) / peer_total_mean)
                                           if peer_total_mean > 0 else 1.0,
            },
        })
    return records


def detect_consumption_anomalies(conn: sqlite3.Connection, lookback_days: int = 90) -> list[dict]:
    """IsolationForest over the reconciled daily consumption dataset.

    Same two-layer shape as the stock-loss pass, for the same reason: a shop
    that has just started reconciling has nothing to train on, and refusing to
    say anything for its first fortnight would be useless. Below
    MIN_RECONCILED_DAYS the forest does not run at all and only the rule
    fires — a day whose variance is a large multiple of the tolerance the
    owner themselves configured needs no model to be worth a look.

    Descriptive only. A flag says a day's numbers are unusual against the rest
    of the reconciled history; it never names a cause, and nothing downstream
    acts on one.
    """
    records = _consumption_records(conn, lookback_days)
    if not records:
        return []

    flagged: dict[int, dict] = {}

    def describe(rec) -> dict:
        r, f = rec["row"], rec["features"]
        return {
            "reconciliation_id": r["reconciliation_id"],
            "service_date": r["service_date"],
            "item_key": r["item_key"], "label": r["label"],
            "ingredient_name": r["ingredient_name"], "base_unit": r["base_unit"],
            "heads": r["heads"],
            "theoretical_base": round(r["theoretical_base"], 2),
            "actual_base": round(r["actual_base"], 2),
            "actual_per_head": round(f["actual_per_head"], 2),
            "variance_base": round(r["variance_base"], 2),
            "variance_pct": round((r["variance_ratio"] or 0.0) * 100, 1),
            "status": r["status"], "validation_state": r["validation_state"],
            "features": {k: round(v, 4) for k, v in f.items()},
        }

    if len(records) >= MIN_RECONCILED_DAYS:
        X = np.array([[rec["features"][name] for name in FEATURE_NAMES] for rec in records])
        model = IsolationForest(n_estimators=N_ESTIMATORS, contamination="auto",
                                random_state=RANDOM_STATE)
        preds = model.fit_predict(X)
        scores = model.decision_function(X)
        outliers = [(rec, score) for rec, pred, score in zip(records, preds, scores)
                    if pred == -1]
        # Lowest (most negative) score first, then truncated — so when the cap
        # bites it keeps the genuinely worst days rather than an arbitrary
        # slice of an over-broad verdict.
        outliers.sort(key=lambda pair: pair[1])
        cap = max(1, int(len(records) * MAX_FLAG_FRACTION))
        for rec, score in outliers[:cap]:
            f = rec["features"]
            out = describe(rec)
            out.update({
                "method": "isolation_forest",
                "score": round(-float(score), 3),
                "why": (
                    f"{f['actual_per_head']:.0f} {rec['row']['base_unit']} a head is "
                    f"{f['peer_rate_ratio']:.2f}x this stream's usual reconciled rate and "
                    f"{f['baseline_deviation_ratio']:.2f}x its baseline coefficient, on "
                    f"{rec['row']['heads']} covers — flagged against the whole pattern of "
                    f"reconciled days, not one number in isolation"
                ),
                "severity": "high" if score < -0.15 else "medium",
            })
            flagged[out["reconciliation_id"]] = out

    tol = conn.execute(
        "SELECT value FROM app_setting WHERE key='unli_variance_tolerance'"
    ).fetchone()
    tol = float(tol["value"]) if tol else 0.10

    for rec in records:
        r, f = rec["row"], rec["features"]
        if r["reconciliation_id"] in flagged:
            continue
        vr = abs(f["variance_ratio"])
        if vr >= tol * RULE_VARIANCE_MULTIPLE:
            out = describe(rec)
            out.update({
                "method": "large_variance",
                "score": round(vr, 3),
                "why": (
                    f"measured consumption is {vr * 100:.0f}% away from the provisional "
                    f"figure — more than {RULE_VARIANCE_MULTIPLE:g}x the {tol * 100:.0f}% "
                    f"tolerance configured for this shop"
                ),
                "severity": "high" if vr >= tol * 4 else "medium",
            })
            flagged[out["reconciliation_id"]] = out

    out = list(flagged.values())
    out.sort(key=lambda f: f["service_date"], reverse=True)
    return out


def consumption_model_details(conn: sqlite3.Connection, lookback_days: int = 90) -> dict:
    """Parameters and live counts for the consumption pass, for the technical
    panel. Reports the training-set size honestly, including when it is below
    the threshold and the forest therefore did not run."""
    records = _consumption_records(conn, lookback_days)
    anomalies = detect_consumption_anomalies(conn, lookback_days)
    by_method: dict[str, int] = {}
    for a in anomalies:
        by_method[a["method"]] = by_method.get(a["method"], 0) + 1
    return {
        "model": "IsolationForest (scikit-learn)",
        "data_source": "v_unli_daily_consumption (reconciled service days)",
        "n_estimators": N_ESTIMATORS,
        "random_state": RANDOM_STATE,
        "n_features": len(FEATURE_NAMES),
        "features": list(FEATURE_NAMES),
        "min_reconciled_days": MIN_RECONCILED_DAYS,
        "rule_variance_multiple": RULE_VARIANCE_MULTIPLE,
        "max_flag_fraction": MAX_FLAG_FRACTION,
        "lookback_days": lookback_days,
        "records_evaluated": len(records),
        "trained_this_call": len(records) >= MIN_RECONCILED_DAYS,
        "flagged": len(anomalies),
        "flagged_by_method": by_method,
    }


def _review_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "ledger_id": row["ledger_id"], "status": row["status"],
        "note": row["note"], "user_id": row["user_id"], "created_at": row["created_at"],
    }


def record_anomaly_review(
    conn: sqlite3.Connection, ledger_id: int, status: str,
    note: str | None = None, user_id: str | None = None,
    client_request_id: str | None = None,
) -> dict:
    """Record a human reviewer's classification of one flagged (or any other)
    stock_ledger row -- the ground truth Precision/Recall/F1 below depend on.
    Append-only, exactly like ingredient_variance_review: a reviewer who
    changes their mind submits a NEW review rather than editing the old one.

    Reviewing an ingredient's ledger row that was never actually flagged by
    detect_anomalies() is allowed and meaningful: a reviewer confirming such
    a row as confirmed_anomaly is a false negative the model missed, which is
    exactly what makes Recall (as opposed to Precision alone, computable from
    flagged rows only) measurable at all -- see model_details()'s
    validation block.
    """
    if status not in REVIEW_STATUSES:
        raise InvalidStateError(f"unknown anomaly review status '{status}'")

    if client_request_id:
        existing = conn.execute(
            "SELECT * FROM anomaly_review WHERE client_request_id=?", (client_request_id,),
        ).fetchone()
        if existing:
            return _review_row(existing)

    with write_tx(conn):
        ledger = conn.execute("SELECT id FROM stock_ledger WHERE id=?", (ledger_id,)).fetchone()
        if not ledger:
            raise InvalidStateError(f"stock_ledger row {ledger_id} does not exist")
        cur = conn.execute(
            "INSERT INTO anomaly_review (ledger_id, status, note, user_id, client_request_id) "
            "VALUES (?,?,?,?,?)",
            (ledger_id, status, note, user_id, client_request_id),
        )
        row = conn.execute("SELECT * FROM anomaly_review WHERE id=?", (cur.lastrowid,)).fetchone()
    return _review_row(row)


def anomaly_review_history(conn: sqlite3.Connection, ledger_id: int) -> list[dict]:
    """Every review ever recorded for this ledger row, oldest first."""
    return [
        _review_row(r) for r in conn.execute(
            "SELECT * FROM anomaly_review WHERE ledger_id=? ORDER BY id", (ledger_id,),
        ).fetchall()
    ]


def _latest_review_statuses(conn: sqlite3.Connection) -> dict[int, str]:
    """ledger_id -> most recent review status, across every ledger row that
    has ever been reviewed at all (not scoped to the current lookback window
    -- a validated label does not expire just because the underlying ledger
    row aged out of a 90-day query)."""
    rows = conn.execute(
        """
        SELECT ar.ledger_id, ar.status FROM anomaly_review ar
         WHERE ar.id = (SELECT MAX(ar2.id) FROM anomaly_review ar2 WHERE ar2.ledger_id = ar.ledger_id)
        """
    ).fetchall()
    return {r["ledger_id"]: r["status"] for r in rows}


def validation_metrics(conn: sqlite3.Connection, lookback_days: int = 90) -> dict:
    """Precision/Recall/F1/confusion matrix for the stock-loss Isolation
    Forest pass, built ONLY from human-reviewed ledger rows -- never
    fabricated, never inferred from the model's own scores.

    TP = flagged by the model AND a human confirmed it as an anomaly.
    FP = flagged by the model AND a human classified it false_positive.
    FN = NOT flagged AND a human separately confirmed it as an anomaly
         (a reviewer catching something the model missed).
    TN = NOT flagged AND a human classified it false_positive (i.e.
         reviewed and found ordinary).

    Below MIN_VALIDATED_LABELS confirmed_anomaly + false_positive labels,
    or with zero labels on either side of "flagged by the model", the
    corresponding metric is reported as unavailable rather than computed
    from an incomplete confusion matrix cell -- see the returned
    `available`/`reason` fields, which the API/frontend must show instead of
    a number.
    """
    anomalies = detect_anomalies(conn, lookback_days)
    flagged_ids = {a["ledger_id"] for a in anomalies}
    statuses = _latest_review_statuses(conn)

    tp = fp = fn = tn = 0
    for ledger_id, status in statuses.items():
        is_flagged = ledger_id in flagged_ids
        if status == "confirmed_anomaly":
            tp += is_flagged
            fn += (not is_flagged)
        elif status == "false_positive":
            fp += is_flagged
            tn += (not is_flagged)

    total_labels = tp + fp + fn + tn
    if total_labels < MIN_VALIDATED_LABELS:
        return {
            "available": False,
            "reason": ("Validation metrics unavailable — insufficient confirmed labels "
                      f"({total_labels}/{MIN_VALIDATED_LABELS} required: confirmed_anomaly "
                      "or false_positive reviews)"),
            "confirmed_labels": total_labels, "min_required": MIN_VALIDATED_LABELS,
            "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        }

    precision = (tp / (tp + fp)) if (tp + fp) > 0 else None
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall and (precision + recall) > 0 else None
    return {
        "available": True,
        "confirmed_labels": total_labels,
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": round(precision, 3) if precision is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
        "recall_note": (None if fn > 0 or tn > 0 else
                        "no unflagged ledger row has been independently reviewed yet, so Recall "
                        "and the FN/TN cells cannot yet reflect anomalies the model missed"),
    }


def model_details(conn: sqlite3.Connection, lookback_days: int = 90) -> dict:
    """Real parameters and current stats for the Isolation Forest pass —
    backs the technical panel under System Check, not the main Anomaly
    Detection page (which stays plain-language)."""
    anomalies = detect_anomalies(conn, lookback_days)
    placeholders = ",".join("?" * len(ANOMALY_REASONS))
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM stock_ledger WHERE reason IN ({placeholders}) "
        "AND created_at >= ?",
        (*ANOMALY_REASONS, timeutil.utc_bound_n_manila_days_ago(lookback_days)),
    ).fetchone()["n"]
    by_method: dict[str, int] = {}
    for a in anomalies:
        by_method[a["method"]] = by_method.get(a["method"], 0) + 1
    return {
        "model": "IsolationForest (scikit-learn)",
        "n_estimators": N_ESTIMATORS,
        "min_pooled_samples": MIN_POOLED_SAMPLES,
        "rule_fraction_threshold": RULE_FRACTION_THRESHOLD,
        "max_flag_fraction": MAX_STOCK_LOSS_FLAG_FRACTION,
        "lookback_days": lookback_days,
        "records_evaluated": total,
        "trained_this_call": total >= MIN_POOLED_SAMPLES,
        "flagged": len(anomalies),
        "flagged_by_method": by_method,
        "validation": validation_metrics(conn, lookback_days),
    }
