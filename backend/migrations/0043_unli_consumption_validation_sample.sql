-- =============================================================================
-- 0043  Review-sampling infrastructure for the Unlimited-Wings consumption
--       Isolation Forest (wt/anomaly.py's detect_consumption_anomalies()).
--
-- Mirrors anomaly_validation_sample (migration 0041) exactly in shape and
-- reasoning -- see that migration's own extensive comment for the full
-- rationale (flagged-only review cannot measure FN/TN; prediction and human
-- label must stay in separate columns; TP/FP/FN/TN are derived, never
-- stored directly; metrics are version-scoped). This is a SEPARATE,
-- independent table, not a shared/generic one: the stock-loss pass
-- evaluates ledger rows, this pass evaluates reconciled service-date x
-- stream days (keyed by unli_reconciliation.id), and the two questions a
-- human reviewer answers are genuinely different in kind (see wt/anomaly.py's
-- CONSUMPTION_HUMAN_LABELS and the API layer's actual review question).
--
-- Two differences from anomaly_validation_sample, both deliberate:
--
--   rule_flagged (new column, no stock-loss equivalent) -- 2026-10 audit
--   section 12: this pass's deterministic large_variance rule and its
--   IsolationForest can independently flag the same day. model_prediction
--   here is ALWAYS the forest's own verdict alone (sample_consumption_for_
--   review() never samples a rule-only catch as 'model_flagged' -- see that
--   function's docstring), so the derived TP/FP/FN/TN can never attribute
--   the rule's own correctness to the ML model. rule_flagged is preserved
--   purely for audit/context, never fed into the confusion matrix.
--
--   human_label values are ('pending','confirmed_anomaly',
--   'expected_or_explained') -- not 'expected_or_normal' (the stock-loss
--   wording) -- because the operational question differs: a stock
--   movement is either expected or it is not, but a measured consumption
--   variance is better answered as "expected, or explained by a real
--   operational reason" (e.g. a birthday party drove unusually high
--   consumption) -- still independent of the model's own prediction,
--   exactly like the stock-loss label, just worded for what a reviewer is
--   actually being asked to judge here.
-- =============================================================================

CREATE TABLE unli_consumption_validation_sample (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  reconciliation_id   INTEGER NOT NULL UNIQUE REFERENCES unli_reconciliation(id) ON DELETE RESTRICT,

  sample_source       TEXT NOT NULL CHECK (sample_source IN
                        ('model_flagged', 'near_threshold', 'random_unflagged')),
  model_prediction    TEXT NOT NULL CHECK (model_prediction IN ('flagged', 'unflagged')),
  -- Preserved for audit only -- see this migration's own comment above.
  -- Never read by scientific_consumption_validation_metrics()'s confusion
  -- matrix, which uses model_prediction alone.
  rule_flagged        INTEGER NOT NULL DEFAULT 0 CHECK (rule_flagged IN (0, 1)),
  anomaly_score       REAL,
  decision_threshold  REAL,

  model_version       TEXT NOT NULL,   -- anomaly.CONSUMPTION_MODEL_VERSION at evaluation time
  eligibility_version TEXT NOT NULL,   -- anomaly.CONSUMPTION_ELIGIBILITY_VERSION at evaluation time
  evaluated_at        TEXT NOT NULL DEFAULT (datetime('now')),

  human_label         TEXT NOT NULL DEFAULT 'pending'
                        CHECK (human_label IN ('pending', 'confirmed_anomaly', 'expected_or_explained')),
  reviewed_by         TEXT,
  reviewed_at         TEXT,
  review_note         TEXT,

  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX ix_unli_consumption_validation_sample_label
  ON unli_consumption_validation_sample (human_label);
CREATE INDEX ix_unli_consumption_validation_sample_version
  ON unli_consumption_validation_sample (model_version, eligibility_version);

INSERT INTO schema_version (version, name) VALUES (43, 'unli_consumption_validation_sample');
