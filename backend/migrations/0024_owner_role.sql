-- =============================================================================
-- 0024  Add 'owner' as a genuine fifth role.
--
-- 'admin' already holds full system authority and 'manager' is already
-- (2026-09 RBAC revision, see wt/auth.py) a pure read-only monitoring role.
-- The business wants a distinct executive role, read-only across every
-- existing operational capability, with exactly two new write capabilities
-- designed specifically for it (instruction.create, purchase_order.create --
-- see migrations 0025/0026 and wt/auth.py's CAPABILITIES table). This is
-- purely additive: no account is renamed, reseeded, or has its role changed,
-- and no 'owner' account is created here -- see the new POST /console/users
-- endpoint for how one gets provisioned afterward.
--
-- SQLite cannot ALTER a CHECK constraint in place, so app_user is rebuilt
-- under the same name, exactly the pattern migration 0005 already used for
-- stock_ledger: create under a new name with every existing column plus the
-- widened CHECK, copy every row verbatim, drop the old table, rename. No
-- other table has a foreign key on app_user (user_id elsewhere is a plain
-- TEXT username, not an FK), so this carries no cascade risk.
-- -----------------------------------------------------------------------------
CREATE TABLE app_user_new (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  username              TEXT    NOT NULL UNIQUE,
  display_name          TEXT    NOT NULL,
  role                  TEXT    NOT NULL CHECK (role IN ('admin','manager','cashier','kitchen','owner')),
  password_hash         TEXT    NOT NULL,
  password_salt         TEXT    NOT NULL,
  is_active             INTEGER NOT NULL DEFAULT 1,
  created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
  password_changed_at   TEXT,
  must_change_password  INTEGER NOT NULL DEFAULT 0,
  failed_login_count    INTEGER NOT NULL DEFAULT 0,
  locked_until          TEXT
);

INSERT INTO app_user_new
  (id, username, display_name, role, password_hash, password_salt, is_active,
   created_at, password_changed_at, must_change_password, failed_login_count, locked_until)
  SELECT id, username, display_name, role, password_hash, password_salt, is_active,
         created_at, password_changed_at, must_change_password, failed_login_count, locked_until
    FROM app_user;

DROP TABLE app_user;
ALTER TABLE app_user_new RENAME TO app_user;

INSERT INTO schema_version (version, name) VALUES (24, 'owner_role');
