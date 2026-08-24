"""General admin-action audit trail (admin_audit_log, migration 0016).

Separate from verification.py's verification_event, which is scoped to the
six owner-verification-queue fields specifically. This module covers every
other sensitive mutation that has no verification-queue home: general
ingredient edits, recipe creation, menu price changes, admin password
resets. Same append-only contract: log(), never update or delete.

Callers pass an already-open write transaction -- this never opens its own,
matching verification._log()'s pattern, so it composes inside the caller's
existing `with db.write_tx(c):` block instead of raising write_tx's
nested-transaction guard.
"""
from __future__ import annotations

import sqlite3


def log(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str | int,
    action: str,
    user_id: str | None,
    *,
    field: str | None = None,
    previous_value=None,
    new_value=None,
    note: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO admin_audit_log "
        "(entity_type, entity_id, action, field, previous_value, new_value, user_id, note) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            entity_type, str(entity_id), action, field,
            None if previous_value is None else str(previous_value),
            None if new_value is None else str(new_value),
            user_id, note,
        ),
    )
