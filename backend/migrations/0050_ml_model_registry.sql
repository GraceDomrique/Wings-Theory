-- =============================================================================
-- 0050  ML model registry, persisted training-run history, and the approved-
--       synthetic-batch gate that makes "train on the current synthetic
--       import" an explicit governance step rather than an assumption.
--
--       Backfills stock_ledger.source_batch_id for the synthetic rows
--       simulated_workbook_import.py already imported (batch
--       'simrun-20261012161127' -- the only generation that has ever
--       existed for this table; unli_flavor_reconciliation already carries
--       this column from migration 0044, stock_ledger never did). The
--       backfill UPDATE is scoped to rows carrying the sentinel user_id
--       only, and the append-only triggers are lifted/restored identically
--       around it -- the same lift/restore pattern
--       simulated_workbook_import.py's own _purge_previous_import() already
--       uses for exactly this reason.
--
--       Adds a read-only view that lets the generic (per-unli_item) daily
--       consumption pass train on the already-imported per-flavor
--       reconciliation data for the 'wings' stream, without a second write
--       path. heads is SUMMED across that day's flavor rows, not taken from
--       one row or MAX'd: validated against the live data before writing
--       this (heads differs across flavor rows on 362 of 365 service dates
--       -- it is a per-flavor diner subcount, not a value shared identically
--       across a day's rows). wt/anomaly.py's _consumption_records
--       anti-joins this view against real unli_reconciliation rows by
--       service_date before unioning, so a date is never read from both
--       places at once.
-- =============================================================================

ALTER TABLE stock_ledger ADD COLUMN source_batch_id TEXT;

DROP TRIGGER IF EXISTS trg_ledger_no_update;
DROP TRIGGER IF EXISTS trg_ledger_no_delete;

UPDATE stock_ledger
   SET source_batch_id = 'simrun-20261012161127'
 WHERE user_id = 'simulated_workbook_import'
   AND source_batch_id IS NULL;

CREATE TRIGGER trg_ledger_no_update BEFORE UPDATE ON stock_ledger
BEGIN SELECT RAISE(ABORT, 'stock_ledger is append-only'); END;
CREATE TRIGGER trg_ledger_no_delete BEFORE DELETE ON stock_ledger
BEGIN SELECT RAISE(ABORT, 'stock_ledger is append-only'); END;

CREATE TABLE ml_model_registry (
  model_key              TEXT PRIMARY KEY,
  display_name           TEXT NOT NULL,
  mode                   TEXT NOT NULL CHECK (mode IN ('SIMULATED','MIXED','REAL')),
  mode_locked            INTEGER NOT NULL DEFAULT 0 CHECK (mode_locked IN (0,1)),
  auto_promote           INTEGER NOT NULL DEFAULT 1 CHECK (auto_promote IN (0,1)),
  active_training_run_id INTEGER,
  updated_by             TEXT,
  updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE ml_training_run (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  model_key       TEXT NOT NULL REFERENCES ml_model_registry(model_key),
  trained_at      TEXT NOT NULL DEFAULT (datetime('now')),
  mode            TEXT NOT NULL CHECK (mode IN ('SIMULATED','MIXED','REAL')),
  dataset_version TEXT NOT NULL,
  real_count      INTEGER NOT NULL,
  synthetic_count INTEGER NOT NULL,
  schema_version  TEXT NOT NULL,
  metrics_json    TEXT NOT NULL,
  artifact_path   TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('active','superseded','rolled_back','failed')),
  triggered_by    TEXT NOT NULL,
  notes           TEXT
);
CREATE INDEX ix_ml_training_run_model ON ml_training_run (model_key, trained_at DESC);

-- SQLite can't add a REFERENCES constraint to an existing column, so the
-- registry->run back-reference is enforced in application code
-- (model_registry.activate_training_run) rather than at the schema level --
-- consistent with how prophet_refresh_status (app_setting) has no FK either.

CREATE TABLE ml_approved_synthetic_batch (
  batch_id    TEXT PRIMARY KEY,
  approved_by TEXT NOT NULL,
  approved_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- No batch is pre-approved here, and no model starts in SIMULATED/MIXED by
-- this migration -- both are deliberately NOT baked into generic schema.
-- ml_approved_synthetic_batch and ml_model_registry.mode describe ONE
-- specific deployment's data (which synthetic import is currently approved,
-- and which models should therefore start reading it) -- a migration is the
-- wrong place for that, since it runs identically against every database
-- this schema is ever applied to: the live production DB (which legitimately
-- has the simrun-20261012161127 batch imported and approved), but also
-- every fresh dev/test/demo database, which has none of that data and must
-- not claim a batch is "approved" when nothing with that tag exists there.
-- All three anomaly models seed REAL -- the same unconditional real-only
-- filtering this codebase has always used -- so this migration changes
-- nothing observable for any database that has no synthetic data yet
-- (confirmed: every pre-existing test in tests/run_tests.py that exercises
-- detect_anomalies()/detect_consumption_anomalies()/
-- detect_flavor_consumption_anomalies() against real/NULL-provenance
-- fixture rows passed unchanged under this seed). The live production
-- database's actual SIMULATED-mode/approved-batch state (already trained
-- and verified) was applied directly, once, as the one-time operational
-- action it actually is: an INSERT into ml_approved_synthetic_batch for
-- that one real batch_id, followed by the ordinary
-- wt.ml_training.train_one(conn, model_key, mode='SIMULATED', ...) path --
-- not through this migration.
INSERT INTO ml_model_registry (model_key, display_name, mode, mode_locked, auto_promote) VALUES
  ('stock_loss_isoforest',        'Stock-loss anomaly (IsolationForest, Pass A)',          'REAL', 0, 1),
  ('unli_consumption_isoforest',  'Unli consumption anomaly (IsolationForest, Pass B)',    'REAL', 0, 1),
  ('flavor_consumption_isoforest','Flavor consumption anomaly (IsolationForest, Pass C)',  'REAL', 0, 1),
  ('demand_prophet',              'Demand forecasting (Prophet/baseline/hybrid)',          'REAL', 1, 1),
  ('fpgrowth_combos',             'Menu combo association mining (FP-Growth)',             'REAL', 1, 1);

CREATE VIEW v_unli_wings_daily_from_flavor AS
SELECT service_date,
       (SELECT id FROM unli_item WHERE item_key = 'wings') AS unli_item_id,
       SUM(heads)                                       AS heads,
       SUM(provisional_base)                            AS theoretical_base,
       SUM(actual_measured_base)                        AS actual_base,
       SUM(actual_measured_base) - SUM(provisional_base) AS variance_base,
       CASE WHEN SUM(provisional_base) > 0
            THEN (SUM(actual_measured_base) - SUM(provisional_base)) / SUM(provisional_base)
            END                                          AS variance_ratio,
       recorded_by,
       source_batch_id
  FROM unli_flavor_reconciliation
 WHERE observation_status <> 'rejected'
 GROUP BY service_date, recorded_by, source_batch_id;
