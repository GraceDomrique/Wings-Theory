-- =============================================================================
-- 0016  General admin-action audit trail, plus login-lockout counters.
--
-- Part 1: admin_audit_log
-- ------------------------
-- The 2026-08 audit found that verification_event (migration 0013) only
-- covers the six owner-verification fields (pack_size, yield_factor,
-- unit_cost, recipe_line.qty_base, fefo_batch.expires_at,
-- unli_coefficient.lambda) reached through the verification queue's
-- confirm/change/keep_estimate flow. Every OTHER sensitive mutation --
-- general ingredient field edits via PATCH /console/ingredients/{pk}
-- (supplier, reorder point, unit_cost via that path too), recipe creation,
-- menu price changes, and admin password resets -- wrote no durable record
-- of who did it or when. That is a direct gap against the requirement that
-- every sensitive action be attributable to a specific user with a
-- timestamp and an audit trail, so nobody can alter inventory or records
-- and blame another employee.
--
-- Same append-only pattern as stock_ledger, order_charge and
-- verification_event: a decision is a historical fact the moment it's
-- made, corrected by a new row, never by editing the old one. Kept as a
-- separate table rather than widening verification_event because
-- verification_event's field/row_id vocabulary is scoped to the six
-- verification-queue fields specifically (see 0013's own comment) --
-- entity_type/entity_id here is deliberately broader (ingredient, recipe,
-- menu_item, user) and not constrained to those six.
--
-- Part 2: login lockout counters
-- --------------------------------
-- auth.py's own threat model comment is explicit that this system does not
-- defend a public endpoint. It still defends against a lost/compromised
-- device or a LAN guest guessing a still-active seeded password (all four
-- real accounts share one password until an owner rotates them -- see
-- GO_LIVE_CHECKLIST.md) with no limit on attempts. failed_login_count and
-- locked_until are added with constant defaults (not CURRENT_TIMESTAMP)
-- for the same reason 0014 documents at length: SQLite refuses a
-- non-constant ADD COLUMN default against a populated table with foreign
-- keys on, which is exactly the shape of the real production app_user
-- table.
-- =============================================================================

CREATE TABLE admin_audit_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type    TEXT    NOT NULL,   -- 'ingredient' | 'recipe' | 'menu_item' | 'user'
  entity_id      TEXT    NOT NULL,   -- pk (as text) or username
  action         TEXT    NOT NULL,   -- 'edit' | 'create' | 'price_change' | 'password_reset'
  field          TEXT,               -- which column, for a single-field change; NULL for multi-field/create
  previous_value TEXT,
  new_value      TEXT,
  user_id        TEXT    NOT NULL,
  note           TEXT,
  created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_admin_audit_log_entity ON admin_audit_log(entity_type, entity_id);

CREATE TRIGGER trg_admin_audit_log_no_update BEFORE UPDATE ON admin_audit_log
BEGIN SELECT RAISE(ABORT, 'admin_audit_log is append-only'); END;
CREATE TRIGGER trg_admin_audit_log_no_delete BEFORE DELETE ON admin_audit_log
BEGIN SELECT RAISE(ABORT, 'admin_audit_log is append-only'); END;

ALTER TABLE app_user ADD COLUMN failed_login_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE app_user ADD COLUMN locked_until TEXT;

INSERT INTO schema_version (version, name) VALUES (16, 'admin_audit_log_and_login_lockout');
