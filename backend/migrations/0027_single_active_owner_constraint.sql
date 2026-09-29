-- =============================================================================
-- 0027  Enforce at most one active OWNER account at the database level.
--
-- wt/owner_provisioning.py's check-then-insert already runs inside a single
-- BEGIN IMMEDIATE write transaction (db.write_tx). SQLite serializes writers
-- by file lock across every connection and process touching this database,
-- so that alone already prevents two concurrent provisioning attempts from
-- both succeeding: whichever acquires the lock second re-reads current state
-- before it inserts anything, and sees the first attempt's committed row.
--
-- This index is the backstop underneath that transaction, not a replacement
-- for it: a partial UNIQUE index that makes "more than one active owner" a
-- constraint violation the database itself refuses, independent of whether
-- every future code path that might ever write to app_user remembers to
-- take the same lock/check discipline.
--
-- Partial (WHERE role='owner' AND is_active=1), not a plain UNIQUE(role):
-- every other role already has, and is expected to keep having, more than
-- one active account (multiple cashiers, kitchen staff, etc.) -- only
-- 'owner' is capped at one.
--
-- Purely additive: no existing row is touched or renamed, and today's
-- production data (zero role='owner' rows) already satisfies this
-- constraint trivially, so this migration cannot fail against it.
-- -----------------------------------------------------------------------------
CREATE UNIQUE INDEX idx_app_user_one_active_owner
  ON app_user(role)
  WHERE role = 'owner' AND is_active = 1;

INSERT INTO schema_version (version, name) VALUES (27, 'single_active_owner_constraint');
