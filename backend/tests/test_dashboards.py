"""Phase 2 audit (2026-10): targeted tests for the four dashboard endpoints
(Admin Today, Owner Overview, Owner Sales, Admin Sales) — role authorization,
period-comparison arithmetic, and empty-data handling.

Run:  python -m tests.test_dashboards

Same stdlib-unittest + FastAPI TestClient + tempfile-database pattern as
tests/run_tests.py's TestConsoleAPI/TestVarianceReviewAPI classes (see those
for the house convention this file follows): every test runs against a
throwaway seeded SQLite file, never the dev or production database, and the
file is removed in tearDownClass.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wt import db, owner_provisioning  # noqa: E402
from wt import sales_overview  # noqa: E402


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


class TestDashboardEndpoints(unittest.TestCase):
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
        cls.conn.execute("UPDATE app_user SET must_change_password = 0")

        from fastapi.testclient import TestClient
        from wt.api import app
        cls.client = TestClient(app)

        def token(username, password="wingstheory"):
            r = cls.client.post("/auth/login", json={"username": username, "password": password})
            return r.json()["token"]

        # username 'owner' seeds role='admin' (see wt/seed.py's own comment —
        # an unfortunate but pre-existing naming collision in the fixture,
        # not something this test file introduces).
        cls.H_ADMIN = {"Authorization": f"Bearer {token('owner')}"}

        provisioned = owner_provisioning.provision_first_owner(cls.conn, "biz_owner", "B. Owner")
        cls.conn.execute("UPDATE app_user SET must_change_password=0 WHERE username='biz_owner'")
        cls.H_OWNER = {"Authorization": f"Bearer {token('biz_owner', provisioned['temporary_password'])}"}

    @classmethod
    def tearDownClass(cls):
        db.close_conn()
        os.environ.pop("WT_DB", None)
        os.environ.pop("WT_ENV", None)
        _scrub(cls.path)

    # -----------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------
    def test_all_four_endpoints_require_authentication(self):
        for path in (
            "/console/dashboard/admin-today", "/console/dashboard/owner-overview",
            "/console/sales/admin", "/console/sales/owner",
        ):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 401, path)

    # -----------------------------------------------------------------
    # RBAC — the finding this audit pass exists to fix: an admin token must
    # not be able to pull the Owner-only business-intelligence payloads just
    # because the frontend nav never shows them a link to it.
    # -----------------------------------------------------------------
    def test_admin_cannot_access_owner_overview(self):
        r = self.client.get("/console/dashboard/owner-overview", headers=self.H_ADMIN)
        self.assertEqual(r.status_code, 403)

    def test_admin_cannot_access_owner_sales(self):
        r = self.client.get("/console/sales/owner", headers=self.H_ADMIN)
        self.assertEqual(r.status_code, 403)

    def test_owner_role_can_access_owner_overview(self):
        r = self.client.get("/console/dashboard/owner-overview", headers=self.H_OWNER)
        self.assertEqual(r.status_code, 200)

    def test_owner_role_can_access_owner_sales(self):
        r = self.client.get("/console/sales/owner", headers=self.H_OWNER)
        self.assertEqual(r.status_code, 200)

    def test_both_roles_can_access_admin_today(self):
        """Unchanged by this audit, on purpose: Admin Today/Admin Sales are
        operational screens, not strategic BI — owner legitimately has
        deeper read access across this console than admin does everywhere
        else (audit.read, inventory.read, instruction.read all list owner
        too), so an owner account being able to pull admin's own
        operational dashboard was never the gap this pass closes."""
        for headers in (self.H_ADMIN, self.H_OWNER):
            r = self.client.get("/console/dashboard/admin-today", headers=headers)
            self.assertEqual(r.status_code, 200)

    def test_both_roles_can_access_admin_sales(self):
        for headers in (self.H_ADMIN, self.H_OWNER):
            r = self.client.get("/console/sales/admin", headers=headers)
            self.assertEqual(r.status_code, 200)

    # -----------------------------------------------------------------
    # Empty-data handling — a freshly seeded shop has (at most) a handful of
    # demo orders and no sales history to speak of; none of the four
    # endpoints may 500 or divide by zero against that.
    # -----------------------------------------------------------------
    def test_admin_today_handles_empty_data(self):
        r = self.client.get("/console/dashboard/admin-today", headers=self.H_ADMIN)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("storeStatus", "todaySales", "inventoryPulse", "orderActivity", "recentOrders"):
            self.assertIn(key, body)

    def test_owner_overview_handles_empty_data(self):
        r = self.client.get("/console/dashboard/owner-overview", headers=self.H_OWNER)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("businessPulse", body)
        self.assertIn("customerPulse", body)

    def test_owner_sales_handles_empty_data_for_every_period(self):
        for period in ("this_week", "this_month", "this_year"):
            r = self.client.get("/console/sales/owner", headers=self.H_OWNER, params={"period": period})
            self.assertEqual(r.status_code, 200, period)
            body = r.json()
            self.assertIn("howAreSalesDoing", body)
            self.assertIn("customerFavorites", body)
            # an empty comparison period must read as "no data", never a
            # fabricated 0% or a crash
            self.assertIsNone(body["howAreSalesDoing"]["deltas"]["revenue_pct"])

    def test_admin_sales_handles_empty_data_for_every_period(self):
        for period in ("today", "this_week", "this_month"):
            r = self.client.get("/console/sales/admin", headers=self.H_ADMIN, params={"period": period})
            self.assertEqual(r.status_code, 200, period)
            self.assertIn("salesSummary", r.json())

    def test_owner_sales_custom_period_validates_dates(self):
        r = self.client.get("/console/sales/owner", headers=self.H_OWNER,
                            params={"period": "custom", "period_start": "2026-01-01", "period_end": "2026-01-31"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["period"]["startDate"], "2026-01-01")
        self.assertEqual(r.json()["period"]["endDate"], "2026-01-31")

    # -----------------------------------------------------------------
    # Items per Order — Items per Order = Eligible Items Sold / Eligible
    # Orders; Percentage Change = ((Current − Previous) / Previous) × 100;
    # zero denominators must come back as "not computed", never a crash or
    # an invented number.
    # -----------------------------------------------------------------
    def test_items_per_order_formula(self):
        self.assertEqual(sales_overview._items_per_order(100, 25), 4.0)
        self.assertEqual(sales_overview._items_per_order(0, 0), None)
        self.assertEqual(sales_overview._items_per_order(5, 0), None)

    def test_items_per_order_percentage_change_formula(self):
        from wt import business_overview as bo
        current = sales_overview._items_per_order(120, 30)   # 4.0
        prior = sales_overview._items_per_order(80, 20)      # 4.0
        self.assertEqual(bo._delta_pct(current, prior), 0.0)

        current = sales_overview._items_per_order(150, 30)   # 5.0
        prior = sales_overview._items_per_order(80, 20)      # 4.0
        self.assertEqual(bo._delta_pct(current, prior), 25.0)

        # zero eligible orders in the comparison period -> no percentage,
        # not a ZeroDivisionError and not an invented 0%/100%
        prior_zero = sales_overview._items_per_order(0, 0)
        self.assertIsNone(bo._delta_pct(current, prior_zero))

    def test_owner_sales_items_per_order_field_present_and_safe(self):
        r = self.client.get("/console/sales/owner", headers=self.H_OWNER)
        self.assertEqual(r.status_code, 200)
        ipo = r.json()["whatChanged"]["itemsPerOrder"]
        self.assertIn("current", ipo)
        self.assertIn("prior", ipo)
        self.assertIn("pct", ipo)

    # -----------------------------------------------------------------
    # Sales -> Stock: a genuinely critical/out ingredient must never be
    # silently dropped just because it isn't behind a top-10 seller.
    # -----------------------------------------------------------------
    def test_sales_to_stock_never_silently_drops_a_critical_risk(self):
        top_risks = [
            {"ingredientId": 999999, "name": "Unlinked Critical Ingredient",
             "status": "critical", "daysOfSupply": 0.2},
        ]
        # `current` has no items at all, so nothing can link via recipe —
        # the backfill pass is the only thing that can surface this risk.
        out = sales_overview._sales_to_stock(self.conn, current={}, top_risks=top_risks)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["ingredientName"], "Unlinked Critical Ingredient")
        self.assertFalse(out[0]["linkedToTopSeller"])

    def test_sales_to_stock_ignores_non_critical_unlinked_risks(self):
        """at_risk (not yet out/critical) ingredients are NOT force-included
        when unlinked to a top seller — the guarantee is "no critical risk
        goes missing", not "show every ingredient on the watch list"."""
        top_risks = [
            {"ingredientId": 1, "name": "Merely At Risk", "status": "at_risk", "daysOfSupply": 6.0},
        ]
        out = sales_overview._sales_to_stock(self.conn, current={}, top_risks=top_risks)
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
