-- =============================================================================
-- 0056  POS / Kitchen system lock.
--
-- Lets an admin close the POS (cashier) or Kitchen (KDS) station from the
-- console -- e.g. end of shift, a device being reset, an incident -- so that
-- nobody can sign in to that station, even with a correct password, until an
-- admin reopens it. This is deliberately NOT a per-user account lock
-- (app_user.locked_until, auth.py's failed-attempt lockout): it is a single
-- station-wide switch an admin can throw/release in one action, independent
-- of any individual account's credentials or lockout state. Admin and owner
-- console sign-in is never affected -- only the 'cashier' (POS) and
-- 'kitchen' (KDS) roles are gated by this at /auth/login. See
-- wt/system_lock.py and the login() check in wt/api.py.
--
-- One row per station, not a generic key/value table: two known stations,
-- a fixed, readable shape beats a string-keyed store for something this
-- small and this security-relevant.
-- -----------------------------------------------------------------------------
CREATE TABLE system_lock (
    station    TEXT PRIMARY KEY CHECK (station IN ('pos', 'kitchen')),
    closed     INTEGER NOT NULL DEFAULT 0 CHECK (closed IN (0,1)),
    closed_at  TEXT,
    closed_by  TEXT,
    reason     TEXT
);

INSERT INTO system_lock (station, closed) VALUES ('pos', 0), ('kitchen', 0);

INSERT INTO schema_version (version, name) VALUES (56, 'system_lock');
