-- =============================================================================
-- 0045  Review-sampling infrastructure for the flavor-level Unlimited-Wings
--       consumption Isolation Forest pass (wt/anomaly.py's
--       detect_flavor_consumption_anomalies(), migration 0044's
--       unli_flavor_reconciliation).
--
-- Mirrors unli_consumption_validation_sample (migration 0043) in shape and
-- reasoning exactly -- model_prediction and human_label stay in separate
-- columns, TP/FP/FN/TN are derived never stored, sample_source records why
-- a row entered the queue, model_version/eligibility_version/anomaly_score/
-- decision_threshold are frozen at evaluation time. See that migration's
-- own extensive comment for the shared rationale, not repeated here.
--
-- Two differences, both deliberate:
--
--   human_label has FOUR values, not two -- CONFIRMED_ANOMALY, FALSE_
--   POSITIVE, EXPECTED_OPERATIONAL_VARIATION, INSUFFICIENT_INFORMATION
--   (lowercase_with_underscores here, matching this schema's own
--   convention) -- a reviewer judging a flavor-level day genuinely has a
--   fourth answer the stream-level questions don't: "I cannot tell from
--   what's recorded" (no note, no POS cross-check, whatever). Forcing that
--   reviewer into confirmed/false-positive/explained would manufacture a
--   confident label where none exists, polluting the eventual confusion
--   matrix with a guess. TP/FP/FN/TN in scientific_flavor_validation_
--   metrics() are computed ONLY from confirmed_anomaly/false_positive --
--   expected_operational_variation and insufficient_information rows are
--   retained (full audit trail, see 'mutable, not append-only' below) but
--   excluded from the derived confusion matrix the same way 'pending' is.
--
--   rule_causes (new column, no stream-level equivalent) -- the flavor
--   pass's rule layer can have MULTIPLE simultaneous causes (large_
--   variance, large_leftover_ratio, abnormal_flavor_share all independent
--   checks -- see wt/anomaly.py's rule_causes list), not one boolean. Text,
--   not a new table: a frozen comma-joined snapshot of which rule(s) fired
--   at evaluation time, for audit/context only, exactly as rule_flagged is
--   on the stream-level table -- never read by the confusion matrix, which
--   uses model_prediction (the forest's own verdict) alone.
--
--   is_primary / secondary_of_reconciliation_id (new columns) -- 2026-10
--   compositional-flavor-share finding: because all flavors' shares sum to
--   1, one genuinely abnormal flavor mechanically shifts its peers' shares
--   the same day. detect_flavor_consumption_anomalies() attributes ONE
--   primary (root-cause) row per event day and marks any purely
--   share-compositional peer flags as secondary, pointing back at the
--   primary's reconciliation_id -- frozen here at sampling time for the
--   same reproducibility reason model_version is. A secondary row is NOT
--   excluded from review (a reviewer may legitimately disagree with the
--   attribution), just labeled, so the confusion matrix stays row-level
--   and honest while the console can still show one grouped alert.
-- =============================================================================

CREATE TABLE unli_flavor_validation_sample (
  id                            INTEGER PRIMARY KEY AUTOINCREMENT,
  reconciliation_id             INTEGER NOT NULL UNIQUE
                                  REFERENCES unli_flavor_reconciliation(id) ON DELETE RESTRICT,

  sample_source                 TEXT NOT NULL CHECK (sample_source IN
                                  ('model_flagged', 'near_threshold', 'random_unflagged')),
  model_prediction               TEXT NOT NULL CHECK (model_prediction IN ('flagged', 'unflagged')),
  -- Preserved for audit only -- see this migration's own comment above.
  -- Never read by scientific_flavor_validation_metrics()'s confusion
  -- matrix, which uses model_prediction alone.
  rule_flagged                  INTEGER NOT NULL DEFAULT 0 CHECK (rule_flagged IN (0, 1)),
  rule_causes                   TEXT,
  anomaly_score                 REAL,
  decision_threshold             REAL,

  is_primary                    INTEGER CHECK (is_primary IN (0, 1)),
  secondary_of_reconciliation_id INTEGER REFERENCES unli_flavor_reconciliation(id) ON DELETE SET NULL,

  model_version                 TEXT NOT NULL,   -- anomaly.FLAVOR_MODEL_VERSION at evaluation time
  eligibility_version           TEXT NOT NULL,   -- anomaly.FLAVOR_ELIGIBILITY_VERSION at evaluation time
  evaluated_at                  TEXT NOT NULL DEFAULT (datetime('now')),

  human_label                   TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (human_label IN (
                                    'pending', 'confirmed_anomaly', 'false_positive',
                                    'expected_operational_variation', 'insufficient_information'
                                  )),
  reviewed_by                   TEXT,
  reviewed_at                   TEXT,
  review_note                   TEXT,

  created_at                    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX ix_unli_flavor_validation_sample_label
  ON unli_flavor_validation_sample (human_label);
CREATE INDEX ix_unli_flavor_validation_sample_version
  ON unli_flavor_validation_sample (model_version, eligibility_version);

INSERT INTO schema_version (version, name) VALUES (45, 'unli_flavor_validation_sample');
