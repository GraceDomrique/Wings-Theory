"""Data-quality audit — read-only, deterministic checks over the real
production tables the ML layer (forecasting.py, anomaly.py, associations.py,
depletion.py) actually reads from.

Every check here answers one yes/no question with a SQL query against real
rows, reports a count plus a handful of example row ids, and changes nothing.
No check "fixes" a questionable row — see the module-level docstring
convention already established by anomaly.py/associations.py: this system
flags and explains, it does not silently correct historical data. A finding
here is a data-quality fact the thesis's Chapter 4 "data limitations" section
should cite, not something for this module to resolve on its own.
"""
from __future__ import annotations

import sqlite3

from . import timeutil
from .forecasting import MIN_DEMAND_SAMPLE_DAYS

EXAMPLE_LIMIT = 5


def _check(name: str, description: str, rows: list, example_key: str | None = None) -> dict:
    examples = [dict(r) for r in rows[:EXAMPLE_LIMIT]]
    return {
        "check": name,
        "description": description,
        "count": len(rows),
        "status": "ok" if not rows else "found",
        "examples": examples,
    }


def _duplicate_orders(conn: sqlite3.Connection) -> dict:
    """Two non-void orders with the same client_request_id would mean the
    idempotency guard (migration 0009) failed -- this checks the guard held,
    it does not re-implement it."""
    rows = conn.execute(
        """
        SELECT client_request_id, COUNT(*) AS n FROM customer_order
         WHERE client_request_id IS NOT NULL
         GROUP BY client_request_id HAVING COUNT(*) > 1
        """
    ).fetchall()
    return _check("duplicate_orders",
                 "Non-void orders sharing the same client_request_id (idempotency guard failure)", rows)


def _duplicate_ledger_entries(conn: sqlite3.Connection) -> dict:
    """Two stock_ledger rows for the same ingredient/reason/delta/timestamp
    AND the same (ref_type, ref_id) -- i.e. genuinely traceable to the same
    underlying event (the same order, the same wastage entry), not merely
    coincidentally identical numbers. Matching on ingredient/reason/delta/
    timestamp alone is not a safe signature: two different customers
    ordering the same popular item in the same second produce identical
    tuples by pure coincidence, which is not a data-quality problem —
    requiring ref_id to also match is what tells the two apart. Rows with
    NULL ref_id (older entries predating that column being populated
    end-to-end) are excluded rather than treated as one enormous group."""
    rows = conn.execute(
        """
        SELECT ingredient_id, reason, delta_base, created_at, ref_type, ref_id, COUNT(*) AS n
          FROM stock_ledger
         WHERE ref_id IS NOT NULL
         GROUP BY ingredient_id, reason, delta_base, created_at, ref_type, ref_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    return _check("duplicate_ledger_entries",
                 "stock_ledger rows identical in ingredient/reason/delta/timestamp AND "
                 "traceable to the same (ref_type, ref_id) event", rows)


def _missing_timestamps(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT id FROM customer_order WHERE created_at IS NULL OR created_at = ''"
    ).fetchall()
    rows += conn.execute(
        "SELECT id FROM stock_ledger WHERE created_at IS NULL OR created_at = ''"
    ).fetchall()
    return _check("missing_timestamps", "customer_order/stock_ledger rows with no created_at", rows)


def _orphaned_order_lines(conn: sqlite3.Connection) -> dict:
    """order_line rows pointing at a menu_item_id or order_id that no longer
    resolves -- would silently under-count demand and basket contents in
    forecasting.py/associations.py without ever raising an error."""
    rows = conn.execute(
        """
        SELECT ol.id, ol.order_id, ol.menu_item_id FROM order_line ol
         LEFT JOIN customer_order o ON o.id = ol.order_id
         LEFT JOIN menu_item m ON m.id = ol.menu_item_id
         WHERE o.id IS NULL OR m.id IS NULL
        """
    ).fetchall()
    return _check("orphaned_order_lines", "order_line rows with a dangling order_id or menu_item_id", rows)


def _orphaned_ledger_ingredients(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT l.id, l.ingredient_id FROM stock_ledger l
         LEFT JOIN ingredient i ON i.id = l.ingredient_id
         WHERE i.id IS NULL
        """
    ).fetchall()
    return _check("orphaned_ledger_ingredients", "stock_ledger rows whose ingredient_id no longer resolves", rows)


def _negative_or_invalid_quantities(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT id, qty FROM order_line WHERE qty IS NULL OR qty <= 0").fetchall()
    return _check("negative_or_invalid_order_qty", "order_line rows with qty <= 0 or NULL", rows)


def _void_orders_in_active_state(conn: sqlite3.Connection) -> dict:
    """A void order whose reservation still shows 'held'/'committed' would
    mean release_order/void_after_commit never ran for it -- checked here
    rather than assumed from the status column alone."""
    rows = conn.execute(
        """
        SELECT o.id FROM customer_order o
         WHERE o.status = 'void'
           AND EXISTS (SELECT 1 FROM reservation r WHERE r.order_id = o.id AND r.state != 'released')
        """
    ).fetchall()
    return _check("void_orders_with_unreleased_reservation",
                 "Void orders whose reservation rows were never released", rows)


def _missing_recipe_relationships(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT id, name FROM menu_item WHERE pos_visible = 1 AND recipe_id IS NULL AND is_unli = 0"
    ).fetchall()
    return _check("menu_items_missing_recipe",
                 "POS-visible, non-unlimited menu items with no recipe_id (unforecastable at the ingredient level)", rows)


def _missing_supplier_info(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT id, name FROM ingredient WHERE is_active = 1 AND (supplier IS NULL OR supplier = '')"
    ).fetchall()
    return _check("ingredients_missing_supplier", "Active ingredients with no supplier recorded", rows)


def _missing_reorder_points(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT i.id, i.name FROM ingredient i JOIN v_ingredient_readiness r ON r.id = i.id
         WHERE i.is_active = 1 AND r.is_deductable = 1 AND i.reorder_point_base IS NULL
        """
    ).fetchall()
    return _check("ingredients_missing_reorder_point",
                 "Deductable ingredients with no owner-configured reorder point "
                 "(system falls back to a derived suggestion — see forecasting.py's ROP note)", rows)


def _unit_inconsistencies(conn: sqlite3.Connection) -> dict:
    """pack_size <= 0 or yield_factor <= 0 would corrupt every purchase-
    quantity calculation downstream (division/multiplication by a
    nonsensical factor) -- this is the one check that overlaps with a
    correctness bug rather than a missing-data gap."""
    rows = conn.execute(
        """
        SELECT id, name, pack_size, yield_factor FROM ingredient
         WHERE is_active = 1 AND (pack_size IS NOT NULL AND pack_size <= 0
                                  OR yield_factor IS NOT NULL AND yield_factor <= 0)
        """
    ).fetchall()
    return _check("invalid_pack_or_yield", "Ingredients with pack_size or yield_factor <= 0", rows)


def _insufficient_history_items(conn: sqlite3.Connection, lookback_days: int) -> dict:
    bd = timeutil.sql_business_date("o.created_at")
    rows = conn.execute(
        f"""
        SELECT m.id, m.name, COUNT(DISTINCT {bd}) AS active_days
          FROM menu_item m
          LEFT JOIN order_line ol ON ol.menu_item_id = m.id
          LEFT JOIN customer_order o ON o.id = ol.order_id AND o.status != 'void'
                                     AND o.created_at >= ?
         WHERE m.pos_visible = 1
         GROUP BY m.id
        HAVING active_days < ?
        """,
        (timeutil.utc_bound_n_manila_days_ago(lookback_days), MIN_DEMAND_SAMPLE_DAYS),
    ).fetchall()
    return _check("insufficient_history_menu_items",
                 f"POS-visible menu items with fewer than {MIN_DEMAND_SAMPLE_DAYS} real sale-days "
                 f"in the last {lookback_days} days (forecasting.py reports these as insufficient "
                 "data rather than forcing a forecast)", rows)


def data_quality_report(conn: sqlite3.Connection, lookback_days: int = 90) -> dict:
    """Runs every check above and returns one machine-readable report.
    Nothing here writes to the database or alters any row — see the module
    docstring."""
    checks = [
        _duplicate_orders(conn),
        _duplicate_ledger_entries(conn),
        _missing_timestamps(conn),
        _orphaned_order_lines(conn),
        _orphaned_ledger_ingredients(conn),
        _negative_or_invalid_quantities(conn),
        _void_orders_in_active_state(conn),
        _missing_recipe_relationships(conn),
        _missing_supplier_info(conn),
        _missing_reorder_points(conn),
        _unit_inconsistencies(conn),
        _insufficient_history_items(conn, lookback_days),
    ]
    return {
        "generated_at": timeutil.utc_now().isoformat(timespec="seconds"),
        "lookback_days": lookback_days,
        "checks_run": len(checks),
        "checks_with_findings": sum(1 for c in checks if c["status"] == "found"),
        "checks": checks,
    }
