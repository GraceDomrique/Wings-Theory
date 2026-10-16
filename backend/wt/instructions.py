"""Owner -> Admin instructions (migration 0025).

A lightweight operational task, not a chat: the owner names something that
needs doing, admin works it through a fixed set of states, and every
creation/transition is attributable, timestamped, and mirrored into
admin_audit_log via the existing audit.log() — inside the same transaction
as the row update, so the record and its audit trail can never disagree.

State machine (no 'draft' — an instruction exists the moment it's created):

    open -> acknowledged -> in_progress -> completed      (admin only)
    open | acknowledged | in_progress -> cancelled         (admin any time;
                                                             owner only their
                                                             own, only from
                                                             'open')

completed/cancelled are terminal.
"""
from __future__ import annotations

import sqlite3

from . import audit
from .db import write_tx
from .errors import InvalidStateError, NotFoundError, PermissionError_, ValidationError

PRIORITIES = ("normal", "important", "urgent")
CANCELLABLE_FROM = ("open", "acknowledged", "in_progress")

# action -> (required current status, new status, timestamp column, who)
_ADVANCE = {
    "acknowledge": ("open", "acknowledged", "acknowledged_at"),
    "start": ("acknowledged", "in_progress", "in_progress_at"),
}


def _row(conn: sqlite3.Connection, instruction_pk: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM owner_instruction WHERE id=?", (instruction_pk,)
    ).fetchone()
    if not row:
        raise NotFoundError(f"instruction {instruction_pk} not found")
    return row


def list_for_role(conn: sqlite3.Connection, status: str | None = None) -> dict:
    """Owner and admin see the same list — this is a shared task board, not a
    per-user inbox. `status` optionally narrows to one state (e.g. the admin
    Instructions screen defaulting to everything not yet completed/cancelled)."""
    sql = "SELECT * FROM owner_instruction"
    params: tuple = ()
    if status:
        sql += " WHERE status=?"
        params = (status,)
    sql += " ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'important' THEN 1 ELSE 2 END, created_at DESC"
    items = [dict(r) for r in conn.execute(sql, params)]
    return {"items": items, "count": len(items)}


def create(
    conn: sqlite3.Connection, title: str, body: str, priority: str,
    assigned_to: str | None, due_date: str | None, created_by: str | None,
) -> dict:
    if priority not in PRIORITIES:
        raise ValidationError(f"unknown priority '{priority}'")
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("title is required")
    if not isinstance(body, str) or not body.strip():
        raise ValidationError("instruction body is required")
    with write_tx(conn):
        cur = conn.execute(
            "INSERT INTO owner_instruction (title, body, priority, assigned_to, due_date, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (title.strip(), body.strip(), priority, assigned_to, due_date, created_by),
        )
        pk = cur.lastrowid
        audit.log(
            conn, "owner_instruction", pk, "create", created_by,
            field="status", previous_value=None, new_value="open", note=title.strip(),
        )
        return dict(_row(conn, pk))


def _advance(conn: sqlite3.Connection, instruction_pk: int, action: str, user_id: str | None) -> dict:
    from_status, to_status, ts_col = _ADVANCE[action]
    with write_tx(conn):
        row = _row(conn, instruction_pk)
        if row["status"] != from_status:
            raise InvalidStateError(
                f"cannot {action} an instruction in status '{row['status']}' "
                f"(expected '{from_status}')"
            )
        extra = ", acknowledged_by=:user" if action == "acknowledge" else ""
        conn.execute(
            f"UPDATE owner_instruction SET status=:status, {ts_col}=datetime('now','subsec'), "
            f"updated_at=datetime('now','subsec'){extra} WHERE id=:id",
            {"status": to_status, "id": instruction_pk, "user": user_id},
        )
        audit.log(
            conn, "owner_instruction", instruction_pk, action, user_id,
            field="status", previous_value=from_status, new_value=to_status,
        )
        return dict(_row(conn, instruction_pk))


def acknowledge(conn: sqlite3.Connection, instruction_pk: int, user_id: str | None) -> dict:
    return _advance(conn, instruction_pk, "acknowledge", user_id)


def start(conn: sqlite3.Connection, instruction_pk: int, user_id: str | None) -> dict:
    return _advance(conn, instruction_pk, "start", user_id)


def complete(
    conn: sqlite3.Connection, instruction_pk: int, completion_note: str, user_id: str | None,
) -> dict:
    if not isinstance(completion_note, str) or not completion_note.strip():
        raise ValidationError("a completion note is required to complete an instruction")
    with write_tx(conn):
        row = _row(conn, instruction_pk)
        if row["status"] != "in_progress":
            raise InvalidStateError(
                f"cannot complete an instruction in status '{row['status']}' "
                "(expected 'in_progress')"
            )
        conn.execute(
            "UPDATE owner_instruction SET status='completed', completed_at=datetime('now','subsec'), "
            "completed_by=:user, completion_note=:note, updated_at=datetime('now','subsec') WHERE id=:id",
            {"user": user_id, "note": completion_note.strip(), "id": instruction_pk},
        )
        audit.log(
            conn, "owner_instruction", instruction_pk, "complete", user_id,
            field="status", previous_value="in_progress", new_value="completed",
            note=completion_note.strip(),
        )
        return dict(_row(conn, instruction_pk))


def cancel(
    conn: sqlite3.Connection, instruction_pk: int, reason: str | None,
    user_id: str | None, role: str,
) -> dict:
    """Admin may cancel from any of CANCELLABLE_FROM. Owner may cancel only
    their own instruction, and only while it is still 'open' — once admin has
    acknowledged it, retracting it is an admin decision, not the owner's."""
    with write_tx(conn):
        row = _row(conn, instruction_pk)
        if row["status"] not in CANCELLABLE_FROM:
            raise InvalidStateError(f"cannot cancel an instruction in status '{row['status']}'")
        if role == "owner":
            if row["created_by"] != user_id:
                raise PermissionError_("owner may only cancel their own instruction")
            if row["status"] != "open":
                raise InvalidStateError(
                    "owner may only cancel an instruction that is still 'open' — "
                    "ask admin to cancel it once acknowledged"
                )
        previous_status = row["status"]
        conn.execute(
            "UPDATE owner_instruction SET status='cancelled', cancelled_at=datetime('now','subsec'), "
            "cancelled_by=:user, cancel_reason=:reason, updated_at=datetime('now','subsec') WHERE id=:id",
            {"user": user_id, "reason": reason, "id": instruction_pk},
        )
        audit.log(
            conn, "owner_instruction", instruction_pk, "cancel", user_id,
            field="status", previous_value=previous_status, new_value="cancelled", note=reason,
        )
        return dict(_row(conn, instruction_pk))
