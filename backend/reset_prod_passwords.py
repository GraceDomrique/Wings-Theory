"""
Run this yourself from backend/, against your REAL production database.
It resets the password for Administrator, business_owner, cashier1, and kitchen1
to the values you specified, using the app's own hash_password() so the format
matches exactly what wt/auth.py expects (pbkdf2_hmac-sha256, per-user salt).

Usage (from backend/):
    python reset_prod_passwords.py

It targets wings_theory.db in the current directory. Double-check that path
before running -- this writes directly to your live database.
"""
import sqlite3
import sys
from wt.auth import hash_password, validate_new_password

DB_PATH = "wings_theory.db"

RESETS = {
    "cashier1": "wingstheorycash",
    "kitchen1": "wingstheorykds",
    "Administrator": "wingstheoryadd",
    "business_owner": "ownerwingstheory",
}

def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    for username, new_password in RESETS.items():
        cur.execute("SELECT id FROM app_user WHERE username = ?", (username,))
        row = cur.fetchone()
        if row is None:
            print(f"SKIP {username}: no such user in {DB_PATH}")
            continue
        try:
            validate_new_password(new_password, username=username)
        except Exception as e:
            print(f"SKIP {username}: password rejected by validate_new_password() -- {e}")
            continue
        password_hash, password_salt = hash_password(new_password)
        cur.execute(
            """UPDATE app_user
               SET password_hash = ?, password_salt = ?,
                   password_changed_at = datetime('now'),
                   must_change_password = 0,
                   failed_login_count = 0, locked_until = NULL
               WHERE username = ?""",
            (password_hash, password_salt, username),
        )
        print(f"OK {username}: password reset, account unlocked")
    conn.commit()
    conn.close()

if __name__ == "__main__":
    main()
