-- =============================================================================
-- 0017  Durable login audit trail (login_event).
--
-- 2026-08 audit: migration 0016 added failed_login_count/locked_until, a
-- COUNTER used to decide whether to lock an account -- it answers "should
-- this attempt be refused right now", and gets overwritten on the very
-- next attempt either way. It does not answer "who signed in, when, from
-- which account, and did it succeed" as a durable record, which is the
-- explicit requirement: successful AND failed login attempts, with
-- identity, role and time, are supposed to be attributable after the
-- fact -- not just gate the next request.
--
-- Same append-only pattern as admin_audit_log, stock_ledger, and
-- verification_event: a login attempt is a historical fact the instant it
-- happens, never corrected in place.
--
-- 'logout or session end' is deliberately NOT a column here: auth.py
-- issues a stateless bearer JWT with no server-side session record (see
-- decode_token's own docstring) -- there is no reliable server-observed
-- moment when a session "ends" short of its 12-hour TOKEN_TTL expiring on
-- its own, which is not an event, it's the absence of one. Logging a
-- fabricated "logout" the client merely claims happened (a button click
-- with no guarantee the request ever reaches the server, e.g. the tab was
-- just closed) would be a record of unknown reliability presented as fact
-- -- worse than not recording it. See wt/audit.py's log_login docstring.
-- =============================================================================

CREATE TABLE login_event (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  username    TEXT    NOT NULL,   -- as typed; may not match a real account (unknown_user)
  role        TEXT,               -- the account's role at login time; NULL if the username didn't resolve
  success     INTEGER NOT NULL CHECK (success IN (0,1)),
  reason      TEXT,               -- NULL on success; 'invalid_credentials' | 'unknown_user' | 'locked_out' on failure
  created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_login_event_username ON login_event(username, created_at);
CREATE INDEX idx_login_event_created  ON login_event(created_at);

CREATE TRIGGER trg_login_event_no_update BEFORE UPDATE ON login_event
BEGIN SELECT RAISE(ABORT, 'login_event is append-only'); END;
CREATE TRIGGER trg_login_event_no_delete BEFORE DELETE ON login_event
BEGIN SELECT RAISE(ABORT, 'login_event is append-only'); END;

INSERT INTO schema_version (version, name) VALUES (17, 'login_event_log');
