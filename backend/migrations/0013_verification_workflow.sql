-- =============================================================================
-- 0013  Append-only audit trail for the owner verification workflow.
--
-- What this migration is, and is not
-- -----------------------------------
-- It is a pure schema addition: one new table, two guard triggers. No
-- existing table, column, view, or CHECK constraint changes. Nothing about
-- FEFO, reservation, the ledger, reconciliation, or POS deduction is touched.
--
-- Why a new table instead of new columns on ingredient/recipe_line/etc.
-- -----------------------------------------------------------------------
-- pack_size_status, yield_factor_status, unit_cost_status, qty_status and
-- unli_coefficient.status all already CHECK to exactly
-- ('verified','needs_verification','unset') -- there is no room in that
-- vocabulary for "the owner looked at this and chose to leave it as an
-- estimate for now", and widening the CHECK would touch five existing
-- constraints for a fact that belongs in a log, not a current-state column.
-- A "confirm" or "change" action already has a real place to land: the
-- field's own status flips to 'verified' exactly as /console/ingredients/
-- {pk}/verify already does today. What was missing was a durable record of
-- *every* decision -- including "keep as estimate", which touches no
-- existing status at all -- with the previous value preserved alongside the
-- new one. That is what verification_event is for: one append-only row per
-- decision, independent of and in addition to the field's own status.
-- =============================================================================

CREATE TABLE verification_event (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  field          TEXT    NOT NULL,   -- e.g. 'ingredient.pack_size', matches v_needs_verification.field
  row_id         INTEGER NOT NULL,   -- matches v_needs_verification.row_id for this field
  entity_name    TEXT    NOT NULL,
  action         TEXT    NOT NULL CHECK (action IN ('confirmed', 'changed', 'kept_estimate')),
  previous_value TEXT,
  new_value      TEXT,
  note           TEXT,
  user_id        TEXT,
  created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_verification_event_field_row ON verification_event(field, row_id);

-- Same append-only pattern as stock_ledger and order_charge: a verification
-- decision is a historical fact the moment it is made. Correcting a mistake
-- means recording a new event on top of it, not editing history.
CREATE TRIGGER trg_verification_event_no_update BEFORE UPDATE ON verification_event
BEGIN SELECT RAISE(ABORT, 'verification_event is append-only'); END;
CREATE TRIGGER trg_verification_event_no_delete BEFORE DELETE ON verification_event
BEGIN SELECT RAISE(ABORT, 'verification_event is append-only'); END;

INSERT INTO schema_version (version, name) VALUES (13, 'verification_workflow');
