-- =============================================================================
-- 0044  Per-flavour unlimited-wings reconciliation — the dimension the
--       existing unli_reconciliation table cannot represent.
--
-- unli_reconciliation (migration 0001) is keyed to unli_item_id, one of
-- exactly four fixed lambda streams (wings/rice/fries/drinks). It has no
-- flavour dimension and was never meant to: at the time it was designed,
-- nothing measured per-flavour consumption at all. This table adds that
-- dimension as a NEW, separate table rather than reshaping
-- unli_reconciliation itself — the existing table, its UNIQUE
-- (service_date, unli_item_id), and everything that already reads it
-- (wt/anomaly.py's v_unli_daily_consumption pass) stay exactly as they are.
--
-- Provenance follows the SAME structural convention anomaly.py already uses
-- for stock_ledger/unli_reconciliation (see wt/anomaly.py's
-- SYNTHETIC_SEED_USER_IDS / _REAL_USER_CLAUSE / _REAL_RECORDER_CLAUSE): a
-- row recorded by a sentinel username that is NOT a real app_user account is
-- structurally excluded from real-data ML eligibility, rather than trusting
-- a separate is_synthetic flag someone could forget to check. recorded_by
-- here plays exactly that role.
--
-- Deliberately does NOT persist source_isolation_forest_flag/
-- source_human_ground_truth as if they were this system's own model output
-- or genuine human review — a workbook-simulated verdict is neither. Those
-- two columns are kept ONLY as an audit trail of what the SOURCE SIMULATION
-- claimed, clearly prefixed source_, and are never read by wt/anomaly.py's
-- own detection/validation pipeline. A real flag/ground-truth pair for this
-- flavour-level dimension can only come from actually running detection
-- against this table and a real reviewer labelling the result — which is
-- future work, not something this migration or its importer fabricates.
--
-- Append-only, matching stock_ledger / anomaly_review / ingredient_variance_
-- review: a reconciliation observation is a record of a measurement, never
-- edited in place. source_ref carries the workbook's own row ID (e.g.
-- SIM-REC-000001) for import idempotency — see wt/simulated_workbook_import.py.
-- =============================================================================

CREATE TABLE unli_flavor_reconciliation (
  id                            INTEGER PRIMARY KEY AUTOINCREMENT,
  service_date                  TEXT    NOT NULL,
  flavor_id                     INTEGER NOT NULL REFERENCES unli_flavor(id) ON DELETE RESTRICT,
  heads                         INTEGER NOT NULL CHECK (heads >= 0),
  servings                      INTEGER NOT NULL CHECK (servings >= 0),
  wings_served_pcs              REAL    NOT NULL CHECK (wings_served_pcs >= 0),
  leftover_pcs                  REAL    NOT NULL CHECK (leftover_pcs >= 0),
  net_consumed_pcs              REAL    NOT NULL,
  actual_measured_base          REAL    NOT NULL,          -- in `unit`, the raw-ingredient quantity actually measured
  unit                          TEXT    NOT NULL CHECK (unit IN ('g','ml')),
  provisional_base              REAL,                       -- the provisional/theoretical figure this was reconciled against, if the source recorded one
  variance_base                 REAL,
  variance_ratio                REAL,
  deterministic_rule_flag       INTEGER NOT NULL DEFAULT 0 CHECK (deterministic_rule_flag IN (0,1)),
  observation_status            TEXT    NOT NULL DEFAULT 'reconciled'
                                  CHECK (observation_status IN ('reconciled','pending','rejected')),
  -- audit trail only — what the SOURCE simulation claimed, never this
  -- system's own detection/review output. See this migration's own header.
  source_isolation_forest_flag  TEXT,
  source_human_ground_truth     TEXT,
  source_evidence_note          TEXT,
  -- provenance: who/what recorded this row, structurally checked against
  -- app_user the same way stock_ledger.user_id already is (see header)
  recorded_by                   TEXT,
  -- idempotent re-import: the workbook's own row identifier (e.g.
  -- SIM-REC-000001), unique per import so a second run of the same
  -- workbook is a no-op rather than a duplicate row
  source_ref                    TEXT    UNIQUE,
  source_batch_id               TEXT,
  created_at                    TEXT    NOT NULL DEFAULT (datetime('now','subsec'))
);
CREATE INDEX ix_unli_flavor_recon_date   ON unli_flavor_reconciliation (service_date, flavor_id);
CREATE INDEX ix_unli_flavor_recon_batch  ON unli_flavor_reconciliation (source_batch_id);

CREATE TRIGGER trg_unli_flavor_recon_no_update
BEFORE UPDATE ON unli_flavor_reconciliation
BEGIN SELECT RAISE(ABORT, 'unli_flavor_reconciliation is append-only'); END;
CREATE TRIGGER trg_unli_flavor_recon_no_delete
BEFORE DELETE ON unli_flavor_reconciliation
BEGIN SELECT RAISE(ABORT, 'unli_flavor_reconciliation is append-only'); END;

INSERT INTO schema_version (version, name) VALUES (44, 'unli_flavor_reconciliation');
