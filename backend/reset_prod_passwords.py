"""
Run this yourself from backend/, against your REAL production database.
It resets the password for Administrator, business_owner, cashier1, and kitchen1
to the values you specified, using the app's own hash_password() so the format
matches exactly what wt/auth.py expects (pbkdf2_hmac-sha256, per-user salt).

Usage (from backend/), with WT_ENV/WT_DB set the same way the app itself
requires everywhere else:
    WT_ENV=production WT_DB=wings_theory.db python reset_prod_passwords.py

This deliberately goes through wt.db.connect()/db_path() rather than a bare
sqlite3.connect("wings_theory.db") -- a hardcoded relative path has no way to
tell a shell running from the wrong directory, or pointed at the demo/dev
database, that it is about to silently reset the WRONG file's accounts. The
WT_ENV<->WT_DB pairing check in wt/db.py (the same one every other entry
point into this database goes through) makes "ran this from the wrong
checkout" or "ran this against the demo database" a refusal, not a silent
write. A full on-disk backup is taken first (db.backup_now(), the same
pre-risky-change backup the migration runner takes) so the previous account
state can be restored. check_same_thread=False and busy_timeout keep this
one-off script from deadlocking against a running server holding the write
lock, the same as every other connection in this app.
"""
import sys
from wt import db
from wt.auth import hash_password, validate_new_password

RESETS = {
    "cashier1": "wingstheorycash",
    "kitchen1": "wingstheorykds",
    "Administrator": "wingstheoryadd",
    "business_owner": "ownerwingstheory",
}

def main():
    target = db.db_path()  # fails closed if WT_ENV/WT_DB are unset or mismatched
    print(f"targeting {target} (WT_ENV={db.environment()})")
    backup_path = db.backup_now("reset_prod_passwords")
    if backup_path:
        print(f"backed up to {backup_path}")

    conn = db.connect(target)
    try:
        with db.write_tx(conn):
            for username, new_password in RESETS.items():
                row = conn.execute(
                    "SELECT id FROM app_user WHERE username = ?", (username,)
                ).fetchone()
                if row is None:
                    print(f"SKIP {username}: no such user in {target}")
                    continue
                try:
                    validate_new_password(new_password, username=username)
                except Exception as e:
                    print(f"SKIP {username}: password rejected by validate_new_password() -- {e}")
                    continue
                password_hash, password_salt = hash_password(new_password)
                conn.execute(
                    """UPDATE app_user
                       SET password_hash = ?, password_salt = ?,
                           password_changed_at = datetime('now'),
                           must_change_password = 0,
                           failed_login_count = 0, locked_until = NULL
                       WHERE username = ?""",
                    (password_hash, password_salt, username),
                )
                print(f"OK {username}: password reset, account unlocked")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
