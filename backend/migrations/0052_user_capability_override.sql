-- =============================================================================
-- 0052  Per-user capability overrides.
--
-- Background: wt/auth.CAPABILITIES grants/denies a capability per ROLE, and
-- that table is a deliberate, audited policy (see its own 2026-08/09 RBAC
-- audit comments) -- this migration does not touch it or weaken it as a
-- default. What it adds is an explicit, admin-editable EXCEPTION layer on
-- top: Administration > Users & Access can now grant or revoke one specific
-- capability for one specific account, without changing what every other
-- account with that same role can do.
--
-- Absence of a row means "use the role default", same convention
-- role_page_permission (migration 0023) already established for page
-- visibility -- this is the equivalent for individual action capabilities.
-- A row with allowed=1 grants a capability that account's role wouldn't
-- normally have; allowed=0 revokes one it would. See wt/user_permissions.py
-- for the read/write helpers and api.py's needs() for enforcement.
-- -----------------------------------------------------------------------------
CREATE TABLE user_capability_override (
    username   TEXT NOT NULL REFERENCES app_user(username),
    capability TEXT NOT NULL,
    allowed    INTEGER NOT NULL CHECK (allowed IN (0,1)),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','subsec')),
    updated_by TEXT,
    PRIMARY KEY (username, capability)
);

INSERT INTO schema_version (version, name) VALUES (52, 'user_capability_override');
