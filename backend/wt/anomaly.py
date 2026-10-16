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
import statistics

import numpy as np
from sklearn.ensemble import IsolationForest

from . import audit, model_registry, timeutil
from . import ml_artifacts_io as artifacts
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

# --- 2026-10 capability-gap fix: leftover-only / scale-only days -----------
#
# Forensic diagnosis (numbers from a fixed-seed severity-graded simulation,
# see the project's ML validation report): leftover_ratio for an injected
# "excessive leftover" day sits 4-25 STANDARD DEVIATIONS from the normal
# population's own leftover_ratio distribution — an enormous, unambiguous
# signal — yet the IsolationForest flagged only 1-2 of 24 such days (and
# the large_variance rule, which only ever looks at variance_ratio, flagged
# zero of them by construction). The cause is not a weak signal; it is
# where that signal lives. IsolationForest isolates a point by repeatedly
# splitting on a RANDOMLY CHOSEN feature among all seven; a point extreme in
# exactly one dimension while ordinary in the other six needs many more
# splits, on average, to isolate than a point extreme across several
# CORRELATED dimensions at once (variance_ratio/baseline_deviation_ratio/
# peer_rate_ratio move together by construction — three of the seven
# features are really one signal). With contamination='auto' picking its
# cutoff from the pooled score distribution, single-axis outliers routinely
# lose the competition for a flagged slot to multi-axis ones, no matter how
# extreme that one axis is. This is a well-documented, textbook property of
# tree-ensemble isolation methods, not a bug in this implementation and not
# something feature reweighting fixes honestly (reweighting toward whichever
# axis the CURRENT test population happens to stress is exactly the
# "manipulate the validation data until F1 improves" this fix must not do).
#
# leftover_ratio and consumption_scale_ratio are also not naturally
# IsolationForest's kind of problem in the first place: each has a direct,
# single-number business interpretation an owner can be told in one
# sentence ("more came back than usual" / "today's volume was unusual for
# this many covers"), and each already behaves as a tight, well-calibrated
# distribution when the shop is operating normally (see the diagnostic:
# leftover_ratio's normal-population stdev is a small fraction of its
# normal-population mean). That is exactly the shape a robust statistical
# threshold handles better than an ensemble method tuned for
# higher-dimensional, less-interpretable structure — restaurant-operational
# reasoning, not just a metrics argument (see Part 2 of that report).
#
# So: two new deterministic rules, matching RULE_VARIANCE_MULTIPLE's own
# shape exactly (a configurable multiple of a robust spread, never a raw
# global constant), computed PER STREAM against THIS WINDOW's own
# population — self-calibrating per shop, same philosophy peer_rate_ratio/
# heads/consumption_scale_ratio already use. Median + scaled-MAD (1.4826x,
# the standard normal-equivalent scaling) rather than mean/stdev: a median/
# MAD-based center and spread is not dragged around by the handful of
# anomalies it is trying to detect the way a mean/stdev would be.
RULE_ROBUST_MAD_MULTIPLE = 3.0   # a standard "3 robust-sigma" outlier threshold
_MAD_TO_SIGMA = 1.4826

# Minimum reconciled days for THIS stream before a robust per-stream
# median/MAD is trusted at all. Deliberately lower than MIN_RECONCILED_DAYS
# (12): a median/MAD needs far fewer points to be a stable, meaningful
# center/spread than a 7-dimensional IsolationForest fit does, and this
# rule's cost of being wrong (fires on an under-evidenced stream) is bounded
# by the same MAD_MULTIPLE discipline, whereas the forest's cost would be
# fitting noise. Not 12 to avoid artificially matching an unrelated
# constant; not lower than 6 because 2-3 points cannot give a non-degenerate
# median split either side.
MIN_ROBUST_RULE_DAYS = 6

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

# The one, current synthetic importer this registry's SIMULATED/MIXED modes
# train on (migration 0050). Deliberately NOT the same set as
# SYNTHETIC_SEED_USER_IDS above -- those are retired seeders with no batch
# tracking; "simulated" for mode purposes means "the currently-approved
# simulated_workbook_import batch", never an arbitrary past synthetic write.
CURRENT_SYNTHETIC_IMPORTER_USER_ID = "simulated_workbook_import"

# schema_version embedded in every artifact this module writes/reads (see
# ml_training.py's train_*() functions and ml_startup.py's compatibility
# check) -- bump whenever a feature list below changes shape, so a stale
# artifact built under the old shape is rejected at startup instead of
# silently fed the wrong number of columns.
ANOMALY_ARTIFACT_SCHEMA_VERSION = "anomaly_isoforest_v1"


def _provenance_branches(conn: sqlite3.Connection, user_col: str, batch_col: str, mode: str) -> tuple[str, str, tuple]:
    """Returns (real_predicate, synthetic_predicate, synthetic_params) —
    the two eligibility tests kept SEPARATE (not OR'd together) for queries
    that UNION two differently-shaped sources (e.g. Pass B's real
    unli_reconciliation rows vs. its derived-from-flavor synthetic rows)
    rather than filtering one shared table. REAL keeps today's exact
    behavior (the OR-NULL/app_user test, unchanged bit for bit); SIMULATED
    is rows from the current synthetic importer whose batch is in
    ml_approved_synthetic_batch (never a stale generation a future
    re-import might leave behind); MIXED returns both predicates "live" so
    a caller ORs them -- never an unfiltered 'all rows'."""
    real = f"({user_col} IS NULL OR {user_col} IN (SELECT username FROM app_user))"
    approved = model_registry.approved_batch_ids(conn)
    if approved:
        placeholders = ",".join("?" * len(approved))
        simulated = f"({user_col} = ? AND {batch_col} IN ({placeholders}))"
        sim_params = (CURRENT_SYNTHETIC_IMPORTER_USER_ID, *approved)
    else:
        simulated, sim_params = "0", ()
    if mode == "REAL":
        return real, "0", ()
    if mode == "SIMULATED":
        return "0", simulated, sim_params
    if mode == "MIXED":
        return real, simulated, sim_params
    raise ValueError(f"unknown mode {mode!r}")


def _provenance_clause(conn: sqlite3.Connection, user_col: str, batch_col: str, mode: str) -> tuple[str, tuple]:
    """Single combined predicate for queries that filter one shared table
    (Pass A, Pass C) -- see _provenance_branches for the per-branch form
    Pass B's UNION needs. Returns (sql_fragment, extra_params): extra_params
    must be spliced into the query's param tuple at the fragment's
    position."""
    real, simulated, sim_params = _provenance_branches(conn, user_col, batch_col, mode)
    if mode == "REAL":
        return real, ()
    if mode == "SIMULATED":
        return simulated, sim_params
    return f"({real} OR {simulated})", sim_params


def _row_provenance(user_id: str | None) -> str:
    return "synthetic" if user_id == CURRENT_SYNTHETIC_IMPORTER_USER_ID else "real"


def _score_with_isolation_forest(
    conn: sqlite3.Connection, model_key: str, X: "np.ndarray", *, use_artifact: bool,
) -> tuple["np.ndarray | None", "np.ndarray | None", str]:
    """Returns (preds, raw_scores, source).

    source is 'artifact' when a trained, persisted model served the
    prediction (the normal live path); 'fit_fresh' when use_artifact is
    False (ml_training.py building a candidate) or no artifact exists yet
    AND the fresh-fit fallback is allowed in this environment (see
    _fresh_fit_fallback_allowed) -- outside production this still produces
    predictions rather than refusing to serve anything.

    In production with no valid artifact, returns (None, None,
    'unavailable') instead of fitting fresh: the caller's own rule-based
    layer (large_fraction / variance-MAD) keeps answering, and the ML
    verdict is reported absent rather than fabricated."""
    if use_artifact:
        path = model_registry.active_artifact_path(conn, model_key)
        if path:
            try:
                payload = artifacts.load_joblib(path)
                model = payload["model"]
                return model.predict(X), model.decision_function(X), "artifact"
            except Exception:
                # Missing/corrupt artifact file, or a pickle that no longer
                # loads under the running sklearn version -- never let a
                # bad artifact take the whole detection pass down. Falls
                # through to the fresh-fit-or-unavailable decision below,
                # same as "no artifact yet". See ml_startup.py for the
                # equivalent safety net at process startup (which
                # additionally rolls back the registry pointer so this
                # fallback stops being needed on the next call).
                pass
        if not artifacts.fresh_fit_fallback_allowed():
            return None, None, "unavailable"
    model = IsolationForest(n_estimators=N_ESTIMATORS, contamination="auto", random_state=RANDOM_STATE)
    preds = model.fit_predict(X)
    raw_scores = model.decision_function(X)
    return preds, raw_scores, "fit_fresh"

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


def _evaluate_stock_loss(
    conn: sqlite3.Connection, lookback_days: int = 90, *, mode: str | None = None, use_artifact: bool = True,
) -> tuple[list[dict], float | None]:
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
    mode = mode or model_registry.get_mode(conn, "stock_loss_isoforest")
    prov_clause, prov_params = _provenance_clause(conn, "l.user_id", "l.source_batch_id", mode)
    placeholders = ",".join("?" * len(ANOMALY_REASONS))
    rows = conn.execute(
        f"""
        SELECT l.id, l.ingredient_id, i.name AS ingredient_name, i.base_unit,
               l.delta_base, l.balance_after_base, l.reason, l.wastage_reason,
               l.note, l.user_id, l.created_at
          FROM stock_ledger l JOIN ingredient i ON i.id = l.ingredient_id
         WHERE l.reason IN ({placeholders})
           AND l.created_at >= ?
           AND {prov_clause}
         ORDER BY l.ingredient_id, l.created_at
        """,
        (*ANOMALY_REASONS, timeutil.utc_bound_n_manila_days_ago(lookback_days), *prov_params),
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
            "provenance": _row_provenance(r["user_id"]),
        })

    decision_threshold = None

    if len(records) >= MIN_POOLED_SAMPLES:
        X = np.array([[rec["relative_mag"], rec["fraction"]] for rec in records])
        preds, raw_scores, model_source = _score_with_isolation_forest(
            conn, "stock_loss_isoforest", X, use_artifact=use_artifact)
        for rec in records:
            rec["_model_source"] = model_source
        if model_source != "unavailable":
            for rec, raw in zip(records, raw_scores):
                rec["_raw_score"] = float(raw)
                rec["score"] = round(-rec["_raw_score"], 3)   # public sign: higher = more anomalous
            # Cap to the worst-scoring MAX_STOCK_LOSS_FLAG_FRACTION of the
            # pool — see that constant's comment for why 'auto' alone
            # over-flags this data. Sorted first so a cap that bites keeps
            # the genuinely worst entries, not an arbitrary slice of an
            # over-broad verdict.
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
        # model_source == 'unavailable': no artifact, production forbids a
        # fresh in-request fit. The rule-based pass below still runs on
        # every record -- it never depended on the ML layer having run.

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


def detect_anomalies(conn: sqlite3.Connection, lookback_days: int = 90, *, mode: str | None = None) -> list[dict]:
    """Live predictions. mode defaults to this model's current registry
    mode (migration 0050) -- SIMULATED/MIXED means the currently-approved
    synthetic import batch's rows are included here, not just in
    wt/demo_validation.py's isolated harness (see _provenance_clause).
    Serves from the persisted, scheduler-trained artifact when one exists
    (use_artifact=True, the default inside _evaluate_stock_loss) rather than
    refitting inline -- see _score_with_isolation_forest."""
    records, _ = _evaluate_stock_loss(conn, lookback_days, mode=mode)
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
            "provenance": rec["provenance"], "model_source": rec.get("_model_source"),
        })
    flagged.sort(key=lambda f: f["created_at"], reverse=True)
    return flagged


# ===========================================================================
# Consumption anomalies — the specified model, over reconciled service days
# ===========================================================================
def _consumption_records(conn: sqlite3.Connection, lookback_days: int, *, mode: str | None = None) -> list[dict]:
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
    mode = mode or model_registry.get_mode(conn, "unli_consumption_isoforest")
    real_clause, synthetic_clause, sim_params = _provenance_branches(
        conn, "recorded_by", "source_batch_id", mode)
    lookback_param = timeutil.business_date_n_days_ago(lookback_days)
    tol_row = conn.execute("SELECT value FROM app_setting WHERE key='unli_variance_tolerance'").fetchone()
    tol = float(tol_row["value"]) if tol_row else 0.10
    # Real rows come from unli_reconciliation (any stream) exactly as
    # before, with every column detect_consumption_anomalies()'s describe()
    # reads. Synthetic rows come from v_unli_wings_daily_from_flavor
    # (migration 0050/0051) -- a day-level aggregate of the imported
    # per-flavor reconciliation data, 'wings' stream only -- shaped to match
    # the same columns (status/validation_state computed the same way
    # record_reconciliation() would have, from the same configured
    # tolerance; reconciliation_id left NULL since there is no real
    # unli_reconciliation row to point at -- sample_consumption_for_review()
    # excludes NULL-id rows from the human-review queue for exactly that
    # reason, see its own filter). ANTI-JOINED against real
    # unli_reconciliation by (unli_item_id, service_date) so a date that
    # already has a genuine reconciliation row is never also pulled in from
    # the derived view: one underlying fact per day, never counted from
    # both places. rice/fries/drinks have no synthetic source and only ever
    # appear via real_rows.
    rows = conn.execute(
        f"""
        WITH real_rows AS (
            SELECT reconciliation_id, service_date, unli_item_id, item_key, label,
                   ingredient_name, base_unit, heads, theoretical_base, actual_base,
                   variance_base, actual_per_head, variance_ratio, leftover_ratio, status,
                   validation_state, recorded_by, NULL AS source_batch_id
              FROM v_unli_daily_consumption
             WHERE service_date >= ?
               AND heads > 0
               AND validation_state <> 'rejected'
               AND {real_clause}
        ),
        synthetic_rows AS (
            SELECT NULL AS reconciliation_id, f.service_date, f.unli_item_id,
                   ui.item_key, ui.label, i.name AS ingredient_name, i.base_unit,
                   f.heads, f.theoretical_base, f.actual_base, f.variance_base,
                   f.actual_per_head, f.variance_ratio, f.leftover_ratio,
                   CASE WHEN ABS(COALESCE(f.variance_ratio, 0)) <= ? THEN 'within_tolerance'
                        WHEN f.variance_ratio > 0 THEN 'over_tolerance'
                        ELSE 'under_tolerance' END AS status,
                   'validated' AS validation_state,
                   f.recorded_by, f.source_batch_id
              FROM v_unli_wings_daily_from_flavor f
              JOIN unli_item ui ON ui.id = f.unli_item_id
              JOIN ingredient i ON i.id = ui.ingredient_id
             WHERE f.service_date >= ?
               AND f.heads > 0
               AND NOT EXISTS (
                     SELECT 1 FROM unli_reconciliation ur
                      WHERE ur.unli_item_id = f.unli_item_id AND ur.service_date = f.service_date
                   )
               AND {synthetic_clause}
        ),
        combined AS (
            SELECT * FROM real_rows UNION ALL SELECT * FROM synthetic_rows
        )
        SELECT cc.*, bl.lambda_value AS baseline_lambda
          FROM combined cc
          LEFT JOIN (
                SELECT unli_item_id, lambda_value FROM unli_coefficient c1
                 WHERE role = 'baseline'
                   AND id = (SELECT MAX(c2.id) FROM unli_coefficient c2
                              WHERE c2.unli_item_id = c1.unli_item_id AND c2.role = 'baseline')
               ) bl ON bl.unli_item_id = cc.unli_item_id
         ORDER BY cc.unli_item_id, cc.service_date
        """,
        (lookback_param, tol, lookback_param, *sim_params),
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
            "provenance": _row_provenance(r["recorded_by"]),
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


def _robust_center_spread(values: list[float]) -> tuple[float, float]:
    """Median and scaled-MAD (see RULE_ROBUST_MAD_MULTIPLE's own comment for
    why median/MAD rather than mean/stdev). Returns (0.0, 0.0) for an empty
    input; callers must treat a zero spread as "not enough evidence to
    judge", never as "everything is infinitely far away"."""
    if not values:
        return 0.0, 0.0
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    return med, mad * _MAD_TO_SIGMA


def _evaluate_consumption(
    conn: sqlite3.Connection, lookback_days: int = 90, *, mode: str | None = None, use_artifact: bool = True,
) -> tuple[list[dict], float | None]:
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
      rule_flagged        bool, independent of the forest entirely — True if
                          ANY of the three deterministic rules below fires
                          for this record, computed exactly the same way
                          regardless of whether the forest ran. Kept as a
                          single bool (not a list) because it is persisted
                          to unli_consumption_validation_sample as one
                          column (migration 0043) and existing callers/tests
                          read it as "did a rule catch this" generically —
                          see rule_causes below for which one(s).
      rule_causes         list of (method, severity_ratio, score, why) for
                          every rule that fired, most-severe first; empty
                          when rule_flagged is False. severity_ratio is
                          "how many multiples of this rule's own threshold"
                          — comparable across the three different rules,
                          used by detect_consumption_anomalies() to pick the
                          clearest single explanation when more than one
                          fires (see Part 9/explainability).

    Three rules, same "needs no training data, fires from day one" shape as
    the stock-loss pass's large_fraction rule:
      large_variance              |variance_ratio| >= tol * RULE_VARIANCE_MULTIPLE
                                   (owner-configured tolerance; unchanged)
      large_leftover_ratio        robust per-stream deviation of leftover_ratio
                                   >= RULE_ROBUST_MAD_MULTIPLE (2026-10 addition,
                                   see that constant's own comment for why)
      large_consumption_scale     same, over consumption_scale_ratio

    decision_threshold is the forest's own live cutoff (None when it did not
    train) — sample_consumption_for_review() only samples 'near_threshold'
    rows when this is not None, and classifies 'model_flagged' using
    iforest_prediction alone, never rule_flagged.
    """
    mode = mode or model_registry.get_mode(conn, "unli_consumption_isoforest")
    records = _consumption_records(conn, lookback_days, mode=mode)
    if not records:
        return [], None

    tol = conn.execute(
        "SELECT value FROM app_setting WHERE key='unli_variance_tolerance'"
    ).fetchone()
    tol = float(tol["value"]) if tol else 0.10

    # Per-stream robust center/spread for the two new rules — computed once
    # per stream against THIS WINDOW's own population (self-calibrating,
    # same philosophy as peer_rate_ratio/heads/consumption_scale_ratio
    # already use), not a single pooled-across-streams number: wings/rice/
    # fries/drinks have very different normal leftover/scale behavior.
    by_stream_robust: dict[int, dict] = {}
    recs_by_stream: dict[int, list] = {}
    for rec in records:
        recs_by_stream.setdefault(rec["row"]["unli_item_id"], []).append(rec)
    for stream_id, recs in recs_by_stream.items():
        if len(recs) < MIN_ROBUST_RULE_DAYS:
            by_stream_robust[stream_id] = None
            continue
        leftover_med, leftover_spread = _robust_center_spread(
            [r["features"]["leftover_ratio"] for r in recs])
        scale_med, scale_spread = _robust_center_spread(
            [r["features"]["consumption_scale_ratio"] for r in recs])
        by_stream_robust[stream_id] = {
            "leftover_med": leftover_med, "leftover_spread": leftover_spread,
            "scale_med": scale_med, "scale_spread": scale_spread,
        }

    for rec in records:
        rec["iforest_prediction"] = None
        rec["score"] = None
        f = rec["features"]
        robust = by_stream_robust.get(rec["row"]["unli_item_id"])

        variance_ratio_x = abs(f["variance_ratio"]) / (tol * RULE_VARIANCE_MULTIPLE) if tol else 0.0
        variance_flagged = abs(f["variance_ratio"]) >= tol * RULE_VARIANCE_MULTIPLE

        leftover_dev = leftover_flagged = False
        scale_dev = scale_flagged = False
        leftover_x = scale_x = 0.0
        if robust:
            if robust["leftover_spread"] > 1e-9:
                leftover_dev = abs(f["leftover_ratio"] - robust["leftover_med"]) / robust["leftover_spread"]
                leftover_x = leftover_dev / RULE_ROBUST_MAD_MULTIPLE
                leftover_flagged = leftover_dev >= RULE_ROBUST_MAD_MULTIPLE
            if robust["scale_spread"] > 1e-9:
                scale_dev = abs(f["consumption_scale_ratio"] - robust["scale_med"]) / robust["scale_spread"]
                scale_x = scale_dev / RULE_ROBUST_MAD_MULTIPLE
                scale_flagged = scale_dev >= RULE_ROBUST_MAD_MULTIPLE

        causes = []
        if variance_flagged:
            causes.append(("large_variance", variance_ratio_x, round(abs(f["variance_ratio"]), 3)))
        if leftover_flagged:
            causes.append(("large_leftover_ratio", leftover_x, round(leftover_dev, 3)))
        if scale_flagged:
            causes.append(("large_consumption_scale", scale_x, round(scale_dev, 3)))
        causes.sort(key=lambda c: -c[1])

        rec["rule_flagged"] = bool(causes)
        rec["rule_causes"] = causes
        # back-compat scalar, same convention as before (whatever the
        # variance rule alone would have reported) -- no existing caller
        # reads this when rule_causes is available, kept only so nothing
        # that still looks at rec["rule_score"] directly breaks.
        rec["rule_score"] = round(abs(f["variance_ratio"]), 3)

    decision_threshold = None
    if len(records) >= MIN_RECONCILED_DAYS:
        X = np.array([[rec["features"][name] for name in FEATURE_NAMES] for rec in records])
        preds, raw_scores, model_source = _score_with_isolation_forest(
            conn, "unli_consumption_isoforest", X, use_artifact=use_artifact)
        if model_source == "unavailable":
            for rec in records:
                rec["iforest_prediction"] = "unflagged"
                rec["_model_source"] = model_source
        else:
            for rec, raw in zip(records, raw_scores):
                rec["iforest_prediction"] = "unflagged"
                rec["_raw_score"] = float(raw)
                rec["score"] = round(-rec["_raw_score"], 3)
                rec["_model_source"] = model_source
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


def detect_consumption_anomalies(
    conn: sqlite3.Connection, lookback_days: int = 90, *, mode: str | None = None,
) -> list[dict]:
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
    records, _ = _evaluate_consumption(conn, lookback_days, mode=mode)

    def describe(rec) -> dict:
        r, f = rec["row"], rec["features"]
        return {
            "reconciliation_id": r["reconciliation_id"],
            "service_date": r["service_date"],
            "item_key": r["item_key"], "label": r["label"],
            "ingredient_name": r["ingredient_name"], "base_unit": r["base_unit"],
            "heads": r["heads"],
            "theoretical_base": round(r["theoretical_base"], 2) if r["theoretical_base"] is not None else None,
            "actual_base": round(r["actual_base"], 2),
            "actual_per_head": round(f["actual_per_head"], 2),
            "variance_base": round(r["variance_base"], 2) if r["variance_base"] is not None else None,
            "variance_pct": round((r["variance_ratio"] or 0.0) * 100, 1),
            "status": r["status"], "validation_state": r["validation_state"],
            "features": {k: round(v, 4) for k, v in f.items()},
            "provenance": rec["provenance"], "model_source": rec.get("_model_source"),
        }

    tol_row = conn.execute(
        "SELECT value FROM app_setting WHERE key='unli_variance_tolerance'"
    ).fetchone()
    tol = float(tol_row["value"]) if tol_row else 0.10

    # Plain-language cause text per rule method — Part 9: an alert must say
    # WHY in words a kitchen/floor reader understands, never raw
    # IsolationForest/statistical jargon. Isolation-forest flags keep their
    # existing feature-pattern phrasing (already shipped, not changed here).
    def _rule_why(method: str, score: float, r, f) -> str:
        if method == "large_variance":
            vr = score
            return (
                f"measured consumption is {vr * 100:.0f}% away from the provisional "
                f"figure — more than {RULE_VARIANCE_MULTIPLE:g}x the {tol * 100:.0f}% "
                f"tolerance configured for this shop"
            )
        if method == "large_leftover_ratio":
            pct = f["leftover_ratio"] * 100
            return (
                f"leftover was {pct:.0f}% of what was put out — well outside this "
                f"stream's normal leftover range, regardless of how much was consumed"
            )
        # large_consumption_scale
        return (
            f"total {r['label']} consumption on {r['heads']} covers was unusual for a "
            f"day like this, even though the per-head rate itself looked ordinary"
        )

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
            # rule_causes is sorted most-severe-first (§_evaluate_consumption) —
            # the console shows ONE clear cause, not a committee of three.
            method, severity_ratio, score = rec["rule_causes"][0]
            out = describe(rec)
            out.update({
                "method": method,
                "score": score,
                "why": _rule_why(method, score, r, f),
                "severity": "high" if severity_ratio >= 2.0 else "medium",
                "other_rule_causes": [c[0] for c in rec["rule_causes"][1:]],
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
        "rule_robust_mad_multiple": RULE_ROBUST_MAD_MULTIPLE,
        "min_robust_rule_days": MIN_ROBUST_RULE_DAYS,
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
        audit.log(
            conn, "stock_loss_ledger", ledger_id, "review", user_id,
            field="status", new_value=status, note=note,
        )
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
    user_id: str | None = None,
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
        if inserted:
            audit.log(
                conn, "anomaly_validation_sample", "batch", "sample", user_id,
                note=f"{len(inserted)} stock-loss row(s) added to the review queue",
            )

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
        audit.log(
            conn, "anomaly_validation_sample", sample_id, "label", user_id,
            field="human_label", new_value=human_label, note=note,
        )
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
                                 key, max_share: float = MAX_STREAM_SHARE_OF_RANDOM_SAMPLE) -> list[dict]:
    """Generalized form of _stratified_random_sample() — same algorithm, over
    whatever correlation key the caller's observation unit actually has
    (ingredient for stock-loss, unli_item_id/stream for consumption,
    flavor_id for the flavour-level pass — see max_share, parameterized so
    each caller's own cap constant actually takes effect rather than
    silently reusing the stream-level one)."""
    if not candidates or n <= 0:
        return []
    per_key_cap = max(1, int(round(n * max_share)))
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
    user_id: str | None = None,
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

    # A synthetic, derived-from-flavor row (reconciliation_id IS NULL, see
    # _consumption_records) has no real unli_reconciliation row to point at
    # -- it is correctly part of the IsolationForest's training/scoring
    # pool above, but cannot be sent to a human reviewer or persisted here
    # (the table's reconciliation_id column is NOT NULL UNIQUE REFERENCES
    # unli_reconciliation(id)).
    flagged = [rec for rec in records
              if rec["iforest_prediction"] == "flagged"
              and rec["row"]["reconciliation_id"] is not None
              and rec["row"]["reconciliation_id"] not in already_sampled]
    unflagged = [rec for rec in records
                if rec["iforest_prediction"] == "unflagged"
                and rec["row"]["reconciliation_id"] is not None
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
        if inserted:
            audit.log(
                conn, "unli_consumption_validation_sample", "batch", "sample", user_id,
                note=f"{len(inserted)} reconciled day(s) added to the review queue",
            )

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
        audit.log(
            conn, "unli_consumption_validation_sample", sample_id, "label", user_id,
            field="human_label", new_value=human_label, note=note,
        )
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




# ===========================================================================
# Flavor-level consumption anomalies -- a SEPARATE detector, migration 0044's
# unli_flavor_reconciliation (14 flavors), not a replacement for the
# 4-stream pass above.
#
# Why this is a distinct model rather than folded into the stream-level one
# ---------------------------------------------------------------------------
# The 4-stream pass pools ALL flavours of wings into one "wings" signal --
# it answers "was overall wings consumption unusual today", and by
# construction it CANNOT answer "was Garlic Parmesan specifically unusual
# today, even though the other 13 flavours were ordinary" -- a single
# flavour's abnormal day is diluted into, and can be invisible inside, the
# pooled stream-level average. That is a genuinely different operational
# question (restaurant-operational reasoning, not just "more data exists"):
#   - is THIS flavour's per-head consumption unusual relative to its OWN
#     history (a recipe/prep drift specific to one sauce)?
#   - is THIS flavour taking an unusually large SHARE of total wings volume
#     relative to its normal share (over-serving one flavour, under-serving
#     others)?
#   - is THIS flavour unusual relative to its PEERS on the same day (every
#     flavour served that day, cross-sectionally)?
# None of those are answerable from the 4-stream model's own feature set,
# which has no flavour dimension at all. The two detectors are kept
# structurally separate -- different function, different feature set,
# different table -- exactly as the stream-level pass was never touched or
# repurposed to build this.
#
# This detector applies the SAME lesson the stream-level fix above just
# demonstrated numerically: a single-axis anomaly (one feature extreme,
# the rest ordinary) is NOT reliably caught by a pooled multi-feature
# IsolationForest alone, because isolation path length is computed jointly
# across all dimensions, chosen at random. Rather than repeat that mistake
# and need a second capability-gap pass later, the rule+forest hybrid
# ships here from day one.
# ===========================================================================

FLAVOR_MIN_RECONCILED_DAYS = 20      # pooled across all 14 flavours before the forest trains
FLAVOR_MAX_FLAG_FRACTION = 0.25      # same cap philosophy as MAX_FLAG_FRACTION
FLAVOR_RULE_VARIANCE_MULTIPLE = RULE_VARIANCE_MULTIPLE   # same configured shop tolerance, same concept
FLAVOR_RULE_ROBUST_MAD_MULTIPLE = RULE_ROBUST_MAD_MULTIPLE
FLAVOR_MIN_ROBUST_RULE_DAYS = 10     # per-flavour days needed before ITS OWN robust baseline is trusted

FLAVOR_MODEL_VERSION = "unli_flavor_consumption_isoforest_v1"
FLAVOR_ELIGIBILITY_VERSION = "recorded_by_app_user_membership_v1"

# --- review-sampling pool sizes, same shape as the stream-level pass's own
# CONSUMPTION_DEFAULT_*/MAX_STREAM_SHARE_OF_RANDOM_SAMPLE constants --------
FLAVOR_DEFAULT_FLAGGED_SAMPLE_CAP = 15
FLAVOR_DEFAULT_NEAR_THRESHOLD_SAMPLE = 5
FLAVOR_DEFAULT_RANDOM_SAMPLE = 5
MAX_FLAVOR_SHARE_OF_RANDOM_SAMPLE = 0.5   # no single flavour supplies more than half the random pool

# --- Part 4: real-data readiness thresholds (flavor_real_data_readiness())
# Same reasoning as CONSUMPTION_MIN_SCIENTIFIC_VALIDATION_LABELS/_PER_
# PREDICTION_SIDE (small enough to be reachable, large enough that 1-2
# lucky/unlucky reviews cannot swing a reported metric), scaled up for this
# pass's larger dimensionality: 14 distinct per-flavour distributions, not
# one stream, so "is this normal" is a harder question with more ways to be
# unrepresentative. Fixed in code, never a caller-supplied parameter --
# nothing can lower these just to reach a more flattering readiness state
# sooner.
FLAVOR_MIN_ELIGIBLE_REAL_OBSERVATIONS = 50   # real (non-synthetic) reconciled flavor-days, any flavor
FLAVOR_MIN_SERVICE_DATE_COVERAGE_DAYS = 14   # distinct real service_dates represented
FLAVOR_MIN_REVIEWED_PREDICTIONS = 20         # human-reviewed sample rows, any label, this model/eligibility version
FLAVOR_MIN_SCIENTIFIC_VALIDATION_LABELS = 30 # confirmed_anomaly + false_positive combined
FLAVOR_MIN_LABELS_PER_PREDICTION_SIDE = 8    # each of flagged-side / unflagged-side
FLAVOR_MIN_FLAVOR_COVERAGE = 8               # distinct flavours represented among reviewed labels (of 14)

FLAVOR_FEATURE_NAMES = (
    "consumption_per_head",
    "refill_intensity",
    "flavor_share_of_wings",
    "baseline_deviation_ratio",
    "peer_flavor_deviation_ratio",
    "leftover_ratio",
    "variance_ratio",
)

# Same structural provenance test as _REAL_RECORDER_CLAUSE, over
# unli_flavor_reconciliation.recorded_by (migration 0044) -- a row is
# eligible if recorded_by is NULL (legitimate system-internal write with no
# specific human operator) or a genuine app_user account. The simulated-
# workbook importer stamps every row with user_id='simulated_workbook_
# import' (wt/simulated_workbook_import.py), which is never an app_user row
# (verified live in the integration report this builds on) -- so every one
# of the 5,071 imported rows is automatically excluded from this detector's
# training/flagging population by the SAME mechanism the rest of this
# module already relies on. No new provenance column, no new exclusion
# code: a future REAL flavour reconciliation recorded through the normal
# operational path with a real username becomes eligible the moment it is
# written, with no migration and no code change (Part 6's requirement).
_REAL_FLAVOR_RECORDER_CLAUSE = (
    "(fr.recorded_by IS NULL OR fr.recorded_by IN (SELECT username FROM app_user))"
)


def _flavor_consumption_records(conn: sqlite3.Connection, lookback_days: int, *, mode: str | None = None) -> list[dict]:
    """One feature vector per (service_date, flavor) reconciled observation.
    All seven features are derived only from columns unli_flavor_
    reconciliation actually has (migration 0044) -- no invented
    measurement. See the module docstring above for each feature's exact
    formula and the operational question it answers.

      1 consumption_per_head        actual_measured_base / heads. Direct
                                    measurement, comparable across days for
                                    the SAME flavour.
      2 refill_intensity            servings / heads. How many refill rounds
                                    per customer this flavour saw.
      3 flavor_share_of_wings       this flavour's wings_served_pcs / the
                                    SUM of wings_served_pcs across every
                                    flavour reconciled on the same
                                    service_date. Over/under-serving one
                                    flavour relative to the others that day.
      4 baseline_deviation_ratio    consumption_per_head / this flavour's
                                    OWN robust (median) rate across the
                                    lookback window -- a slow per-flavour
                                    drift stays visible.
      5 peer_flavor_deviation_ratio consumption_per_head / the MEAN
                                    consumption_per_head across ALL flavours
                                    reconciled on the SAME service_date --
                                    cross-sectional: unusual relative to its
                                    peers that specific day, not its own
                                    history.
      6 leftover_ratio              leftover_pcs / wings_served_pcs. How
                                    much of what was served for this flavour
                                    came back.
      7 variance_ratio              the importer/source's own stored
                                    variance_ratio (measured vs provisional),
                                    read directly -- not recomputed.
    """
    mode = mode or model_registry.get_mode(conn, "flavor_consumption_isoforest")
    prov_clause, prov_params = _provenance_clause(conn, "fr.recorded_by", "fr.source_batch_id", mode)
    rows = conn.execute(
        f"""
        SELECT fr.id AS reconciliation_id, fr.service_date, fr.flavor_id,
               f.display_name AS flavor_name, fr.heads, fr.servings,
               fr.wings_served_pcs, fr.leftover_pcs, fr.net_consumed_pcs,
               fr.actual_measured_base, fr.unit, fr.provisional_base,
               fr.variance_base, fr.variance_ratio, fr.recorded_by
          FROM unli_flavor_reconciliation fr
          JOIN unli_flavor f ON f.id = fr.flavor_id
         WHERE fr.service_date >= ?
           AND fr.heads > 0
           AND fr.observation_status <> 'rejected'
           AND {prov_clause}
         ORDER BY fr.flavor_id, fr.service_date
        """,
        (timeutil.business_date_n_days_ago(lookback_days), *prov_params),
    ).fetchall()
    if not rows:
        return []

    by_flavor: dict[int, list] = {}
    by_date: dict[str, list] = {}
    for r in rows:
        by_flavor.setdefault(r["flavor_id"], []).append(r)
        by_date.setdefault(r["service_date"], []).append(r)

    flavor_baseline: dict[int, float] = {}
    for flavor_id, recs in by_flavor.items():
        rates = [(rec["actual_measured_base"] / rec["heads"]) for rec in recs if rec["heads"]]
        flavor_baseline[flavor_id] = statistics.median(rates) if rates else 0.0

    records = []
    for r in rows:
        rate = (r["actual_measured_base"] / r["heads"]) if r["heads"] else 0.0
        day_peers = by_date[r["service_date"]]
        day_rates = [(x["actual_measured_base"] / x["heads"]) for x in day_peers if x["heads"]]
        day_rate_mean = sum(day_rates) / len(day_rates) if day_rates else 0.0
        day_wings_total = sum(x["wings_served_pcs"] for x in day_peers)
        baseline = flavor_baseline.get(r["flavor_id"], 0.0)

        records.append({
            "row": r,
            "provenance": _row_provenance(r["recorded_by"]),
            "features": {
                "consumption_per_head": rate,
                "refill_intensity": (r["servings"] / r["heads"]) if r["heads"] else 0.0,
                "flavor_share_of_wings": (r["wings_served_pcs"] / day_wings_total)
                                        if day_wings_total > 0 else 0.0,
                "baseline_deviation_ratio": (rate / baseline) if baseline > 0 else 1.0,
                "peer_flavor_deviation_ratio": (rate / day_rate_mean) if day_rate_mean > 0 else 1.0,
                "leftover_ratio": (r["leftover_pcs"] / r["wings_served_pcs"])
                                  if r["wings_served_pcs"] else 0.0,
                "variance_ratio": r["variance_ratio"] if r["variance_ratio"] is not None else 0.0,
            },
        })
    return records


def _evaluate_flavor_consumption(
    conn: sqlite3.Connection, lookback_days: int = 90, *, mode: str | None = None, use_artifact: bool = True,
) -> tuple[list[dict], float | None]:
    """Same rule+forest shape as _evaluate_consumption(), applied to the
    flavour-level dataset. See that function's own docstring for the
    iforest_prediction/score/rule_flagged/rule_causes contract -- identical
    here."""
    mode = mode or model_registry.get_mode(conn, "flavor_consumption_isoforest")
    records = _flavor_consumption_records(conn, lookback_days, mode=mode)
    if not records:
        return [], None

    tol_row = conn.execute(
        "SELECT value FROM app_setting WHERE key='unli_variance_tolerance'"
    ).fetchone()
    tol = float(tol_row["value"]) if tol_row else 0.10

    recs_by_flavor: dict[int, list] = {}
    for rec in records:
        recs_by_flavor.setdefault(rec["row"]["flavor_id"], []).append(rec)

    by_flavor_robust: dict[int, dict | None] = {}
    for flavor_id, recs in recs_by_flavor.items():
        if len(recs) < FLAVOR_MIN_ROBUST_RULE_DAYS:
            by_flavor_robust[flavor_id] = None
            continue
        leftover_med, leftover_spread = _robust_center_spread(
            [r["features"]["leftover_ratio"] for r in recs])
        share_med, share_spread = _robust_center_spread(
            [r["features"]["flavor_share_of_wings"] for r in recs])
        by_flavor_robust[flavor_id] = {
            "leftover_med": leftover_med, "leftover_spread": leftover_spread,
            "share_med": share_med, "share_spread": share_spread,
        }

    for rec in records:
        f = rec["features"]
        robust = by_flavor_robust.get(rec["row"]["flavor_id"])

        variance_flagged = abs(f["variance_ratio"]) >= tol * FLAVOR_RULE_VARIANCE_MULTIPLE
        variance_x = abs(f["variance_ratio"]) / (tol * FLAVOR_RULE_VARIANCE_MULTIPLE) if tol else 0.0

        leftover_dev = leftover_flagged = False
        share_dev = share_flagged = False
        leftover_x = share_x = 0.0
        if robust:
            if robust["leftover_spread"] > 1e-9:
                leftover_dev = abs(f["leftover_ratio"] - robust["leftover_med"]) / robust["leftover_spread"]
                leftover_x = leftover_dev / FLAVOR_RULE_ROBUST_MAD_MULTIPLE
                leftover_flagged = leftover_dev >= FLAVOR_RULE_ROBUST_MAD_MULTIPLE
            if robust["share_spread"] > 1e-9:
                share_dev = abs(f["flavor_share_of_wings"] - robust["share_med"]) / robust["share_spread"]
                share_x = share_dev / FLAVOR_RULE_ROBUST_MAD_MULTIPLE
                share_flagged = share_dev >= FLAVOR_RULE_ROBUST_MAD_MULTIPLE

        causes = []
        if variance_flagged:
            causes.append(("large_variance", variance_x, round(abs(f["variance_ratio"]), 3)))
        if leftover_flagged:
            causes.append(("large_leftover_ratio", leftover_x, round(leftover_dev, 3)))
        if share_flagged:
            causes.append(("abnormal_flavor_share", share_x, round(share_dev, 3)))
        causes.sort(key=lambda c: -c[1])

        rec["iforest_prediction"] = None
        rec["score"] = None
        rec["rule_flagged"] = bool(causes)
        rec["rule_causes"] = causes

    decision_threshold = None
    if len(records) >= FLAVOR_MIN_RECONCILED_DAYS:
        X = np.array([[rec["features"][name] for name in FLAVOR_FEATURE_NAMES] for rec in records])
        preds, raw_scores, model_source = _score_with_isolation_forest(
            conn, "flavor_consumption_isoforest", X, use_artifact=use_artifact)
        if model_source == "unavailable":
            for rec in records:
                rec["iforest_prediction"] = "unflagged"
                rec["_model_source"] = model_source
        else:
            for rec, raw in zip(records, raw_scores):
                rec["iforest_prediction"] = "unflagged"
                rec["_raw_score"] = float(raw)
                rec["score"] = round(-rec["_raw_score"], 3)
                rec["_model_source"] = model_source
            candidates = sorted(
                (rec for rec, pred in zip(records, preds) if pred == -1),
                key=lambda rec: rec["_raw_score"],
            )
            cap = max(1, int(len(records) * FLAVOR_MAX_FLAG_FRACTION))
            for rec in candidates[:cap]:
                rec["iforest_prediction"] = "flagged"
            if cap and candidates:
                decision_threshold = candidates[min(cap, len(candidates)) - 1]["score"]

    return records, decision_threshold


def detect_flavor_consumption_anomalies(
    conn: sqlite3.Connection, lookback_days: int = 90, *, mode: str | None = None,
) -> list[dict]:
    """Flavour-level counterpart to detect_consumption_anomalies() -- see
    the module docstring above for why this is a separate detector rather
    than a change to the 4-stream one. Same descriptive-only contract:
    flags, explains, nothing downstream acts on one.
    """
    records, _ = _evaluate_flavor_consumption(conn, lookback_days, mode=mode)

    tol_row = conn.execute(
        "SELECT value FROM app_setting WHERE key='unli_variance_tolerance'"
    ).fetchone()
    tol = float(tol_row["value"]) if tol_row else 0.10

    def describe(rec) -> dict:
        r, f = rec["row"], rec["features"]
        return {
            "reconciliation_id": r["reconciliation_id"],
            "service_date": r["service_date"],
            "flavor_id": r["flavor_id"], "flavor_name": r["flavor_name"],
            "heads": r["heads"], "servings": r["servings"],
            "actual_measured_base": round(r["actual_measured_base"], 2),
            "unit": r["unit"],
            "consumption_per_head": round(f["consumption_per_head"], 2),
            "variance_pct": round((r["variance_ratio"] or 0.0) * 100, 1),
            "features": {k: round(v, 4) for k, v in f.items()},
            "provenance": rec["provenance"], "model_source": rec.get("_model_source"),
        }

    def why(method: str, score: float, r, f) -> str:
        if method == "large_variance":
            return (
                f"{r['flavor_name']} consumption measured {score * 100:.0f}% away from the "
                f"provisional figure -- more than {FLAVOR_RULE_VARIANCE_MULTIPLE:g}x the "
                f"{tol * 100:.0f}% tolerance configured for this shop"
            )
        if method == "large_leftover_ratio":
            return (
                f"{r['flavor_name']} leftover was {f['leftover_ratio'] * 100:.0f}% of what was "
                f"served -- well outside this flavour's normal leftover range"
            )
        if method == "abnormal_flavor_share":
            return (
                f"{r['flavor_name']} took {f['flavor_share_of_wings'] * 100:.0f}% of total wings "
                f"served that day -- well outside its normal share relative to the other flavours"
            )
        return (
            f"{r['flavor_name']} consumption per customer was unusually high or low compared "
            f"with its recent pattern and its peer flavours that day"
        )

    flagged: dict[int, dict] = {}
    for rec in records:
        r, f = rec["row"], rec["features"]
        if rec["iforest_prediction"] == "flagged":
            out = describe(rec)
            out.update({
                "method": "isolation_forest",
                "score": rec["score"],
                "why": why("isolation_forest", 0.0, r, f),
                "severity": "high" if rec["_raw_score"] < -0.15 else "medium",
                "other_rule_causes": [],
            })
            flagged[out["reconciliation_id"]] = out
        elif rec["rule_flagged"]:
            method, severity_ratio, score = rec["rule_causes"][0]
            out = describe(rec)
            out.update({
                "method": method,
                "score": score,
                "why": why(method, score, r, f),
                "severity": "high" if severity_ratio >= 2.0 else "medium",
                "other_rule_causes": [c[0] for c in rec["rule_causes"][1:]],
            })
            flagged[out["reconciliation_id"]] = out

    out = list(flagged.values())
    _attribute_flavor_events(out)
    out.sort(key=lambda x: x["service_date"], reverse=True)
    return out


def _attribute_flavor_events(flagged: list[dict]) -> None:
    """2026-10 compositional-flavor-share finding: flavor_share_of_wings is
    zero-sum across all 14 flavours (they must sum to 1), so one genuinely
    abnormal flavour MECHANICALLY shifts its peers' shares the same day --
    confirmed numerically (day-level precision 1.0 vs row-level precision
    0.59 on the identical fixed-seed validation population; every row-level
    "false positive" landed on a day that had a genuine injected anomaly,
    none on a purely normal day). That is a real, correctly-detected
    deviation, NOT a detector flaw -- so this does not relabel anything or
    drop any row. It groups same-day flags into one event and marks which
    one is the clearest single explanation (Part 1, option C: primary
    root-cause flavour + secondary compositional effects), purely as
    PRESENTATION. Row-level detail (every field already on each dict) is
    left completely intact; scientific_flavor_validation_metrics() and
    sample_flavor_for_review() never read is_primary/secondary_of and
    continue to score every row independently.

    Mutates each dict in place, adding event_id/is_primary/secondary_of.
    A row is a demotion CANDIDATE if its own per-head measurements look
    ordinary -- baseline_deviation_ratio and peer_flavor_deviation_ratio
    both close to 1.0, and it was not caught by the variance or leftover
    rule specifically -- regardless of whether the SHARE deviation itself
    was caught by the abnormal_flavor_share rule or by the forest (the
    forest also has flavor_share_of_wings as one of its 7 input
    dimensions, so it can catch the same compositional echo the rule does;
    checking the method name alone, as an earlier version of this function
    did, missed that case). A row flagged for its own direct per-head
    measurement, even on the same date, is an independent finding and is
    never demoted. When the top two candidates are comparably severe
    (within 80% of each other), both are kept as primary rather than
    forcing a single-cause narrative onto what may genuinely be two
    unrelated unusual flavours the same day.
    """
    by_date: dict[str, list[dict]] = {}
    for item in flagged:
        by_date.setdefault(item["service_date"], []).append(item)

    def severity_proxy(item: dict) -> float:
        # isolation_forest's "score" and a rule's "score" are both already
        # "higher = more anomalous" on their own scale -- not cross-
        # comparable in absolute terms, but comparable enough to rank
        # same-method candidates, which is all primacy ordering needs.
        return abs(item["score"]) if item["score"] is not None else 0.0

    def is_share_driven(item: dict) -> bool:
        if item["method"] in ("large_variance", "large_leftover_ratio"):
            return False
        f = item["features"]
        return (abs(f["baseline_deviation_ratio"] - 1.0) < 0.15
                and abs(f["peer_flavor_deviation_ratio"] - 1.0) < 0.15)

    def primacy_key(item: dict) -> tuple:
        return (1 if is_share_driven(item) else 0, -severity_proxy(item))

    for service_date, items in by_date.items():
        items.sort(key=primacy_key)
        primary = items[0]
        primary_mag = severity_proxy(primary)
        primary["event_id"] = service_date
        primary["is_primary"] = True
        primary["secondary_of"] = None
        for other in items[1:]:
            other["event_id"] = service_date
            other_mag = severity_proxy(other)
            comparable = primary_mag > 0 and (other_mag / primary_mag) >= 0.8
            if is_share_driven(other) and not comparable:
                other["is_primary"] = False
                other["secondary_of"] = primary["reconciliation_id"]
                other["why"] += (
                    f" -- likely a secondary (compositional) effect of {primary['flavor_name']}'s "
                    f"unusual day, not an independent issue"
                )
            else:
                other["is_primary"] = True
                other["secondary_of"] = None


def flavor_anomaly_events(conn: sqlite3.Connection, lookback_days: int = 90) -> list[dict]:
    """The grouped, event-level view of detect_flavor_consumption_anomalies()
    -- one entry per service_date that had at least one flagged flavour,
    naming the primary (root-cause) flavour(s) and which others are
    annotated as likely compositional/secondary effects. This is a
    PRESENTATION grouping only (see _attribute_flavor_events's own
    docstring) -- never used for scientific metrics, which stay strictly
    row-level. Exists so a restaurant user sees one alert per real event
    instead of up to 14 on a single compositional day.
    """
    flagged = detect_flavor_consumption_anomalies(conn, lookback_days)
    by_date: dict[str, list[dict]] = {}
    for item in flagged:
        by_date.setdefault(item["service_date"], []).append(item)

    events = []
    for service_date, items in by_date.items():
        primaries = [i for i in items if i["is_primary"]]
        secondaries = [i for i in items if not i["is_primary"]]
        if len(primaries) == 1:
            lead = primaries[0]
            why = f"{lead['flavor_name']} appears to be the primary unusual flavour"
            if secondaries:
                why += (
                    f"; changes in {len(secondaries)} other flavour "
                    f"share{'s' if len(secondaries) != 1 else ''} are likely secondary effects"
                )
        else:
            names = ", ".join(p["flavor_name"] for p in primaries)
            why = (
                f"Multiple flavours ({names}) were unusual on this date -- not confidently "
                f"attributable to a single root cause"
            )
        events.append({
            "service_date": service_date,
            "primary_flavors": [p["flavor_name"] for p in primaries],
            "secondary_flavors": [s["flavor_name"] for s in secondaries],
            "total_flagged": len(items),
            "why": why,
            "rows": items,
        })
    events.sort(key=lambda e: e["service_date"], reverse=True)
    return events


def flavor_consumption_model_details(conn: sqlite3.Connection, lookback_days: int = 90) -> dict:
    """Technical-panel parameters/live counts for the flavour-level pass --
    mirrors consumption_model_details()'s honesty about synthetic exclusion
    and below-threshold non-training."""
    records = _flavor_consumption_records(conn, lookback_days)
    anomalies = detect_flavor_consumption_anomalies(conn, lookback_days)
    by_method: dict[str, int] = {}
    for a in anomalies:
        by_method[a["method"]] = by_method.get(a["method"], 0) + 1

    since = timeutil.business_date_n_days_ago(lookback_days)
    total_all = conn.execute(
        "SELECT COUNT(*) AS n FROM unli_flavor_reconciliation fr "
        "WHERE fr.service_date >= ? AND fr.heads > 0 AND fr.observation_status <> 'rejected'",
        (since,),
    ).fetchone()["n"]
    synthetic = total_all - len(records)

    return {
        "model": "IsolationForest (scikit-learn) + robust-threshold rule layer",
        "data_source": "unli_flavor_reconciliation (per-flavour reconciled service days)",
        "n_estimators": N_ESTIMATORS,
        "random_state": RANDOM_STATE,
        "n_features": len(FLAVOR_FEATURE_NAMES),
        "features": list(FLAVOR_FEATURE_NAMES),
        "min_reconciled_days": FLAVOR_MIN_RECONCILED_DAYS,
        "rule_variance_multiple": FLAVOR_RULE_VARIANCE_MULTIPLE,
        "rule_robust_mad_multiple": FLAVOR_RULE_ROBUST_MAD_MULTIPLE,
        "min_robust_rule_days": FLAVOR_MIN_ROBUST_RULE_DAYS,
        "max_flag_fraction": FLAVOR_MAX_FLAG_FRACTION,
        "lookback_days": lookback_days,
        "records_evaluated": len(records),
        "synthetic_excluded": synthetic,
        "synthetic_excluded_note": (
            None if synthetic == 0 else
            f"{synthetic} flavour-reconciliation record(s) in this window were recorded by a "
            "non-account source (e.g. the simulated-workbook importer) -- excluded from "
            "training and flagging. See _REAL_FLAVOR_RECORDER_CLAUSE."
        ),
        "trained_this_call": len(records) >= FLAVOR_MIN_RECONCILED_DAYS,
        "flagged": len(anomalies),
        "flagged_by_method": by_method,
        "readiness": flavor_real_data_readiness(conn, lookback_days),
    }


def _flavor_validation_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "reconciliation_id": row["reconciliation_id"],
        "sample_source": row["sample_source"], "model_prediction": row["model_prediction"],
        "rule_flagged": bool(row["rule_flagged"]), "rule_causes": row["rule_causes"],
        "anomaly_score": row["anomaly_score"], "decision_threshold": row["decision_threshold"],
        "is_primary": None if row["is_primary"] is None else bool(row["is_primary"]),
        "secondary_of_reconciliation_id": row["secondary_of_reconciliation_id"],
        "model_version": row["model_version"], "eligibility_version": row["eligibility_version"],
        "evaluated_at": row["evaluated_at"], "human_label": row["human_label"],
        "reviewed_by": row["reviewed_by"], "reviewed_at": row["reviewed_at"],
        "review_note": row["review_note"], "created_at": row["created_at"],
    }


def sample_flavor_for_review(
    conn: sqlite3.Connection,
    n_flagged: int | None = None,
    n_near_threshold: int = FLAVOR_DEFAULT_NEAR_THRESHOLD_SAMPLE,
    n_random: int = FLAVOR_DEFAULT_RANDOM_SAMPLE,
    lookback_days: int = 90,
    rng: random.Random | None = None,
    user_id: str | None = None,
) -> dict:
    """Flavour-level counterpart to sample_consumption_for_review() -- same
    three-pool shape (model_flagged / near_threshold / random_unflagged),
    same rule-vs-ML separation (only ever samples a genuine IsolationForest
    'flagged' verdict as model_flagged, never a rule-only catch -- see that
    function's own docstring for the full rationale, identical here),
    same refusal-not-substitution below FLAVOR_MIN_RECONCILED_DAYS.
    Stratifies the random pool by flavor_id (MAX_FLAVOR_SHARE_OF_RANDOM_
    SAMPLE) rather than unli_item_id -- the correlation unit this pass's
    observations actually have.
    """
    records, decision_threshold = _evaluate_flavor_consumption(conn, lookback_days)
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
            "model_version": FLAVOR_MODEL_VERSION,
            "eligibility_version": FLAVOR_ELIGIBILITY_VERSION,
        }

    already_sampled = {
        r["reconciliation_id"] for r in
        conn.execute("SELECT reconciliation_id FROM unli_flavor_validation_sample").fetchall()
    }

    flagged = [rec for rec in records
              if rec["iforest_prediction"] == "flagged"
              and rec["row"]["reconciliation_id"] not in already_sampled]
    unflagged = [rec for rec in records
                if rec["iforest_prediction"] == "unflagged"
                and rec["row"]["reconciliation_id"] not in already_sampled]

    flagged_cap = n_flagged if n_flagged is not None else min(len(flagged), FLAVOR_DEFAULT_FLAGGED_SAMPLE_CAP)
    flagged_pool = flagged[:flagged_cap]

    scored_unflagged = sorted(unflagged, key=lambda rec: -rec["score"])
    near_threshold_pool = scored_unflagged[:n_near_threshold]

    near_threshold_ids = {rec["row"]["reconciliation_id"] for rec in near_threshold_pool}
    random_candidates = [rec for rec in unflagged
                         if rec["row"]["reconciliation_id"] not in near_threshold_ids]
    random_pool = _stratified_random_sample_by(
        random_candidates, n_random, rng or random.Random(),
        key=lambda rec: rec["row"]["flavor_id"], max_share=MAX_FLAVOR_SHARE_OF_RANDOM_SAMPLE)

    inserted: list[int] = []
    with write_tx(conn):
        for sample_source, pool in (
            ("model_flagged", flagged_pool),
            ("near_threshold", near_threshold_pool),
            ("random_unflagged", random_pool),
        ):
            for rec in pool:
                r = rec["row"]
                causes_text = ",".join(c[0] for c in rec["rule_causes"]) if rec["rule_causes"] else None
                cur = conn.execute(
                    "INSERT OR IGNORE INTO unli_flavor_validation_sample "
                    "(reconciliation_id, sample_source, model_prediction, rule_flagged, "
                    " rule_causes, anomaly_score, decision_threshold, model_version, "
                    " eligibility_version) VALUES (?,?,?,?,?,?,?,?,?)",
                    (r["reconciliation_id"], sample_source, rec["iforest_prediction"],
                     1 if rec["rule_flagged"] else 0, causes_text, rec["score"], decision_threshold,
                     FLAVOR_MODEL_VERSION, FLAVOR_ELIGIBILITY_VERSION),
                )
                if cur.rowcount:
                    inserted.append(r["reconciliation_id"])
        if inserted:
            audit.log(
                conn, "unli_flavor_validation_sample", "batch", "sample", user_id,
                note=f"{len(inserted)} reconciled flavour-day(s) added to the review queue",
            )

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
        "model_version": FLAVOR_MODEL_VERSION,
        "eligibility_version": FLAVOR_ELIGIBILITY_VERSION,
    }


def submit_flavor_validation_label(
    conn: sqlite3.Connection, sample_id: int, human_label: str,
    note: str | None = None, user_id: str | None = None,
) -> dict:
    """A human's one-time ground-truth verdict on one sampled flavour-day.

    Four possible labels (migration 0045) -- richer than the stream-level
    pass's two, because a flavour-level reviewer genuinely has a fourth
    honest answer the stream-level question doesn't: 'insufficient_
    information' (nothing recorded lets them judge this one either way).
    Only confirmed_anomaly/false_positive enter the scientific confusion
    matrix (scientific_flavor_validation_metrics()); expected_operational_
    variation and insufficient_information are retained for audit but
    excluded, the same way 'pending' is. Set exactly once -- a reviewer who
    changes their mind is a product decision for later, not silently
    allowed here, matching every other one-time-label convention in this
    module.
    """
    valid = ("confirmed_anomaly", "false_positive", "expected_operational_variation",
            "insufficient_information")
    if human_label not in valid:
        raise InvalidStateError(
            f"human_label must be one of {valid}, got {human_label!r} -- pending is a state "
            "this record starts in, never one a reviewer submits"
        )
    with write_tx(conn):
        row = conn.execute(
            "SELECT * FROM unli_flavor_validation_sample WHERE id=?", (sample_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"flavor validation sample {sample_id} not found")
        if row["human_label"] != "pending":
            raise InvalidStateError(
                f"sample {sample_id} was already labeled '{row['human_label']}' on "
                f"{row['reviewed_at']} -- labeling is one-time per sample"
            )
        conn.execute(
            "UPDATE unli_flavor_validation_sample SET human_label=?, reviewed_by=?, "
            "reviewed_at=datetime('now'), review_note=? WHERE id=?",
            (human_label, user_id, note, sample_id),
        )
        updated = conn.execute(
            "SELECT * FROM unli_flavor_validation_sample WHERE id=?", (sample_id,)
        ).fetchone()
        audit.log(
            conn, "unli_flavor_validation_sample", sample_id, "label", user_id,
            field="human_label", new_value=human_label, note=note,
        )
    return _flavor_validation_row(updated)


def flavor_validation_queue(conn: sqlite3.Connection, status: str = "pending",
                            blind: bool = True) -> list[dict]:
    """The human-facing review queue for the flavour-level pass. blind=True
    (the only mode the labeling screen may use) omits sample_source/
    model_prediction/rule_flagged/rule_causes/anomaly_score/decision_
    threshold/model_version/eligibility_version/evaluated_at/is_primary/
    secondary_of entirely -- a reviewer judging whether a day's numbers
    were genuinely unusual must not see the model's own opinion (or this
    pass's compositional-event attribution) first, same blinding rationale
    as every other review queue in this module."""
    rows = conn.execute(
        """
        SELECT s.*, fr.service_date, f.display_name AS flavor_name, fr.heads, fr.servings,
               fr.actual_measured_base, fr.unit, fr.provisional_base, fr.variance_base,
               fr.variance_ratio
          FROM unli_flavor_validation_sample s
          JOIN unli_flavor_reconciliation fr ON fr.id = s.reconciliation_id
          JOIN unli_flavor f ON f.id = fr.flavor_id
         WHERE s.human_label = ?
         ORDER BY s.evaluated_at
        """,
        (status,),
    ).fetchall()
    out = []
    for r in rows:
        item = {
            "sample_id": r["id"], "reconciliation_id": r["reconciliation_id"],
            "service_date": r["service_date"], "flavor_name": r["flavor_name"],
            "heads": r["heads"], "servings": r["servings"],
            "actual_measured_base": round(r["actual_measured_base"], 2),
            "unit": r["unit"],
            "variance_pct": round((r["variance_ratio"] or 0.0) * 100, 1),
            "human_label": r["human_label"],
        }
        if not blind:
            item.update({
                "sample_source": r["sample_source"], "model_prediction": r["model_prediction"],
                "rule_flagged": bool(r["rule_flagged"]), "rule_causes": r["rule_causes"],
                "anomaly_score": r["anomaly_score"], "decision_threshold": r["decision_threshold"],
                "is_primary": None if r["is_primary"] is None else bool(r["is_primary"]),
                "secondary_of_reconciliation_id": r["secondary_of_reconciliation_id"],
                "model_version": r["model_version"], "eligibility_version": r["eligibility_version"],
                "evaluated_at": r["evaluated_at"],
            })
        out.append(item)
    return out


def scientific_flavor_validation_metrics(
    conn: sqlite3.Connection, model_version: str | None = None, eligibility_version: str | None = None,
) -> dict:
    """TP/FP/FN/TN and derived metrics for the flavour-level pass, computed
    ONLY from unli_flavor_validation_sample rows a human has labeled
    confirmed_anomaly or false_positive (expected_operational_variation and
    insufficient_information are real reviews but carry no usable ground-
    truth polarity for a confusion matrix -- see migration 0045's own
    comment), scoped to one (model_version, eligibility_version) pair.

        TP = model_prediction='flagged'   AND human_label='confirmed_anomaly'
        FP = model_prediction='flagged'   AND human_label='false_positive'
        FN = model_prediction='unflagged' AND human_label='confirmed_anomaly'
        TN = model_prediction='unflagged' AND human_label='false_positive'

    model_prediction is always the IsolationForest's own verdict alone
    (sample_flavor_for_review() never samples a rule-only catch as
    model_flagged) -- this is the ISOLATION-FOREST-ONLY confusion matrix.
    It is NOT the combined rule+forest operational detector's performance
    (that figure is the row-level precision/recall reported directly
    against detect_flavor_consumption_anomalies()'s own output in the
    validation harness -- see tests.run_tests's TestFlavorAnomalySimulation
    for the SIMULATION/TECHNICAL figure, never to be confused with this
    REAL-DATA-ONLY function). rule_causes is preserved on every sample row
    for audit, never read here, for the identical reason rule_flagged isn't
    read by the stream-level scientific_consumption_validation_metrics().
    """
    model_version = model_version or FLAVOR_MODEL_VERSION
    eligibility_version = eligibility_version or FLAVOR_ELIGIBILITY_VERSION

    rows = conn.execute(
        "SELECT * FROM unli_flavor_validation_sample "
        "WHERE model_version=? AND eligibility_version=? "
        "AND human_label IN ('confirmed_anomaly','false_positive')",
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
        elif r["human_label"] == "false_positive":
            fp += is_flagged
            tn += (not is_flagged)

    total = tp + fp + fn + tn
    flagged_side, unflagged_side = tp + fp, fn + tn
    base = {
        "model_version": model_version, "eligibility_version": eligibility_version,
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "labels_by_sample_source": by_source,
    }
    if (total < FLAVOR_MIN_SCIENTIFIC_VALIDATION_LABELS
            or flagged_side < FLAVOR_MIN_LABELS_PER_PREDICTION_SIDE
            or unflagged_side < FLAVOR_MIN_LABELS_PER_PREDICTION_SIDE):
        return {
            **base,
            "available": False,
            "reason": "insufficient_validation_data",
            "detail": (
                f"{total} labeled sample(s) under this model/eligibility version "
                f"({flagged_side} on the flagged side, {unflagged_side} on the unflagged side). "
                f"Needs >= {FLAVOR_MIN_SCIENTIFIC_VALIDATION_LABELS} total and >= "
                f"{FLAVOR_MIN_LABELS_PER_PREDICTION_SIDE} on each side of the model's own "
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
    fnr = (fn / (fn + tp)) if (fn + tp) > 0 else None
    npv = (tn / (tn + fn)) if (tn + fn) > 0 else None
    accuracy = ((tp + tn) / total) if total > 0 else None

    return {
        **base,
        "available": True,
        "methodology": (
            "ISOLATION-FOREST-ONLY sample-based validation metrics, not a population census "
            "and not the combined rule+forest operational detector's performance: the flagged "
            "population is reviewed up to its cap, but near-threshold and random-unflagged "
            "days were SAMPLED rather than exhaustively reviewed. These figures describe the "
            "reviewed cohort under this model/eligibility version and must not be quoted as "
            "population-wide accuracy, and must never be reported as 'the detector's' "
            "precision/recall without noting the rule layer is excluded."
        ),
        "confirmed_labels": total,
        "precision": round(precision, 3) if precision is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "specificity": round(specificity, 3) if specificity is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
        "false_positive_rate": round(fpr, 3) if fpr is not None else None,
        "false_negative_rate": round(fnr, 3) if fnr is not None else None,
        "negative_predictive_value": round(npv, 3) if npv is not None else None,
        "accuracy": round(accuracy, 3) if accuracy is not None else None,
    }


def flavor_real_data_readiness(conn: sqlite3.Connection, lookback_days: int = 90) -> dict:
    """Part 4's readiness policy -- the ONLY function that may report
    READY_REAL_DATA for the flavour-level pass, and only when every
    criterion below is met by GENUINE (non-synthetic) evidence. Thresholds
    are fixed module constants, never a caller-supplied parameter, so
    nothing can lower them just to reach a more flattering state sooner.

    States, in order of increasing evidence:
      SIMULATION_ONLY        zero genuine observations exist at all
      INSUFFICIENT_REAL_DATA genuine observations exist but below the
                             volume/date-coverage floor to train or
                             evaluate meaningfully
      READY_WITH_LIMITATIONS the forest can train on real data, but
                             human-reviewed ground truth is not yet
                             sufficient for a scientific claim
      READY_REAL_DATA        sufficient real observations, date coverage,
                             AND genuinely human-reviewed labels on both
                             sides of the model's own prediction
    Simulated-workbook rows (recorded_by='simulated_workbook_import', never
    an app_user) never count toward any criterion below -- every count here
    is already filtered through _REAL_FLAVOR_RECORDER_CLAUSE
    (_flavor_consumption_records) or model_version/eligibility_version
    scoping (the validation-sample table), the same structural exclusion
    the rest of this module relies on throughout.
    """
    records = _flavor_consumption_records(conn, lookback_days, mode="REAL")
    real_observations = len(records)
    distinct_dates = len({r["row"]["service_date"] for r in records})

    labeled = conn.execute(
        "SELECT * FROM unli_flavor_validation_sample WHERE model_version=? AND eligibility_version=?",
        (FLAVOR_MODEL_VERSION, FLAVOR_ELIGIBILITY_VERSION),
    ).fetchall()
    reviewed = [r for r in labeled if r["human_label"] != "pending"]
    confirmed_or_fp = [r for r in reviewed if r["human_label"] in ("confirmed_anomaly", "false_positive")]
    flagged_side = sum(1 for r in confirmed_or_fp if r["model_prediction"] == "flagged")
    unflagged_side = sum(1 for r in confirmed_or_fp if r["model_prediction"] == "unflagged")

    reviewed_flavor_ids: set[int] = set()
    for r in reviewed:
        fr = conn.execute(
            "SELECT flavor_id FROM unli_flavor_reconciliation WHERE id=?", (r["reconciliation_id"],)
        ).fetchone()
        if fr:
            reviewed_flavor_ids.add(fr["flavor_id"])

    criteria = {
        "real_observations": {
            "value": real_observations, "required": FLAVOR_MIN_ELIGIBLE_REAL_OBSERVATIONS,
            "met": real_observations >= FLAVOR_MIN_ELIGIBLE_REAL_OBSERVATIONS},
        "service_date_coverage_days": {
            "value": distinct_dates, "required": FLAVOR_MIN_SERVICE_DATE_COVERAGE_DAYS,
            "met": distinct_dates >= FLAVOR_MIN_SERVICE_DATE_COVERAGE_DAYS},
        "reviewed_predictions": {
            "value": len(reviewed), "required": FLAVOR_MIN_REVIEWED_PREDICTIONS,
            "met": len(reviewed) >= FLAVOR_MIN_REVIEWED_PREDICTIONS},
        "scientific_validation_labels": {
            "value": len(confirmed_or_fp), "required": FLAVOR_MIN_SCIENTIFIC_VALIDATION_LABELS,
            "met": len(confirmed_or_fp) >= FLAVOR_MIN_SCIENTIFIC_VALIDATION_LABELS},
        "labels_on_flagged_side": {
            "value": flagged_side, "required": FLAVOR_MIN_LABELS_PER_PREDICTION_SIDE,
            "met": flagged_side >= FLAVOR_MIN_LABELS_PER_PREDICTION_SIDE},
        "labels_on_unflagged_side": {
            "value": unflagged_side, "required": FLAVOR_MIN_LABELS_PER_PREDICTION_SIDE,
            "met": unflagged_side >= FLAVOR_MIN_LABELS_PER_PREDICTION_SIDE},
        "flavor_coverage": {
            "value": len(reviewed_flavor_ids), "required": FLAVOR_MIN_FLAVOR_COVERAGE,
            "met": len(reviewed_flavor_ids) >= FLAVOR_MIN_FLAVOR_COVERAGE},
    }
    state, reason = _readiness_ladder(
        criteria, zero_observations=(real_observations == 0),
        insufficient_volume=(not criteria["real_observations"]["met"]
                              or not criteria["service_date_coverage_days"]["met"]),
        no_evidence_reason="zero genuine (non-synthetic) flavour reconciliation observations exist yet",
    )

    return {
        "state": state, "reason": reason, "criteria": criteria,
        "model_version": FLAVOR_MODEL_VERSION, "eligibility_version": FLAVOR_ELIGIBILITY_VERSION,
    }


def _readiness_ladder(
    criteria: dict[str, dict], *, zero_observations: bool, insufficient_volume: bool,
    no_evidence_reason: str,
) -> tuple[str, str]:
    """The SIMULATED->MIXED->REAL readiness state machine shared by every
    pass's own *_real_data_readiness() wrapper (originally built for the
    flavour pass alone; generalized here so Pass A/B promote through the
    exact same ladder with their own domain-specific criteria dicts rather
    than a parallel, possibly-drifting copy of this logic). Each model's
    mode-transition decision (wt/ml_training.py) steps mode forward exactly
    once (SIMULATED->MIXED, never SIMULATED->REAL) only when this returns
    READY_REAL_DATA."""
    all_met = all(c["met"] for c in criteria.values())
    if zero_observations:
        return "SIMULATION_ONLY", no_evidence_reason
    if insufficient_volume:
        return "INSUFFICIENT_REAL_DATA", (
            "real observation volume and/or date coverage is below the minimum needed to "
            "train or evaluate meaningfully"
        )
    if not all_met:
        return "READY_WITH_LIMITATIONS", (
            "the forest can train on real data, but human-reviewed ground truth is not yet "
            "sufficient for a scientific Precision/Recall claim"
        )
    return "READY_REAL_DATA", (
        "sufficient real observations, date coverage, and genuinely human-reviewed labels "
        "on both sides of the model's own prediction"
    )


def stock_loss_real_data_readiness(conn: sqlite3.Connection, lookback_days: int = 90) -> dict:
    """Pass A's counterpart to flavor_real_data_readiness() — same ladder
    (_readiness_ladder), same reuse-not-reinvent principle for thresholds:
    MIN_POOLED_SAMPLES/MIN_VALIDATED_LABELS are this pass's own existing
    training/validation-metrics minimums, not new numbers invented for this
    function."""
    records, _ = _evaluate_stock_loss(conn, lookback_days, mode="REAL", use_artifact=False)
    real_observations = len(records)
    distinct_days = len({r["row"]["created_at"][:10] for r in records})

    labeled = conn.execute(
        "SELECT * FROM anomaly_validation_sample WHERE model_version=? AND eligibility_version=?",
        (ANOMALY_MODEL_VERSION, ELIGIBILITY_FILTER_VERSION),
    ).fetchall()
    reviewed = [r for r in labeled if r["human_label"] != "pending"]
    confirmed_or_fp = [r for r in reviewed if r["human_label"] in ("confirmed_anomaly", "expected_or_normal")]
    flagged_side = sum(1 for r in confirmed_or_fp if r["model_prediction"] == "flagged")
    unflagged_side = sum(1 for r in confirmed_or_fp if r["model_prediction"] == "unflagged")

    criteria = {
        "real_observations": {
            "value": real_observations, "required": MIN_POOLED_SAMPLES * 2,
            "met": real_observations >= MIN_POOLED_SAMPLES * 2},
        "service_date_coverage_days": {
            "value": distinct_days, "required": 14,
            "met": distinct_days >= 14},
        "reviewed_predictions": {
            "value": len(reviewed), "required": MIN_VALIDATED_LABELS,
            "met": len(reviewed) >= MIN_VALIDATED_LABELS},
        "scientific_validation_labels": {
            "value": len(confirmed_or_fp), "required": MIN_VALIDATED_LABELS,
            "met": len(confirmed_or_fp) >= MIN_VALIDATED_LABELS},
        "labels_on_flagged_side": {
            "value": flagged_side, "required": MIN_LABELS_PER_PREDICTION_SIDE,
            "met": flagged_side >= MIN_LABELS_PER_PREDICTION_SIDE},
        "labels_on_unflagged_side": {
            "value": unflagged_side, "required": MIN_LABELS_PER_PREDICTION_SIDE,
            "met": unflagged_side >= MIN_LABELS_PER_PREDICTION_SIDE},
    }
    state, reason = _readiness_ladder(
        criteria, zero_observations=(real_observations == 0),
        insufficient_volume=(not criteria["real_observations"]["met"]
                              or not criteria["service_date_coverage_days"]["met"]),
        no_evidence_reason="zero genuine (non-synthetic) stock-loss ledger observations exist yet",
    )
    return {
        "state": state, "reason": reason, "criteria": criteria,
        "model_version": ANOMALY_MODEL_VERSION, "eligibility_version": ELIGIBILITY_FILTER_VERSION,
    }


def consumption_real_data_readiness(conn: sqlite3.Connection, lookback_days: int = 90) -> dict:
    """Pass B's counterpart — see stock_loss_real_data_readiness's own
    comment. 'wings' stream only has any real path today since that is the
    only stream _consumption_records' derived-view fallback covers; a real
    rice/fries/drinks reconciliation would count here too the moment one
    exists, through the same v_unli_daily_consumption read REAL mode
    already uses."""
    records, _ = _evaluate_consumption(conn, lookback_days, mode="REAL", use_artifact=False)
    real_observations = len(records)
    distinct_dates = len({r["row"]["service_date"] for r in records})

    labeled = conn.execute(
        "SELECT * FROM unli_consumption_validation_sample WHERE model_version=? AND eligibility_version=?",
        (CONSUMPTION_MODEL_VERSION, CONSUMPTION_ELIGIBILITY_VERSION),
    ).fetchall()
    reviewed = [r for r in labeled if r["human_label"] != "pending"]
    confirmed_or_fp = [r for r in reviewed if r["human_label"] in ("confirmed_anomaly", "expected_or_explained")]
    flagged_side = sum(1 for r in confirmed_or_fp if r["model_prediction"] == "flagged")
    unflagged_side = sum(1 for r in confirmed_or_fp if r["model_prediction"] == "unflagged")

    criteria = {
        "real_observations": {
            "value": real_observations, "required": MIN_RECONCILED_DAYS * 2,
            "met": real_observations >= MIN_RECONCILED_DAYS * 2},
        "service_date_coverage_days": {
            "value": distinct_dates, "required": 14,
            "met": distinct_dates >= 14},
        "reviewed_predictions": {
            "value": len(reviewed), "required": CONSUMPTION_MIN_SCIENTIFIC_VALIDATION_LABELS,
            "met": len(reviewed) >= CONSUMPTION_MIN_SCIENTIFIC_VALIDATION_LABELS},
        "scientific_validation_labels": {
            "value": len(confirmed_or_fp), "required": CONSUMPTION_MIN_SCIENTIFIC_VALIDATION_LABELS,
            "met": len(confirmed_or_fp) >= CONSUMPTION_MIN_SCIENTIFIC_VALIDATION_LABELS},
        "labels_on_flagged_side": {
            "value": flagged_side, "required": CONSUMPTION_MIN_LABELS_PER_PREDICTION_SIDE,
            "met": flagged_side >= CONSUMPTION_MIN_LABELS_PER_PREDICTION_SIDE},
        "labels_on_unflagged_side": {
            "value": unflagged_side, "required": CONSUMPTION_MIN_LABELS_PER_PREDICTION_SIDE,
            "met": unflagged_side >= CONSUMPTION_MIN_LABELS_PER_PREDICTION_SIDE},
    }
    state, reason = _readiness_ladder(
        criteria, zero_observations=(real_observations == 0),
        insufficient_volume=(not criteria["real_observations"]["met"]
                              or not criteria["service_date_coverage_days"]["met"]),
        no_evidence_reason="zero genuine (non-synthetic) reconciled consumption days exist yet",
    )
    return {
        "state": state, "reason": reason, "criteria": criteria,
        "model_version": CONSUMPTION_MODEL_VERSION, "eligibility_version": CONSUMPTION_ELIGIBILITY_VERSION,
    }
