"""Reconcile the ~8,918 already-imported historical orders (see
wt/xlsx_history_import.py) against the workbook they came from
(data/Wings_Theory_Order_History.xlsx), without re-running the import.

What this fixes, and why a second pass instead of re-running the importer
--------------------------------------------------------------------------
The original import replayed each workbook order through the real
reserve_order/commit_order pipeline -- correct, and not touched here -- but
left four things a real historical sales record should not have:

1. subtotal/total NULL, even though every order's own order_line rows fully
   determine them (qty x unit_price, summed). A receipt for a real historical
   sale should not show "SUBTOTAL --" when the number is already sitting in
   the database.
2. No way to tell "this row came from the workbook" from any other
   created_by IS NULL row, and no way to trace a row back to the specific
   workbook Order ID it came from -- both discarded at import time.
3. table_label ("T-07") and, for non-unlimited orders, head_count (1-4) were
   randomly generated at import time to satisfy required parameters neither
   the schema nor the workbook actually calls for. They read as recorded
   facts but are not; a real historical receipt must not display a random
   number as if the source data said so.
4. A workbook order that lists both a-la-carte and unlimited-wings lines in
   one Order ID was replayed as TWO separate customer_order rows (see (3) in
   the class docstring below), which makes one real historical purchase look
   like two unrelated ones in Order History.

None of this can be fixed by re-running xlsx_history_import.py: every date in
the workbook already holds orders, so its own protected_dates check would
skip all of it, and even if it didn't, re-running would create a second,
duplicate set of orders. This module instead reconciles IN PLACE: it
rebuilds the same workbook groupings the original import used, matches them
against the orders already in the database by content (not by re-deriving
timestamps and hoping insertion order held), and backfills only what is
deterministically knowable -- never touching cash/payment/discount/cashier
fields the workbook never had.

(1) Regular (non-unlimited) orders: matched with certainty
------------------------------------------------------------
A workbook order's regular lines resolve to an exact
{menu_item_id: (total_qty, unit_price)} signature and an exact timestamp
(date + hour + minute -- the original import truncated seconds to :00, so
matching is to the minute, which is more than fine-grained enough: with
~5,000 regular orders spread over 100 days x 1,440 minutes, an accidental
collision on both timestamp AND exact line composition is not a realistic
risk, and this script only accepts a link when EXACTLY one unclaimed
candidate matches both). Only 100% (or, on the rare timestamp collision,
zero -- reported, never guessed) confident matches are linked to
source_order_id.

(2) The unlimited half of a MIXED order: linked by corroboration, not by ID adjacency alone
---------------------------------------------------------------------------------------------
Once a mixed order's regular half is matched, its unlimited half is
findable: the original import's single loop iteration writes the unlimited
half immediately before the regular half for the same workbook Order ID, so
its customer_order.id is (regular_id - 1) *and* it independently must have
head_count equal to the sum of that source order's own unlimited-line
quantities, in the same hour, with exactly one order_line and it is_unli.
This was verified against real production data before being trusted (see
the implementation notes in the project history) -- id adjacency alone would
be a guess; requiring it to also agree with head_count and hour, computed
independently from the workbook, is what makes it a corroborated match
rather than one. If any check fails, the sibling is left unlinked and
reported rather than forced.

(3) Pure-unlimited orders: deliberately NOT linked to a specific source_order_id
------------------------------------------------------------------------------------
A pure-unlimited workbook order has no regular lines to anchor a content
match to, and the import gave every unlimited order a RANDOMIZED minute
(only the hour is preserved from the workbook -- see
xlsx_history_import._seed_unlimited_order's own minute = rng.randint(0, 59)).
On a busy hour with several unlimited tables, more than one already-imported
order can share the same (hour, head_count) signature with no further
distinguishing information ever having been recorded (the import's own
synthetic refill/flavour simulation does not replay the workbook's actual
flavour pick). Forcing a 1:1 link here would be a guess dressed up as a
fact -- exactly what this whole exercise exists to avoid. These orders are
still correctly marked source='historical' (see the fallback pass below,
which is certain at the "is this historical" level even where it cannot be
certain at the "which exact source row" level) and get their subtotal/total
backfilled; source_order_id is left NULL. This is a real, bounded, disclosed
limitation of the ALREADY-COMPLETED bulk import, not something a
reconciliation pass over already-committed data can retroactively invent
information for.

(4) Nothing about inventory, reservations, or the stock ledger is touched
---------------------------------------------------------------------------
This script only ever UPDATEs customer_order columns (source,
source_order_id, subtotal, total, table_label, head_count-when-zeroing-a-
random-non-unlimited value). It never re-reserves, re-commits, re-deducts,
or writes to stock_ledger/reservation/fefo_batch. head_count is normalized
to 0 ONLY on rows that carry no unlimited order_line at all (there,
head_count is unused by any reservation math and was set to a random 1-4 by
_new_order's required parameter, not a source fact) -- it is never changed
on a row that has an unlimited line, because that value already determined
how much was actually deducted from inventory at commit time, and correcting
it now would make the row disagree with a stock movement that already
happened, which would be worse than leaving a known, disclosed limitation
(see (3) above and Problem 7 of the project history: 403 workbook
unlimited-line rows recorded quantity != 1, which the ORIGINAL import
undercounted as a flat 1-per-line; xlsx_history_import.py has been corrected
for any FUTURE import, but this cannot be corrected retroactively on rows
whose inventory deduction has already posted).

Run: python -m wt.xlsx_history_reconcile [--apply]
(dry-run is the default; nothing is written without --apply)
"""
from __future__ import annotations

import argparse
import random

from . import db, demo_seed
from .refills import list_flavors
from . import xlsx_history_import
from .xlsx_history_import import REGULAR_ALIASES, UNLI_PREFIX, _group_orders, _load_rows, _resolve_time

WORKBOOK_DATE_MIN = "2026-06-01"
WORKBOOK_DATE_MAX = "2026-09-09"


def _resolve_menu_id(menu: dict, name: str) -> int | None:
    row = menu.get(REGULAR_ALIASES.get(name, name))
    return row["id"] if row else None


def _regular_signature(lines: list[dict], menu: dict) -> tuple[dict[int, tuple[int, float]] | None, set[str]]:
    """{menu_item_id: (total_qty, unit_price)} for a workbook order's
    non-unlimited lines, or None if any item cannot be resolved (never
    guessed -- reported instead, same policy as the original import)."""
    sig: dict[int, tuple[int, float]] = {}
    unmapped: set[str] = set()
    for l in lines:
        mid = _resolve_menu_id(menu, l["item"])
        if mid is None:
            unmapped.add(l["item"])
            continue
        prev_qty, _ = sig.get(mid, (0, l["unit_price"]))
        sig[mid] = (prev_qty + int(l["qty"]), l["unit_price"])
    if unmapped:
        return None, unmapped
    return sig, unmapped


def _actual_signature(conn, order_id: int) -> dict[int, tuple[int, float]]:
    rows = conn.execute(
        "SELECT menu_item_id, qty, unit_price FROM order_line WHERE order_id=?", (order_id,)
    ).fetchall()
    sig: dict[int, tuple[int, float]] = {}
    for r in rows:
        prev_qty, _ = sig.get(r["menu_item_id"], (0, r["unit_price"]))
        sig[r["menu_item_id"]] = (prev_qty + r["qty"], r["unit_price"])
    return sig


def run(conn=None, apply: bool = False, verbose: bool = True) -> dict:
    conn = conn or db.get_conn()
    db.migrate(conn)  # ensures 0030's source/source_order_id columns exist

    orders = _group_orders(_load_rows()[0])
    menu = {r["name"]: dict(r) for r in conn.execute(
        "SELECT id, name FROM menu_item WHERE pos_visible = 1"
    ).fetchall()}

    already_linked = {
        r["source_order_id"] for r in conn.execute(
            "SELECT DISTINCT source_order_id FROM customer_order WHERE source_order_id IS NOT NULL"
        ).fetchall()
    }

    report = {
        "workbook_orders_total": len(orders),
        "already_linked_skipped": 0,
        "regular_matched": 0,
        "regular_unmatched": [],
        "pure_unli_orders_unlinked_by_design": 0,
        "unli_sibling_linked": 0,
        "unli_sibling_not_found": 0,
        "unmapped_items": set(),
    }

    # order_id -> {"source_order_id": str|None, "clear_table_label": bool,
    #              "zero_head_count": bool}
    updates: dict[int, dict] = {}

    for o in orders:
        oid = o["order_id"]
        if oid in already_linked:
            report["already_linked_skipped"] += 1
            continue

        unli_lines = [l for l in o["lines"] if l["item"].startswith(UNLI_PREFIX)]
        regular_lines = [l for l in o["lines"] if not l["item"].startswith(UNLI_PREFIX)]

        if not regular_lines:
            # Pure-unlimited workbook order -- see module docstring (3).
            report["pure_unli_orders_unlinked_by_design"] += 1
            continue

        sig, unmapped = _regular_signature(regular_lines, menu)
        report["unmapped_items"] |= unmapped
        if sig is None:
            report["regular_unmatched"].append({"source_order_id": oid, "reason": f"unmapped item(s): {sorted(unmapped)}"})
            continue

        hour, minute = _resolve_time(o["time"])
        ts = f"{o['date'].isoformat()} {hour:02d}:{minute:02d}:00"

        candidates = conn.execute(
            "SELECT id FROM customer_order "
            "WHERE created_at=? AND created_by IS NULL AND source_order_id IS NULL AND status != 'void'",
            (ts,),
        ).fetchall()
        matches = [c["id"] for c in candidates if _actual_signature(conn, c["id"]) == sig]

        if len(matches) != 1:
            report["regular_unmatched"].append({
                "source_order_id": oid, "timestamp": ts, "candidates_matched": len(matches),
            })
            continue

        regular_id = matches[0]
        report["regular_matched"] += 1
        updates[regular_id] = {"source_order_id": oid, "clear_table_label": True, "zero_head_count": True}

        if unli_lines:
            expected_head_count = sum(int(l["qty"]) for l in unli_lines)
            hour_prefix = f"{o['date'].isoformat()} {hour:02d}"
            sib = conn.execute(
                """
                SELECT co.id FROM customer_order co
                 WHERE co.id = ?
                   AND co.created_by IS NULL AND co.source_order_id IS NULL
                   AND co.head_count = ?
                   AND substr(co.created_at, 1, 13) = ?
                   AND EXISTS (SELECT 1 FROM order_line ol JOIN menu_item m ON m.id = ol.menu_item_id
                               WHERE ol.order_id = co.id AND m.is_unli = 1)
                   AND NOT EXISTS (SELECT 1 FROM order_line ol JOIN menu_item m ON m.id = ol.menu_item_id
                                   WHERE ol.order_id = co.id AND m.is_unli = 0)
                """,
                (regular_id - 1, expected_head_count, hour_prefix),
            ).fetchone()
            if sib:
                report["unli_sibling_linked"] += 1
                updates[sib["id"]] = {"source_order_id": oid, "clear_table_label": True, "zero_head_count": False}
            else:
                report["unli_sibling_not_found"] += 1

    # Universal fallback: every already-imported row still unmarked, in the
    # workbook's verified date range, becomes source='historical' regardless
    # of whether source_order_id could be pinned down above -- this is what
    # guarantees no historical order is left blank/unlabeled even where
    # fine-grained linkage genuinely cannot be achieved (module docstring 3).
    fallback_rows = conn.execute(
        "SELECT id FROM customer_order "
        "WHERE created_by IS NULL AND source IS NULL "
        "  AND date(created_at) BETWEEN ? AND ?",
        (WORKBOOK_DATE_MIN, WORKBOOK_DATE_MAX),
    ).fetchall()
    fallback_marked = 0
    for row in fallback_rows:
        if row["id"] in updates:
            continue
        has_unli = conn.execute(
            "SELECT 1 FROM order_line ol JOIN menu_item m ON m.id = ol.menu_item_id "
            "WHERE ol.order_id=? AND m.is_unli=1", (row["id"],)
        ).fetchone()
        updates[row["id"]] = {"source_order_id": None, "clear_table_label": True, "zero_head_count": not has_unli}
        fallback_marked += 1
    report["fallback_marked_historical"] = fallback_marked
    report["total_orders_touched"] = len(updates)
    report["unmapped_items"] = sorted(report["unmapped_items"])

    if not apply:
        return report

    table_label_cleared = 0
    head_count_normalized = 0
    subtotal_backfilled = 0
    with db.write_tx(conn):
        for order_id, fields in updates.items():
            row = conn.execute(
                "SELECT subtotal, service_charge, discount_amount FROM customer_order WHERE id=?",
                (order_id,),
            ).fetchone()
            sets, params = ["source='historical'"], []
            if fields["source_order_id"] is not None:
                sets.append("source_order_id=?")
                params.append(fields["source_order_id"])
            if fields["clear_table_label"]:
                sets.append("table_label=NULL")
                table_label_cleared += 1
            if fields["zero_head_count"]:
                sets.append("head_count=0")
                head_count_normalized += 1
            if row["subtotal"] is None:
                line_sum = conn.execute(
                    "SELECT COALESCE(SUM(qty * unit_price), 0) AS s FROM order_line WHERE order_id=?",
                    (order_id,),
                ).fetchone()["s"]
                total = round(line_sum + (row["service_charge"] or 0) - (row["discount_amount"] or 0), 2)
                sets.append("subtotal=?"); params.append(round(line_sum, 2))
                sets.append("total=?"); params.append(total)
                subtotal_backfilled += 1
            params.append(order_id)
            conn.execute(f"UPDATE customer_order SET {', '.join(sets)} WHERE id=?", params)

    report["table_label_cleared"] = table_label_cleared
    report["head_count_normalized"] = head_count_normalized
    report["subtotal_backfilled"] = subtotal_backfilled
    return report


def import_missing(conn=None, apply: bool = False) -> dict:
    """Import workbook orders that were never represented in the database AT
    ALL — distinct from run() above, which only links/backfills orders that
    already exist. This is what actually closes a gap left by
    xlsx_history_import.py's own protected_dates check: that check is
    all-or-nothing per calendar date (any date already holding one order —
    even a single real, live-punched one — skips the ENTIRE date's workbook
    orders), and once a date holds a mix of real + historical orders,
    simply re-running the bulk import would still skip it wholesale.

    Every workbook order not yet linked to a source_order_id is checked
    INDIVIDUALLY before anything is written — exactly the source-order-level
    duplicate prevention the historical-data reconciliation project calls
    for, replacing "this whole date already has something" with "does this
    specific transaction already exist":

    - A workbook order with regular lines: confirmed missing only when its
      exact (timestamp, line-composition) signature matches zero existing
      customer_order rows (regardless of status/source) — the same
      zero-guessing signature match run() uses to link, reused here to
      confirm absence instead. A signature that already exists is left
      alone; that is run()'s job, not this function's.
    - A pure-unlimited workbook order: confirmed missing only when its
      ENTIRE calendar date holds zero source='historical' rows. The original
      import cannot partially import a date (protected_dates is checked
      once, per date, before any of that date's orders are processed), so a
      date with even one historical row already underwent that all-or-
      nothing pass and this order exists somewhere in it — just not
      individually re-verifiable on its own (see run()'s own docstring on
      why a pure-unlimited order is never force-linked to one specific row).

    Replays each confirmed-missing order through the exact same
    xlsx_history_import.replay_order() used by the original bulk import —
    same reservation/commit/ledger pipeline, same source/source_order_id
    tagging, same subtotal/total backfill — so a gap-filled order is
    indistinguishable in shape from one the original run created.
    """
    conn = conn or db.get_conn()
    db.migrate(conn)
    orders = _group_orders(_load_rows()[0])
    menu_lookup = {r["name"]: dict(r) for r in conn.execute(
        "SELECT id, name FROM menu_item WHERE pos_visible = 1"
    ).fetchall()}

    already_linked = {
        r["source_order_id"] for r in conn.execute(
            "SELECT DISTINCT source_order_id FROM customer_order WHERE source_order_id IS NOT NULL"
        ).fetchall()
    }

    to_import: list[dict] = []
    for o in orders:
        if o["order_id"] in already_linked:
            continue
        regular_lines = [l for l in o["lines"] if not l["item"].startswith(UNLI_PREFIX)]

        if regular_lines:
            sig, unmapped = _regular_signature(regular_lines, menu_lookup)
            if sig is None:
                continue  # unmapped item(s) -- already reported by run(), not re-litigated here
            hour, minute = _resolve_time(o["time"])
            ts = f"{o['date'].isoformat()} {hour:02d}:{minute:02d}:00"
            candidates = conn.execute(
                "SELECT id FROM customer_order WHERE created_at=? AND status != 'void'", (ts,)
            ).fetchall()
            if any(_actual_signature(conn, c["id"]) == sig for c in candidates):
                continue  # exists -- just not yet linked; run()'s job
            to_import.append(o)
        else:
            has_any_historical_that_day = conn.execute(
                "SELECT 1 FROM customer_order WHERE date(created_at)=? AND source='historical' LIMIT 1",
                (o["date"].isoformat(),),
            ).fetchone()
            if has_any_historical_that_day:
                continue
            to_import.append(o)

    report = {
        "workbook_orders_total": len(orders),
        "already_linked": len(already_linked),
        "confirmed_missing": len(to_import),
        "confirmed_missing_order_ids": [o["order_id"] for o in to_import],
        "confirmed_missing_dates": sorted({o["date"].isoformat() for o in to_import}),
    }
    if not apply or not to_import:
        return report

    unli_row = conn.execute(
        "SELECT id, price FROM menu_item WHERE is_unli = 1 AND pos_visible = 1 LIMIT 1"
    ).fetchone()
    flavors = list_flavors(conn, active_only=True)
    flavors_included = [f for f in flavors if f["flavor_type"] == "included"]
    flavors_premium = [f for f in flavors if f["flavor_type"] == "special"]

    def resolve_menu_id(name: str) -> int | None:
        row = menu_lookup.get(REGULAR_ALIASES.get(name, name))
        return row["id"] if row else None

    rng = random.Random(20260601)
    unmapped_all: set[str] = set()
    imported_regular = imported_unli = 0

    prev_demo_user_id = demo_seed.USER_ID
    demo_seed.USER_ID = xlsx_history_import.USER_ID
    conn.execute("DROP TRIGGER IF EXISTS trg_ledger_no_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_order_charge_no_update")
    try:
        # Not wrapped in one outer write_tx: replay_order's own calls
        # (reserve_order/commit_order) each open and manage their own
        # transaction already — write_tx does not nest — exactly matching
        # how xlsx_history_import.run() replays its own loop, order by
        # order, rather than as a single all-or-nothing batch.
        for o in to_import:
            _order_pk, kind = xlsx_history_import.replay_order(
                conn, o, resolve_menu_id=resolve_menu_id, unli_row=unli_row,
                flavors_included=flavors_included, flavors_premium=flavors_premium,
                rng=rng, unmapped=unmapped_all,
            )
            if kind == "unli":
                imported_unli += 1
            elif kind == "regular":
                imported_regular += 1
    finally:
        conn.execute(demo_seed._LEDGER_NO_UPDATE_TRIGGER)
        conn.execute(demo_seed._ORDER_CHARGE_NO_UPDATE_TRIGGER)
        demo_seed.USER_ID = prev_demo_user_id

    report["imported_regular"] = imported_regular
    report["imported_unli"] = imported_unli
    report["unmapped_items"] = sorted(unmapped_all)
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="write changes (default is dry-run)")
    p.add_argument("--import-missing", action="store_true",
                   help="also import workbook orders with zero existing DB representation "
                        "(a per-source-order gap-fill, not the link/backfill pass above)")
    args = p.parse_args()

    print(f"database: {db.db_path()}")
    if args.apply:
        db.require_destructive_confirmation("reconcile historical source tracking against")
    c = db.get_conn()
    import json
    result = run(c, apply=args.apply)
    print(json.dumps(result, indent=2, default=str))
    if args.import_missing:
        result2 = import_missing(c, apply=args.apply)
        print(json.dumps(result2, indent=2, default=str))
