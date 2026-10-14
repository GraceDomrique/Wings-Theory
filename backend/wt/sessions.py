"""Server-side session table backing explicit logout and token revocation.

wt/auth.py issues and verifies the JWT itself (signature, expiry, the 'pwd'
claim); this module is the second, independent authority the token's 'jti'
claim is checked against on every request (see api.principal()) -- whether
that specific session has been explicitly signed out. Without this, "Sign
out" was only ever a client-side localStorage.removeItem() and the bearer
token itself kept working, unrevoked, for the rest of its 12-hour TTL.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3

from . import timeutil


def _exp_to_sql(exp: int) -> str:
    """A token's integer 'exp' claim (seconds since epoch) as the same SQL
    datetime string format timeutil.utc_now_sql() produces, so expires_at is
    directly comparable to it."""
    return _dt.datetime.fromtimestamp(exp, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def create(c: sqlite3.Connection, jti: str, username: str, role: str, exp: int) -> None:
    """Called once, right after auth.issue_token mints a token -- see
    api.py's login()/change_password(). Must run inside the same
    db.write_tx() as the rest of that request's writes."""
    c.execute(
        "INSERT INTO auth_session (jti, username, role, issued_at, expires_at) "
        "VALUES (?,?,?,?,?)",
        (jti, username, role, timeutil.utc_now_sql(), _exp_to_sql(exp)),
    )


def is_active(c: sqlite3.Connection, jti: str | None) -> bool:
    """False for a missing jti (a token minted before this module existed,
    or one with no session row for any other reason -- deliberately fails
    closed rather than treating "we have no record of this session" as
    equivalent to "this session is fine"), a revoked session, or one whose
    row has already been purged as expired."""
    if not jti:
        return False
    row = c.execute(
        "SELECT revoked_at FROM auth_session WHERE jti=?", (jti,)
    ).fetchone()
    return row is not None and row["revoked_at"] is None


def revoke(c: sqlite3.Connection, jti: str) -> None:
    """POST /auth/logout's entire effect: the caller's own token stops
    working immediately, including for a direct API request replaying the
    captured bearer value -- not merely "no longer offered" by the browser
    that logged out."""
    c.execute(
        "UPDATE auth_session SET revoked_at = ? WHERE jti=? AND revoked_at IS NULL",
        (timeutil.utc_now_sql(), jti),
    )


def revoke_all_for_user(c: sqlite3.Connection, username: str) -> int:
    """Every other device/tab/token this account was signed in from stops
    working too. Called alongside a password change or an admin reset --
    both already invalidate every pre-existing token via the 'pwd' claim
    (see auth.issue_token), so this is defense in depth plus keeping this
    table an accurate picture of which sessions are actually live, rather
    than a second, independent security boundary on its own."""
    cur = c.execute(
        "UPDATE auth_session SET revoked_at = ? WHERE username=? AND revoked_at IS NULL",
        (timeutil.utc_now_sql(), username),
    )
    return cur.rowcount


def purge_expired(c: sqlite3.Connection) -> int:
    """Housekeeping only -- a purged row and an un-purged-but-expired row
    behave identically to is_active() (TTL is still enforced independently
    by auth.decode_token's own 'exp' check), this just keeps the table from
    growing forever on a system that never restarts."""
    cur = c.execute(
        "DELETE FROM auth_session WHERE expires_at < ?", (timeutil.utc_now_sql(),)
    )
    return cur.rowcount
