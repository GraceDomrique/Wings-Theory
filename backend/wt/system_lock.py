"""POS / Kitchen station lock (migration 0053).

An admin-operated switch, separate from account-level auth entirely: closing
a station blocks sign-in at /auth/login for the 'cashier' (POS) or 'kitchen'
(KDS) role, no matter how correct the password is, until an admin reopens it.
Admin and owner console sign-in never goes through this check.
"""
from __future__ import annotations

import sqlite3

STATIONS = ("pos", "kitchen")


def get_all(c: sqlite3.Connection) -> dict:
    rows = c.execute("SELECT * FROM system_lock").fetchall()
    return {r["station"]: dict(r) for r in rows}


def is_closed(c: sqlite3.Connection, station: str) -> bool:
    row = c.execute("SELECT closed FROM system_lock WHERE station=?", (station,)).fetchone()
    return bool(row and row["closed"])


def set_closed(c: sqlite3.Connection, station: str, closed: bool, by: str, reason: str | None) -> None:
    if station not in STATIONS:
        raise ValueError(f"unknown station '{station}'")
    c.execute(
        "UPDATE system_lock SET closed=?, closed_at=CASE WHEN ? THEN datetime('now','subsec') ELSE NULL END, "
        "closed_by=?, reason=? WHERE station=?",
        (1 if closed else 0, 1 if closed else 0, by if closed else None, reason if closed else None, station),
    )
