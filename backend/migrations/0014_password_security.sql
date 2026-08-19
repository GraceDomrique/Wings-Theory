-- Phase 09A P0-2 — support for the password-change/reset mechanism in
-- wt/auth.py and the new /auth/change_password and
-- /console/users/{username}/reset_password endpoints.
--
-- password_changed_at is embedded as a claim in every JWT issued from now
-- on (see auth.issue_token/api.principal) so a token minted before a
-- password change stops being accepted the moment the change lands,
-- instead of remaining valid for up to its full 12-hour TTL.
--
-- Added nullable, with no column-level default, then backfilled by the
-- UPDATE below -- NOT a NOT NULL DEFAULT CURRENT_TIMESTAMP column, even
-- though SQLite's docs describe that as legal for ADD COLUMN. It IS legal
-- against an empty table (which is all this project's own test suite ever
-- exercises, since fresh_db() migrates before any row exists), but SQLite
-- refuses it with "Cannot add a column with non-constant default" against
-- a table that already has rows AND has foreign-key enforcement on
-- (PRAGMA foreign_keys=ON, which every connection in this project sets) --
-- exactly the shape of the real, already-seeded production app_user table.
-- Caught by testing this migration against a disposable copy of the real
-- production database, not by the test suite (which only ever migrates
-- empty databases). See wt/seed.py's seed_users(), which now sets this
-- column explicitly on every INSERT rather than relying on a schema
-- default, so no code path in this project ever leaves it NULL even
-- though the column itself does not enforce NOT NULL.
--
-- 'subsec' (millisecond precision) everywhere this column is written, not
-- bare datetime('now') (whole seconds only): the token-freshness check in
-- api.principal compares this column's value against a token's own 'pwd'
-- claim for exact equality, and a login immediately followed by a
-- password change within the same second -- routine in an automated test
-- suite, and not impossible for a real user double-clicking -- would
-- otherwise stamp both with an identical value and the "old" token would
-- wrongly keep working.
ALTER TABLE app_user ADD COLUMN password_changed_at TEXT;
UPDATE app_user SET password_changed_at = datetime('now', 'subsec') WHERE password_changed_at IS NULL;

-- A genuine constant default (unlike CURRENT_TIMESTAMP above), so this one
-- is unaffected by the same restriction and can keep NOT NULL.
--
-- Data only, not an enforced gate: nothing in the backend blocks a login
-- or a request because it is set. It exists so the go-live action list
-- (which accounts still carry the shared seed password) can be read
-- directly out of the database, and so the flag clears itself the moment
-- an account's password is actually changed via either endpoint. The four
-- accounts seed.py creates today all still carry the shared 'wingstheory'
-- password at the time this migration runs, so they are flagged here.
ALTER TABLE app_user ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;

UPDATE app_user SET must_change_password = 1
 WHERE username IN ('owner', 'manager', 'cashier1', 'kitchen1');
