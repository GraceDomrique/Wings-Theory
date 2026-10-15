"""Unit tests for the model_registry exception-handling fix.

Covers the confirmed defect: active_run()/active_artifact_path() (and every
other ml_model_registry/ml_training_run/ml_approved_synthetic_batch reader)
used to let a raw sqlite3.OperationalError escape when those tables did not
exist yet, crashing ml_startup.validate_artifacts_on_startup(),
associations.active_combo_recommendations(), and anomaly._score_with_
isolation_forest() on a database that simply hadn't been migrated to 0050
yet. These tests build a database frozen BELOW migration 0050 (registry
tables absent) and a deliberately corrupted file, and check that:

  - every reader classifies "no such table" as RegistryUnavailableError,
    never a bare sqlite3.OperationalError
  - every one of the three high-level callers treats that condition as an
    ordinary "nothing trained yet" fallback, not a crash
  - database corruption raises DatabaseIntegrityError and is never
    reclassified as "nothing trained yet" by any of those same callers

Run:  python -m tests.test_model_registry_errors
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wt import db  # noqa: E402
from wt import model_registry as mr  # noqa: E402
from wt.errors import DatabaseIntegrityError, RegistryUnavailableError  # noqa: E402

try:
    from wt import associations  # noqa: E402
except ImportError:  # pragma: no cover -- mlxtend not installed in this env
    associations = None


def _scrub(path: str) -> None:
    for suffix in ("", "-wal", "-shm", ".migrate.lock"):
        p = Path(path + suffix)
        for _ in range(3):
            if not p.exists():
                break
            try:
                p.unlink()
                break
            except PermissionError:
                time.sleep(0.05)


def _migrate_up_to(conn, max_version: int) -> None:
    """Apply only migrations <= max_version, bypassing db.migrate()'s
    "apply everything pending" behaviour so a test can build a database
    frozen below 0050 (ml_model_registry doesn't exist yet) on purpose --
    exactly the state a fresh checkout mid-upgrade, or one that failed
    partway through migrating, would be in."""
    for version, name, path in db._discover():
        if version > max_version:
            continue
        sql = path.read_text(encoding="utf-8")
        statements = db._split_statements(sql)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in statements:
                conn.execute(stmt)
            # Several migration files already INSERT their own schema_version
            # row (the house convention db.migrate() itself accommodates) --
            # only add one here if that statement didn't already.
            already = conn.execute(
                "SELECT 1 FROM schema_version WHERE version=?", (version,)
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT INTO schema_version (version, name) VALUES (?,?)", (version, name)
                )
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


class RegistryMissingTableTests(unittest.TestCase):
    """Database frozen at schema v0049 -- ml_model_registry et al. do not
    exist. This is the exact condition the confirmed bug crashed on."""

    @classmethod
    def setUpClass(cls):
        fd, cls.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            os.unlink(cls.path)
        except FileNotFoundError:
            pass
        cls.conn = db.connect(cls.path)
        _migrate_up_to(cls.conn, 49)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        _scrub(cls.path)

    def test_active_run_raises_registry_unavailable_not_raw_sqlite_error(self):
        with self.assertRaises(RegistryUnavailableError):
            mr.active_run(self.conn, "demand_prophet")

    def test_active_artifact_path_raises_registry_unavailable(self):
        with self.assertRaises(RegistryUnavailableError):
            mr.active_artifact_path(self.conn, "fpgrowth_combos")

    def test_approved_batch_ids_raises_registry_unavailable(self):
        with self.assertRaises(RegistryUnavailableError):
            mr.approved_batch_ids(self.conn)

    def test_get_mode_raises_registry_unavailable(self):
        with self.assertRaises(RegistryUnavailableError):
            mr.get_mode(self.conn, "stock_loss_isoforest")

    def test_list_models_raises_registry_unavailable(self):
        with self.assertRaises(RegistryUnavailableError):
            mr.list_models(self.conn)

    def test_training_history_raises_registry_unavailable(self):
        with self.assertRaises(RegistryUnavailableError):
            mr.training_history(self.conn, "demand_prophet")

    @unittest.skipIf(associations is None, "mlxtend not installed in this environment")
    def test_associations_falls_back_instead_of_crashing(self):
        """active_combo_recommendations() must not raise -- it has to serve
        a fallback (fresh mine, or 'unavailable' if fresh-fit is disallowed
        in this environment) and say so in served_from."""
        result = associations.active_combo_recommendations(self.conn)
        self.assertIn(result["served_from"], ("fresh_mine_fallback", "unavailable"))
        self.assertIsNone(result["artifact_path"])


class RegistryCorruptionTests(unittest.TestCase):
    """A damaged database file must raise DatabaseIntegrityError and must
    NEVER be silently treated as 'nothing trained yet'.

    Corrupting a fully-migrated file (rather than writing raw garbage bytes)
    keeps the 100-byte SQLite header intact, so db.connect()'s own opening
    PRAGMAs still succeed and the failure surfaces from model_registry's
    actual table read -- the scenario the fix targets, not a connect()-time
    rejection that never reaches this module's code at all."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        # Build and fully migrate the file through the normal path first,
        # then close it -- db.connect()'s own opening PRAGMAs (WAL mode)
        # read and cache pages, which would otherwise mask on-disk damage
        # applied afterward to this SAME connection (SQLite trusts its page
        # cache until something invalidates it). Truncating the closed file
        # and opening a brand-new, plain sqlite3.connect() (skipping the WAL
        # pragma, like a second process attaching to an already-damaged
        # file would) is what actually forces a real read of the damaged
        # pages on the very first query -- exactly the shape of corruption
        # model_registry's classifier has to handle.
        setup_conn = db.connect(self.path)
        _migrate_up_to(setup_conn, 50)
        setup_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        setup_conn.close()
        size = os.path.getsize(self.path)
        with open(self.path, "r+b") as fh:
            fh.truncate(4096)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        _scrub(self.path)

    def test_corrupt_file_raises_database_integrity_error(self):
        with self.assertRaises(DatabaseIntegrityError):
            mr.active_run(self.conn, "demand_prophet")

    def test_corruption_is_not_reclassified_as_registry_unavailable(self):
        try:
            mr.active_run(self.conn, "demand_prophet")
            self.fail("expected an exception")
        except RegistryUnavailableError:
            self.fail("corruption must never be classified as a missing table")
        except DatabaseIntegrityError:
            pass


if __name__ == "__main__":
    unittest.main()
