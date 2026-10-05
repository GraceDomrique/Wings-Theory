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

import random
import sqlite3

import numpy as np
from sklearn.ensemble import IsolationForest

from . import timeutil
from .db import write_tx
from .errors import InvalidStateError, NotFoundError

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

# Same real-vs-synthetic test as _REAL_USER_CLAUSE, over unli_reconciliation.
# recorded_by instead of stock_ledger.user_id -- record_reconciliation()
# (wt/unli.py) stamps this with the authenticated caller's real username on
# every live call and demo_seed._seed_unlimited_reconciliation() stamps the
# identical column with its own fixed, non-account USER_ID on every
# synthetic call (see that function's own docstring: actual_base there is
# theoretical_base times a random ratio, never a scale reading). NULL is
# treated the same as in the stock-loss clause -- a legitimate value with no
# specific human operator, never excluded by itself.
_REAL_RECORDER_CLAUSE = "(d.recorded_by IS NULL OR d.recorded_by IN (SELECT username FROM app_user))"

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

# 2026-09-30 finding, stock-loss pass, RE-AUDITED 2026-10 for exact
# provenance (see that audit's own report for the full per-mechanism trace):
# every wastage/spoilage/count_adjustment row in the ledger -- 11,135 of
# 11,135, zero exceptions, reconciled exactly by SQL against the real
# database -- carries user_id='xlsx_history_import'. The 2026-09-30 note
# attributed the whole 11,135 to demo_seed._seed_wastage_history() alone;
# that was imprecise about the MECHANISM (the exclusion conclusion itself
# was and remains correct). The real breakdown, reconciled exactly:
#
#   11,036 (99.1%) ref_type='unli_refill', wastage_reason='corrected: demo:
#          wrong flavour served' -- NOT from _seed_wastage_history() at all.
#          These come from refills.correct_refill() (a REAL, live production
#          function -- see POST .../refills/{id}/correct), invoked by
#          demo_seed._seed_unlimited_order() on a 7%-per-served-round coin
#          flip (`rng.random() < 0.07`) while xlsx_history_import.py replays
#          each GENUINE historical unlimited-wings order from the order-
#          history workbook. The order being replayed is real; the specific
#          claim "this particular round came out as the wrong flavour" is
#          not -- the workbook records no refill-round-level data at all for
#          it to be derived from, so a random coin flip is the only possible
#          source. Volume is high because real historical order volume is
#          high (~16,700 unlimited orders replayed, each with 1-5 refill
#          rounds, 7% of served rounds flipped) -- that correlation with real
#          order volume does not make the individual "wrong flavour" claim
#          itself any less fabricated.
#      70  ref_type='manual_writeoff': demo_seed._seed_wastage_history()'s
#          actual output -- 67 reason='wastage' (random ingredient, random
#          date, random 0.5-2% of current on-hand, one of four canned notes)
#          + 3 reason='spoilage' (random ingredient, random 30-50% of
#          on-hand, one of two canned notes). Exactly the function this note
#          used to credit with the entire 11,135.
#      29  ref_type='manual_count': demo_seed._seed_stock_counts() -- random
#          ingredient sample, random date/time, a random +/-% applied to
#          whatever the ingredient's already-synthetic-consumption-adjusted
#          on_hand_base happened to be, never compared against an
#          independently observed physical count.
#
# None of the three mechanisms reads anything from the source workbooks
# (data/Wings_Theory_Order_Historys.xlsx, data/Wings_Theory_Menu_Ingredient_
# List.xlsx) to decide an event's existence, quantity, ingredient, or date --
# confirmed by reading both workbooks directly: neither contains a wastage,
# spoilage, physical-count, receiving, or stock-discrepancy column of any
# kind, only order/sales transactions and recipe/menu/ingredient master
# data. There is no genuine stock-loss observation available anywhere in
# this project's source material to have been wrongly excluded. demo_seed's
# own non-import CLI path (dev-only) stamps the same category of row with
# user_id='demo_seed'.
#
# A human reviewer confirmed the exclusion conclusion empirically at scale,
# independent of this mechanism trace: reviewing 1,140 flagged rows (100% of
# them synthetic-sourced), 1,094 (96%) were marked false_positive. Precision
# computed against a population that is entirely synthetic filler is not a
# measurement of this model's real-world detection ability -- it is a
# measurement of how often a random generator's idea of "unusual" agrees
# with a human's, which is a different and far less useful question.
#
# The fix is exclusion, not deletion: these rows stay in stock_ledger
# (removing them would be a destructive rewrite of the ledger's own history
# and would also corrupt the on-hand balances every later row's
# balance_after_base was computed relative to) but are no longer offered to
# IsolationForest as training data or candidates to flag. See
# detect_anomalies()'s own use of this below, and validation_metrics(),
# which must also stop scoring REVIEWS of now-excluded synthetic rows as if
# they still applied to the model's live candidate population.
#
# 2026-10 audit: kept as the known, named historical sources (used only for
# the human-readable synthetic_excluded_note below) -- but no longer the
# actual eligibility TEST. A hardcoded list only protects against the two
# importers that happen to be on it; any future bulk importer whose author
# forgot to add its username here would silently become "real" operational
# data. The real test (_REAL_USER_CLAUSE) is now structural: a row is
# eligible if its user_id is NULL (a legitimate system-internal write with no
# specific operator) or belongs to a genuine app_user account. Every live
# write that can reach record_count/record_wastage/commit_order already
# carries the authenticated caller's real username (api.py's p["sub"]) --
# neither 'xlsx_history_import' nor 'demo_seed' is ever a row in app_user
# (confirmed: both are offline scripts, never issued a login token), so this
# single rule already covers both named sources AND generalizes to any
# future one without needing to be remembered and extended by hand.
SYNTHETIC_SEED_USER_IDS = ("xlsx_history_import", "demo_seed")

# See SYNTHETIC_SEED_USER_IDS's comment above for why membership in app_user,
# not a hardcoded exclusion list, is the actual eligibility test everywhere
# below. NULL is a legitimate real value (a system-internal write with no
# specific human operator) and is never excluded.
#
# 2026-10 provenance audit (re-evaluating this exact clause, not just the
# two named sources it replaced): the question this answers is "did the
# underlying observation come from a real operational event", which is NOT
# the same question as "who/what inserted this row" -- a username is a proxy
# for provenance, not provenance itself. Evaluated against both failure
# modes directly:
#
#   FALSE EXCLUSION -- could a genuine observation be thrown out merely
#   because an importer's code path touched it? Checked against this
#   project's one real importer (xlsx_history_import.py): its sale_commit
#   rows (DERIVED_OPERATIONAL -- real order x real recipe = deterministic
#   consumption, see resolver.order_requirements) also carry user_id=
#   'xlsx_history_import' and WOULD be excluded by this clause today. This
#   is currently harmless only because 'sale_commit' is not, and must never
#   become, an ANOMALY_REASONS entry (see that constant and detect_
#   anomalies()'s own docstring: ordinary recipe consumption is not stock
#   loss, regardless of how real the underlying order is) -- but it is a
#   real, named architectural edge rather than a hypothetical one, worth
#   stating plainly rather than leaving implicit: THIS clause answers
#   eligibility for the three loss reasons only; it is not a general-purpose
#   "is this row real" oracle for every ledger reason.
#
#   FALSE INCLUSION -- could a generated row be accepted merely because it
#   used a real app_user's username or NULL? Structurally yes: this clause
#   trusts that code writing to stock_ledger tells the truth about who
#   authored the write. Every actual generator in this codebase (demo_seed,
#   xlsx_history_import) consistently uses a fixed, non-account string for
#   exactly this reason (see their own USER_ID constants) and is never
#   invoked against a production database (demo_seed.py's own module
#   docstring; db.validate_production_startup), so this does not currently
#   manifest. It is the same trust boundary any provenance scheme has,
#   including a dedicated is_synthetic column: a column is only as honest as
#   the code that sets it, same as a username is only as honest as the code
#   that stamps it. Solving it would mean verifying DOWNSTREAM of the write
#   (e.g. reconciling a claimed event against the source workbooks the way
#   this very audit did by hand) and is out of proportion for this project's
#   scale -- see TestAnomalyProvenanceTaxonomy's own test documenting this
#   exact boundary rather than hiding it.
#
# Net: sufficient and safe for this project's actual scope (zero false
# exclusions or inclusions against the 11,135 synthetic + 0 real rows this
# audit reconciled exactly), not a claim that it is unconditionally
# tamper-proof. A real schema-level provenance column (source_type,
# is_synthetic) was evaluated and rejected FOR NOW: it would not close the
# trust-boundary gap above (a generator could misreport that column exactly
# as easily as it could misreport user_id) and would touch the core,
# heavily-tested stock_ledger table for no corresponding reduction in actual
# risk at this project's current scale.
_REAL_USER_CLAUSE = "(l.user_id IS NULL OR l.user_id IN (SELECT username FROM app_user))"

# --- reproducibility: version tags frozen onto every anomaly_validation_
# sample row at evaluation time (migration 0041) ---------------------------
#
# IsolationForest is refit fresh on every call (see the module docstring) --
# there is no persisted model artifact, and random_state=42 only makes a
# fit deterministic for a GIVEN training population; the population itself
# grows as new real ledger rows accumulate, so the exact same code can
# legitimately score the same ledger row differently on two different days.
# Freezing the full feature matrix or training-population fingerprint at
# every evaluation would make that reproducible bit-for-bit, but is not
# justified at this project's scale (a single-branch shop whose real
# eligible population is currently zero -- see model_details()). What IS
# frozen, and is enough to explain any later question about why a given
# sample entered the review queue with a given score: the CONFIGURATION that
# produced it (this string) and the ELIGIBILITY rule that decided what was
# even in the candidate pool (the one below). Bump either literal whenever
# the corresponding logic changes in a way that could move scores or
# eligibility; scientific_validation_metrics() refuses to pool samples
# frozen under different version pairs into one confusion matrix.
ANOMALY_MODEL_VERSION = "stock_loss_isoforest_v1"
ELIGIBILITY_FILTER_VERSION = "app_user_membership_v1"

HUMAN_LABELS = ("pending", "confirmed_anomaly", "expected_or_normal")

# --- review-sampling defaults (sample_for_review) --------------------------
# Deliberately small, proportionate to a single-branch restaurant: the goal
# is enough independent human evidence to eventually measure Recall/
# Specificity, not to turn an admin into a full-time labeler. All three are
# overridable per call.
DEFAULT_FLAGGED_SAMPLE_CAP = 15     # review every real flag up to this many, not just a slice
DEFAULT_NEAR_THRESHOLD_SAMPLE = 5
DEFAULT_RANDOM_SAMPLE = 5
# No single ingredient may contribute more than this fraction of the random
# sample -- see sample_for_review()'s own docstring for why (one high-volume
# ingredient's routine counts would otherwise dominate the "normal" pool and
# bias Specificity toward how well the model handles THAT ingredient alone).
MAX_INGREDIENT_SHARE_OF_RANDOM_SAMPLE = 0.5

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


def _evaluate_stock_loss(conn: sqlite3.Connection, lookback_days: int = 90) -> tuple[list[dict], float | None]:
    """The shared evaluation core behind detect_anomalies() (below) AND
    sample_for_review() -- one real-eligible record list, each carrying its
    model_prediction/score/method, so a human sampling "near threshold" rows
    and the console's flagged list are always reading the SAME evaluation,
    never two independently-run passes that could disagree with each other.

    Returns (records, decision_threshold). Each record carries: the raw
    ledger row, the two engineered features (relative_mag, fraction),
    `prediction` ('flagged'/'unflagged'), `score` (public sign convention:
    higher = more anomalous; None only when nothing scoreable ran at all,
    which cannot happen for a non-empty records list since the rule-based
    layer always assigns a score), `method` ('isolation_forest'/
    'large_fraction'/None), and `severity`. decision_threshold is the public-
    convention score of the least-anomalous row that still made the
    IsolationForest cap -- the live boundary "near threshold" sampling reads
    against -- or None when the forest did not train this call (pool below
    MIN_POOLED_SAMPLES, so every prediction came from the rule alone).
    """
    placeholders = ",".join("?" * len(ANOMALY_REASONS))
    rows = conn.execute(
        f"""
        SELECT l.id, l.ingredient_id, i.name AS ingredient_name, i.base_unit,
               l.delta_base, l.balance_after_base, l.reason, l.wastage_reason,
               l.note, l.user_id, l.created_at
          FROM stock_ledger l JOIN ingredient i ON i.id = l.ingredient_id
         WHERE l.reason IN ({placeholders})
           AND l.created_at >= ?
           AND {_REAL_USER_CLAUSE}
         ORDER BY l.ingredient_id, l.created_at
        """,
        (*ANOMALY_REASONS, timeutil.utc_bound_n_manila_days_ago(lookback_days)),
    ).fetchall()
    if not rows:
        return [], None

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
        records.append({
            "row": r, "mag": mag, "balance_before": balance_before,
            "fraction": fraction, "relative_mag": relative_mag,
            "prediction": "unflagged", "score": None, "method": None,
        })

    decision_threshold = None

    if len(records) >= MIN_POOLED_SAMPLES:
        X = np.array([[rec["relative_mag"], rec["fraction"]] for rec in records])
        model = IsolationForest(n_estimators=N_ESTIMATORS, contamination="auto",
                                random_state=RANDOM_STATE)
        preds = model.fit_predict(X)
        raw_scores = model.decision_function(X)   # lower (more negative) = more anomalous
        for rec, raw in zip(records, raw_scores):
            rec["_raw_score"] = float(raw)
            rec["score"] = round(-rec["_raw_score"], 3)   # public sign: higher = more anomalous
        # Cap to the worst-scoring MAX_STOCK_LOSS_FLAG_FRACTION of the pool —
        # see that constant's comment for why 'auto' alone over-flags this
        # data. Sorted first so a cap that bites keeps the genuinely worst
        # entries, not an arbitrary slice of an over-broad verdict.
        candidates = sorted(
            (rec for rec, pred in zip(records, preds) if pred == -1),
            key=lambda rec: rec["_raw_score"],
        )
        cap = max(1, int(len(records) * MAX_STOCK_LOSS_FLAG_FRACTION)) if candidates else 0
        for rec in candidates[:cap]:
            rec["prediction"] = "flagged"
            rec["method"] = "isolation_forest"
        if cap and candidates:
            decision_threshold = candidates[min(cap, len(candidates)) - 1]["score"]

    # Always runs, even when IsolationForest didn't (too little pooled
    # volume) or didn't flag a given entry — an ingredient's first big loss
    # has no peers to be unusual *relative to* yet.
    for rec in records:
        if rec["prediction"] == "flagged":
            continue
        if rec["balance_before"] > 0 and rec["fraction"] >= RULE_FRACTION_THRESHOLD:
            rec["prediction"] = "flagged"
            rec["method"] = "large_fraction"
            rec["score"] = round(rec["fraction"], 3)

    return records, decision_threshold


def detect_anomalies(conn: sqlite3.Connection, lookback_days: int = 90) -> list[dict]:
    """See SYNTHETIC_SEED_USER_IDS's own comment for why rows sourced from
    the demo/import seeders (and any future equivalent) are excluded from
    both training and flagging here -- confirmed, not assumed, against the
    real production ledger."""
    records, _ = _evaluate_stock_loss(conn, lookback_days)
    flagged = []
    for rec in records:
        if rec["prediction"] != "flagged":
            continue
        r = rec["row"]
        if rec["method"] == "isolation_forest":
            severity = "high" if rec["_raw_score"] < -0.15 else "medium"
            why = (f"{rec['mag']:.2f} {r['base_unit']} is {rec['relative_mag']:.1f}x this "
                   f"ingredient's typical {r['reason']}, and {rec['fraction'] * 100:.0f}% of "
                   f"what was on hand right before it — flagged against the full pattern of "
                   f"recent losses, not one number in isolation")
        else:
            severity = "high" if rec["fraction"] >= 0.5 else "medium"
            why = (f"{rec['mag']:.2f} {r['base_unit']} is {rec['fraction'] * 100:.0f}% of the "
                   f"{rec['balance_before']:.2f} {r['base_unit']} on hand right before this entry")
        flagged.append({
            "ledger_id": r["id"], "ingredient_id": r["ingredient_id"],
            "ingredient_name": r["ingredient_name"], "base_unit": r["base_unit"],
            "reason": r["reason"], "wastage_reason": r["wastage_reason"],
            "delta_base": round(r["delta_base"], 3), "created_at": r["created_at"],
            "note": r["note"], "user_id": r["user_id"],
            "method": rec["method"], "score": rec["score"],
            "why": why, "severity": severity,
        })
    flagged.sort(key=lambda f: f["created_at"], reverse=True)
    return flagged


# ===========================================================================
# Consumption anomalies — the specified model, over reconciled service days
# ===========================================================================
def _consumption_records(conn: sqlite3.Connection, lookback_days: int) -> list[dict]:
    """The seven-feature vectors, one per GENUINE reconciled service day per
    stream.

    Every value below is derived from a unli_reconciliation row whose head
    count came off actual orders and whose actual_base is meant to be a
    consumption figure that came off a scale. "Meant to be" matters: this
    query excludes rows recorded by demo_seed._seed_unlimited_reconciliation()
    (user_id='demo_seed', or 'xlsx_history_import' when invoked from that
    importer), whose actual_base is theoretical_base times a RANDOM ratio —
    never a real measurement — via the same real-vs-synthetic test the
    stock-loss pass uses (_REAL_RECORDER_CLAUSE below), applied to unli_
    reconciliation.recorded_by (exposed through v_unli_daily_consumption as
    of migration 0042; it was not selected there before, so this filter had
    no column to act on until that migration). None of it is a renamed
    ledger statistic: a stock-loss entry has no head count, no coefficient
    and no leftover, so four of these seven features have no ledger
    analogue at all and the other three mean something different here.

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
        f"""
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
           AND {_REAL_RECORDER_CLAUSE}
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


def _evaluate_consumption(conn: sqlite3.Connection, lookback_days: int = 90) -> tuple[list[dict], float | None]:
    """The shared evaluation core behind detect_consumption_anomalies() AND
    sample_consumption_for_review() — see _evaluate_stock_loss()'s identical
    role for the stock-loss pass.

    2026-10 audit (section 12, rule-vs-ML separation): a record's "flagged"
    status in detect_consumption_anomalies()'s PUBLIC output can come from
    either the IsolationForest or the deterministic large_variance rule —
    that merge is correct for the console's own flagged list (a human does
    not need to know which layer caught a day), but it would be a real
    scientific error for VALIDATION SAMPLING: the rule is a fixed threshold
    an owner configured, not a statistical model, and attributing its
    catches to "Isolation Forest performance" would misrepresent what was
    actually evaluated. So this function keeps them as separate fields on
    every record:

      iforest_prediction  'flagged' | 'unflagged' | None. None exactly when
                          the forest did not train this call (pool below
                          MIN_RECONCILED_DAYS) -- there is then no
                          Isolation-Forest verdict for this record at all,
                          not an implicit 'unflagged'.
      score               public sign (higher = more anomalous), the
                          forest's own decision_function output. None when
                          the forest did not train.
      rule_flagged        bool, independent of the forest entirely — whether
                          the large_variance rule's own fixed threshold
                          fires for this record, computed exactly the same
                          way regardless of whether the forest ran.

    decision_threshold is the forest's own live cutoff (None when it did not
    train) — sample_consumption_for_review() only samples 'near_threshold'
    rows when this is not None, and classifies 'model_flagged' using
    iforest_prediction alone, never rule_flagged.
    """
    records = _consumption_records(conn, lookback_days)
    if not records:
        return [], None

    tol = conn.execute(
        "SELECT value FROM app_setting WHERE key='unli_variance_tolerance'"
    ).fetchone()
    tol = float(tol["value"]) if tol else 0.10

    for rec in records:
        rec["iforest_prediction"] = None
        rec["score"] = None
        f = rec["features"]
        rec["rule_flagged"] = abs(f["variance_ratio"]) >= tol * RULE_VARIANCE_MULTIPLE
        rec["rule_score"] = round(abs(f["variance_ratio"]), 3)

    decision_threshold = None
    if len(records) >= MIN_RECONCILED_DAYS:
        X = np.array([[rec["features"][name] for name in FEATURE_NAMES] for rec in records])
        model = IsolationForest(n_estimators=N_ESTIMATORS, contamination="auto",
                                random_state=RANDOM_STATE)
        preds = model.fit_predict(X)
        raw_scores = model.decision_function(X)
        for rec, raw in zip(records, raw_scores):
            rec["iforest_prediction"] = "unflagged"
            rec["_raw_score"] = float(raw)
            rec["score"] = round(-rec["_raw_score"], 3)
        candidates = sorted(
            (rec for rec, pred in zip(records, preds) if pred == -1),
            key=lambda rec: rec["_raw_score"],
        )
        cap = max(1, int(len(records) * MAX_FLAG_FRACTION))
        for rec in candidates[:cap]:
            rec["iforest_prediction"] = "flagged"
        if cap and candidates:
            decision_threshold = candidates[min(cap, len(candidates)) - 1]["score"]

    return records, decision_threshold


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
    acts on one. Public output unchanged by the 2026-10 rule/ML-separation
    refactor: a record appears here whenever EITHER layer catches it,
    exactly as before — see _evaluate_consumption()'s own docstring for
    where the two are now kept separate for validation purposes only.
    """
    records, _ = _evaluate_consumption(conn, lookback_days)

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

    tol_row = conn.execute(
        "SELECT value FROM app_setting WHERE key='unli_variance_tolerance'"
    ).fetchone()
    tol = float(tol_row["value"]) if tol_row else 0.10

    flagged: dict[int, dict] = {}
    for rec in records:
        r, f = rec["row"], rec["features"]
        if rec["iforest_prediction"] == "flagged":
            out = describe(rec)
            out.update({
                "method": "isolation_forest",
                "score": rec["score"],
                "why": (
                    f"{f['actual_per_head']:.0f} {r['base_unit']} a head is "
                    f"{f['peer_rate_ratio']:.2f}x this stream's usual reconciled rate and "
                    f"{f['baseline_deviation_ratio']:.2f}x its baseline coefficient, on "
                    f"{r['heads']} covers — flagged against the whole pattern of "
                    f"reconciled days, not one number in isolation"
                ),
                "severity": "high" if rec["_raw_score"] < -0.15 else "medium",
            })
            flagged[out["reconciliation_id"]] = out
        elif rec["rule_flagged"]:
            vr = abs(f["variance_ratio"])
            out = describe(rec)
            out.update({
                "method": "large_variance",
                "score": rec["rule_score"],
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
    the threshold and the forest therefore did not run.

    2026-10: records_evaluated counts only genuine (non-synthetic-recorded)
    reconciled days -- see _consumption_records()/_REAL_RECORDER_CLAUSE --
    with synthetic_excluded reporting how many were held back, mirroring
    model_details()'s identical honesty fix for the stock-loss pass."""
    records = _consumption_records(conn, lookback_days)
    anomalies = detect_consumption_anomalies(conn, lookback_days)
    by_method: dict[str, int] = {}
    for a in anomalies:
        by_method[a["method"]] = by_method.get(a["method"], 0) + 1

    since = timeutil.business_date_n_days_ago(lookback_days)
    total_all = conn.execute(
        "SELECT COUNT(*) AS n FROM v_unli_daily_consumption d "
        "WHERE d.service_date >= ? AND d.heads > 0 AND d.validation_state <> 'rejected'",
        (since,),
    ).fetchone()["n"]
    synthetic = total_all - len(records)

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
        "synthetic_excluded": synthetic,
        "synthetic_excluded_note": (
            None if synthetic == 0 else
            f"{synthetic} reconciled-day record(s) in this window were recorded by the "
            "demo/historical-import seeder (demo_seed._seed_unlimited_reconciliation), whose "
            "actual_base is a random multiple of the provisional estimate, not a real scale "
            "reading -- excluded from training and flagging. See _REAL_RECORDER_CLAUSE."
        ),
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


def latest_review_statuses(conn: sqlite3.Connection) -> dict[int, str]:
    """ledger_id -> most recent review status, across every ledger row that
    has ever been reviewed at all (not scoped to the current lookback window
    -- a validated label does not expire just because the underlying ledger
    row aged out of a 90-day query). Public (2026-09-30): the console's
    Losses & Stock Issues screen needs this to hide the review buttons on a
    row already reviewed, not just validation_metrics()."""
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

    2026-09-30: a review of a ledger row later excluded as synthetic-sourced
    (see SYNTHETIC_SEED_USER_IDS) is dropped from this calculation entirely,
    not counted as FN/TN -- that row is no longer part of the model's
    candidate population at all, so "the model didn't flag it" would be
    meaningless for it, not a genuine missed detection. This is exactly the
    1,140 existing reviews' current status: every one of them reviewed a
    synthetic-sourced row, so today this function honestly falls back to
    "insufficient labels" until real operational wastage/spoilage/
    count_adjustment history accumulates and gets reviewed.
    """
    anomalies = detect_anomalies(conn, lookback_days)
    flagged_ids = {a["ledger_id"] for a in anomalies}
    statuses = latest_review_statuses(conn)

    if statuses:
        placeholders = ",".join("?" * len(statuses))
        non_operational_ids = {
            r["id"] for r in conn.execute(
                f"SELECT l.id FROM stock_ledger l WHERE l.id IN ({placeholders}) "
                f"AND NOT {_REAL_USER_CLAUSE}",
                (*statuses.keys(),),
            ).fetchall()
        }
        statuses = {lid: s for lid, s in statuses.items() if lid not in non_operational_ids}

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
            "reason": ("Validation metrics unavailable — insufficient confirmed labels on "
                      "real (non-synthetic-seed) ledger rows "
                      f"({total_labels}/{MIN_VALIDATED_LABELS} required: confirmed_anomaly "
                      "or false_positive reviews). Existing reviews of synthetic-seed rows "
                      "(see SYNTHETIC_SEED_USER_IDS) do not count toward this, since those "
                      "rows are no longer in the model's candidate population."),
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
    Detection page (which stays plain-language).

    2026-09-30: `records_evaluated` now counts only real (non-synthetic-seed)
    rows -- see SYNTHETIC_SEED_USER_IDS -- with `synthetic_excluded` reporting
    how many were held back, so this number can never again silently imply
    the model is training on organic operational history when it is not."""
    anomalies = detect_anomalies(conn, lookback_days)
    placeholders = ",".join("?" * len(ANOMALY_REASONS))
    lookback_bound = timeutil.utc_bound_n_manila_days_ago(lookback_days)
    total_all = conn.execute(
        f"SELECT COUNT(*) AS n FROM stock_ledger l WHERE l.reason IN ({placeholders}) "
        "AND l.created_at >= ?",
        (*ANOMALY_REASONS, lookback_bound),
    ).fetchone()["n"]
    real = conn.execute(
        f"SELECT COUNT(*) AS n FROM stock_ledger l WHERE l.reason IN ({placeholders}) "
        f"AND l.created_at >= ? AND {_REAL_USER_CLAUSE}",
        (*ANOMALY_REASONS, lookback_bound),
    ).fetchone()["n"]
    synthetic = total_all - real
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
        "records_evaluated": real,
        "synthetic_excluded": synthetic,
        "synthetic_excluded_note": (
            None if synthetic == 0 else
            f"{synthetic} wastage/spoilage/count_adjustment row(s) in this window were "
            "generated by the historical-data seeder/importer (demo_seed._seed_wastage_history "
            "via xlsx_history_import), not real operations, and are excluded from training and "
            "flagging -- see SYNTHETIC_SEED_USER_IDS."
        ),
        "trained_this_call": real >= MIN_POOLED_SAMPLES,
        "flagged": len(anomalies),
        "flagged_by_method": by_method,
        "validation": validation_metrics(conn, lookback_days),
    }


# ===========================================================================
# Review-sampling infrastructure (migration 0041) — the scientifically
# defensible successor to validation_metrics() above, which only ever scores
# a model-flagged row (never an unflagged one), so the only review a human
# could do that fed it was a review of a flag. This section is what makes a
# False Negative or a True Negative — the two cells validation_metrics()
# above structurally cannot populate — measurable at all.
# ===========================================================================
def _validation_sample_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "ledger_id": row["ledger_id"],
        "sample_source": row["sample_source"], "model_prediction": row["model_prediction"],
        "anomaly_score": row["anomaly_score"], "decision_threshold": row["decision_threshold"],
        "model_version": row["model_version"], "eligibility_version": row["eligibility_version"],
        "evaluated_at": row["evaluated_at"], "human_label": row["human_label"],
        "reviewed_by": row["reviewed_by"], "reviewed_at": row["reviewed_at"],
        "review_note": row["review_note"], "created_at": row["created_at"],
    }


def _stratified_random_sample(candidates: list[dict], n: int, rng: random.Random) -> list[dict]:
    """Up to `n` records drawn at random from `candidates`, capped so no
    single ingredient can supply more than
    MAX_INGREDIENT_SHARE_OF_RANDOM_SAMPLE of the result. Without this, a
    routinely-counted ingredient (one physically verified every shift, say)
    would dominate the "normal" validation pool purely by volume — biasing
    Specificity toward how well the model handles THAT ingredient's typical
    variance, not the fleet's. Not stratification in the full statistical
    sense (no attempt to balance by ledger reason, magnitude, or service
    day) — proportionate to a single-branch restaurant's real data volume,
    where even avoiding one obvious dominance failure is the thing worth
    guarding against; see this module's docstring section on duplicate/
    correlated events for why finer stratification was not built."""
    if not candidates or n <= 0:
        return []
    per_ingredient_cap = max(1, int(round(n * MAX_INGREDIENT_SHARE_OF_RANDOM_SAMPLE)))
    shuffled = candidates[:]
    rng.shuffle(shuffled)
    out: list[dict] = []
    per_ingredient_count: dict[int, int] = {}
    leftover: list[dict] = []
    for rec in shuffled:
        ing_id = rec["row"]["ingredient_id"]
        if per_ingredient_count.get(ing_id, 0) < per_ingredient_cap:
            out.append(rec)
            per_ingredient_count[ing_id] = per_ingredient_count.get(ing_id, 0) + 1
        else:
            leftover.append(rec)
        if len(out) >= n:
            break
    # The cap trades completeness for balance; if too few distinct
    # ingredients were available to fill `n` under it, top up from the
    # leftover pool rather than silently under-sampling.
    if len(out) < n:
        out.extend(leftover[: n - len(out)])
    return out[:n]


def sample_for_review(
    conn: sqlite3.Connection,
    n_flagged: int | None = None,
    n_near_threshold: int = DEFAULT_NEAR_THRESHOLD_SAMPLE,
    n_random: int = DEFAULT_RANDOM_SAMPLE,
    lookback_days: int = 90,
    rng: random.Random | None = None,
) -> dict:
    """Select real, non-synthetic stock-loss ledger rows for blind human
    review, writing one anomaly_validation_sample row per newly-selected
    record. Pure selection — never writes to stock_ledger (append-only
    regardless) or to `ingredient`, never infers or stores a label.

    Three pools, matching the review-sampling spec exactly:
      model_flagged     every currently-flagged real row not already
                        sampled, up to n_flagged (default: all of them, up
                        to DEFAULT_FLAGGED_SAMPLE_CAP — a real flagged
                        population small enough to review in full should be,
                        on a single-branch restaurant's volume).
      near_threshold    the n_near_threshold unflagged rows whose score came
                        closest to the live decision boundary — only
                        populated once the forest actually trained
                        (decision_threshold is not None); otherwise
                        genuinely empty, not approximated by the rule.
      random_unflagged  n_random rows chosen uniformly at random from
                        whatever remains, stratified by ingredient (see
                        _stratified_random_sample) and drawn independently
                        of Count Today's risk-based selection — this pool
                        must not be read as representative of "how stock
                        behaves across the whole catalog" if physical counts
                        themselves are concentrated on risk-flagged
                        ingredients; see this module's docstring.

    A ledger row already present in anomaly_validation_sample (any status)
    is never selected again — INSERT OR IGNORE against the table's
    ledger_id UNIQUE constraint is the actual guarantee; the pre-filter here
    only avoids wasted candidate-pool work.
    """
    records, decision_threshold = _evaluate_stock_loss(conn, lookback_days)
    if not records:
        return {
            "sampled": 0, "ledger_ids": [],
            "pools": {"model_flagged": 0, "near_threshold": 0, "random_unflagged": 0},
            "candidates_considered": 0,
            "model_version": ANOMALY_MODEL_VERSION, "eligibility_version": ELIGIBILITY_FILTER_VERSION,
        }

    already_sampled = {
        r["ledger_id"] for r in conn.execute("SELECT ledger_id FROM anomaly_validation_sample").fetchall()
    }

    flagged = [rec for rec in records
              if rec["prediction"] == "flagged" and rec["row"]["id"] not in already_sampled]
    unflagged = [rec for rec in records
                if rec["prediction"] == "unflagged" and rec["row"]["id"] not in already_sampled]

    flagged_cap = n_flagged if n_flagged is not None else min(len(flagged), DEFAULT_FLAGGED_SAMPLE_CAP)
    flagged_pool = flagged[:flagged_cap]

    near_threshold_pool: list[dict] = []
    if decision_threshold is not None:
        scored_unflagged = [rec for rec in unflagged if rec["score"] is not None]
        scored_unflagged.sort(key=lambda rec: -rec["score"])  # closest to the boundary first
        near_threshold_pool = scored_unflagged[:n_near_threshold]

    near_threshold_ids = {rec["row"]["id"] for rec in near_threshold_pool}
    random_candidates = [rec for rec in unflagged if rec["row"]["id"] not in near_threshold_ids]
    random_pool = _stratified_random_sample(random_candidates, n_random, rng or random.Random())

    inserted: list[int] = []
    with write_tx(conn):
        for sample_source, pool in (
            ("model_flagged", flagged_pool),
            ("near_threshold", near_threshold_pool),
            ("random_unflagged", random_pool),
        ):
            for rec in pool:
                r = rec["row"]
                cur = conn.execute(
                    "INSERT OR IGNORE INTO anomaly_validation_sample "
                    "(ledger_id, sample_source, model_prediction, anomaly_score, "
                    " decision_threshold, model_version, eligibility_version) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (r["id"], sample_source, rec["prediction"], rec["score"],
                     decision_threshold, ANOMALY_MODEL_VERSION, ELIGIBILITY_FILTER_VERSION),
                )
                if cur.rowcount:
                    inserted.append(r["id"])

    return {
        "sampled": len(inserted),
        "ledger_ids": inserted,
        "pools": {
            "model_flagged": len(flagged_pool),
            "near_threshold": len(near_threshold_pool),
            "random_unflagged": len(random_pool),
        },
        "candidates_considered": len(records),
        "model_version": ANOMALY_MODEL_VERSION,
        "eligibility_version": ELIGIBILITY_FILTER_VERSION,
    }


def submit_validation_label(
    conn: sqlite3.Connection, sample_id: int, human_label: str,
    note: str | None = None, user_id: str | None = None,
) -> dict:
    """A human's ground-truth verdict on one sampled record, asked as an
    operational question ("was this stock movement expected?") — never
    phrased, here or in the UI that calls this, in terms of the model's own
    prediction. See migration 0041 and HUMAN_LABELS.

    Set exactly once: a sample already labeled is refused rather than
    silently overwritten. A reviewer who wants to correct a mistake does so
    through a separate, visible, deliberate action (not built in this pass)
    rather than an unnoticed second submission quietly changing the
    evaluation record.
    """
    if human_label not in ("confirmed_anomaly", "expected_or_normal"):
        raise InvalidStateError(
            f"human_label must be 'confirmed_anomaly' or 'expected_or_normal', got {human_label!r} "
            "— pending is a state this record starts in, never one a reviewer submits"
        )
    with write_tx(conn):
        row = conn.execute(
            "SELECT * FROM anomaly_validation_sample WHERE id=?", (sample_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"validation sample {sample_id} not found")
        if row["human_label"] != "pending":
            raise InvalidStateError(
                f"sample {sample_id} was already labeled '{row['human_label']}' on "
                f"{row['reviewed_at']} — labeling is one-time per sample"
            )
        conn.execute(
            "UPDATE anomaly_validation_sample SET human_label=?, reviewed_by=?, "
            "reviewed_at=datetime('now'), review_note=? WHERE id=?",
            (human_label, user_id, note, sample_id),
        )
        updated = conn.execute(
            "SELECT * FROM anomaly_validation_sample WHERE id=?", (sample_id,)
        ).fetchone()
    return _validation_sample_row(updated)


def validation_queue(conn: sqlite3.Connection, status: str = "pending", blind: bool = True) -> list[dict]:
    """The human-facing review queue, joined back to the ledger row it
    evaluates for display (ingredient, quantity, reason, before/after stock,
    when).

    blind=True (the default, and the ONLY mode a labeling screen may use)
    omits sample_source/model_prediction/anomaly_score/decision_threshold/
    model_version/eligibility_version/evaluated_at entirely — a reviewer
    judges the operational evidence on its own terms, never knowing which
    pool put the record in front of them or what the model guessed, so nei-
    ther 'this was flagged' nor 'this was a random normal-looking pick'
    can steer the verdict. blind=False exists for a separate technical/audit
    view only (e.g. a model-performance panel an admin opens afterward) —
    never for the labeling interface itself.
    """
    rows = conn.execute(
        """
        SELECT s.*, l.ingredient_id, i.name AS ingredient_name, i.base_unit,
               l.delta_base, l.balance_after_base, l.reason, l.wastage_reason,
               l.note AS ledger_note, l.created_at AS ledger_created_at
          FROM anomaly_validation_sample s
          JOIN stock_ledger l ON l.id = s.ledger_id
          JOIN ingredient i ON i.id = l.ingredient_id
         WHERE s.human_label = ?
         ORDER BY s.evaluated_at
        """,
        (status,),
    ).fetchall()
    out = []
    for r in rows:
        balance_before = r["balance_after_base"] - r["delta_base"]
        item = {
            "sample_id": r["id"], "ledger_id": r["ledger_id"],
            "ingredient_id": r["ingredient_id"], "ingredient_name": r["ingredient_name"],
            "base_unit": r["base_unit"], "reason": r["reason"], "wastage_reason": r["wastage_reason"],
            "delta_base": round(r["delta_base"], 3),
            "balance_before_base": round(balance_before, 3),
            "balance_after_base": round(r["balance_after_base"], 3),
            "note": r["ledger_note"], "event_at": r["ledger_created_at"],
            "human_label": r["human_label"],
        }
        if not blind:
            item.update({
                "sample_source": r["sample_source"], "model_prediction": r["model_prediction"],
                "anomaly_score": r["anomaly_score"], "decision_threshold": r["decision_threshold"],
                "model_version": r["model_version"], "eligibility_version": r["eligibility_version"],
                "evaluated_at": r["evaluated_at"],
            })
        out.append(item)
    return out


# Minimum reviewed evidence before scientific_validation_metrics() reports a
# number instead of insufficient_validation_data — see that function. Same
# order of magnitude as validation_metrics()'s own MIN_VALIDATED_LABELS, for
# the same reason: small enough to be reachable by a single-branch
# restaurant's real review workload, large enough that one or two lucky or
# unlucky labels cannot swing a reported metric wildly.
CONSUMPTION_MODEL_VERSION = "unli_consumption_isoforest_v1"
CONSUMPTION_ELIGIBILITY_VERSION = "recorded_by_app_user_membership_v1"
CONSUMPTION_HUMAN_LABELS = ("pending", "confirmed_anomaly", "expected_or_explained")
CONSUMPTION_DEFAULT_FLAGGED_SAMPLE_CAP = 15
CONSUMPTION_DEFAULT_NEAR_THRESHOLD_SAMPLE = 5
CONSUMPTION_DEFAULT_RANDOM_SAMPLE = 5
# Same reasoning as MAX_INGREDIENT_SHARE_OF_RANDOM_SAMPLE, applied to the
# consumption pass's own unit of correlation: a stream served every single
# day (e.g. the one included-flavour stream almost every table picks) would
# otherwise dominate the "normal" pool purely by how often it gets
# reconciled, not because its variance is actually representative of every
# stream.
MAX_STREAM_SHARE_OF_RANDOM_SAMPLE = 0.5
# Reusing the stock-loss pass's own reasoning (see MIN_SCIENTIFIC_VALIDATION_
# LABELS/MIN_LABELS_PER_PREDICTION_SIDE) at the same magnitude — a
# single-branch restaurant's real review workload is the same order for
# either pass, and there is no principled reason this pass should need a
# materially different amount of evidence. Kept as the consumption pass's
# own named constants rather than literally importing the stock-loss ones,
# so either can be retuned independently without silently moving the other.
CONSUMPTION_MIN_SCIENTIFIC_VALIDATION_LABELS = 20
CONSUMPTION_MIN_LABELS_PER_PREDICTION_SIDE = 5

MIN_SCIENTIFIC_VALIDATION_LABELS = 20
# Independent of the total: guards against e.g. 19 confirmed_anomaly labels
# and 1 expected_or_normal label reporting a deceptively perfect Precision —
# there must be real evidence on BOTH sides of the model's own prediction
# before Recall (needs unflagged-side evidence) or Precision/Specificity
# (need flagged-side evidence, respectively) can be trusted at all.
MIN_LABELS_PER_PREDICTION_SIDE = 5


def scientific_validation_metrics(
    conn: sqlite3.Connection, model_version: str | None = None, eligibility_version: str | None = None,
) -> dict:
    """TP/FP/FN/TN and the metrics derived from them, computed ONLY from
    anomaly_validation_sample rows a human has actually labeled, scoped to
    ONE model_version/eligibility_version pair (defaulting to the current
    live one) so reviews collected under a materially different
    configuration are never silently pooled into this configuration's
    confusion matrix — see migration 0041's docstring and this module's
    ANOMALY_MODEL_VERSION/ELIGIBILITY_FILTER_VERSION.

        TP = model_prediction='flagged'   AND human_label='confirmed_anomaly'
        FP = model_prediction='flagged'   AND human_label='expected_or_normal'
        FN = model_prediction='unflagged' AND human_label='confirmed_anomaly'
        TN = model_prediction='unflagged' AND human_label='expected_or_normal'

    Below MIN_SCIENTIFIC_VALIDATION_LABELS total labels, or below
    MIN_LABELS_PER_PREDICTION_SIDE on either side of the model's own
    prediction, every metric is reported unavailable — never computed from
    an incomplete or lopsided confusion matrix and presented as if it were
    a real number.

    These are SAMPLE-BASED metrics, not a population census: near_threshold
    and random_unflagged rows were deliberately sampled rather than every
    unflagged row being reviewed, so a reported Recall/Specificity describes
    the reviewed cohort, not a weighted estimate of the full ledger
    population — see `methodology` in the returned dict, which the caller
    must surface rather than quoting a bare percentage.
    """
    model_version = model_version or ANOMALY_MODEL_VERSION
    eligibility_version = eligibility_version or ELIGIBILITY_FILTER_VERSION

    rows = conn.execute(
        "SELECT * FROM anomaly_validation_sample "
        "WHERE model_version=? AND eligibility_version=? AND human_label != 'pending'",
        (model_version, eligibility_version),
    ).fetchall()

    tp = fp = fn = tn = 0
    by_source: dict[str, int] = {"model_flagged": 0, "near_threshold": 0, "random_unflagged": 0}
    for r in rows:
        by_source[r["sample_source"]] = by_source.get(r["sample_source"], 0) + 1
        is_flagged = r["model_prediction"] == "flagged"
        if r["human_label"] == "confirmed_anomaly":
            tp += is_flagged
            fn += (not is_flagged)
        elif r["human_label"] == "expected_or_normal":
            fp += is_flagged
            tn += (not is_flagged)

    total = tp + fp + fn + tn
    flagged_side, unflagged_side = tp + fp, fn + tn
    base = {
        "model_version": model_version, "eligibility_version": eligibility_version,
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "labels_by_sample_source": by_source,
    }
    if (total < MIN_SCIENTIFIC_VALIDATION_LABELS
            or flagged_side < MIN_LABELS_PER_PREDICTION_SIDE
            or unflagged_side < MIN_LABELS_PER_PREDICTION_SIDE):
        return {
            **base,
            "available": False,
            "reason": "insufficient_validation_data",
            "detail": (
                f"{total} labeled sample(s) under this model/eligibility version "
                f"({flagged_side} on the flagged side, {unflagged_side} on the unflagged side). "
                f"Needs >= {MIN_SCIENTIFIC_VALIDATION_LABELS} total and >= "
                f"{MIN_LABELS_PER_PREDICTION_SIDE} on each side of the model's own prediction "
                "before Precision/Recall/Specificity can be reported rather than guessed at."
            ),
        }

    precision = (tp / (tp + fp)) if (tp + fp) > 0 else None
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else None
    specificity = (tn / (tn + fp)) if (tn + fp) > 0 else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall and (precision + recall) > 0 else None)
    fpr = (fp / (fp + tn)) if (fp + tn) > 0 else None
    npv = (tn / (tn + fn)) if (tn + fn) > 0 else None
    accuracy = ((tp + tn) / total) if total > 0 else None

    return {
        **base,
        "available": True,
        "methodology": (
            "Sample-based validation metrics, not a population census: the flagged "
            "population is reviewed up to its cap, but near-threshold and random-"
            "unflagged rows were SAMPLED rather than exhaustively reviewed. These "
            "figures describe the reviewed cohort under this model/eligibility "
            "version and must not be quoted as population-wide accuracy."
        ),
        "confirmed_labels": total,
        "precision": round(precision, 3) if precision is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "specificity": round(specificity, 3) if specificity is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
        "false_positive_rate": round(fpr, 3) if fpr is not None else None,
        "negative_predictive_value": round(npv, 3) if npv is not None else None,
        "accuracy": round(accuracy, 3) if accuracy is not None else None,
    }


# ===========================================================================
# Review-sampling infrastructure for the CONSUMPTION pass (migration 0043).
# Mirrors the stock-loss pass's architecture (sample_for_review/
# submit_validation_label/validation_queue/scientific_validation_metrics)
# over the consumption pass's own observation unit (one reconciled service-
# date x stream day, keyed by reconciliation_id) — kept as a parallel,
# independent mechanism rather than a shared/generic one, per the 2026-10
# audit's own instruction not to reuse stock-loss labels/semantics for a
# conceptually different question. See CONSUMPTION_HUMAN_LABELS for why the
# human label wording differs ('expected_or_explained', not
# 'expected_or_normal') -- the operational question a reviewer answers here
# is different in kind from the stock-loss one (see _consumption_validation_
# row's docstring and the API layer's question text).
# ===========================================================================
def _consumption_validation_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "reconciliation_id": row["reconciliation_id"],
        "sample_source": row["sample_source"], "model_prediction": row["model_prediction"],
        "rule_flagged": bool(row["rule_flagged"]),
        "anomaly_score": row["anomaly_score"], "decision_threshold": row["decision_threshold"],
        "model_version": row["model_version"], "eligibility_version": row["eligibility_version"],
        "evaluated_at": row["evaluated_at"], "human_label": row["human_label"],
        "reviewed_by": row["reviewed_by"], "reviewed_at": row["reviewed_at"],
        "review_note": row["review_note"], "created_at": row["created_at"],
    }


def _stratified_random_sample_by(candidates: list[dict], n: int, rng: random.Random,
                                 key) -> list[dict]:
    """Generalized form of _stratified_random_sample() — same algorithm, over
    whatever correlation key the caller's observation unit actually has
    (ingredient for stock-loss, unli_item_id/stream for consumption)."""
    if not candidates or n <= 0:
        return []
    per_key_cap = max(1, int(round(n * MAX_STREAM_SHARE_OF_RANDOM_SAMPLE)))
    shuffled = candidates[:]
    rng.shuffle(shuffled)
    out: list[dict] = []
    per_key_count: dict = {}
    leftover: list[dict] = []
    for rec in shuffled:
        k = key(rec)
        if per_key_count.get(k, 0) < per_key_cap:
            out.append(rec)
            per_key_count[k] = per_key_count.get(k, 0) + 1
        else:
            leftover.append(rec)
        if len(out) >= n:
            break
    if len(out) < n:
        out.extend(leftover[: n - len(out)])
    return out[:n]


def sample_consumption_for_review(
    conn: sqlite3.Connection,
    n_flagged: int | None = None,
    n_near_threshold: int = CONSUMPTION_DEFAULT_NEAR_THRESHOLD_SAMPLE,
    n_random: int = CONSUMPTION_DEFAULT_RANDOM_SAMPLE,
    lookback_days: int = 90,
    rng: random.Random | None = None,
) -> dict:
    """Select genuine, non-synthetic reconciled days for blind human review.

    2026-10 audit, section 12 (rule vs. ML separation): sampling is only
    attempted when the IsolationForest actually trained this evaluation
    (_evaluate_consumption's decision_threshold is not None, i.e. at least
    MIN_RECONCILED_DAYS genuine reconciled days exist). Below that, there is
    no Isolation-Forest prediction for ANY record to sample as
    'model_flagged' — a day caught only by the deterministic large_variance
    rule is a fixed-threshold business rule firing, not a statistical model
    being unusual, and sampling it into an "Isolation Forest validation"
    queue would misattribute the rule's own correctness to the ML model.
    This function returns immediately, with `forest_trained: False` and
    nothing sampled, rather than quietly substituting the rule's catches.

    Three pools, same shape as the stock-loss sampler:
      model_flagged     real, IsolationForest-flagged days (iforest_
                        prediction == 'flagged'), never rule-only catches.
      near_threshold    real, IForest-unflagged days whose score came
                        closest to the live decision boundary.
      random_unflagged  real, IForest-unflagged days chosen at random,
                        stratified by stream (no single stream may supply
                        more than MAX_STREAM_SHARE_OF_RANDOM_SAMPLE of it).

    A reconciliation_id already present in unli_consumption_validation_
    sample (any status) is never selected again (table UNIQUE constraint;
    the pre-filter here only avoids wasted candidate-pool work).
    """
    records, decision_threshold = _evaluate_consumption(conn, lookback_days)
    if decision_threshold is None:
        return {
            "sampled": 0, "reconciliation_ids": [],
            "pools": {"model_flagged": 0, "near_threshold": 0, "random_unflagged": 0},
            "candidates_considered": len(records),
            "forest_trained": False,
            "reason": (
                "insufficient_genuine_reconciled_days" if records else
                "no_genuine_reconciled_days"
            ),
            "model_version": CONSUMPTION_MODEL_VERSION,
            "eligibility_version": CONSUMPTION_ELIGIBILITY_VERSION,
        }

    already_sampled = {
        r["reconciliation_id"] for r in
        conn.execute("SELECT reconciliation_id FROM unli_consumption_validation_sample").fetchall()
    }

    flagged = [rec for rec in records
              if rec["iforest_prediction"] == "flagged"
              and rec["row"]["reconciliation_id"] not in already_sampled]
    unflagged = [rec for rec in records
                if rec["iforest_prediction"] == "unflagged"
                and rec["row"]["reconciliation_id"] not in already_sampled]

    flagged_cap = n_flagged if n_flagged is not None else min(len(flagged), CONSUMPTION_DEFAULT_FLAGGED_SAMPLE_CAP)
    flagged_pool = flagged[:flagged_cap]

    scored_unflagged = sorted(unflagged, key=lambda rec: -rec["score"])
    near_threshold_pool = scored_unflagged[:n_near_threshold]

    near_threshold_ids = {rec["row"]["reconciliation_id"] for rec in near_threshold_pool}
    random_candidates = [rec for rec in unflagged
                         if rec["row"]["reconciliation_id"] not in near_threshold_ids]
    random_pool = _stratified_random_sample_by(
        random_candidates, n_random, rng or random.Random(),
        key=lambda rec: rec["row"]["unli_item_id"])

    inserted: list[int] = []
    with write_tx(conn):
        for sample_source, pool in (
            ("model_flagged", flagged_pool),
            ("near_threshold", near_threshold_pool),
            ("random_unflagged", random_pool),
        ):
            for rec in pool:
                r = rec["row"]
                cur = conn.execute(
                    "INSERT OR IGNORE INTO unli_consumption_validation_sample "
                    "(reconciliation_id, sample_source, model_prediction, rule_flagged, "
                    " anomaly_score, decision_threshold, model_version, eligibility_version) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (r["reconciliation_id"], sample_source, rec["iforest_prediction"],
                     1 if rec["rule_flagged"] else 0, rec["score"], decision_threshold,
                     CONSUMPTION_MODEL_VERSION, CONSUMPTION_ELIGIBILITY_VERSION),
                )
                if cur.rowcount:
                    inserted.append(r["reconciliation_id"])

    return {
        "sampled": len(inserted),
        "reconciliation_ids": inserted,
        "pools": {
            "model_flagged": len(flagged_pool),
            "near_threshold": len(near_threshold_pool),
            "random_unflagged": len(random_pool),
        },
        "candidates_considered": len(records),
        "forest_trained": True,
        "model_version": CONSUMPTION_MODEL_VERSION,
        "eligibility_version": CONSUMPTION_ELIGIBILITY_VERSION,
    }


def submit_consumption_validation_label(
    conn: sqlite3.Connection, sample_id: int, human_label: str,
    note: str | None = None, user_id: str | None = None,
) -> dict:
    """A human's one-time ground-truth verdict on one sampled reconciled day.

    Asked as an operational question independent of the model — something
    like "was this consumption unusually high or low for a real operational
    reason that should be investigated?" — never "was Isolation Forest
    correct" or "is this a false positive". See CONSUMPTION_HUMAN_LABELS.
    Set exactly once, same convention as the stock-loss pass's
    submit_validation_label()."""
    if human_label not in ("confirmed_anomaly", "expected_or_explained"):
        raise InvalidStateError(
            f"human_label must be 'confirmed_anomaly' or 'expected_or_explained', got "
            f"{human_label!r} — pending is a state this record starts in, never one a "
            "reviewer submits"
        )
    with write_tx(conn):
        row = conn.execute(
            "SELECT * FROM unli_consumption_validation_sample WHERE id=?", (sample_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"consumption validation sample {sample_id} not found")
        if row["human_label"] != "pending":
            raise InvalidStateError(
                f"sample {sample_id} was already labeled '{row['human_label']}' on "
                f"{row['reviewed_at']} — labeling is one-time per sample"
            )
        conn.execute(
            "UPDATE unli_consumption_validation_sample SET human_label=?, reviewed_by=?, "
            "reviewed_at=datetime('now'), review_note=? WHERE id=?",
            (human_label, user_id, note, sample_id),
        )
        updated = conn.execute(
            "SELECT * FROM unli_consumption_validation_sample WHERE id=?", (sample_id,)
        ).fetchone()
    return _consumption_validation_row(updated)


def consumption_validation_queue(conn: sqlite3.Connection, status: str = "pending",
                                 blind: bool = True) -> list[dict]:
    """The human-facing review queue for the consumption pass. blind=True
    (the only mode the labeling screen may use) omits sample_source/
    model_prediction/rule_flagged/anomaly_score/decision_threshold/model_
    version/eligibility_version/evaluated_at entirely. See wt/anomaly.py's
    validation_queue() (stock-loss) for the identical blinding rationale."""
    rows = conn.execute(
        """
        SELECT s.*, d.service_date, d.item_key, d.label, d.ingredient_name, d.base_unit,
               d.heads, d.theoretical_base, d.actual_base, d.variance_base, d.variance_ratio,
               d.actual_per_head, d.created_at AS event_created_at
          FROM unli_consumption_validation_sample s
          JOIN v_unli_daily_consumption d ON d.reconciliation_id = s.reconciliation_id
         WHERE s.human_label = ?
         ORDER BY s.evaluated_at
        """,
        (status,),
    ).fetchall()
    out = []
    for r in rows:
        item = {
            "sample_id": r["id"], "reconciliation_id": r["reconciliation_id"],
            "service_date": r["service_date"], "item_key": r["item_key"], "label": r["label"],
            "ingredient_name": r["ingredient_name"], "base_unit": r["base_unit"],
            "heads": r["heads"],
            "theoretical_base": round(r["theoretical_base"], 2),
            "actual_base": round(r["actual_base"], 2),
            "actual_per_head": round(r["actual_per_head"], 2) if r["actual_per_head"] is not None else None,
            "variance_base": round(r["variance_base"], 2),
            "variance_pct": round((r["variance_ratio"] or 0.0) * 100, 1),
            "human_label": r["human_label"],
        }
        if not blind:
            item.update({
                "sample_source": r["sample_source"], "model_prediction": r["model_prediction"],
                "rule_flagged": bool(r["rule_flagged"]),
                "anomaly_score": r["anomaly_score"], "decision_threshold": r["decision_threshold"],
                "model_version": r["model_version"], "eligibility_version": r["eligibility_version"],
                "evaluated_at": r["evaluated_at"],
            })
        out.append(item)
    return out


def scientific_consumption_validation_metrics(
    conn: sqlite3.Connection, model_version: str | None = None, eligibility_version: str | None = None,
) -> dict:
    """TP/FP/FN/TN and derived metrics for the consumption pass, computed
    ONLY from unli_consumption_validation_sample rows a human has labeled,
    scoped to one (model_version, eligibility_version) pair. Identical
    structure and identical minimum-evidence reasoning to the stock-loss
    pass's scientific_validation_metrics() — see that function's own
    docstring for the full rationale, not repeated here.

        TP = model_prediction='flagged'   AND human_label='confirmed_anomaly'
        FP = model_prediction='flagged'   AND human_label='expected_or_explained'
        FN = model_prediction='unflagged' AND human_label='confirmed_anomaly'
        TN = model_prediction='unflagged' AND human_label='expected_or_explained'

    model_prediction here is always the IsolationForest's own verdict
    (sample_consumption_for_review() never samples a rule-only catch as
    'model_flagged' — see that function's docstring) — so these cells can
    never attribute the deterministic large_variance rule's performance to
    Isolation Forest, even though rule_flagged is preserved on every row for
    audit.
    """
    model_version = model_version or CONSUMPTION_MODEL_VERSION
    eligibility_version = eligibility_version or CONSUMPTION_ELIGIBILITY_VERSION

    rows = conn.execute(
        "SELECT * FROM unli_consumption_validation_sample "
        "WHERE model_version=? AND eligibility_version=? AND human_label != 'pending'",
        (model_version, eligibility_version),
    ).fetchall()

    tp = fp = fn = tn = 0
    by_source: dict[str, int] = {"model_flagged": 0, "near_threshold": 0, "random_unflagged": 0}
    for r in rows:
        by_source[r["sample_source"]] = by_source.get(r["sample_source"], 0) + 1
        is_flagged = r["model_prediction"] == "flagged"
        if r["human_label"] == "confirmed_anomaly":
            tp += is_flagged
            fn += (not is_flagged)
        elif r["human_label"] == "expected_or_explained":
            fp += is_flagged
            tn += (not is_flagged)

    total = tp + fp + fn + tn
    flagged_side, unflagged_side = tp + fp, fn + tn
    base = {
        "model_version": model_version, "eligibility_version": eligibility_version,
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "labels_by_sample_source": by_source,
    }
    if (total < CONSUMPTION_MIN_SCIENTIFIC_VALIDATION_LABELS
            or flagged_side < CONSUMPTION_MIN_LABELS_PER_PREDICTION_SIDE
            or unflagged_side < CONSUMPTION_MIN_LABELS_PER_PREDICTION_SIDE):
        return {
            **base,
            "available": False,
            "reason": "insufficient_validation_data",
            "detail": (
                f"{total} labeled sample(s) under this model/eligibility version "
                f"({flagged_side} on the flagged side, {unflagged_side} on the unflagged side). "
                f"Needs >= {CONSUMPTION_MIN_SCIENTIFIC_VALIDATION_LABELS} total and >= "
                f"{CONSUMPTION_MIN_LABELS_PER_PREDICTION_SIDE} on each side of the model's own "
                "prediction before Precision/Recall/Specificity can be reported rather than "
                "guessed at."
            ),
        }

    precision = (tp / (tp + fp)) if (tp + fp) > 0 else None
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else None
    specificity = (tn / (tn + fp)) if (tn + fp) > 0 else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall and (precision + recall) > 0 else None)
    fpr = (fp / (fp + tn)) if (fp + tn) > 0 else None
    npv = (tn / (tn + fn)) if (tn + fn) > 0 else None
    accuracy = ((tp + tn) / total) if total > 0 else None

    return {
        **base,
        "available": True,
        "methodology": (
            "Sample-based validation metrics, not a population census: the flagged "
            "population is reviewed up to its cap, but near-threshold and random-"
            "unflagged days were SAMPLED rather than exhaustively reviewed. These "
            "figures describe the reviewed cohort under this model/eligibility "
            "version and must not be quoted as population-wide accuracy."
        ),
        "confirmed_labels": total,
        "precision": round(precision, 3) if precision is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "specificity": round(specificity, 3) if specificity is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
        "false_positive_rate": round(fpr, 3) if fpr is not None else None,
        "negative_predictive_value": round(npv, 3) if npv is not None else None,
        "accuracy": round(accuracy, 3) if accuracy is not None else None,
    }
