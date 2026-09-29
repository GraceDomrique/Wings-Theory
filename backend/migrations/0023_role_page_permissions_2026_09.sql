-- =============================================================================
-- 0023  Admin-configurable page visibility for the console sidebar.
--
-- Background: the manager permission spec calls for two things the console
-- did not have -- a simplified, read-only manager experience (frontend-only,
-- no schema change needed for that part) and an Administrator-facing toggle
-- for which console PAGES a role can reach at all (distinct from
-- wt/auth.py's CAPABILITIES, which governs individual read/write ACTIONS and
-- is staying exactly as the 2026-08/09 RBAC audit left it).
--
-- Scope is deliberately narrow: two page keys ('verify', 'system'), one role
-- that needs them ('manager' -- 'admin' is always fully granted and is never
-- stored here; 'cashier'/'kitchen' don't use this console at all, see
-- ALLOWED_ROLES in the frontend). See wt/permissions.py for the read/write
-- helpers and wt/auth.py's new 'permissions.manage' capability (admin-only)
-- that gates changing this table.
--
-- Absence of a row is itself meaningful and is NOT backfilled here: it means
-- "use the system default" (wt/permissions.DEFAULT_GRANTS), which today is
-- "not granted" for both keys -- matching the spec's "hidden unless the
-- administrator has explicitly granted that permission". A row only ever
-- exists once an admin has actually made an explicit choice through the new
-- Role & Permissions UI.
-- -----------------------------------------------------------------------------
CREATE TABLE role_page_permission (
    role       TEXT NOT NULL CHECK (role IN ('admin','manager','cashier','kitchen')),
    page_key   TEXT NOT NULL,
    allowed    INTEGER NOT NULL CHECK (allowed IN (0,1)),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','subsec')),
    updated_by TEXT,
    PRIMARY KEY (role, page_key)
);

INSERT INTO schema_version (version, name) VALUES (23, 'role_page_permissions_2026_09');
