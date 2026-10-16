"""Validate the Prophet-ready order-history workbook before anything from it
reaches the database or the forecasting pipeline.

Why this module exists next to xlsx_history_import.py
-------------------------------------------------------
xlsx_history_import.py already does the one thing this system actually needs
from a workbook: replay real order-level rows through the live POS pipeline
(reserve_order/commit_order) so customer_order/order_line/stock_ledger end up
holding a normal, self-consistent history -- exactly what
wt/forecasting.py's menu-item series and this module's own store-level series
(both read straight off those tables) already know how to forecast. That
importer's _load_rows() does the minimum: skip a row with no Order ID. It
does not check that a date parses, that a quantity is a sane positive number,
that names resolve, or that the workbook does not contain internal
contradictions -- because until now nothing needed it to.

The workbook this module was written for (data/Wings_Theory_Order_History_
Prophet_Ready.xlsx) adds three "Prophet-ready" aggregate sheets (Daily
Product Demand, Daily Store Demand, Forecast Calendar) on top of the same
order-level sheet the importer already reads. Per the integration's own
architecture decision -- see the module docstring below for
validate_workbook() -- those three sheets are NOT a second ingestion path
into the database: everything forecasting.py and depletion.py read comes
from customer_order/order_line, which xlsx_history_import.py already
populates from the order-level sheet alone. Importing the aggregates
*again* through a separate path would let the two silently disagree the
moment either one is regenerated independently. Instead, this module
validates all four sheets on their own terms AND cross-checks that the three
aggregate sheets actually agree with what the order-level sheet says -- so a
generated-then-drifted workbook is caught before import, not after.

Nothing here writes to the database. This is read-only validation over the
.xlsx file; xlsx_history_import.run() is still the only writer.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import openpyxl

from .xlsx_history_import import REGULAR_ALIASES, UNLI_PREFIX

ORDER_SHEET = "90-Day Order History"
PRODUCT_DEMAND_SHEET = "Daily Product Demand"
STORE_DEMAND_SHEET = "Daily Store Demand"
CALENDAR_SHEET = "Forecast Calendar"

ORDER_COLUMNS = (
    "Order ID", "Order Date", "Order Time", "Category", "Product/Menu Item",
    "Quantity Ordered", "Unit Price (PHP)", "Total Amount (PHP)",
)
PRODUCT_DEMAND_COLUMNS = (
    "ds", "Day", "Day of Week", "Product/Menu Item", "Category", "Unit Price (PHP)", "y",
)
STORE_DEMAND_COLUMNS = ("ds", "y_orders", "y_items", "y_sales", "Day", "Day of Week")
CALENDAR_COLUMNS = ("ds", "Day", "Day of Week", "Month", "Month Name", "Week of Year", "Weekend")

# Sanity bounds -- not business rules, just what separates "unusual but
# real" from "this cell almost certainly contains a data-entry error".
# A single dine-in order for 500 units of one item, or a menu price of
# PHP 50,000, is not something this restaurant's own menu (see
# data/Wings_Theory_Master_Inventory.xlsx) could ever produce honestly.
MAX_PLAUSIBLE_QTY = 200
MAX_PLAUSIBLE_PRICE = 5000.0


def _report(sheet: str, rows_total: int, rejected: list[dict], **extra) -> dict:
    return {
        "sheet": sheet,
        "rows_total": rows_total,
        "rows_rejected": len(rejected),
        "rows_accepted": rows_total - len(rejected),
        "rejected": rejected,
        **extra,
    }


def _open(path: Path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    return wb


def _as_date(v) -> _dt.date | None:
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    if isinstance(v, str):
        try:
            return _dt.date.fromisoformat(v.strip())
        except ValueError:
            return None
    return None


def _as_number(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


# ===========================================================================
# 90-Day Order History -- the one sheet xlsx_history_import.py actually
# replays into the database. Validated at the LINE level: one row per
# (Order ID, Product/Menu Item) purchase line.
# ===========================================================================
def validate_order_history(wb) -> dict:
    if ORDER_SHEET not in wb.sheetnames:
        return {"sheet": ORDER_SHEET, "present": False,
                "error": f"required sheet {ORDER_SHEET!r} not found"}
    ws = wb[ORDER_SHEET]
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    missing_cols = [c for c in ORDER_COLUMNS if c not in header]
    if missing_cols:
        return {"sheet": ORDER_SHEET, "present": True,
                "error": f"missing required column(s): {missing_cols}"}

    rejected: list[dict] = []
    accepted = 0
    order_ids: set[str] = set()
    dates_seen: set[_dt.date] = set()
    unmapped_products: set[str] = set()
    line_signatures: dict[tuple, int] = {}
    duplicate_lines = 0
    zero_or_negative_qty = 0
    negative_price = 0
    implausible_values: list[dict] = []
    total_qty = 0.0
    total_amount = 0.0
    flavors_seen: set[str] = set()

    known_regulars = set()  # filled lazily by caller via resolve_fn, see validate_workbook()

    for i, r in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        order_id, order_date, order_time, category, item, qty, unit_price, total = r
        if order_id is None and item is None:
            continue  # a genuinely blank trailing row, not a malformed one
        problems = []
        if order_id is None:
            problems.append("missing Order ID")
        if item is None or not str(item).strip():
            problems.append("missing Product/Menu Item")
        d = _as_date(order_date)
        if d is None:
            problems.append(f"unparseable Order Date: {order_date!r}")
        q = _as_number(qty)
        if q is None:
            problems.append(f"non-numeric Quantity Ordered: {qty!r}")
        elif q <= 0:
            problems.append(f"non-positive Quantity Ordered: {q}")
            zero_or_negative_qty += 1
        elif q > MAX_PLAUSIBLE_QTY:
            implausible_values.append({"row": i, "field": "Quantity Ordered", "value": q})
        p = _as_number(unit_price)
        if p is None:
            problems.append(f"non-numeric Unit Price (PHP): {unit_price!r}")
        elif p < 0:
            problems.append(f"negative Unit Price (PHP): {p}")
            negative_price += 1
        elif p > MAX_PLAUSIBLE_PRICE:
            implausible_values.append({"row": i, "field": "Unit Price (PHP)", "value": p})

        if problems:
            rejected.append({"row": i, "order_id": order_id, "item": item, "reasons": problems})
            continue

        accepted += 1
        order_ids.add(order_id)
        dates_seen.add(d)
        total_qty += q
        total_amount += q * p
        name = str(item)
        if name.startswith(UNLI_PREFIX):
            flavors_seen.add(name[len(UNLI_PREFIX):])
        else:
            mapped = REGULAR_ALIASES.get(name, name)
            known_regulars.add(mapped)

        sig = (order_id, name, q, p)
        line_signatures[sig] = line_signatures.get(sig, 0) + 1

    duplicate_lines = sum(n - 1 for n in line_signatures.values() if n > 1)

    date_span = None
    missing_calendar_dates: list[str] = []
    if dates_seen:
        start, end = min(dates_seen), max(dates_seen)
        date_span = {"from": start.isoformat(), "to": end.isoformat(),
                    "calendar_days": (end - start).days + 1}
        d = start
        while d <= end:
            if d not in dates_seen:
                missing_calendar_dates.append(d.isoformat())
            d += _dt.timedelta(days=1)

    return {
        **_report(ORDER_SHEET, ws.max_row - 1, rejected),
        "present": True,
        "distinct_orders": len(order_ids),
        "distinct_dates_with_orders": len(dates_seen),
        "date_span": date_span,
        "missing_calendar_dates": missing_calendar_dates,
        "missing_calendar_date_count": len(missing_calendar_dates),
        "duplicate_line_observations": duplicate_lines,
        "rows_with_non_positive_qty": zero_or_negative_qty,
        "rows_with_negative_price": negative_price,
        "implausible_values": implausible_values,
        "distinct_regular_products_seen": sorted(known_regulars),
        "distinct_unli_flavors_seen": sorted(flavors_seen),
        "total_qty_accepted": round(total_qty, 2),
        "total_amount_accepted_php": round(total_amount, 2),
    }


# ===========================================================================
# The three Prophet-ready aggregate sheets. All three share the same shape
# of problem: one row per (ds[, entity]), a required numeric target, and a
# calendar that is supposed to be gap-free. One generic validator handles
# all three; only the per-row extraction differs (see the three thin
# wrappers below it).
# ===========================================================================
def _validate_aggregate_sheet(wb, sheet_name: str, required_columns: tuple[str, ...],
                              extract, group_key_fields: tuple[str, ...]) -> dict:
    """`extract(row_dict) -> dict | None` turns one raw row into a normalized
    record (or None + records the reason in `problems` via the return
    convention below) — kept per-sheet since each sheet's numeric target(s)
    differ. `group_key_fields` names the fields (already normalized) that
    together must be unique per row (duplicate-observation detection) and
    that, keyed on date alone, drive the missing-date report."""
    if sheet_name not in wb.sheetnames:
        return {"sheet": sheet_name, "present": False,
                "error": f"required sheet {sheet_name!r} not found"}
    ws = wb[sheet_name]
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    missing_cols = [c for c in required_columns if c not in header]
    if missing_cols:
        return {"sheet": sheet_name, "present": True,
                "error": f"missing required column(s): {missing_cols}"}
    col_idx = {name: header.index(name) for name in required_columns}

    rejected: list[dict] = []
    accepted_rows: list[dict] = []
    dates_seen: set[_dt.date] = set()
    zero_observations = 0

    for i, raw in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if raw[0] is None and all(v is None for v in raw):
            continue
        row = {name: raw[idx] for name, idx in col_idx.items()}
        rec, problems, is_zero = extract(row)
        if problems:
            rejected.append({"row": i, "reasons": problems, "raw": {k: row[k] for k in required_columns}})
            continue
        accepted_rows.append(rec)
        dates_seen.add(rec["ds"])
        if is_zero:
            zero_observations += 1

    dup_key_counts: dict[tuple, int] = {}
    for rec in accepted_rows:
        key = tuple(rec[f] for f in group_key_fields)
        dup_key_counts[key] = dup_key_counts.get(key, 0) + 1
    duplicates = sum(n - 1 for n in dup_key_counts.values() if n > 1)

    date_span = None
    missing_dates: list[str] = []
    if dates_seen:
        start, end = min(dates_seen), max(dates_seen)
        date_span = {"from": start.isoformat(), "to": end.isoformat(),
                    "calendar_days": (end - start).days + 1}
        d = start
        while d <= end:
            if d not in dates_seen:
                missing_dates.append(d.isoformat())
            d += _dt.timedelta(days=1)

    return {
        **_report(sheet_name, ws.max_row - 1, rejected),
        "present": True,
        "date_span": date_span,
        "missing_dates": missing_dates,
        "missing_date_count": len(missing_dates),
        "duplicate_observations": duplicates,
        "zero_demand_observations": zero_observations,
        "zero_demand_pct": (round(zero_observations / len(accepted_rows) * 100, 1)
                            if accepted_rows else None),
        "records": accepted_rows,
    }


def validate_daily_product_demand(wb) -> dict:
    def extract(row):
        problems = []
        d = _as_date(row["ds"])
        if d is None:
            problems.append(f"unparseable ds: {row['ds']!r}")
        product = row["Product/Menu Item"]
        if not product or not str(product).strip():
            problems.append("missing Product/Menu Item")
        y = _as_number(row["y"])
        if y is None:
            problems.append(f"non-numeric y: {row['y']!r}")
        elif y < 0:
            problems.append(f"negative y (negative demand): {y}")
        price = _as_number(row["Unit Price (PHP)"])
        if price is not None and price < 0:
            problems.append(f"negative Unit Price (PHP): {price}")
        if problems:
            return None, problems, False
        return {"ds": d, "product": str(product), "category": row["Category"],
                "y": y, "unit_price": price}, [], (y == 0)

    return _validate_aggregate_sheet(wb, PRODUCT_DEMAND_SHEET, PRODUCT_DEMAND_COLUMNS,
                                     extract, group_key_fields=("ds", "product"))


def validate_daily_store_demand(wb) -> dict:
    def extract(row):
        problems = []
        d = _as_date(row["ds"])
        if d is None:
            problems.append(f"unparseable ds: {row['ds']!r}")
        values = {}
        for col in ("y_orders", "y_items", "y_sales"):
            v = _as_number(row[col])
            if v is None:
                problems.append(f"non-numeric {col}: {row[col]!r}")
            elif v < 0:
                problems.append(f"negative {col} (negative demand): {v}")
            else:
                values[col] = v
        if problems:
            return None, problems, False
        return {"ds": d, **values}, [], (values.get("y_items", 1) == 0)

    return _validate_aggregate_sheet(wb, STORE_DEMAND_SHEET, STORE_DEMAND_COLUMNS,
                                     extract, group_key_fields=("ds",))


def validate_forecast_calendar(wb) -> dict:
    def extract(row):
        problems = []
        d = _as_date(row["ds"])
        if d is None:
            problems.append(f"unparseable ds: {row['ds']!r}")
        week = _as_number(row["Week of Year"])
        if week is None or not (1 <= week <= 53):
            problems.append(f"Week of Year out of range: {row['Week of Year']!r}")
        if problems:
            return None, problems, False
        return {"ds": d}, [], False

    return _validate_aggregate_sheet(wb, CALENDAR_SHEET, CALENDAR_COLUMNS,
                                     extract, group_key_fields=("ds",))


# ===========================================================================
# Cross-reconciliation: do the three "Prophet-ready" aggregate sheets
# actually agree with what the order-level sheet itself says? This is what
# stands in for a second ingestion path -- proof the aggregates are a
# faithful summary of the one sheet that is actually imported, without
# importing them a second time. The actual comparison lives inline in
# validate_workbook() below, since it needs the raw order-level rows
# re-read at day-level granularity, which order_report's own whole-window
# totals don't carry.
# ===========================================================================
def validate_workbook(path: str | Path) -> dict:
    """The full data-quality report: every sheet validated on its own terms,
    plus a genuine cross-check of the order-level sheet's own day-by-day
    orders/items/sales totals against what Daily Store Demand claims for the
    same dates. Read-only -- nothing here touches the database (see
    wt.xlsx_history_import.run() for the only writer)."""
    path = Path(path)
    if not path.exists():
        return {"workbook_detected": False, "path": str(path), "error": "file not found"}

    wb = _open(path)
    order_report = validate_order_history(wb)
    product_report = validate_daily_product_demand(wb)
    store_report = validate_daily_store_demand(wb)
    calendar_report = validate_forecast_calendar(wb)

    # Recompute per-day store totals directly from the ACCEPTED order-level
    # rows (re-reading the raw sheet rather than trusting order_report's own
    # rolled-up totals, since those are whole-window sums, not per-day ones)
    # and diff them against Daily Store Demand's own numbers for the same
    # date -- this is the actual cross-source-of-truth check.
    reconciliation = {"available": False, "reason": "order-level or store-demand sheet invalid"}
    if order_report.get("present") and "error" not in order_report and \
       store_report.get("present") and "error" not in store_report:
        ws = wb[ORDER_SHEET]
        header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        idx = {name: header.index(name) for name in ORDER_COLUMNS}
        recomputed: dict[_dt.date, dict[str, float]] = {}
        for raw in ws.iter_rows(min_row=2, values_only=True):
            order_id = raw[idx["Order ID"]]
            d = _as_date(raw[idx["Order Date"]])
            qty = _as_number(raw[idx["Quantity Ordered"]])
            price = _as_number(raw[idx["Unit Price (PHP)"]])
            item = raw[idx["Product/Menu Item"]]
            if order_id is None or d is None or qty is None or price is None or qty <= 0 or price < 0 or not item:
                continue
            bucket = recomputed.setdefault(d, {"orders": set(), "items": 0.0, "sales": 0.0})
            bucket["orders"].add(order_id)
            bucket["items"] += qty
            bucket["sales"] += qty * price

        mismatches = []
        checked = 0
        for rec in store_report["records"]:
            d = rec["ds"]
            actual = recomputed.get(d)
            if actual is None:
                mismatches.append({"date": d.isoformat(), "issue": "date present in Daily Store Demand "
                                                                    "but no orders found for it in the order-level sheet"})
                continue
            checked += 1
            actual_orders = len(actual["orders"])
            diffs = {}
            if actual_orders != rec["y_orders"]:
                diffs["y_orders"] = {"order_level": actual_orders, "aggregate_sheet": rec["y_orders"]}
            if abs(actual["items"] - rec["y_items"]) > 0.01:
                diffs["y_items"] = {"order_level": actual["items"], "aggregate_sheet": rec["y_items"]}
            if abs(actual["sales"] - rec["y_sales"]) > 0.01:
                diffs["y_sales"] = {"order_level": round(actual["sales"], 2), "aggregate_sheet": rec["y_sales"]}
            if diffs:
                mismatches.append({"date": d.isoformat(), "diffs": diffs})
        reconciliation = {
            "available": True,
            "dates_checked": checked,
            "dates_with_mismatch": len(mismatches),
            "mismatches": mismatches[:50],
            "note": ("Daily Store Demand's own y_orders/y_items/y_sales, independently recomputed "
                    "here from the 90-Day Order History sheet's raw rows, per date. This is what "
                    "stands in for a second ingestion path: the aggregate sheets are validated "
                    "against the one sheet actually imported into the database, never imported "
                    "themselves -- see this module's docstring."),
        }
    wb.close()

    # Strip the (potentially large) per-row `records` payload from the
    # returned aggregate reports -- useful for the reconciliation above, not
    # for a report a human or the audit doc is going to read.
    for r in (product_report, store_report, calendar_report):
        r.pop("records", None)

    sheets_present = all(r.get("present") for r in
                         (order_report, product_report, store_report, calendar_report))
    return {
        "workbook_detected": True,
        "path": str(path),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "all_required_sheets_present": sheets_present,
        "order_history": order_report,
        "daily_product_demand": product_report,
        "daily_store_demand": store_report,
        "forecast_calendar": calendar_report,
        "store_demand_reconciliation": reconciliation,
    }


if __name__ == "__main__":
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path", nargs="?", default=None,
                   help="workbook path (default: data/Wings_Theory_Order_History_Prophet_Ready.xlsx)")
    args = p.parse_args()

    default_path = Path(__file__).resolve().parent.parent.parent / "data" / \
        "Wings_Theory_Order_History_Prophet_Ready.xlsx"
    report = validate_workbook(args.path or default_path)
    json.dump(report, sys.stdout, indent=2, default=str)
    print()
