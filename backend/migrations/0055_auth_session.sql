-- =============================================================================
-- 0055  Server-side session table for explicit logout / token revocation.
--
-- wt/auth.py's JWTs are stateless by design (see that file's own docstring) --
-- until now, the only way a token stopped working before its 12h TTL was a
-- password change (the 'pwd' claim check in api.principal()). There was no
-- way to invalidate a single session on demand, which meant "Sign out" was
-- purely a client-side localStorage.removeItem(): the bearer token itself
-- stayed valid and could still be replayed directly against the API for the
-- rest of its 12-hour life, including by someone who captured it before the
-- browser session ended.
--
-- Every token issued from here on (wt/auth.py's issue_token) carries a
-- random 'jti' claim. This table is the authority on whether that jti is
-- still good: a row with revoked_at IS NULL is a live session; POST
-- /auth/logout sets revoked_at on the caller's own jti; a password
-- change/reset revokes every other row for that username. api.principal()
-- checks this table on every request (see wt/sessions.py), so a revoked
-- token is rejected immediately, not merely "no longer offered" by the
-- client that logged out.
--
-- expires_at mirrors the token's own 'exp' claim so a cleanup pass
-- (wt/sessions.purge_expired) can drop rows nobody will ever check again
-- without needing to decode a token to find out.
-- -----------------------------------------------------------------------------
CREATE TABLE auth_session (
    jti         TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    role        TEXT NOT NULL,
    issued_at   TEXT NOT NULL DEFAULT (datetime('now','subsec')),
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT
);

CREATE INDEX ix_auth_session_username ON auth_session (username);
CREATE INDEX ix_auth_session_expires_at ON auth_session (expires_at);

INSERT INTO schema_version (version, name) VALUES (55, 'auth_session');
