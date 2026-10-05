-- =============================================================================
-- 0041  Review-sampling infrastructure for the stock-loss Isolation Forest
--       pass (wt/anomaly.py's detect_anomalies()).
--
-- 2026-09-30 finding (migration 0035's own comment, anomaly.py's
-- SYNTHETIC_SEED_USER_IDS): every reviewed row to date was synthetic-sourced,
-- so Precision/Recall/F1 have never actually been measured against real
-- operations. The gap is not "not enough reviews" -- it is that the existing
-- anomaly_review table (0035) can only ever hold a review of a ledger row
-- the model's LIVE output currently flags. There was no way to review an
-- UNFLAGGED row at all, so False Negatives and True Negatives were
-- structurally unmeasurable regardless of how many flagged rows got
-- reviewed. Recall and specificity need unflagged evidence; this table is
-- where that evidence is recorded.
--
-- Why a new table rather than reusing anomaly_review
-- ---------------------------------------------------
-- anomaly_review's `status` enum ('flagged','under_review','confirmed_
-- anomaly','false_positive','resolved') conflates the model's own verdict
-- with the human's -- there is no "pending, sampled on purpose, model said
-- unflagged" state, and bending 'flagged' to mean that would silently
-- change what every existing row using that value already means. Rather
-- than widen an enum whose existing values are relied on elsewhere
-- (validation_metrics(), Losses.js), this is a parallel, narrower table
-- purpose-built for the sampling workflow. anomaly_review is untouched --
-- its flagged-row quick-review workflow (Losses.js's Confirm/False positive
-- buttons) keeps working exactly as before.
--
-- Prediction vs. human truth, kept in separate columns on purpose
-- -----------------------------------------------------------------
-- model_prediction ('flagged'/'unflagged') is what the model said at
-- evaluation time. human_label ('pending'/'confirmed_anomaly'/
-- 'expected_or_normal') is what a human later decided, asked as an
-- operational question ("was this movement expected?"), never as "is this a
-- false positive" -- that phrasing presupposes the model's verdict and
-- would bias the reviewer toward agreeing or disagreeing with it rather
-- than judging the event on its own terms. TP/FP/FN/TN are DERIVED from the
-- pair of these two columns (see wt/anomaly.py's scientific_validation_
-- metrics()), never stored directly, so the derivation can be audited and
-- corrected without touching stored data.
--
-- sample_source records WHY this record entered the review queue
-- (model_flagged / near_threshold / random_unflagged) -- a sampling-design
-- concept, independent of model_prediction: a near_threshold or
-- random_unflagged row is always model_prediction='unflagged' by
-- construction, but WHICH of those two pools it came from matters for how
-- the resulting metrics must be interpreted (stratified-sample, not a
-- population census -- see scientific_validation_metrics()'s docstring).
--
-- Reproducibility: model_version/eligibility_version/anomaly_score/
-- decision_threshold are frozen at evaluation time, not recomputed from
-- live code on every read, so a review's prediction can always be explained
-- even after a later anomaly.py change alters ANOMALY_MODEL_VERSION/
-- ELIGIBILITY_FILTER_VERSION. Metrics are computed ONLY over samples sharing
-- the CURRENT version pair (see scientific_validation_metrics()) -- older
-- versions' samples stay queryable for audit but are never silently pooled
-- into a changed classifier's confusion matrix.
--
-- Mutable, not append-only, unlike stock_ledger/anomaly_review/ingredient_
-- variance_review: this is a review QUEUE (closer in shape to
-- purchase_order's mutable status than to a permanent event ledger) --
-- human_label/reviewed_by/reviewed_at/review_note are set exactly once, by
-- submit_validation_label(), which refuses to overwrite an already-labeled
-- row rather than allowing a silent re-label. ledger_id is UNIQUE so the
-- same ledger row is never sampled into a second, duplicate pending row --
-- re-running the sampler either skips a ledger_id already present here or,
-- if desired, that is a decision made in Python, never by inserting twice.
-- =============================================================================

CREATE TABLE anomaly_validation_sample (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  ledger_id           INTEGER NOT NULL UNIQUE REFERENCES stock_ledger(id) ON DELETE RESTRICT,

  sample_source       TEXT NOT NULL CHECK (sample_source IN
                        ('model_flagged', 'near_threshold', 'random_unflagged')),
  model_prediction    TEXT NOT NULL CHECK (model_prediction IN ('flagged', 'unflagged')),
  anomaly_score       REAL,     -- IsolationForest decision_function score, or the rule-based
                                 -- score when the forest did not train this call; NULL only if
                                 -- genuinely unscoreable (should not occur for a real candidate)
  decision_threshold  REAL,     -- score cutoff in effect for this evaluation; NULL when the
                                 -- forest did not train (pool below MIN_POOLED_SAMPLES)

  model_version       TEXT NOT NULL,   -- anomaly.ANOMALY_MODEL_VERSION at evaluation time
  eligibility_version TEXT NOT NULL,   -- anomaly.ELIGIBILITY_FILTER_VERSION at evaluation time
  evaluated_at        TEXT NOT NULL DEFAULT (datetime('now')),

  human_label         TEXT NOT NULL DEFAULT 'pending'
                        CHECK (human_label IN ('pending', 'confirmed_anomaly', 'expected_or_normal')),
  reviewed_by         TEXT,
  reviewed_at         TEXT,
  review_note         TEXT,

  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX ix_anomaly_validation_sample_label ON anomaly_validation_sample (human_label);
CREATE INDEX ix_anomaly_validation_sample_version
  ON anomaly_validation_sample (model_version, eligibility_version);

INSERT INTO schema_version (version, name) VALUES (41, 'anomaly_validation_sample');
