"""SQLite connection handling and the migration runner.

Why raw sqlite3 rather than an ORM session for the write paths: the reservation
logic in reservation.py depends on knowing exactly what SQL is emitted and how
many rows it touched. An ORM's unit-of-work would buffer and reorder those
statements, which is the one thing that must not happen there.

Concurrency model
-----------------
WAL journalling lets readers run while a writer holds the write lock, which is
what a POS floor actually does: many availability reads, occasional writes.

Every write transaction opens with BEGIN IMMEDIATE. SQLite's default DEFERRED
transaction takes a read lock first and only tries to upgrade on the first
write; if two connections both hold read locks and both try to upgrade, one
gets SQLITE_BUSY immediately and cannot wait it out. BEGIN IMMEDIATE takes the
write lock up front, so contenders queue on busy_timeout instead of failing.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = BACKEND_DIR / "migrations"
DEFAULT_DB = BACKEND_DIR / "wings_theory.db"

_local = threading.local()


def db_path() -> Path:
    return Path(os.environ.get("WT_DB", str(DEFAULT_DB)))


def connect(path: str | Path | None = None, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """A configured connection. isolation_level=None puts transaction control in
    our hands so we can issue BEGIN IMMEDIATE explicitly."""
    target = str(path or db_path())
    conn = sqlite3.connect(target, timeout=10.0, isolation_level=None,
                           check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def get_conn() -> sqlite3.Connection:
    """One connection per thread. sqlite3 objects are not thread-safe, and
    FastAPI runs sync endpoints on a worker threadpool."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect()
        _local.conn = conn
    return conn


def close_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


@contextmanager
def write_tx(conn: sqlite3.Connection | None = None):
    """Exclusive write transaction. Commits on clean exit, rolls back on any
    exception. Nested use is rejected rather than silently flattened, because a
    partial rollback in the reservation path would leave stock double-counted."""
    conn = conn or get_conn()
    if conn.in_transaction:
        raise RuntimeError("write_tx cannot be nested")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def read_tx(conn: sqlite3.Connection | None = None):
    conn = conn or get_conn()
    yield conn


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def _discover() -> list[tuple[int, str, Path]]:
    out = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _MIGRATION_RE.match(p.name)
        if not m:
            raise ValueError(f"migration filename must be NNNN_name.sql: {p.name}")
        out.append((int(m.group(1)), m.group(2), p))
    versions = [v for v, _, _ in out]
    if len(set(versions)) != len(versions):
        raise ValueError("duplicate migration version numbers")
    return out


def applied_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not row:
        return 0
    r = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return r["v"] or 0


def migrate(conn: sqlite3.Connection | None = None, verbose: bool = False) -> int:
    """Apply every migration above the current version, each in its own
    transaction. Returns the resulting version."""
    conn = conn or get_conn()
    current = applied_version(conn)
    for version, name, path in _discover():
        if version <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executescript(sql)
            # executescript commits any open transaction before running, so the
            # version row is recorded explicitly rather than relied upon.
            already = conn.execute(
                "SELECT 1 FROM schema_version WHERE version=?", (version,)
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT INTO schema_version (version, name) VALUES (?,?)",
                    (version, name),
                )
        except Exception:
            conn.rollback()
            raise
        else:
            if conn.in_transaction:
                conn.commit()
        if verbose:
            print(f"  applied {version:04d}_{name}")
        current = version
    return current


def reset(path: str | Path | None = None) -> sqlite3.Connection:
    """Drop and rebuild. Test helper — never call this against a live database.

    The rebuilt connection is registered as this thread's connection. Returning
    an unregistered one would leave a later get_conn() opening a *second*
    handle on the same file, which on Windows keeps the file locked after the
    caller thinks it has closed everything.
    """
    target = Path(path or db_path())
    close_conn()
    if str(target) != ":memory:":
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(target) + suffix)
            if p.exists():
                p.unlink()
    conn = connect(target)
    migrate(conn)
    _local.conn = conn
    return conn
