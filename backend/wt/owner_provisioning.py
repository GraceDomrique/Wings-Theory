"""Deployment-only, one-time provisioning of the single OWNER account.

OWNER represents the business owner/CEO, not a staff role ADMIN routinely
provisions -- see wt/api.py's create_user(), which flatly refuses
role='owner' regardless of caller or whether one already exists. The only
way an OWNER account comes into being is a deployment operator running this
module directly against the server's own database file:

    python -m wt.owner_provisioning --username <u> --display-name <d>

That is the same trust boundary already required to run a migration or
restore a backup -- filesystem/process access on the machine the backend
runs on -- not an HTTP endpoint, so it carries no additional network attack
surface and is never reachable from a browser, the Admin console, or a
crafted API request.

Mirrors create_user()'s one-time-secret contract exactly: a random,
server-generated temporary password, hashed with the same PBKDF2 path as
every other account (auth.hash_password), returned exactly once to the
operator's terminal and never stored, logged, or echoed anywhere else.
must_change_password=1 so the real owner sets their own password on first
sign-in, same as every other provisioned account.

Refuses -- with nothing written -- if an active OWNER already exists or the
requested username is taken. There is exactly one active OWNER for this
deployment; this module enforces that invariant and does nothing else. It
does not rename, promote, or otherwise touch any existing account (including
a username that happens to be 'owner' but holds a different role) --
ownership transfer, if ever needed, is a separate, deliberately unbuilt
administrative operation.

Concurrency: the "no owner exists yet" check and the INSERT that follows it
run inside one db.write_tx -- a single BEGIN IMMEDIATE transaction, which
takes SQLite's exclusive write lock immediately rather than on first write.
That serializes every connection and process touching this database file: if
two provisioning attempts race, the second's BEGIN IMMEDIATE blocks (up to
PRAGMA busy_timeout) until the first commits or rolls back, then runs its own
check against the now-current, fully-committed state -- so it is impossible
for both to observe "no owner" and both insert one. Migration 0027's partial
UNIQUE index (role='owner' AND is_active=1) is the backstop underneath that:
even if some future code path ever wrote to app_user outside this function's
own lock/check discipline, the database itself refuses a second active
owner, converted below into the same InvalidStateError this function already
raises for the expected case.
"""
from __future__ import annotations

import argparse
import sqlite3

from . import audit, auth, db
from .errors import InvalidStateError, ValidationError

# Attributed actor for admin_audit_log when there is no authenticated HTTP
# caller to name -- this runs before any account, including the owner's own,
# has ever signed in. audit.log() requires a non-None user_id to write a row.
DEPLOYMENT_ACTOR = "deployment-cli"


_OWNER_UNIQUENESS_MARKERS = ("idx_app_user_one_active_owner", "app_user.role")


def provision_first_owner(conn: sqlite3.Connection, username: str, display_name: str) -> dict:
    """Creates the single OWNER account. Raises InvalidStateError (no write)
    if an active OWNER already exists, or ValidationError (no write) for a
    missing field or a username already in use. Returns the one-time
    temporary password; callers must surface it exactly once and never
    persist it themselves.

    The existence check and the INSERT happen inside the same write
    transaction (see module docstring) — this is what makes the guarantee
    hold under concurrent callers, not merely "check, then insert" as two
    separate steps."""
    if not isinstance(username, str) or not username.strip():
        raise ValidationError("username is required")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValidationError("display name is required")
    username = username.strip()
    display_name = display_name.strip()

    # Hashed before the transaction opens: PBKDF2 at auth.ITERATIONS is real
    # CPU work, and this exclusive write lock also blocks the live POS/KDS
    # write path (see db.py's write_tx docstring) -- nothing that doesn't
    # need the lock should be done while holding it.
    temp_password = auth.generate_temp_password()
    h, s = auth.hash_password(temp_password)

    with db.write_tx(conn):
        if conn.execute("SELECT 1 FROM app_user WHERE role='owner' AND is_active=1").fetchone():
            raise InvalidStateError(
                "an active OWNER account already exists — refusing to create another"
            )
        if conn.execute("SELECT 1 FROM app_user WHERE username=?", (username,)).fetchone():
            raise ValidationError(f"username '{username}' is already in use")
        try:
            conn.execute(
                "INSERT INTO app_user "
                "(username, display_name, role, password_hash, password_salt, "
                "password_changed_at, must_change_password) "
                "VALUES (?,?,?,?,?, datetime('now','subsec'), 1)",
                (username, display_name, "owner", h, s),
            )
        except sqlite3.IntegrityError as e:
            # Unreachable via this function's own two SELECTs above under
            # normal operation (BEGIN IMMEDIATE already serializes them with
            # the INSERT) -- this only fires if some other writer bypassed
            # this function entirely. Kept as a typed, no-write failure
            # rather than letting a raw IntegrityError escape as a 500.
            if any(marker in str(e) for marker in _OWNER_UNIQUENESS_MARKERS):
                raise InvalidStateError(
                    "an active OWNER account already exists — refusing to create another"
                ) from e
            raise ValidationError(f"username '{username}' is already in use") from e
        audit.log(
            conn, "user", username, "create_owner_bootstrap", DEPLOYMENT_ACTOR,
            new_value="owner", note="deployment-only initial OWNER provisioning",
        )
    return {"username": username, "role": "owner", "temporary_password": temp_password}


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "One-time deployment provisioning of the single Wings Theory OWNER "
            "account. Refuses if an OWNER already exists. Run once, on the server, "
            "by whoever controls this deployment -- never exposed through the API "
            "or the Admin console."
        )
    )
    ap.add_argument("--username", required=True)
    ap.add_argument("--display-name", required=True)
    args = ap.parse_args()

    conn = db.get_conn()
    db.migrate(conn)
    result = provision_first_owner(conn, args.username, args.display_name)
    print(f"OWNER account '{result['username']}' created.")
    print(f"Temporary password: {result['temporary_password']}")
    print(
        "Shown once. Give this to the business owner directly — it is not stored "
        "or logged anywhere. They must set their own password on first sign-in."
    )


if __name__ == "__main__":
    main()
