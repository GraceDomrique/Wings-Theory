-- =============================================================================
-- 0053  Drop the "at most one active OWNER" constraint added by migration
--       0027.
--
-- Business decision reversed (2026-10): Administration > Users & Access can
-- now provision additional OWNER accounts (see api.py's create_user, which
-- used to refuse role='owner' unconditionally and no longer does). Every
-- OWNER account holds the identical, fixed CAPABILITIES rows -- there was
-- never a separate "main owner" privilege tier for a second account to
-- threaten -- so capping the role at one active row is no longer the
-- invariant this system enforces.
--
-- wt/owner_provisioning.py's CLI-only provision_first_owner() keeps its own
-- in-code "only while zero owners exist" check unchanged: that tool's job is
-- narrowly the very first deployment bootstrap, before any admin account can
-- sign in to use the normal account-management endpoint at all. This
-- migration only removes the database-level backstop that would have
-- rejected a SECOND owner row regardless of which code path inserted it.
-- -----------------------------------------------------------------------------
DROP INDEX idx_app_user_one_active_owner;

INSERT INTO schema_version (version, name) VALUES (53, 'drop_single_active_owner_constraint');
