-- =============================================================================
-- 0049  Unlimited-item reconciliation revisions — the 2026-10 audit found that
--       record_reconciliation() (wt/unli.py) can run for a service_date
--       before every order for that day has committed, and that
--       UNIQUE(service_date, unli_item_id) on unli_reconciliation then
--       permanently refuses any later attempt to bring that day's figures up
--       to date — a late-committed order's own provisional deduction is
--       still correct (it happens independently at that order's own
--       commit), but the day's reconciled VARIANCE ANALYSIS silently
--       undercounts it forever.
--
-- This table is the smallest safe fix: it never touches unli_reconciliation
-- or its existing UNIQUE constraint (every pre-existing row, test, and
-- report keeps meaning exactly what it always meant). A revision is a new,
-- separate, append-only row that references the ORIGINAL reconciliation it
-- revises; wt/unli.revise_reconciliation() always recomputes theoretical_base
-- fresh from provisional_for_date() (so a late-committed order is now
-- included) and posts only the INCREMENTAL correction beyond what has
-- already been applied across the original row and any prior revisions —
-- never the full theoretical-vs-actual figure again, which is what would
-- double-deduct. client_request_id gives it the same idempotent-retry
-- guarantee every other mutating write in this codebase already has.
-- =============================================================================

CREATE TABLE unli_reconciliation_revision (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  reconciliation_id        INTEGER NOT NULL REFERENCES unli_reconciliation(id),
  heads                    INTEGER NOT NULL,
  theoretical_base         REAL    NOT NULL,
  actual_base              REAL    NOT NULL,
  variance_base            REAL    NOT NULL,
  variance_ratio           REAL,
  opening_base             REAL    NOT NULL,
  added_base               REAL    NOT NULL,
  closing_base             REAL    NOT NULL,
  tolerance_ratio          REAL    NOT NULL,
  status                   TEXT    NOT NULL,
  validation_state         TEXT    NOT NULL,
  -- The incremental correction THIS revision posted to stock_ledger/
  -- ingredient.on_hand_base — never the full theoretical-actual gap again.
  correction_base          REAL    NOT NULL,
  -- Running total of every correction applied so far for this original
  -- reconciliation (the original row's own correction_base + every prior
  -- revision's correction_base) — the figure the NEXT revision, if any,
  -- subtracts itself against.
  cumulative_correction_base REAL  NOT NULL,
  ledger_id                INTEGER REFERENCES stock_ledger(id),
  client_request_id        TEXT UNIQUE,
  recorded_by              TEXT,
  note                     TEXT,
  created_at                TEXT   NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_unli_reconciliation_revision_recon
  ON unli_reconciliation_revision (reconciliation_id, created_at);
