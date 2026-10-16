-- =============================================================================
-- 0035  Anomaly review — human validation for the stock-loss Isolation
--       Forest pass (wt/anomaly.py's detect_anomalies()).
--
-- 2026-09 audit: the model can flag a ledger entry as anomalous, but nothing
-- in this schema ever recorded whether a human actually looked at that flag
-- and agreed or disagreed. Without that, Precision/Recall/F1 cannot be
-- computed honestly -- there is no ground truth to score against, only the
-- model's own opinion of itself. This table is that missing ground truth:
-- a reviewer's classification of ONE flagged ledger row, append-only, same
-- convention as ingredient_variance_review (migration 0033) and
-- verification_event (migration 0013) -- a reviewer who changes their mind
-- submits a NEW review row rather than editing the old one, so the full
-- history of what was believed and when is never lost.
--
-- Deliberately keyed to stock_ledger.id (the flagged row itself) rather than
-- duplicating the model's score/reason/method at review time -- those are
-- always recomputable from the same ledger row and the anomaly-detection
-- parameters recorded in wt/anomaly.py, so freezing a copy here would be a
-- second source of truth that could drift from the first.
--
-- A ledger row can accumulate more than one review over time (flagged ->
-- under_review -> confirmed_anomaly, say); its CURRENT status is the most
-- recent row for that ledger_id. See wt/anomaly.py's anomaly_review_status()
-- for how that "current status" read is done, and model_details()'s
-- validation-metrics gate for how confirmed_anomaly/false_positive rows
-- become Precision/Recall/F1 once enough of them exist.
--
-- Schema-only, no seed data: safe on a fresh install, no guard needed
-- against migration 0032's seeding conventions.
-- =============================================================================

CREATE TABLE anomaly_review (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  ledger_id          INTEGER NOT NULL REFERENCES stock_ledger(id) ON DELETE RESTRICT,
  status             TEXT    NOT NULL CHECK (status IN (
                       'flagged', 'under_review', 'confirmed_anomaly',
                       'false_positive', 'resolved')),
  note               TEXT,
  user_id            TEXT,
  -- Same idempotency convention as ingredient_variance_review.client_request_id
  -- (migration 0033): a retried submission of the exact same review is a
  -- no-op replay, not a second row.
  client_request_id  TEXT UNIQUE,
  created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_anomaly_review_ledger
  ON anomaly_review (ledger_id, created_at);

-- Append-only, matching stock_ledger / verification_event / ingredient_variance_review:
-- a review is a record of what a human concluded at that moment.
CREATE TRIGGER trg_anomaly_review_no_update
BEFORE UPDATE ON anomaly_review
BEGIN SELECT RAISE(ABORT, 'anomaly_review is append-only'); END;
CREATE TRIGGER trg_anomaly_review_no_delete
BEFORE DELETE ON anomaly_review
BEGIN SELECT RAISE(ABORT, 'anomaly_review is append-only'); END;
