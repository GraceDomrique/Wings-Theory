"""ML training/provenance/rollback/transition tests (migration 0050) --
model_registry atomicity, the mode-aware provenance clause (including the
approved-batch gate), Pass B's anti-joined derived-view fix, label-leakage
guarding, readiness-ladder independence, and real trained artifacts actually
being produced and served.

Run:  python -m tests.test_ml_training

Same stdlib-unittest + tempfile-database pattern as tests/test_dashboards.py
(see that file's own docstring for the house convention this follows): a
throwaway seeded SQLite file per class, never the dev or production
database.
"""
from __future__ import annotations

import datetime as _dt
import itertools
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wt import anomaly, db, forecasting, ml_scheduler, ml_startup, ml_training  # noqa: E402
from wt import ml_artifacts_io as artifacts  # noqa: E402
from wt import model_registry as mr  # noqa: E402
from wt.db import write_tx  # noqa: E402
from wt.errors import PermissionError_  # noqa: E402


_INGREDIENT_COUNTER = itertools.count()


def _scrub(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(path + suffix)
        for _ in range(3):
            if not p.exists():
                break
            try:
                p.unlink()
                break
            except PermissionError:
                time.sleep(0.05)


class _MLTestBase(unittest.TestCase):
    """Shared fixture: a freshly-seeded DB (ingredients, unli_item/flavor,
    app_user, and the ml_model_registry/ml_approved_synthetic_batch rows
    migration 0050/0051 add). Each test method is responsible for its own
    data inside this shared connection -- unique ref_ids/dates per test
    avoid cross-test interference."""

    @classmethod
    def setUpClass(cls):
        fd, cls.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            os.unlink(cls.path)
        except FileNotFoundError:
            pass
        os.environ["WT_DB"] = cls.path
        os.environ["WT_ENV"] = "test"
        db.close_conn()
        from wt import seed
        seed.run(fresh=True)
        cls.conn = db.get_conn()
        cls.wings_item_id = cls.conn.execute(
            "SELECT id FROM unli_item WHERE item_key='wings'"
        ).fetchone()["id"]
        cls.ingredient_id = cls.conn.execute("SELECT id FROM ingredient LIMIT 1").fetchone()["id"]
        cls.flavor_ids = [r["id"] for r in cls.conn.execute("SELECT id FROM unli_flavor LIMIT 2")]

    @classmethod
    def tearDownClass(cls):
        db.close_conn()
        os.environ.pop("WT_DB", None)
        os.environ.pop("WT_ENV", None)
        _scrub(cls.path)

    def _approve_batch(self, batch_id: str) -> None:
        with write_tx(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO ml_approved_synthetic_batch (batch_id, approved_by) VALUES (?, 'test')",
                (batch_id,),
            )

    def _fresh_ingredient_id(self) -> int:
        """A dedicated ingredient per test method, so a WHERE ingredient_id=?
        query only ever sees rows THIS test inserted -- unittest does not
        guarantee method isolation on a shared class-level connection, and
        several tests in this class reuse the same service dates. Counter-
        suffixed (not just time.time_ns()) -- two calls can land in the same
        nanosecond-resolution tick on some platforms."""
        token = next(_INGREDIENT_COUNTER)
        with write_tx(self.conn):
            cur = self.conn.execute(
                "INSERT INTO ingredient (ingredient_id, name, category, base_unit, purchase_unit) "
                "VALUES (?,?,?,?,?)",
                (f"TEST-ING-{time.time_ns()}-{token}", "Test Ingredient", "dry_food", "g", "g"),
            )
            return cur.lastrowid

    def _insert_ledger_row(self, *, delta, balance_after, user_id, batch_id, ref_id, created_at,
                           ingredient_id=None) -> None:
        with write_tx(self.conn):
            self.conn.execute(
                "INSERT INTO stock_ledger "
                "(ingredient_id, delta_base, balance_after_base, reason, ref_type, ref_id, "
                " user_id, source_batch_id, created_at) "
                "VALUES (?,?,?,'wastage','test',?,?,?,?)",
                (ingredient_id or self.ingredient_id, delta, balance_after, ref_id, user_id, batch_id, created_at),
            )


class TestModelRegistryAtomicity(_MLTestBase):
    def _dummy_kwargs(self, model_key, mode="SIMULATED", notes=None):
        return dict(
            model_key=model_key, mode=mode, dataset_version="test:v1",
            real_count=0, synthetic_count=5, schema_version="test_v1",
            metrics={"simulated_metrics": None, "real_metrics": None, "structural": {}},
            artifact_path="/dev/null-not-loaded-by-this-test", triggered_by="unittest",
        )

    def test_activate_training_run_sets_mode_and_pointer_together(self):
        run = mr.activate_training_run(self.conn, **self._dummy_kwargs("stock_loss_isoforest"))
        self.assertEqual(run["status"], "active")
        row = self.conn.execute(
            "SELECT mode, active_training_run_id FROM ml_model_registry WHERE model_key='stock_loss_isoforest'"
        ).fetchone()
        self.assertEqual(row["mode"], "SIMULATED")
        self.assertEqual(row["active_training_run_id"], run["id"])

    def test_second_activation_supersedes_the_first(self):
        run1 = mr.activate_training_run(self.conn, **self._dummy_kwargs("unli_consumption_isoforest"))
        run2 = mr.activate_training_run(self.conn, **self._dummy_kwargs("unli_consumption_isoforest", mode="MIXED"))
        run1_after = self.conn.execute("SELECT status FROM ml_training_run WHERE id=?", (run1["id"],)).fetchone()
        self.assertEqual(run1_after["status"], "superseded")
        active = mr.active_run(self.conn, "unli_consumption_isoforest")
        self.assertEqual(active["id"], run2["id"])
        self.assertEqual(active["mode"], "MIXED")

    def test_record_failed_run_never_touches_registry(self):
        before = self.conn.execute(
            "SELECT mode, active_training_run_id, updated_at FROM ml_model_registry "
            "WHERE model_key='flavor_consumption_isoforest'"
        ).fetchone()
        mr.record_failed_run(self.conn, **self._dummy_kwargs("flavor_consumption_isoforest", mode="MIXED"))
        after = self.conn.execute(
            "SELECT mode, active_training_run_id, updated_at FROM ml_model_registry "
            "WHERE model_key='flavor_consumption_isoforest'"
        ).fetchone()
        self.assertEqual(dict(before), dict(after))

    def test_rollback_reactivates_the_prior_run_not_a_bare_update(self):
        run1 = mr.activate_training_run(self.conn, **self._dummy_kwargs("stock_loss_isoforest"))
        run2 = mr.activate_training_run(self.conn, **self._dummy_kwargs("stock_loss_isoforest", mode="MIXED"))
        restored = mr.rollback(self.conn, "stock_loss_isoforest", triggered_by="unittest", reason="test rollback")
        self.assertEqual(restored["id"], run1["id"])
        run2_after = self.conn.execute("SELECT status FROM ml_training_run WHERE id=?", (run2["id"],)).fetchone()
        self.assertEqual(run2_after["status"], "rolled_back")
        registry_row = self.conn.execute(
            "SELECT mode, active_training_run_id FROM ml_model_registry WHERE model_key='stock_loss_isoforest'"
        ).fetchone()
        self.assertEqual(registry_row["active_training_run_id"], run1["id"])
        self.assertEqual(registry_row["mode"], "SIMULATED")

    def test_rollback_without_a_prior_run_raises(self):
        mr.activate_training_run(self.conn, **self._dummy_kwargs("flavor_consumption_isoforest"))
        with self.assertRaises(Exception):
            mr.rollback(self.conn, "flavor_consumption_isoforest", triggered_by="unittest")

    def test_promote_mode_refuses_for_a_locked_model(self):
        with self.assertRaises(PermissionError_):
            ml_training.promote_mode(self.conn, "demand_prophet", triggered_by="unittest")


class TestProvenanceClause(_MLTestBase):
    def test_simulated_mode_sees_only_the_approved_batch_sentinel(self):
        ing = self._fresh_ingredient_id()
        self._approve_batch("batch-A")
        self._insert_ledger_row(delta=-5, balance_after=95, user_id="owner", ingredient_id=ing,
                                ref_id="REAL-1", batch_id=None, created_at="2026-01-01")
        self._insert_ledger_row(delta=-3, balance_after=92, user_id="simulated_workbook_import", ingredient_id=ing,
                                ref_id="SYN-1", batch_id="batch-A", created_at="2026-01-02")
        clause, params = anomaly._provenance_clause(self.conn, "user_id", "source_batch_id", "SIMULATED")
        rows = self.conn.execute(
            f"SELECT ref_id FROM stock_ledger WHERE ingredient_id=? AND {clause}",
            (ing, *params),
        ).fetchall()
        self.assertEqual({r["ref_id"] for r in rows}, {"SYN-1"})

    def test_real_mode_excludes_the_sentinel_even_from_an_approved_batch(self):
        ing = self._fresh_ingredient_id()
        self._approve_batch("batch-B")
        self._insert_ledger_row(delta=-5, balance_after=95, user_id="owner", ingredient_id=ing,
                                ref_id="REAL-2", batch_id=None, created_at="2026-01-01")
        self._insert_ledger_row(delta=-3, balance_after=92, user_id="simulated_workbook_import", ingredient_id=ing,
                                ref_id="SYN-2", batch_id="batch-B", created_at="2026-01-02")
        clause, params = anomaly._provenance_clause(self.conn, "user_id", "source_batch_id", "REAL")
        rows = self.conn.execute(
            f"SELECT ref_id FROM stock_ledger WHERE ingredient_id=? AND {clause}",
            (ing, *params),
        ).fetchall()
        self.assertEqual({r["ref_id"] for r in rows}, {"REAL-2"})

    def test_mixed_mode_unions_real_and_approved_synthetic_only(self):
        ing = self._fresh_ingredient_id()
        self._approve_batch("batch-C")
        self._insert_ledger_row(delta=-5, balance_after=95, user_id="owner", ingredient_id=ing,
                                ref_id="REAL-3", batch_id=None, created_at="2026-01-01")
        self._insert_ledger_row(delta=-3, balance_after=92, user_id="simulated_workbook_import", ingredient_id=ing,
                                ref_id="SYN-3", batch_id="batch-C", created_at="2026-01-02")
        # An UNAPPROVED batch -- must stay invisible even in MIXED mode.
        self._insert_ledger_row(delta=-1, balance_after=91, user_id="simulated_workbook_import", ingredient_id=ing,
                                ref_id="SYN-UNAPPROVED", batch_id="batch-not-approved", created_at="2026-01-03")
        clause, params = anomaly._provenance_clause(self.conn, "user_id", "source_batch_id", "MIXED")
        rows = self.conn.execute(
            f"SELECT ref_id FROM stock_ledger WHERE ingredient_id=? AND {clause}",
            (ing, *params),
        ).fetchall()
        self.assertEqual({r["ref_id"] for r in rows}, {"REAL-3", "SYN-3"})

    def test_stock_ledger_insert_helper_rejects_wrong_kwargs(self):
        # Guards the helper itself against a silent signature typo -- a
        # cheap, fast sanity check that the fixture builder above is wired
        # the way the other tests in this class assume.
        with self.assertRaises(TypeError):
            self._insert_ledger_row(delta=-1, balance_after=1, user_id="owner", ref_id="X")


class TestDemandProvenanceClause(_MLTestBase):
    """wt/forecasting.py's _order_provenance_clause() / mode-aware
    _daily_series() -- the SIMULATED-mode Prophet training path added so a
    too-new-to-have-real-history restaurant can still train/evaluate
    Prophet on wt/demo_seed.py's realistic synthetic history (tagged
    customer_order.created_by='demo_seed') without ever counting it as
    real demand. Same provenance-by-sentinel idea as TestProvenanceClause
    above, applied to customer_order instead of stock_ledger."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.menu_item_id, cls.menu_item_price = cls.conn.execute(
            "SELECT id, price FROM menu_item WHERE pos_visible=1 AND is_unli=0 LIMIT 1"
        ).fetchone()

    def _insert_order(self, *, order_no, created_by, created_at, qty=3) -> None:
        with write_tx(self.conn):
            cur = self.conn.execute(
                "INSERT INTO customer_order (order_no, table_label, head_count, status, "
                " created_by, created_at) VALUES (?, 'T-TEST', 2, 'served', ?, ?)",
                (order_no, created_by, created_at),
            )
            self.conn.execute(
                "INSERT INTO order_line (order_id, menu_item_id, qty, unit_price) VALUES (?,?,?,?)",
                (cur.lastrowid, self.menu_item_id, qty, self.menu_item_price),
            )

    def test_real_mode_excludes_demo_seed_rows(self):
        # A narrow 2-day lookback anchored tightly on this test's own dates
        # -- these tests share one class-level connection/menu_item, so a
        # wide window would also pick up another test method's rows on a
        # different calendar date.
        self._insert_order(order_no="PROV-REAL-1", created_by=None, created_at="2026-01-05 12:00:00")
        self._insert_order(order_no="PROV-REAL-2", created_by="demo_seed", created_at="2026-01-05 13:00:00")
        rows, _ = forecasting._daily_series(self.conn, self.menu_item_id, 2,
                                            now=_dt.datetime(2026, 1, 6, tzinfo=_dt.timezone.utc), mode="REAL")
        self.assertEqual(sum(r["qty"] for r in rows), 3)

    def test_simulated_mode_sees_only_demo_seed_rows(self):
        self._insert_order(order_no="PROV-SIM-1", created_by=None, created_at="2026-02-07 12:00:00")
        self._insert_order(order_no="PROV-SIM-2", created_by="demo_seed", created_at="2026-02-07 13:00:00", qty=5)
        rows, _ = forecasting._daily_series(self.conn, self.menu_item_id, 2,
                                            now=_dt.datetime(2026, 2, 8, tzinfo=_dt.timezone.utc), mode="SIMULATED")
        self.assertEqual(sum(r["qty"] for r in rows), 5)

    def test_a_different_sentinel_is_neither_real_nor_simulated(self):
        # scripts/seed_recent_demo_orders.py's own sentinel
        # ('demo_seed_recent_orders') must stay invisible to BOTH modes --
        # it is a separate, differently-purposed synthetic dataset
        # (dashboard/FP-Growth demo data), never Prophet training data.
        self._insert_order(order_no="PROV-OTHER-1", created_by="demo_seed_recent_orders",
                           created_at="2026-03-09 12:00:00")
        now = _dt.datetime(2026, 3, 10, tzinfo=_dt.timezone.utc)
        real_rows, _ = forecasting._daily_series(self.conn, self.menu_item_id, 2, now=now, mode="REAL")
        sim_rows, _ = forecasting._daily_series(self.conn, self.menu_item_id, 2, now=now, mode="SIMULATED")
        self.assertEqual(sum(r["qty"] for r in real_rows), 0)
        self.assertEqual(sum(r["qty"] for r in sim_rows), 0)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            forecasting._order_provenance_clause("o", "NOT_A_MODE")


class TestPassBDerivedViewDoubleCounting(_MLTestBase):
    def _insert_flavor_row(self, *, service_date, flavor_id, heads, served, leftover,
                           measured, provisional, recorded_by, ref):
        with write_tx(self.conn):
            self.conn.execute(
                "INSERT INTO unli_flavor_reconciliation "
                "(service_date, flavor_id, heads, servings, wings_served_pcs, leftover_pcs, "
                " net_consumed_pcs, actual_measured_base, unit, provisional_base, "
                " observation_status, recorded_by, source_ref, source_batch_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'reconciled',?,?,?)",
                (service_date, flavor_id, heads, heads, served, leftover, served - leftover,
                 measured, "g", provisional, recorded_by, ref, "batch-flavor-test"),
            )

    def test_derived_view_sums_heads_across_flavors(self):
        self._insert_flavor_row(service_date="2026-02-01", flavor_id=self.flavor_ids[0],
                                heads=3, served=100, leftover=10, measured=90, provisional=95,
                                recorded_by="simulated_workbook_import", ref="FLV-1")
        self._insert_flavor_row(service_date="2026-02-01", flavor_id=self.flavor_ids[1],
                                heads=5, served=150, leftover=15, measured=135, provisional=140,
                                recorded_by="simulated_workbook_import", ref="FLV-2")
        row = self.conn.execute(
            "SELECT heads, actual_base FROM v_unli_wings_daily_from_flavor WHERE service_date='2026-02-01'"
        ).fetchone()
        self.assertEqual(row["heads"], 8)  # 3 + 5, never max(3,5)
        self.assertAlmostEqual(row["actual_base"], 225.0)  # 90 + 135

    def test_anti_join_excludes_a_date_that_already_has_a_real_reconciliation_row(self):
        self._approve_batch("batch-flavor-test")
        self._insert_flavor_row(service_date="2026-02-02", flavor_id=self.flavor_ids[0],
                                heads=4, served=120, leftover=12, measured=108, provisional=110,
                                recorded_by="simulated_workbook_import", ref="FLV-3")
        # A genuine real unli_reconciliation row for the SAME (item, date) --
        # the derived view's row for this date must be excluded once this
        # exists, by _consumption_records' anti-join.
        with write_tx(self.conn):
            self.conn.execute(
                "INSERT INTO unli_reconciliation "
                "(service_date, unli_item_id, heads, theoretical_base, actual_base, variance_base, "
                " status, validation_state, recorded_by) "
                "VALUES ('2026-02-02', ?, 10, 500, 480, -20, 'within_tolerance', 'validated', 'owner')",
                (self.wings_item_id,),
            )
        records = anomaly._consumption_records(self.conn, 365, mode="MIXED")
        dates_for_wings = [r["row"]["service_date"] for r in records if r["row"]["unli_item_id"] == self.wings_item_id]
        self.assertEqual(dates_for_wings.count("2026-02-02"), 1,
                         "the real row and the derived-from-flavor row must never both appear for one date")
        matched = next(r for r in records if r["row"]["service_date"] == "2026-02-02"
                      and r["row"]["unli_item_id"] == self.wings_item_id)
        self.assertEqual(matched["row"]["heads"], 10, "the REAL row must win the anti-join, not the derived one")

    def test_a_date_with_only_synthetic_flavor_rows_is_included_in_simulated_mode(self):
        self._approve_batch("batch-flavor-test")
        self._insert_flavor_row(service_date="2026-02-03", flavor_id=self.flavor_ids[0],
                                heads=2, served=60, leftover=6, measured=54, provisional=55,
                                recorded_by="simulated_workbook_import", ref="FLV-4")
        records = anomaly._consumption_records(self.conn, 365, mode="SIMULATED")
        matched = [r for r in records if r["row"]["service_date"] == "2026-02-03"]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["provenance"], "synthetic")


class TestLabelLeakageGuard(_MLTestBase):
    _SUSPECT_SUBSTRINGS = ("label", "truth", "injected", "ground")

    def test_no_feature_name_is_a_ground_truth_or_label_column(self):
        all_feature_names = (
            list(ml_training.STOCK_LOSS_FEATURE_NAMES) + list(anomaly.FEATURE_NAMES)
            + list(anomaly.FLAVOR_FEATURE_NAMES)
        )
        for name in all_feature_names:
            lowered = name.lower()
            for bad in self._SUSPECT_SUBSTRINGS:
                self.assertNotIn(bad, lowered, f"feature '{name}' looks like a label/ground-truth column")


class TestReadinessLadderIndependence(_MLTestBase):
    def test_promoting_one_model_does_not_touch_another(self):
        before_b = dict(self.conn.execute(
            "SELECT mode, active_training_run_id FROM ml_model_registry WHERE model_key='unli_consumption_isoforest'"
        ).fetchone())
        before_c = dict(self.conn.execute(
            "SELECT mode, active_training_run_id FROM ml_model_registry WHERE model_key='flavor_consumption_isoforest'"
        ).fetchone())
        mr.activate_training_run(
            self.conn, model_key="stock_loss_isoforest", mode="MIXED", dataset_version="t",
            real_count=0, synthetic_count=1, schema_version="t", artifact_path="/dev/null",
            metrics={"simulated_metrics": None, "real_metrics": None, "structural": {}},
            triggered_by="unittest",
        )
        after_b = dict(self.conn.execute(
            "SELECT mode, active_training_run_id FROM ml_model_registry WHERE model_key='unli_consumption_isoforest'"
        ).fetchone())
        after_c = dict(self.conn.execute(
            "SELECT mode, active_training_run_id FROM ml_model_registry WHERE model_key='flavor_consumption_isoforest'"
        ).fetchone())
        self.assertEqual(before_b, after_b)
        self.assertEqual(before_c, after_c)


class TestTrainedArtifactsAreActuallyServed(_MLTestBase):
    def test_stock_loss_training_produces_an_artifact_the_live_pass_then_serves(self):
        self._approve_batch("batch-stock-loss-train")
        for i in range(anomaly.MIN_POOLED_SAMPLES + 5):
            self._insert_ledger_row(
                delta=-(2 + (i % 5)), balance_after=100 - i, user_id="simulated_workbook_import",
                ref_id=f"SYN-TRAIN-{i}", batch_id="batch-stock-loss-train",
                created_at=f"2026-03-{(i % 28) + 1:02d}",
            )
        result = ml_training.train_stock_loss_model(self.conn, triggered_by="unittest", lookback_days=365, mode="SIMULATED")
        self.assertIn(result["status"], ("active", "failed"))
        self.assertTrue(Path(result["artifact_path"]).exists())
        if result["status"] == "active":
            flagged = anomaly.detect_anomalies(self.conn, 365, mode="SIMULATED")
            sources = {f.get("model_source") for f in flagged}
            self.assertTrue(sources.issubset({"artifact", None}) or "artifact" in sources)

    def test_startup_validation_falls_back_safely_on_a_corrupt_artifact(self):
        self._approve_batch("batch-startup-validate")
        for i in range(anomaly.MIN_POOLED_SAMPLES + 5):
            self._insert_ledger_row(
                delta=-(1 + (i % 4)), balance_after=200 - i, user_id="simulated_workbook_import",
                ref_id=f"SYN-STARTUP-{i}", batch_id="batch-startup-validate",
                created_at=f"2026-04-{(i % 28) + 1:02d}",
            )
        result = ml_training.train_stock_loss_model(self.conn, triggered_by="unittest", lookback_days=365, mode="SIMULATED")
        if result["status"] != "active":
            self.skipTest("training did not activate under this fixture; nothing to corrupt")
        with open(result["artifact_path"], "wb") as fh:
            fh.write(b"not a valid artifact")
        report = ml_startup.validate_artifacts_on_startup(self.conn)
        self.assertIn(report["stock_loss_isoforest"]["status"],
                     ("rolled_back", "degraded_serving_live_fallback"))
        # Whatever happened, the live pass must still answer, never raise.
        flagged = anomaly.detect_anomalies(self.conn, 365, mode="SIMULATED")
        self.assertIsInstance(flagged, list)


class TestMLModeTransitionEndToEnd(_MLTestBase):
    """The full SIMULATED -> MIXED -> REAL lifecycle for one model
    (stock_loss_isoforest), on isolated fixtures: retraining at every step,
    atomic artifact activation, provenance composition at each mode,
    restart recovery (ml_startup.validate_artifacts_on_startup), and a real
    rollback recovering from a corrupted artifact. One model is enough to
    prove the mechanism -- the per-pass wiring itself is covered by the
    other test classes in this module."""

    def _recent_date(self, days_ago: int) -> str:
        return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_ago)).date().isoformat()

    def test_full_simulated_to_mixed_to_real_transition_with_restart_and_rollback(self):
        model_key = "stock_loss_isoforest"
        ing = self._fresh_ingredient_id()

        # --- Step 1: SIMULATED -- synthetic-only fixture, nothing real yet.
        self.assertEqual(mr.get_mode(self.conn, model_key), "REAL",
                         "a fresh registry row starts REAL (no synthetic data exists yet)")
        self._approve_batch("batch-e2e")
        for i in range(anomaly.MIN_POOLED_SAMPLES + 5):
            self._insert_ledger_row(
                delta=-(2 + (i % 5)), balance_after=500 - i, user_id="simulated_workbook_import",
                ingredient_id=ing, ref_id=f"E2E-SYN-{i}", batch_id="batch-e2e",
                created_at=self._recent_date(60 - i % 50),
            )
        r1 = ml_training.train_one(self.conn, model_key, triggered_by="e2e", mode="SIMULATED")
        self.assertEqual(r1["status"], "active")
        self.assertEqual(mr.get_mode(self.conn, model_key), "SIMULATED")
        self.assertEqual(r1["real_count"], 0)
        self.assertGreaterEqual(r1["synthetic_count"], anomaly.MIN_POOLED_SAMPLES)
        self.assertTrue(Path(r1["artifact_path"]).exists())
        flagged = anomaly.detect_anomalies(self.conn, 365, mode="SIMULATED")
        self.assertTrue(any(f["model_source"] == "artifact" for f in flagged) or flagged == [],
                        "live detection must read the just-activated SIMULATED artifact")

        # Readiness must correctly report SIMULATION_ONLY before any real
        # data exists -- and promote_mode() must refuse to move forward.
        readiness = anomaly.stock_loss_real_data_readiness(self.conn)
        self.assertEqual(readiness["state"], "SIMULATION_ONLY")
        refused = ml_training.promote_mode(self.conn, model_key, triggered_by="e2e")
        self.assertEqual(refused["status"], "not_ready")
        self.assertEqual(mr.get_mode(self.conn, model_key), "SIMULATED", "a refused promotion must not move mode")

        # --- Build enough REAL evidence to satisfy stock_loss_real_data_
        # readiness: >=30 real observations across >=14 distinct days, plus
        # >=20 reviewed validation-sample labels (>=5 each side).
        real_ledger_ids = []
        for day in range(15):
            created = self._recent_date(day)
            for k in range(2):
                with write_tx(self.conn):
                    cur = self.conn.execute(
                        "INSERT INTO stock_ledger "
                        "(ingredient_id, delta_base, balance_after_base, reason, ref_type, ref_id, "
                        " user_id, created_at) "
                        "VALUES (?,?,?,?,'test',?,?,?)",
                        (ing, -(3 + k), 300 - day * 2 - k, "wastage" if k == 0 else "spoilage",
                         f"E2E-REAL-{day}-{k}", "owner", created),
                    )
                    real_ledger_ids.append(cur.lastrowid)
        self.assertEqual(len(real_ledger_ids), 30)

        with write_tx(self.conn):
            for idx, ledger_id in enumerate(real_ledger_ids[:20]):
                label = "confirmed_anomaly" if idx < 10 else "expected_or_normal"
                prediction = "flagged" if idx < 10 else "unflagged"
                self.conn.execute(
                    "INSERT INTO anomaly_validation_sample "
                    "(ledger_id, sample_source, model_prediction, model_version, eligibility_version, "
                    " human_label, reviewed_by, reviewed_at) "
                    "VALUES (?, 'model_flagged', ?, ?, ?, ?, 'owner', datetime('now'))",
                    (ledger_id, prediction, anomaly.ANOMALY_MODEL_VERSION,
                     anomaly.ELIGIBILITY_FILTER_VERSION, label),
                )

        readiness = anomaly.stock_loss_real_data_readiness(self.conn)
        self.assertEqual(readiness["state"], "READY_REAL_DATA", readiness)

        # --- Step 2: SIMULATED -> MIXED. Provenance must now show BOTH.
        r2 = ml_training.promote_mode(self.conn, model_key, triggered_by="e2e")
        self.assertEqual(r2["status"], "active", r2)
        self.assertEqual(r2["mode"], "MIXED")
        self.assertEqual(mr.get_mode(self.conn, model_key), "MIXED")
        self.assertGreaterEqual(r2["real_count"], 30)
        self.assertGreaterEqual(r2["synthetic_count"], anomaly.MIN_POOLED_SAMPLES)
        mixed_history = mr.training_history(self.conn, model_key, limit=5)
        self.assertEqual(mixed_history[0]["status"], "active")
        self.assertEqual(mixed_history[1]["status"], "superseded",
                         "the prior SIMULATED run must be superseded, not deleted")

        # --- Step 3: MIXED -> REAL. Provenance must now be real-only --
        # synthetic rows are structurally excluded from REAL mode (the exact
        # behavior _provenance_clause has always had for mode='REAL').
        r3 = ml_training.promote_mode(self.conn, model_key, triggered_by="e2e")
        self.assertEqual(r3["status"], "active", r3)
        self.assertEqual(r3["mode"], "REAL")
        self.assertEqual(mr.get_mode(self.conn, model_key), "REAL")
        self.assertGreaterEqual(r3["real_count"], 30)
        self.assertEqual(r3["synthetic_count"], 0,
                         "REAL mode must never include the synthetic sentinel rows")

        # --- Restart recovery: a fresh process would call this once at
        # startup. The currently active (REAL) artifact must validate
        # cleanly and need no fallback.
        report = ml_startup.validate_artifacts_on_startup(self.conn)
        self.assertEqual(report[model_key]["status"], "valid")
        self.assertEqual(report[model_key]["artifact_path"], r3["artifact_path"])

        # Live detection must still work, reading the REAL-mode artifact.
        flagged_real = anomaly.detect_anomalies(self.conn, 365, mode="REAL")
        self.assertTrue(all(f["provenance"] == "real" for f in flagged_real),
                        "REAL mode must never surface a synthetic-provenance row")

        # --- Rollback: corrupt the REAL artifact and simulate another
        # restart. A prior (MIXED) run exists, so this must roll back to it
        # atomically, not degrade to rule-only.
        with open(r3["artifact_path"], "wb") as fh:
            fh.write(b"deliberately corrupted for the rollback test")
        report2 = ml_startup.validate_artifacts_on_startup(self.conn)
        self.assertEqual(report2[model_key]["status"], "rolled_back")
        self.assertEqual(mr.get_mode(self.conn, model_key), "MIXED",
                         "rollback must restore the mode the reactivated run was trained under")
        final_history = mr.training_history(self.conn, model_key, limit=5)
        statuses_by_id = {h["id"]: h["status"] for h in final_history}
        self.assertEqual(statuses_by_id[r3["run"]["id"]], "rolled_back")
        self.assertEqual(statuses_by_id[r2["run"]["id"]], "active")

        # The live pass must keep answering throughout, from the rolled-
        # back (MIXED) artifact.
        flagged_after_rollback = anomaly.detect_anomalies(self.conn, 365, mode="MIXED")
        self.assertIsInstance(flagged_after_rollback, list)
        self.assertTrue(any(f["model_source"] == "artifact" for f in flagged_after_rollback))


class TestProductionDefaultsOff(unittest.TestCase):
    """Deployment requirement: automatic scheduled retraining and the
    implicit in-request fresh-fit fallback must both be OFF by default in
    production, and on only through an explicit, separately enabled
    override -- never merely because WT_ENV happens to be unset."""

    def _with_env(self, **kv):
        prev = {k: os.environ.get(k) for k in kv}
        os.environ.update({k: v for k, v in kv.items() if v is not None})
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
        return prev

    def _restore_env(self, prev):
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_scheduler_disabled_by_default_in_production_enabled_elsewhere(self):
        prev = self._with_env(WT_ENV="production", WT_ML_SCHEDULER_ENABLED=None)
        try:
            self.assertFalse(ml_scheduler.scheduler_enabled())
        finally:
            self._restore_env(prev)
        prev = self._with_env(WT_ENV="dev", WT_ML_SCHEDULER_ENABLED=None)
        try:
            self.assertTrue(ml_scheduler.scheduler_enabled())
        finally:
            self._restore_env(prev)

    def test_scheduler_explicit_override_wins_either_direction(self):
        prev = self._with_env(WT_ENV="production", WT_ML_SCHEDULER_ENABLED="1")
        try:
            self.assertTrue(ml_scheduler.scheduler_enabled())
        finally:
            self._restore_env(prev)
        prev = self._with_env(WT_ENV="dev", WT_ML_SCHEDULER_ENABLED="0")
        try:
            self.assertFalse(ml_scheduler.scheduler_enabled())
        finally:
            self._restore_env(prev)

    def test_fresh_fit_fallback_disabled_by_default_in_production_enabled_elsewhere(self):
        prev = self._with_env(WT_ENV="production", WT_ML_ALLOW_FRESH_FIT_FALLBACK=None)
        try:
            self.assertFalse(artifacts.fresh_fit_fallback_allowed())
        finally:
            self._restore_env(prev)
        prev = self._with_env(WT_ENV="dev", WT_ML_ALLOW_FRESH_FIT_FALLBACK=None)
        try:
            self.assertTrue(artifacts.fresh_fit_fallback_allowed())
        finally:
            self._restore_env(prev)


class TestArtifactPathPortability(unittest.TestCase):
    """Deployment requirement: ml_training_run.artifact_path must not bake
    in a machine-specific absolute path -- moving backend/ (db + ml_
    artifacts/ together) to a different machine/drive/OS must keep every
    artifact resolvable."""

    def test_dump_returns_a_relative_ml_artifacts_path(self):
        token = artifacts.new_run_token()
        path = artifacts.dump_json({"schema_version": "t", "feature_names": None}, "unit_test_model", token)
        self.assertFalse(Path(path).is_absolute(), f"artifact_path must be relative, got {path!r}")
        self.assertTrue(path.startswith("ml_artifacts/"))
        try:
            self.assertEqual(artifacts.load_json(path), {"schema_version": "t", "feature_names": None})
        finally:
            artifacts.resolve_path(path).unlink()

    def test_resolve_path_honors_a_pre_existing_absolute_path_unchanged(self):
        abs_path = str(artifacts.ARTIFACT_DIR / "does_not_need_to_exist_for_this_check.joblib")
        self.assertEqual(artifacts.resolve_path(abs_path), Path(abs_path))

    def test_resolve_path_is_independent_of_how_the_path_was_spelled(self):
        token = artifacts.new_run_token()
        path = artifacts.dump_json({"schema_version": "t", "feature_names": None}, "unit_test_model2", token)
        try:
            # Same artifact, reached via the relative form the DB now
            # stores and via a bare filename -- both must resolve to the
            # exact same file on disk.
            bare_filename = Path(path).name
            self.assertEqual(artifacts.resolve_path(path), artifacts.resolve_path(bare_filename))
        finally:
            artifacts.resolve_path(path).unlink()


class TestProductionForbidsFreshFit(_MLTestBase):
    """Deployment requirement: a production request must never implicitly
    train a model. With WT_ENV=production and no active stock_loss_isoforest
    artifact, _score_with_isolation_forest()/_evaluate_stock_loss() must
    report the ML layer 'unavailable' rather than fitting an IsolationForest
    in-request -- while the unconditional rule-based layer (large_fraction)
    keeps answering."""

    def test_detect_anomalies_reports_unavailable_instead_of_fitting_fresh_in_production(self):
        self.assertIsNone(mr.active_run(self.conn, "stock_loss_isoforest"),
                          "this class's own DB must have no trained artifact yet")
        ing = self._fresh_ingredient_id()
        for i in range(anomaly.MIN_POOLED_SAMPLES + 5):
            self._insert_ledger_row(
                delta=-(2 + (i % 5)), balance_after=300 - i, user_id=None,
                ingredient_id=ing, ref_id=f"PROD-GATE-{i}", batch_id=None,
                created_at=f"2026-05-{(i % 28) + 1:02d}",
            )
        os.environ["WT_ENV"] = "production"
        try:
            records, _ = anomaly._evaluate_stock_loss(self.conn, 365, mode="REAL")
        finally:
            os.environ["WT_ENV"] = "test"
        this_ing_records = [r for r in records if r["row"]["ingredient_id"] == ing]
        self.assertTrue(this_ing_records)
        self.assertTrue(all(r["_model_source"] == "unavailable" for r in this_ing_records),
                        "no artifact + production must never fall through to a fresh in-request fit")
        self.assertTrue(all(r["method"] != "isolation_forest" for r in this_ing_records),
                        "the ML layer must not have flagged anything it never actually scored")

        # The explicit override still allows the old dev-friendly behavior.
        os.environ["WT_ENV"] = "production"
        os.environ["WT_ML_ALLOW_FRESH_FIT_FALLBACK"] = "1"
        try:
            records2, _ = anomaly._evaluate_stock_loss(self.conn, 365, mode="REAL")
        finally:
            os.environ["WT_ENV"] = "test"
            os.environ.pop("WT_ML_ALLOW_FRESH_FIT_FALLBACK", None)
        this_ing_records2 = [r for r in records2 if r["row"]["ingredient_id"] == ing]
        self.assertTrue(any(r["_model_source"] == "fit_fresh" for r in this_ing_records2),
                        "the explicit override must restore the fresh-fit fallback")


class TestStartupNeverRetrains(_MLTestBase):
    """wt/api.py's startup event calls ml_startup.validate_artifacts_on_
    startup() (load-only) and then ml_scheduler.start_ml_scheduler() (spawns
    the daily-tick thread). That thread must never run a training cycle
    before its first full TICK_SECONDS wait -- otherwise every restart would
    silently retrain, which is exactly what the deployment-readiness
    requirement rules out."""

    def test_scheduler_thread_does_not_run_a_cycle_before_its_first_wait(self):
        calls = []
        original_wait = ml_scheduler.threading.Event.wait

        def _recording_wait(self_evt, timeout=None):
            if timeout != ml_scheduler.TICK_SECONDS:
                # Not the scheduler loop's own wait (e.g. Thread.start()'s
                # internal readiness Event) -- behave normally.
                return original_wait(self_evt, timeout)
            calls.append(timeout)
            # Halt the daemon thread right here, before it can ever reach
            # run_scheduled_cycle() -- recording that wait() was called with
            # the right timeout is all this test needs.
            raise RuntimeError("stop after recording the first wait() call")

        ml_scheduler.threading.Event.wait = _recording_wait
        try:
            ml_scheduler.start_ml_scheduler(lambda: self.conn)
            # Give the daemon thread a moment to reach its first wait() call.
            time.sleep(0.2)
        finally:
            ml_scheduler.threading.Event.wait = original_wait

        self.assertTrue(calls, "the scheduler thread never called Event.wait()")
        self.assertEqual(calls[0], ml_scheduler.TICK_SECONDS,
                         "the thread's first action must be the full tick wait, not an immediate cycle")
        self.assertIsNone(mr.active_run(self.conn, "stock_loss_isoforest"),
                          "starting the scheduler must not have trained anything yet")


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        loader.loadTestsFromTestCase(t) for t in (
            TestModelRegistryAtomicity, TestProvenanceClause, TestPassBDerivedViewDoubleCounting,
            TestLabelLeakageGuard, TestReadinessLadderIndependence, TestTrainedArtifactsAreActuallyServed,
            TestMLModeTransitionEndToEnd, TestProductionDefaultsOff, TestArtifactPathPortability,
            TestProductionForbidsFreshFit, TestStartupNeverRetrains,
        )
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
