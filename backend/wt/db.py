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
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = BACKEND_DIR / "migrations"
BACKUP_DIR = BACKEND_DIR / "backups"
# The one production database file that exists. Not a "default" any more --
# see ConfigurationError below for why nothing ever falls back to this
# silently. Kept under the old name too (DEFAULT_DB) only where an external
# caller might import it; nothing outside this module does.
PRODUCTION_DB = BACKEND_DIR / "wings_theory.db"
DEFAULT_DB = PRODUCTION_DB  # noqa: same path, kept as an alias for clarity at call sites
# 2026-10: the demonstration database. Deliberately its own file, not a
# second "development" database and not a flag on the production one — see
# wt/demo_mode.py's module docstring for the full reasoning. Same hard
# env<->path pairing as PRODUCTION_DB below: WT_ENV=demo may only open this
# file, and this file may only be opened under WT_ENV=demo, so "demo mode
# pointed at the wrong database" and "real mode accidentally pointed at the
# demo database" are both structurally impossible, not just discouraged.
DEMO_DB = BACKEND_DIR / "wings_theory_demo.db"

VALID_ENVIRONMENTS = ("production", "development", "test", "demo")

_local = threading.local()


class ConfigurationError(RuntimeError):
    """Fail-closed startup/config error: the deployment environment or the
    database target is missing, unrecognized, or inconsistent.

    Deliberately a plain RuntimeError subclass, not a WTError -- this fires
    before there is an HTTP request to attach a status code to (typically at
    process startup, from a CLI script, or from the very first connection a
    thread opens), and it must never be caught by the API layer's WTError
    handler and turned into a JSON 4xx. It should crash the process."""


def environment() -> str:
    """The explicit, required deployment environment. There is no default:
    an unset or unrecognized WT_ENV must refuse to start rather than
    silently behaving as any one of production/development/test. Callers
    that need to know "am I in production" must compare against this, never
    against whether WT_DB happens to look like a particular path."""
    env = os.environ.get("WT_ENV")
    if not env:
        raise ConfigurationError(
            "WT_ENV is not set. Refusing to start. Set WT_ENV to one of "
            f"{', '.join(VALID_ENVIRONMENTS)} before starting the application."
        )
    if env not in VALID_ENVIRONMENTS:
        raise ConfigurationError(
            f"WT_ENV={env!r} is not a recognized environment. "
            f"Use one of {', '.join(VALID_ENVIRONMENTS)}."
        )
    return env


def db_path() -> Path:
    """The database this process may open -- fail-closed on every axis.

    WT_ENV and WT_DB must both be set explicitly; there is no fallback to
    PRODUCTION_DB. A developer running `python -m uvicorn wt.api:app` with
    nothing configured gets a clear refusal here instead of a working
    connection to the shop's real database, which is what happened before
    this function had any opinion about the pairing.

    The pairing itself is validated, not just presence: WT_ENV=production
    must point at PRODUCTION_DB and nothing else, WT_ENV=demo must point at
    DEMO_DB and nothing else, and neither database may be opened under any
    other environment. This is what makes "production database opened by a
    non-production process" AND "demo database opened by a non-demo
    process" (or vice versa) structurally impossible rather than a matter of
    remembering to set WT_DB correctly -- the two variables cross-check each
    other on both special databases, not just the production one.
    """
    env = environment()
    raw = os.environ.get("WT_DB")
    if not raw:
        raise ConfigurationError(
            "WT_DB is not set. Refusing to fall back to the production "
            f"database ({PRODUCTION_DB}). Set WT_DB explicitly."
        )
    path = Path(raw)
    is_memory = str(path) == ":memory:"
    is_prod_path = (not is_memory) and path.resolve() == PRODUCTION_DB.resolve()
    is_demo_path = (not is_memory) and path.resolve() == DEMO_DB.resolve()
    if env == "production" and not is_prod_path:
        raise ConfigurationError(
            f"WT_ENV=production but WT_DB={path} is not the production "
            f"database ({PRODUCTION_DB}). Refusing to start."
        )
    if env != "production" and is_prod_path:
        raise ConfigurationError(
            f"WT_ENV={env} but WT_DB points at the production database "
            f"({PRODUCTION_DB}). Refusing to open the production database "
            "outside WT_ENV=production."
        )
    if env == "demo" and not is_demo_path:
        raise ConfigurationError(
            f"WT_ENV=demo but WT_DB={path} is not the demo database "
            f"({DEMO_DB}). Refusing to start -- demo mode must never run "
            "against any other database."
        )
    if env != "demo" and is_demo_path:
        raise ConfigurationError(
            f"WT_ENV={env} but WT_DB points at the demo database "
            f"({DEMO_DB}). Refusing to open the demo database outside "
            "WT_ENV=demo, so a real-mode process can never silently read "
            "or write demonstration data."
        )
    return path


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


def _split_statements(script: str) -> list[str]:
    """Break a migration file into individually executable statements, using
    sqlite3.complete_statement — the same BEGIN...END/quote/comment-aware
    parser the sqlite3 CLI uses to decide when a multi-line entry is finished
    — rather than a naive split on ';'. A bare split would sever a
    CREATE TRIGGER ... BEGIN ... END; body at its first internal semicolon,
    and several migrations define exactly that shape.

    This exists so migrate() can execute one statement at a time inside a
    connection it controls, instead of conn.executescript(): executescript
    issues its own implicit COMMIT before running and applies no further
    transaction control of its own, so a statement that fails partway through
    a script left every prior statement in that script already committed —
    the surrounding BEGIN IMMEDIATE / rollback() in the old migrate() had
    nothing left to roll back. Splitting the script lets every statement run
    under one real transaction this module controls, so a failure partway
    through actually rolls back everything the migration had done so far.
    """
    stmts: list[str] = []
    buf: list[str] = []
    for part in script.split(";"):
        buf.append(part)
        candidate = ";".join(buf) + ";"
        if sqlite3.complete_statement(candidate):
            stripped = candidate.strip()
            if stripped and stripped != ";":
                stmts.append(stripped)
            buf = []
    remainder = ";".join(buf).strip()
    if remainder:
        raise ValueError(f"incomplete trailing statement in migration: {remainder[:200]!r}")
    return stmts


def backup_now(
    reason: str, path: str | Path | None = None, conn: sqlite3.Connection | None = None,
) -> Path | None:
    """Copy the live database file into backend/backups/ before something
    risky touches it. Returns None for an in-memory database — there is
    nothing on disk to copy, and tests run entirely in memory or against a
    tempfile they clean up themselves.

    When a live connection is passed, this checkpoints the WAL first
    (PRAGMA wal_checkpoint(TRUNCATE)) before copying just the main file.
    Under journal_mode=WAL — which every on-disk connection here uses — a
    recently committed row can still be sitting only in the -wal sidecar,
    not yet folded into the main .db file; copying the main file alone at
    that point would silently produce a backup missing that row. Passing no
    connection (nothing is open on this path yet, e.g. before a destructive
    CLI operation) still copies the plain file — good enough for that case
    since nothing has written through this process' own connection.
    """
    target = Path(path or db_path())
    if str(target) == ":memory:" or not target.exists():
        return None
    if conn is not None:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_reason = re.sub(r"[^a-z0-9_]+", "_", reason.lower())
    dest = BACKUP_DIR / f"{target.stem}.{safe_reason}.{stamp}{target.suffix}"
    shutil.copy2(target, dest)
    return dest


def migrate(conn: sqlite3.Connection | None = None, verbose: bool = False) -> int:
    """Apply every migration above the current version, each in its own
    transaction. Returns the resulting version.

    If this database has ever been migrated before (current > 0 — i.e. this
    is a real, already-used database rather than a brand-new one being
    initialized for the first time), it is backed up before any pending
    migration is applied. A fresh, empty database has nothing worth backing
    up yet, so this does not clutter backups/ on every test run.
    """
    conn = conn or get_conn()
    current = applied_version(conn)
    pending = [(v, n, p) for v, n, p in _discover() if v > current]
    if not pending:
        return current

    if current > 0:
        # The file this specific connection actually has open, from SQLite
        # itself via PRAGMA database_list — not db_path()/WT_DB. Those can
        # disagree: every test in this suite (and seed.run(), and reset())
        # calls migrate() with a connection opened against an explicit
        # tempfile path, never against the environment default. Backing up
        # by re-deriving the path from WT_DB here would silently back up
        # some other database instead of the one actually being migrated —
        # None for an in-memory connection, which correctly skips the
        # backup rather than falling back to an unrelated file on disk.
        row = conn.execute("PRAGMA database_list").fetchone()
        file_path = row["file"] if row and row["file"] else None
        if file_path:
            dest = backup_now(
                f"pre_migrate_to_v{pending[-1][0]:04d}", path=file_path, conn=conn,
            )
            if verbose and dest:
                print(f"  backed up to {dest}")

    for version, name, path in pending:
        sql = path.read_text(encoding="utf-8")
        statements = _split_statements(sql)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in statements:
                conn.execute(stmt)
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
            conn.commit()
        if verbose:
            print(f"  applied {version:04d}_{name}")
        current = version
    return current


def integrity_report(conn: sqlite3.Connection | None = None) -> dict:
    """The honest state of this database, for GET /health and for the
    production startup gate: where it is, what schema version it's on,
    whether SQLite's own checks pass, and a few real row counts. Nothing
    here is cached — every field is read fresh."""
    conn = conn or get_conn()
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    version = applied_version(conn)
    head = max((v for v, _, _ in _discover()), default=0)
    counts = {}
    for table in ("ingredient", "customer_order", "stock_ledger", "fefo_batch"):
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        except sqlite3.OperationalError:
            counts[table] = None
    return {
        "database_path": str(db_path()),
        "schema_version": version,
        "schema_head": head,
        "schema_up_to_date": version == head,
        "integrity_check": integrity,
        "foreign_key_violations": fk_violations,
        "row_counts": counts,
    }


def validate_production_startup(conn: sqlite3.Connection | None = None) -> None:
    """Refuses to start when WT_ENV=production is set but the deployment is
    unsafe. A no-op in every other environment (development, test), so
    those runs are unaffected by the checks below; this only fires for a
    launcher that has explicitly opted into being treated as production.

    db_path() (called earlier, via get_conn()/migrate() in api.py's startup
    hook) has already refused to run at all if WT_ENV is unset/unrecognized
    or if WT_DB doesn't match the environment. What's checked here is
    additional and production-only: a real signing secret, and a database
    that is actually in a healthy, migrated state.
    """
    if environment() != "production":
        return
    from . import auth  # local import: auth does not import db, so this is
                         # one-directional, but importing it at module scope
                         # would still make db.py's import surface depend on
                         # a higher-level module for no reason outside this
                         # one check.

    secret = os.environ.get("WT_SECRET")
    if not secret or secret == auth.DEFAULT_SECRET:
        raise ConfigurationError(
            "WT_ENV=production but WT_SECRET is unset or still the default — "
            "refusing to start with a forgeable signing key"
        )
    report = integrity_report(conn or get_conn())
    if report["integrity_check"] != "ok":
        raise ConfigurationError(f"database integrity check failed: {report['integrity_check']}")
    if report["foreign_key_violations"]:
        raise ConfigurationError(
            f"{report['foreign_key_violations']} foreign key violation(s) found at startup"
        )
    if not report["schema_up_to_date"]:
        raise ConfigurationError(
            f"database is at schema v{report['schema_version']}, "
            f"expected v{report['schema_head']} — migration did not complete"
        )


def require_destructive_confirmation(action: str) -> None:
    """Interactive guard for an operator command that can alter or destroy a
    live database (seeding, --fresh reset). A script fired against the wrong
    WT_DB should fail loud, not silently reseed or wipe whatever database
    happens to be configured — so this requires both an explicit opt-in
    environment variable and the operator typing the resolved database's own
    filename back, which forces them to actually read it first.
    """
    path = db_path()
    if os.environ.get("WT_ALLOW_DESTRUCTIVE") != "1":
        print(f"\nrefusing: {action} would modify {path}")
        print("set WT_ALLOW_DESTRUCTIVE=1 in the environment to allow this, then re-run\n")
        raise SystemExit(1)
    print(f"\nabout to {action}")
    print(f"database: {path}\n")
    reply = input(f"type the database filename ({path.name}) to confirm: ").strip()
    if reply != path.name:
        print("confirmation did not match — aborting")
        raise SystemExit(1)


def open_and_register(path: str | Path) -> sqlite3.Connection:
    """Closes this thread's current connection (if any), opens a fresh one
    against `path`, and registers it as this thread's connection -- the
    same registration reset() does, for the same reason: an unregistered
    connection would leave a later get_conn() opening a *second* handle on
    the same file, which on Windows keeps a lock the caller thinks it has
    already released.

    Used by wt.backup_restore.restore() after copying a backup into place.
    Deliberately does NOT call migrate() -- a restored backup already
    carries whatever schema version it was backed up at, and silently
    migrating it here would make the restore change what it just put
    back instead of returning exactly that snapshot.
    """
    close_conn()
    conn = connect(path)
    _local.conn = conn
    return conn


def reset(path: str | Path | None = None) -> sqlite3.Connection:
    """Drop and rebuild. Test helper — never call this against a live database.

    Self-protecting, not just CLI-wrapper-protected: seed.py's operator CLI
    additionally requires an interactive filename confirmation before it
    ever calls this (require_destructive_confirmation), but that guard lives
    one layer up and would not stop a future script that imports db.reset()
    directly. So the checks below run inside this function itself, on every
    call, regardless of caller or whether an explicit path was passed:

      production  -> always refused, unconditionally. Neither WT_ALLOW_
                     DESTRUCTIVE nor an explicit path override this.
      demo        -> proceeds freely, but ONLY when the resolved target is
                     actually DEMO_DB -- see the check below. Demo
                     initialization is meant to be rebuildable on demand
                     (wt/demo_mode.py's init_demo() calls this every time,
                     for idempotency -- see that module), so this
                     deliberately does not require WT_ALLOW_DESTRUCTIVE the
                     way development does: the blast radius is hard-limited
                     to one file that can never be the production database
                     (db_path()'s own env<->path pairing already guarantees
                     that), so there is nothing here an opt-in flag would be
                     protecting beyond what the path check already does.
      development -> refused unless WT_ALLOW_DESTRUCTIVE=1 is set.
      test        -> proceeds freely; this is what the test suite needs on
                     every run and must not be made to require an opt-in.

    The production check is duplicated deliberately: once via WT_ENV
    (environment() == "production" refuses outright) and once via the
    resolved target path actually being PRODUCTION_DB, even if a caller
    passed that path explicitly while WT_ENV was something else. Either
    condition alone is enough to refuse. The demo check below follows the
    identical shape against DEMO_DB, for the identical reason: a typo or a
    caller passing an explicit path must not be enough to reset (or,
    worse, have this function silently treat as resettable) some other
    database just because WT_ENV happened to say "demo".

    The rebuilt connection is registered as this thread's connection. Returning
    an unregistered one would leave a later get_conn() opening a *second*
    handle on the same file, which on Windows keeps the file locked after the
    caller thinks it has closed everything.
    """
    target = Path(path or db_path())
    env = environment()
    is_prod_path = str(target) != ":memory:" and target.resolve() == PRODUCTION_DB.resolve()
    is_demo_path = str(target) != ":memory:" and target.resolve() == DEMO_DB.resolve()
    if env == "production" or is_prod_path:
        raise ConfigurationError(
            f"refusing to reset {target}: this is the production database "
            "and/or WT_ENV=production. db.reset() can never run against "
            "production, regardless of how it was called."
        )
    if env == "demo" and not is_demo_path:
        raise ConfigurationError(
            f"refusing to reset {target}: WT_ENV=demo but this is not the "
            f"demo database ({DEMO_DB}). db.reset() under WT_ENV=demo can "
            "only ever target the demo database."
        )
    if is_demo_path and env != "demo":
        raise ConfigurationError(
            f"refusing to reset {target}: this is the demo database but "
            "WT_ENV is not 'demo'. db.reset() can only reset the demo "
            "database under WT_ENV=demo."
        )
    if env == "development" and os.environ.get("WT_ALLOW_DESTRUCTIVE") != "1":
        raise ConfigurationError(
            f"refusing to reset {target}: WT_ENV=development requires "
            "WT_ALLOW_DESTRUCTIVE=1 in the environment to authorize a "
            "destructive reset."
        )
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
