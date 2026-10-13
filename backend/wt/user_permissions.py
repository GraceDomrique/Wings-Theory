"""Per-user capability overrides (migration 0052).

Distinct from wt/auth.py's CAPABILITIES (role -> capability policy, unchanged
by this module) and wt/permissions.py (admin-editable PAGE visibility, per
role). This is the per-ACCOUNT exception layer on top of CAPABILITIES: an
explicit override row grants or revokes one capability for one username,
without touching what every other account sharing that role can do. Absence
of a row means "use the role default" -- see api.py's needs(), the only
enforcement point that reads this table.
"""
from __future__ import annotations

import sqlite3

from . import audit
from .auth import CAPABILITIES
from .errors import ValidationError


def get_overrides_for_user(conn: sqlite3.Connection, username: str) -> dict[str, bool]:
    rows = conn.execute(
        "SELECT capability, allowed FROM user_capability_override WHERE username=?",
        (username,),
    ).fetchall()
    return {r["capability"]: bool(r["allowed"]) for r in rows}


def get_effective_capability_matrix(conn: sqlite3.Connection, username: str, role: str) -> list[dict]:
    """One row per known capability: the role default, any override, and
    the effective (override-wins) result -- what Users & Access renders as
    that user's editable checkbox row."""
    overrides = get_overrides_for_user(conn, username)
    rows = []
    for capability, roles in CAPABILITIES.items():
        default = role in roles
        override = overrides.get(capability)
        rows.append({
            "capability": capability,
            "default": default,
            "override": override,
            "effective": default if override is None else override,
        })
    return rows


def set_override(
    conn: sqlite3.Connection, username: str, capability: str, allowed: bool | None,
    admin_username: str | None,
) -> list[dict]:
    """allowed=None clears the override, reverting that capability to the
    account's role default. Caller must already hold an open write
    transaction (matches audit.log's own contract)."""
    if capability not in CAPABILITIES:
        raise ValidationError(f"unknown capability '{capability}'")
    row = conn.execute(
        "SELECT role FROM app_user WHERE username=?", (username,)
    ).fetchone()
    if not row:
        raise ValidationError(f"unknown user '{username}'")
    previous = get_overrides_for_user(conn, username).get(capability)

    if allowed is None:
        conn.execute(
            "DELETE FROM user_capability_override WHERE username=? AND capability=?",
            (username, capability),
        )
    else:
        conn.execute(
            "INSERT INTO user_capability_override (username, capability, allowed, updated_at, updated_by) "
            "VALUES (?,?,?,datetime('now','subsec'),?) "
            "ON CONFLICT(username, capability) DO UPDATE SET "
            "allowed=excluded.allowed, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (username, capability, 1 if allowed else 0, admin_username),
        )
    audit.log(
        conn, "user_capability_override", f"{username}:{capability}", "set_override", admin_username,
        field=capability, previous_value=previous, new_value=allowed,
    )
    return get_effective_capability_matrix(conn, username, row["role"])
