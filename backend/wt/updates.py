"""Owner <-> Admin operational updates (migration 0029): the free-text half
of Owner/Admin communication that owner_instruction's fixed state machine
doesn't cover.

A record is either:
  - threaded onto an existing owner_instruction (owner_instruction_id set) --
    an interim progress note from admin, or a follow-up/question from owner,
    on a task that's still open; or
  - standalone (owner_instruction_id NULL) -- a heads-up either role sends
    without a prior instruction, e.g. admin reporting "Chicken wing demand is
    above recent average" or owner asking "Please check the chicken stock."

Not a chat: no per-user inbox, no read/unread, no edit or delete. The only
state is a single optional acknowledgement by someone on the OTHER side from
whoever posted it -- mirroring the acknowledgement instructions.py already
established for instructions themselves. Every creation and acknowledgement
is mirrored into admin_audit_log via the existing audit.log(), inside the
same transaction as the row write.
"""
from __future__ import annotations

import sqlite3

from . import audit
from .db import write_tx
from .errors import InvalidStateError, NotFoundError, PermissionError_, ValidationError

ROLES = ("owner", "admin")


def _row(conn: sqlite3.Connection, pk: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM operational_update WHERE id=?", (pk,)).fetchone()
    if not row:
        raise NotFoundError(f"update {pk} not found")
    return row


def list_all(conn: sqlite3.Connection, owner_instruction_id: int | None = None) -> dict:
    """All updates, optionally narrowed to one instruction's thread. Like
    owner_instruction, this is a shared board both roles see identically —
    there is no per-recipient filtering to reconcile."""
    sql = "SELECT * FROM operational_update"
    params: tuple = ()
    if owner_instruction_id is not None:
        sql += " WHERE owner_instruction_id=?"
        params = (owner_instruction_id,)
    sql += " ORDER BY created_at DESC"
    items = [dict(r) for r in conn.execute(sql, params)]
    return {"items": items, "count": len(items)}


def create(
    conn: sqlite3.Connection, body: str, owner_instruction_id: int | None,
    created_by: str | None, created_by_role: str,
) -> dict:
    if created_by_role not in ROLES:
        raise ValidationError(f"unknown role '{created_by_role}'")
    if not isinstance(body, str) or not body.strip():
        raise ValidationError("update body is required")
    with write_tx(conn):
        if owner_instruction_id is not None:
            exists = conn.execute(
                "SELECT 1 FROM owner_instruction WHERE id=?", (owner_instruction_id,)
            ).fetchone()
            if not exists:
                raise NotFoundError(f"instruction {owner_instruction_id} not found")
        cur = conn.execute(
            "INSERT INTO operational_update (owner_instruction_id, body, created_by, created_by_role) "
            "VALUES (?,?,?,?)",
            (owner_instruction_id, body.strip(), created_by, created_by_role),
        )
        pk = cur.lastrowid
        audit.log(
            conn, "operational_update", pk, "create", created_by,
            field="body", previous_value=None, new_value=body.strip()[:200],
        )
        return dict(_row(conn, pk))


def acknowledge(conn: sqlite3.Connection, pk: int, user_id: str | None, role: str) -> dict:
    """Only someone on the OTHER side from whoever posted the update may
    acknowledge it — an owner update is acknowledged by admin, an admin
    update is acknowledged by owner, same as instructions.py's owner/admin
    split, just mirrored."""
    with write_tx(conn):
        row = _row(conn, pk)
        if role == row["created_by_role"]:
            raise PermissionError_("only the other role may acknowledge an update")
        if row["acknowledged_at"] is not None:
            raise InvalidStateError("this update has already been acknowledged")
        conn.execute(
            "UPDATE operational_update SET acknowledged_at=datetime('now','subsec'), "
            "acknowledged_by=? WHERE id=?",
            (user_id, pk),
        )
        audit.log(
            conn, "operational_update", pk, "acknowledge", user_id,
            field="acknowledged_at", previous_value=None, new_value="acknowledged",
        )
        return dict(_row(conn, pk))
