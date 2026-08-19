-- Phase 09A P0-2 — support for the password-change/reset mechanism in
-- wt/auth.py and the new /auth/change_password and
-- /console/users/{username}/reset_password endpoints.
--
-- password_changed_at is embedded as a claim in every JWT issued from now
-- on (see auth.issue_token/api.principal) so a token minted before a
-- password change stops being accepted the moment the change lands,
-- instead of remaining valid for up to its full 12-hour TTL. ADD COLUMN
-- cannot take a non-constant default (datetime('now') is rejected by
-- SQLite there), so CURRENT_TIMESTAMP -- the one non-constant default
-- SQLite does allow on ADD COLUMN, and the same UTC format datetime('now')
-- already produces elsewhere in this schema -- is used instead.
--
-- must_change_password is data only, not an enforced gate: nothing in the
-- backend blocks a login or a request because it is set. It exists so the
-- go-live action list (which accounts still carry the shared seed
-- password) can be read directly out of the database, and so the flag
-- clears itself the moment an account's password is actually changed via
-- either endpoint. The four accounts seed.py creates today all still carry
-- the shared 'wingstheory' password at the time this migration runs, so
-- they are flagged here.
ALTER TABLE app_user ADD COLUMN password_changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE app_user ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;

UPDATE app_user SET must_change_password = 1
 WHERE username IN ('owner', 'manager', 'cashier1', 'kitchen1');
