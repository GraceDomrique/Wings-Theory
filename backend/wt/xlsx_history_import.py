"""Import the owner's order-history workbook
(data/Wings_Theory_Order_History_Jan2025_to_Sep2026.xlsx) into a database,
replayed order-by-order through the same real service-layer pipeline
(reserve_order/commit_order, plus the unlimited-order/receiving/wastage/
count machinery already proven in wt/demo_seed.py) rather than a bulk INSERT
of order/order_line rows — for the same reason demo_seed.py's own docstring
gives: a raw insert cannot produce a consistent on_hand/reserved/
stock_ledger picture, and replaying ~34,000 orders through the real pipeline
is cheap enough not to need one.

2026-09 note — this module previously pointed at data/Wings_Theory_Order_
History.xlsx (a 90-day "90-Day Order History" sheet) with a second,
optional data/Wings_Theory_Order_History_Prophet_Ready.xlsx source selected
via --source. Neither file exists anywhere in this repo any more — the
owner replaced both with a single, longer-span workbook, data/Wings_Theory_
Order_History_Jan2025_to_Sep2026.xlsx (Jan 2025 - Sep 2026, sheet
'Order History'), so the --source flag and PROPHET_READY_XLSX_PATH are gone
along with them; there is only one dataset to replay now. See
wt/xlsx_prophet_ingest.py's own docstring for what became of the Prophet-
ready validation path.

Why this exists next to demo_seed.py rather than folding into it
------------------------------------------------------------------
demo_seed.py generates its own independently-randomized history and is
hard-locked to the dev database only. This module instead replays a
SPECIFIC, already-authored dataset — the owner's workbook — so the exact
item mix, dates, times and prices in that file are what land in the
ledger, not a fresh random draw. The two are deliberately kept apart:
demo_seed.py's separation-from-production guarantee must not be touched to
make this possible.

The workbook is a constructed history, not a literal sales log
-----------------------------------------------------------------
Its own 'Notes & Assumptions' sheet describes it as a constructed dataset:
menu items/categories/prices are "retained from the supplied workbook" (the
real menu), and "transaction line combinations are based on observed
co-occurrence patterns in the supplied order history, with normal
variation" — i.e. the item mix, dates and prices are specific and real, but
the basket composition is generated, not a literal receipt-by-receipt
export. Importing it is a deliberate choice, made by the project owner, to
give forecasting/replenishment/anomaly-detection enough history to
demonstrate against — not a claim that these exact transactions happened.
Every row this script writes is tagged user_id="xlsx_history_import"
(never "demo_seed", a different dataset, and never a real cashier's
user_id) so its origin stays identifiable and queryable in the ledger
after the fact, exactly like demo_seed.py's own USER_ID convention.

Real transactions are never touched
-------------------------------------
Any calendar date that already holds an order in the target database
BEFORE this script runs is skipped entirely for that date — read fresh
from the database at the start of the run, not hardcoded — so a real
punched sale is never duplicated, overwritten, or diluted by a synthetic
row landing on the same day. This is intentionally coarse (a whole date, not
a specific Order ID) and is this script's only defense against re-running
itself twice; see wt/xlsx_history_reconcile.py for source_order_id-level
duplicate detection and for backfilling the source/source_order_id/subtotal/
total this script now sets on every NEW row onto the ~8,918 rows an earlier
version of this script already created without them.

Item-name mapping
------------------
The workbook's 37 product names mostly match menu_item.name exactly; the
14 that don't (a "Silog"/"in Can"/"(Tall)" naming difference, mostly) are
listed in REGULAR_ALIASES below. The 14 "Unli Wings - <Flavor>" lines map
by flavor display_name (v_unli_flavor) after stripping the prefix — all
14 match exactly. Any item this run cannot map is reported, never guessed.

Unlimited-wings replay, and mixed orders
------------------------------------------
One Order ID containing at least one "Unli Wings - ..." line is replayed as
exactly ONE table via demo_seed._seed_unlimited_order() — not once per
flavor line or per line quantity — with head_count set to the SUM of that
Order ID's unlimited-line quantities (a line reading qty=2 means two diners
picked that flavor). Refill-round count and timing still come from the same
weighted model demo_seed.py already uses and this codebase already trusts
(the sheet records who ordered what, not how many refill rounds a table
took) — a modelling choice for operational realism, not an invented sale.

If that same Order ID ALSO lists a-la-carte lines, they are attached to the
SAME order (_seed_unlimited_order's extra_lines parameter) rather than
replayed as a second, separate customer_order — the live till already
supports one order carrying both an unlimited-wings line and ordinary items
(see tests.run_tests.TestUnliOrders.test_mixed_order_sums_ala_carte_and_
unli_correctly), so one source transaction stays one order, one receipt,
matching the original sale.

Every order this replays (regular, unlimited, or mixed) is stamped
source='historical' and source_order_id=<the workbook Order ID> on
customer_order — not created_by, which stays NULL exactly as a live order's
would be nothing (see migrations/0030). subtotal/total are computed from the
order's own order_line rows and stored, rather than left NULL for a receipt
to show as "—". table_label is left NULL (the workbook has no
table/seating field — nothing is invented in its place) and head_count is 0
for a non-unlimited order (the schema's own "not applicable" value, not a
random 1-4 draw).

Known, accepted revenue gap on unlimited tables: _seed_unlimited_order()
records exactly one order_line (qty=1, the flat table price) regardless of
head_count — head_count only scales ingredient consumption via refills, not
the order_line revenue figure. The workbook instead lists one line PER
DINER'S flavour pick at full price, so its own per-line total sums to more
than what lands in order_line for the same tables (verified: on the
2026-06 import, workbook unli-line total was ~2.62M against ~1.83M actually
recorded — a ~794k gap, while every non-unlimited line reconciled to the
peso). This is the existing unlimited-order model's pre-existing shape, not
something this script can correct without reinventing that protected
business logic — report it, do not paper over it with an extra synthetic
order_line.

Run: python -m wt.xlsx_history_import [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import random
from pathlib import Path

import openpyxl

from . import db, demo_seed
from .errors import InsufficientStockError
from .reservation import commit_order, reserve_order
from .refills import list_flavors

USER_ID = "xlsx_history_import"
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
# 2026-09-25 note: re-exported again under a new filename (Wings_Theory_
# Order_Historys.xlsx, replacing ..._Jan2025_to_Sep2026.xlsx). Sheet name,
# columns and the Jan 2025-Sep 2026 span are unchanged -- only the filename.
XLSX_PATH = _DATA_DIR / "Wings_Theory_Order_Historys.xlsx"
SHEET_NAME = "Order History"

# xlsx 'Product/Menu Item' -> menu_item.name, only where they differ.
#
# Empty as of the 2026-09 switch to this workbook + the Menu & Ingredient
# Audit workbook (see xlsx_menu_audit_import.py): menu_item.name is now
# seeded VERBATIM from the audit workbook's own '1. Menu Audit' sheet, which
# draws from the same real menu the order-history workbook's 'Product/Menu
# Item' column does — verified directly: every one of this sheet's 37
# distinct item names (23 regular + 14 "Unli Wings - <Flavor>") matches an
# existing menu_item.name exactly, with zero exceptions. The old 14-entry
# alias table existed only because the CSV-era seed.py used different
# internal spellings (e.g. "Wing Rack 12 pcs (2 Flavors)" instead of the
# workbook's own "12 pcs (2 Flavors) Ala Carte Wing Rack") — that mismatch is
# gone now that both workbooks are read from directly. Left as a real dict
# (not deleted) in case a future re-export of either workbook drifts.
REGULAR_ALIASES: dict[str, str] = {}
UNLI_PREFIX = "Unli Wings - "


def _load_rows(source_path: Path = XLSX_PATH) -> tuple[list[dict], list[dict]]:
    """Every purchase-line row from the source workbook's order-level sheet.
    A row with no Order ID at all is a genuinely blank trailing row and is
    skipped silently, same as always. A row that HAS an Order ID but fails a
    basic sanity check (unparseable date, missing item, non-numeric or
    non-positive quantity, negative price) is excluded from the returned
    rows too -- replaying it would either crash the service-layer pipeline
    or silently write a nonsensical line -- but is never dropped quietly:
    it comes back in the second return value with the specific reason(s), so
    a caller can report exactly what it does and does not import. See
    wt/xlsx_prophet_ingest.py for the fuller, read-only version of these same
    checks run ahead of time over the whole workbook (including the three
    Prophet-ready aggregate sheets this function doesn't touch)."""
    wb = openpyxl.load_workbook(source_path, data_only=True)
    ws = wb[SHEET_NAME]
    rows: list[dict] = []
    rejected: list[dict] = []
    for i, r in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        order_id, order_date, order_time, category, item, qty, unit_price, total = r
        if order_id is None:
            continue
        d = order_date.date() if hasattr(order_date, "date") else order_date
        problems = []
        if not isinstance(d, _dt.date):
            problems.append(f"unparseable Order Date: {order_date!r}")
        if not item:
            problems.append("missing Product/Menu Item")
        qty_f = price_f = None
        try:
            qty_f = float(qty)
            if qty_f <= 0:
                problems.append(f"non-positive Quantity Ordered: {qty_f}")
        except (TypeError, ValueError):
            problems.append(f"non-numeric Quantity Ordered: {qty!r}")
        try:
            price_f = float(unit_price)
            if price_f < 0:
                problems.append(f"negative Unit Price (PHP): {price_f}")
        except (TypeError, ValueError):
            problems.append(f"non-numeric Unit Price (PHP): {unit_price!r}")
        if problems:
            rejected.append({"row": i, "order_id": order_id, "item": item, "reasons": problems})
            continue
        rows.append({
            "order_id": order_id, "date": d, "time": order_time, "item": item,
            "qty": qty_f, "unit_price": price_f,
        })
    wb.close()
    return rows, rejected


def _group_orders(rows: list[dict]) -> list[dict]:
    """One entry per Order ID, in first-seen order, each carrying its lines
    and the order's own date/time (every line of one order shares both in
    this workbook)."""
    orders: dict[str, dict] = {}
    order_seq: list[str] = []
    for r in rows:
        oid = r["order_id"]
        if oid not in orders:
            orders[oid] = {"order_id": oid, "date": r["date"], "time": r["time"], "lines": []}
            order_seq.append(oid)
        orders[oid]["lines"].append(r)
    return [orders[oid] for oid in order_seq]


def _resolve_time(t) -> tuple[int, int]:
    if isinstance(t, str):
        h, m, *_ = t.split(":")
        return int(h), int(m)
    return t.hour, t.minute


def replay_order(conn, o: dict, *, resolve_menu_id, unli_row, flavors_included, flavors_premium,
                 rng: random.Random, unmapped: set[str]) -> tuple[int | None, str]:
    """Replay exactly ONE already-grouped workbook order (see _group_orders)
    through the real service pipeline and tag it source='historical' +
    source_order_id. Factored out of run()'s loop so wt/xlsx_history_reconcile.py
    can replay a single confirmed-missing order without duplicating this
    logic — both callers must produce byte-identical customer_order rows for
    the same input, or the two "does this already exist" checks that guard
    them (protected_dates here, per-order signature matching there) would
    quietly drift apart.

    Returns (order_pk, kind) — kind is "unli", "regular", or "empty" (every
    line unmapped); order_pk is None only for "empty" or a table that voided
    before the kitchen started (see demo_seed._seed_unlimited_order).

    Caller's responsibility: demo_seed.USER_ID must already be swapped to
    USER_ID, and the ledger/order_charge append-only triggers already
    dropped, for the duration of the call (see run()'s own `with` scope for
    exactly how narrowly that's held open).
    """
    date_iso = o["date"].isoformat()
    hour, minute = _resolve_time(o["time"])

    unli_lines = [l for l in o["lines"] if l["item"].startswith(UNLI_PREFIX)]
    regular_lines = [l for l in o["lines"] if not l["item"].startswith(UNLI_PREFIX)]

    lines: dict[int, int] = {}
    price_by_id: dict[int, float] = {}
    for l in regular_lines:
        mid = resolve_menu_id(l["item"])
        if mid is None:
            unmapped.add(l["item"])
            continue
        lines[mid] = lines.get(mid, 0) + int(l["qty"])
        price_by_id[mid] = l["unit_price"]

    order_pk: int | None = None
    kind = "empty"

    # One source Order ID = one customer_order, even when it lists both
    # unlimited-wings and a-la-carte lines (a mixed order) -- the live till
    # already supports exactly this in a single order (see
    # TestUnliOrders.test_mixed_order_sums_ala_carte_and_unli_correctly), so
    # replaying it as one order here preserves the source transaction
    # boundary instead of splitting one historical purchase into two
    # unrelated-looking receipts.
    if unli_lines and unli_row:
        result = demo_seed._seed_unlimited_order(
            conn, o["date"], hour, rng, flavors_included, flavors_premium,
            unli_row["id"], unli_row["price"],
            head_count=sum(int(l["qty"]) for l in unli_lines), walkout_rate=0.0,
            random_table_label=False,
            extra_lines=lines or None, extra_prices=price_by_id,
            restock_reference_date=_dt.date.today(),
        )
        if result is not None:
            order_pk = result["order_pk"]
            kind = "unli"
    elif lines:
        ts = f"{date_iso} {hour:02d}:{minute:02d}:00"
        order_pk, _order_no = demo_seed._new_order(conn, None, 0)
        demo_seed._add_lines(conn, order_pk, lines, price_by_id)
        try:
            reserve_order(conn, order_pk, user_id=USER_ID)
        except InsufficientStockError as e:
            # Measured from today, not o["date"] -- see _seed_unlimited_order's
            # restock_reference_date docstring for why a reactive mid-replay
            # restock's shelf life must never be measured from the historical
            # sale date it happens to be triggered by.
            demo_seed._restock_shortfalls(conn, e.shortfalls, ts, _dt.date.today(), rng)
            reserve_order(conn, order_pk, user_id=USER_ID)

        ledger_before = demo_seed._last_ledger_id(conn)
        commit_order(conn, order_pk, user_id=USER_ID)
        ledger_after = demo_seed._last_ledger_id(conn)
        demo_seed._backdate_ledger_range(conn, ledger_before, ledger_after, ts)
        conn.execute(
            "UPDATE customer_order SET status='served', created_at=?, committed_at=?, "
            "served_at=? WHERE id=?",
            (ts, ts, ts, order_pk),
        )
        kind = "regular"

    if order_pk is not None:
        line_sum = conn.execute(
            "SELECT COALESCE(SUM(qty * unit_price), 0) AS s FROM order_line WHERE order_id=?",
            (order_pk,),
        ).fetchone()["s"]
        row = conn.execute(
            "SELECT service_charge, discount_amount FROM customer_order WHERE id=?", (order_pk,)
        ).fetchone()
        total = round(line_sum + (row["service_charge"] or 0) - (row["discount_amount"] or 0), 2)
        conn.execute(
            "UPDATE customer_order SET source='historical', source_order_id=?, "
            "subtotal=?, total=? WHERE id=?",
            (o["order_id"], round(line_sum, 2), total, order_pk),
        )
    return order_pk, kind


def run(conn=None, dry_run: bool = False, verbose: bool = True,
       source_path: Path = XLSX_PATH, seed_synthetic_history: bool = True) -> dict:
    """seed_synthetic_history (2026-10, default True — unchanged behavior for
    every existing caller): whether to run the four demo_seed synthetic
    generators (_seed_periodic_receiving/_seed_stock_counts/_seed_wastage_
    history/_seed_unlimited_reconciliation) after replaying the real orders.
    wt/demo_mode.py's init_demo() passes False — it replays the genuine
    workbook (legitimate historical sales data, copied into the isolated
    demo database) but builds its OWN small, deliberate, narratively
    labeled stock-loss/consumption scenario instead of this module's
    large, undifferentiated random fill — see that module's own docstring
    for why "thousands of near-identical synthetic rows" and "a handful of
    understandable demonstration cases" are different design goals even
    though both are legitimately synthetic."""
    conn = conn or db.get_conn()
    raw_rows, rejected_rows = _load_rows(source_path)
    orders = _group_orders(raw_rows)

    # Real orders already in the database before this run — never touched,
    # and their calendar dates are off-limits for any synthetic order.
    protected_dates = {
        r["d"] for r in conn.execute(
            "SELECT DISTINCT date(created_at) AS d FROM customer_order"
        ).fetchall()
    }

    menu = {r["name"]: dict(r) for r in conn.execute(
        "SELECT id, name, price FROM menu_item WHERE pos_visible = 1"
    ).fetchall()}
    unli_row = conn.execute(
        "SELECT id, price FROM menu_item WHERE is_unli = 1 AND pos_visible = 1 LIMIT 1"
    ).fetchone()
    flavors = list_flavors(conn, active_only=True)
    flavors_by_name = {f["display_name"]: f for f in flavors}
    flavors_included = [f for f in flavors if f["flavor_type"] == "included"]
    flavors_premium = [f for f in flavors if f["flavor_type"] == "special"]

    def resolve_menu_id(name: str) -> int | None:
        row = menu.get(REGULAR_ALIASES.get(name, name))
        return row["id"] if row else None

    unmapped: set[str] = set()
    stats = {
        "orders_total": len(orders), "orders_skipped_real_date": 0,
        "orders_imported": 0, "unli_orders_imported": 0,
        "orders_empty_after_mapping": 0,
        # Rows _load_rows() itself excluded before grouping even started --
        # never silently: see that function's docstring for what disqualifies
        # a row and why. Capped in the report to keep it readable; the count
        # is never capped.
        "source_rows_rejected": len(rejected_rows),
        "source_rows_rejected_examples": rejected_rows[:20],
    }

    if dry_run:
        importable_dates = []
        for o in orders:
            date_iso = o["date"].isoformat()
            if date_iso in protected_dates:
                stats["orders_skipped_real_date"] += 1
                continue
            importable_dates.append(o["date"])
            for line in o["lines"]:
                name = line["item"]
                if name.startswith(UNLI_PREFIX):
                    if name[len(UNLI_PREFIX):] not in flavors_by_name:
                        unmapped.add(name)
                elif resolve_menu_id(name) is None:
                    unmapped.add(name)
        stats["orders_would_import"] = len(importable_dates)
        stats["unmapped_items"] = sorted(unmapped)
        if importable_dates:
            stats["date_range"] = f"{min(importable_dates)} to {max(importable_dates)}"
        stats["protected_dates"] = sorted(protected_dates)
        return stats

    rng = random.Random(20260601)  # the workbook's own start date, for a reproducible replay
    imported_dates: list[_dt.date] = []

    # demo_seed's reused helpers (_seed_unlimited_order, _restock_shortfalls,
    # _seed_periodic_receiving, etc.) stamp every write with the module-level
    # demo_seed.USER_ID global rather than a parameter — swapping it for the
    # duration of this run is what makes those writes traceable to THIS
    # import instead of misreporting as demo_seed.py's own dataset. Restored
    # in the finally below no matter how this exits.
    prev_demo_user_id = demo_seed.USER_ID
    demo_seed.USER_ID = USER_ID
    conn.execute("DROP TRIGGER IF EXISTS trg_ledger_no_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_order_charge_no_update")
    try:
        for o in orders:
            date_iso = o["date"].isoformat()
            if date_iso in protected_dates:
                stats["orders_skipped_real_date"] += 1
                continue
            imported_dates.append(o["date"])

            order_pk, kind = replay_order(
                conn, o, resolve_menu_id=resolve_menu_id, unli_row=unli_row,
                flavors_included=flavors_included, flavors_premium=flavors_premium,
                rng=rng, unmapped=unmapped,
            )
            if kind == "unli":
                stats["unli_orders_imported"] += 1
            elif kind == "regular":
                stats["orders_imported"] += 1
            else:
                stats["orders_empty_after_mapping"] += 1

            done = stats["orders_imported"] + stats["unli_orders_imported"]
            if verbose and done and done % 300 == 0:
                print(f"  ...{stats['orders_imported']} regular, "
                      f"{stats['unli_orders_imported']} unlimited so far")

        if imported_dates:
            start, end = min(imported_dates), max(imported_dates)
            if seed_synthetic_history:
                stats["receiving_events"] = demo_seed._seed_periodic_receiving(conn, start, end, rng)
                stats["stock_counts"] = demo_seed._seed_stock_counts(conn, start, end, rng)
                stats["wastage"] = demo_seed._seed_wastage_history(conn, start, end, rng)
                stats["reconciliation"] = demo_seed._seed_unlimited_reconciliation(conn, start, end, rng)
            stats["date_range"] = f"{start.isoformat()} to {end.isoformat()}"
    finally:
        conn.execute(demo_seed._LEDGER_NO_UPDATE_TRIGGER)
        conn.execute(demo_seed._ORDER_CHARGE_NO_UPDATE_TRIGGER)
        demo_seed.USER_ID = prev_demo_user_id

    stats["unmapped_items"] = sorted(unmapped)
    stats["protected_dates"] = sorted(protected_dates)
    return stats


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    print(f"database: {db.db_path()}")
    print(f"source workbook: {XLSX_PATH}")
    if not args.dry_run:
        db.require_destructive_confirmation("import the order-history workbook into")
    c = db.get_conn()
    result = run(c, dry_run=args.dry_run, source_path=XLSX_PATH)
    print("done:", result)
