"""Admin-configurable page visibility.

Distinct from wt/auth.py's CAPABILITIES: that table governs individual
read/write ACTIONS ("can this role edit an ingredient") and is enforced on
every mutating and most reading endpoints via needs()/auth.require(). This
module governs whether a role can reach a console PAGE at all ("can manager
see the Verification screen") -- a coarser, admin-editable layer that sits on
top of CAPABILITIES rather than replacing it. auth.CAPABILITIES stays exactly
as the 2026-08/09 RBAC audit left it; nothing here changes what a granted
page lets a role actually do once there.

Scope is deliberately narrow (see migration 0023): two page keys, one role
that needs them. 'admin' is always fully granted and is never looked up or
stored. 'cashier'/'kitchen' don't use this console (see ALLOWED_ROLES in the
frontend) and are out of scope.
"""
from __future__ import annotations

import sqlite3

from . import audit
from .errors import ValidationError

PAGE_KEYS = ("verify", "system")

# What a role can see before any admin has made an explicit choice. A row in
# role_page_permission always means an explicit override; absence means "use
# this default" -- see migration 0023's docstring for why that distinction is
# kept rather than seeding rows.
DEFAULT_GRANTS = {
    "manager": {"verify": False, "system": False},
}


def get_effective_grants(conn: sqlite3.Connection, role: str) -> dict[str, bool]:
    """What `role` can currently see, one bool per PAGE_KEYS entry. Admin is
    short-circuited to all-True without a query -- it is never restricted and
    never stored in role_page_permission."""
    if role == "admin":
        return {key: True for key in PAGE_KEYS}

    defaults = DEFAULT_GRANTS.get(role, {key: False for key in PAGE_KEYS})
    grants = dict(defaults)
    rows = conn.execute(
        "SELECT page_key, allowed FROM role_page_permission WHERE role=?", (role,)
    ).fetchall()
    for r in rows:
        if r["page_key"] in grants:
            grants[r["page_key"]] = bool(r["allowed"])
    return grants


def get_all_grants(conn: sqlite3.Connection) -> dict[str, dict[str, bool]]:
    """Effective grants for every role the admin editor can configure --
    currently just 'manager'. Admin/cashier/kitchen are intentionally absent:
    admin isn't editable (always full access), the other two don't use this
    console."""
    return {role: get_effective_grants(conn, role) for role in DEFAULT_GRANTS}


def set_grant(
    conn: sqlite3.Connection, role: str, page_key: str, allowed: bool, admin_username: str | None,
) -> dict[str, bool]:
    """Upsert one (role, page_key) grant and audit the change. Caller must
    already hold an open write transaction (matches audit.log's own
    contract) -- see api.py's set_page_permission for the with db.write_tx()
    wrapper."""
    if role not in DEFAULT_GRANTS:
        raise ValidationError(f"role '{role}' has no configurable page permissions")
    if page_key not in PAGE_KEYS:
        raise ValidationError(f"unknown page '{page_key}'")

    previous = get_effective_grants(conn, role)[page_key]
    conn.execute(
        "INSERT INTO role_page_permission (role, page_key, allowed, updated_at, updated_by) "
        "VALUES (?,?,?,datetime('now','subsec'),?) "
        "ON CONFLICT(role, page_key) DO UPDATE SET "
        "allowed=excluded.allowed, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (role, page_key, 1 if allowed else 0, admin_username),
    )
    audit.log(
        conn, "role_permission", f"{role}:{page_key}", "set_grant", admin_username,
        field=page_key, previous_value=previous, new_value=allowed,
    )
    return get_effective_grants(conn, role)
