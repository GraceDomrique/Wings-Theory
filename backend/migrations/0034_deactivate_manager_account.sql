-- =============================================================================
-- 0034  Deactivate the legacy manager account.
--
-- The Manager role was already retired from active use: 'manager' cannot be
-- assigned to a NEW account (api.py's create_user refuses it), and System &
-- Settings already claimed the seeded manager account was "legacy — no
-- active account". That claim was false until this migration -- the seeded
-- manager/M. Santos row was still is_active=1 and could still log in.
--
-- This flips the one flag that makes the existing claim true. It does NOT:
--   - delete the account, its row, or any of its history
--   - change its role or any of its role_page_permission grants
--   - touch any other account
-- An admin can still see it in the System & Settings roster as an inactive
-- historical account (the roster UI already renders an "inactive" pill for
-- is_active=0 rows). Login for this account already requires is_active=1
-- (wt/api.py's /auth/login), so this alone blocks any future sign-in; no
-- other schema change is needed.
-- -----------------------------------------------------------------------------
UPDATE app_user SET is_active = 0 WHERE username = 'manager' AND role = 'manager';

INSERT INTO schema_version (version, name) VALUES (34, 'deactivate_manager_account');
