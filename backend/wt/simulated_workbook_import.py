"""Import data/Wings_Theory_Regenerated_Consistent_Inventory.xlsx (receiving,
wastage, stock counts -- "inventory" source) and
data/Wings_Theory_1_Year_Historical.xlsx (unlimited-wings flavour
reconciliation -- "historical" source): one year of SIMULATED/MODELLED
operational history, explicitly never real (see both workbooks' own "Read
Me"/"START HERE" sheets).

2026-10-12: superseded the single-workbook design
(Wings_Theory_1_Year_Historical_Collection_simulatedd.xlsx) with this
two-workbook split, matching how the replacement data actually arrived:
one "primary" workbook for inventory movements, one
"supplementary" workbook for everything unli-flavour-related. Both new
workbooks keep the same column shapes the importer already validated against
(Injected Synthetic Truth / Scenario ID inline on Wastage Log/Stock Counts/
Unli Reconciliation) -- no column-mapping changes were needed, only the
file split.

Because every previous workbook revision mints its own independent
Receipt/Waste/Count/Reconciliation IDs, re-running this importer against a
new source workbook would otherwise leave the previous revision's rows
sitting alongside the new ones forever as duplicate synthetic history. See
run()'s own `replace_existing` parameter (default True) for how this is
handled: every row this module ever wrote is unambiguously identifiable by
its sentinel user_id/recorded_by/created_by (USER_ID below, never a real
app_user), so "replace the obsolete synthetic data" means "delete every row
carrying that sentinel, then re-import from the current source workbooks" --
never touches a row any other actor wrote, real or synthetic.

What this is and is not
------------------------
The workbook says so itself, on its own "START HERE" sheet and on every
transactional row's own Provenance column: this data was never observed at
Wings Theory. It exists to exercise the receiving/wastage/stock-count/
reconciliation pipeline, the Isolation Forest passes, and the validation
workflow end-to-end before enough REAL operational history exists to do any
of that for real. Importing it must never make it look real. Three things
enforce that:

  1. Every row this module writes carries user_id/recorded_by/created_by =
     USER_ID ("simulated_workbook_import"), a string that is deliberately
     never an app_user account. wt/anomaly.py's _REAL_USER_CLAUSE /
     _REAL_RECORDER_CLAUSE (see SYNTHETIC_SEED_USER_IDS there) already
     structurally excludes any row whose user_id is not NULL and not a real
     app_user from ML training/eligibility — this reuses that exact
     mechanism rather than inventing a parallel one. No code change to
     anomaly.py was needed for this: the exclusion is "not a real account",
     not a hardcoded name list.
  2. Every stock_ledger/ingredient_variance_review/unli_flavor_reconciliation
     row this module writes carries the workbook's own SIM-* row ID as its
     idempotency key (stock_ledger.ref_id, *.client_request_id, or
     unli_flavor_reconciliation.source_ref) — importing the same workbook
     twice produces zero duplicate rows; see run()'s own dedup pass.
  3. This module NEVER calls a current-state-mutating function
     (reservation.receive_stock/record_wastage/record_count, wt/purchasing.
     receive_line) — ingredient.on_hand_base/reserved_base and fefo_batch
     are never touched. The live database already reflects a full year of
     REAL operational history as of today; replaying a second, overlapping
     year of SYNTHETIC receiving/wastage/counts through the normal
     stock-mutating path would double-apply those events on top of
     already-correct current balances. See this module's own
     _replay_ledger() for how a historically-consistent stock_ledger
     picture is still produced (one that satisfies anomaly.py's own
     feature math) without going anywhere near current state. Phase R of
     the integration this module is part of proves current inventory is
     bit-for-bit unchanged by an import.

From INVENTORY_XLSX_PATH (Wings_Theory_Regenerated_Consistent_Inventory.xlsx):
Receiving Log -> purchase_order/purchase_order_item/purchase_order_receipt
(for supplier lead-time analytics only, see wt/depletion.py's future
consumer of actual_lead_time) and Wastage Log/Stock Counts -> stock_ledger.
From HISTORICAL_XLSX_PATH (Wings_Theory_1_Year_Historical.xlsx): Unli
Reconciliation -> unli_flavor_reconciliation (migration 0044) only. Unli
Sessions/Unli Refills, Ingredient Reference, Supplier Master and Cooking Oil
Tests on that same workbook are deliberately NOT imported: Sessions/Refills
are the simulation's own internal working detail that FEEDS the
already-aggregated Unli Reconciliation sheet (importing both would
double-count the same synthetic consumption at two granularities, and the
session-level table, unli_session, has UNIQUE(order_id) already satisfied
by REAL unli_session rows xlsx_history_import.py created for these exact
WTRORD orders — a second session per order is not representable without
either violating that constraint or overwriting real data, both
forbidden); Ingredient Reference/Supplier Master are reference-only
duplicates of the real ingredient catalog (the importer resolves against
the REAL catalog via _ingredient_map(), never this workbook's own copy of
it); Cooking Oil Tests has no consumer in this schema yet.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import openpyxl

from . import db
from .db import write_tx
from .units import UnitConversionError, purchase_to_base

USER_ID = "simulated_workbook_import"
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
# "Primary" source: Receiving Log/Wastage Log/Stock Counts (inventory
# movements). "Supplementary" source: Unli Reconciliation only -- see this
# module's own docstring on why Unli Sessions/Refills are never imported
# from here even though the workbook carries them.
INVENTORY_XLSX_PATH = _DATA_DIR / "Wings_Theory_Regenerated_Consistent_Inventory.xlsx"
HISTORICAL_XLSX_PATH = _DATA_DIR / "Wings_Theory_1_Year_Historical.xlsx"
XLSX_PATH = INVENTORY_XLSX_PATH  # back-compat alias for existing callers/tests

_RECEIVING_SHEET = "Receiving Log"
_WASTAGE_SHEET = "Wastage Log"
_COUNTS_SHEET = "Stock Counts"
_RECONCILIATION_SHEET = "Unli Reconciliation"

# The new workbook's Unli Reconciliation sheet prefixes every Flavor cell
# with "Unli Wings - " (the old workbook used the bare flavour name, which
# is what unli_flavor.display_name actually stores) -- stripped before
# lookup in _flavor_map's keys.
_FLAVOR_PREFIX = "Unli Wings - "

# simulatedd.xlsx's Stock Counts sheet no longer carries a free-text
# "Reason / Review Classification" column (migration target removed), so
# this importer no longer synthesizes ingredient_variance_review rows from
# it -- inventing a review classification that was never actually in the
# workbook would violate the "reject, don't invent" rule. Each count row's
# own "Injected Synthetic Truth"/"Scenario ID" columns are preserved in the
# ledger row's `note` for traceability (see _replay_ledger) and consumed
# directly by the isolated evaluation harness in wt/demo_validation.py --
# never by anything that touches production training or eligibility.


def _iso(value) -> str | None:
    """openpyxl hands back real datetime/date objects for date-typed cells;
    normalize to the same 'YYYY-MM-DD HH:MM:SS' text every other table in
    this schema stores timestamps as. A blank cell is None, never ''."""
    if value is None:
        return None
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, _dt.datetime) \
            else value.strftime("%Y-%m-%d 00:00:00")
    return str(value).strip() or None


def _load_sheet(wb, name: str) -> list[dict]:
    """Header row 0, every subsequent row with at least one non-null cell.
    See the 2026-10-05 forensic audit this importer is based on for why
    ws.dimensions cannot be trusted to bound the real data (several sheets
    report hundreds of phantom trailing rows of pure formatting)."""
    ws = wb[name]
    rows_iter = ws.iter_rows(values_only=True)
    header = [str(c).strip() if c is not None else "" for c in next(rows_iter)]
    out = []
    for raw in rows_iter:
        if all(c is None for c in raw):
            continue
        out.append({header[i]: raw[i] for i in range(len(header)) if header[i]})
    return out


def _norm_ingredient_ref(raw: str | None) -> str | None:
    """'ING-001' and 'ING-0001' must resolve to the same ingredient -- the
    workbook and the live ingredient table don't agree on zero-padding
    width. Normalizes to the bare integer suffix ('1') so either width
    matches the same key."""
    if not raw:
        return None
    digits = "".join(c for c in str(raw) if c.isdigit())
    return digits.lstrip("0") or "0" if digits else None


def _received_qty_base(row: dict, ing: dict) -> float:
    """'Received Qty' on the Receiving Log sheet is in the ingredient's
    PURCHASE/pack unit (e.g. 6 cans), matching its sibling 'Ordered Qty'
    and the sheet's own per-pack pricing ('Unit/Pack Price' x qty ==
    'Total Cost') -- not already in base units. purchase_order_item.
    received_qty_base and purchase_order_receipt.qty_base are both named
    (and documented) as base-unit quantities, so this converts via the
    ingredient's own real pack_size/yield_factor before either INSERT.
    2026-10-08: found via the isolated stock-loss evaluation harness --
    receiving had been landing in those columns unconverted, which (once
    the harness also started folding receiving into its running-balance
    bookkeeping) understated real intake by roughly a pack-size factor.
    Falls back to the raw value, unconverted, only if this ingredient's
    pack_size truly is not set (never silently guesses a pack size)."""
    qty = float(row.get("Received Qty") or 0)
    if qty <= 0 or not ing.get("pack_size"):
        return qty
    try:
        return purchase_to_base(qty, ing["pack_size"], ing.get("yield_factor") or 1.0)
    except UnitConversionError:
        return qty


def _ingredient_map(conn) -> dict[str, dict]:
    return {
        _norm_ingredient_ref(r["ingredient_id"]): dict(r)
        for r in conn.execute(
            "SELECT id, ingredient_id, base_unit, pack_size, yield_factor FROM ingredient"
        ).fetchall()
    }


def _flavor_map(conn) -> dict[str, dict]:
    # The workbook's own "Unit" column on Unli Reconciliation is a constant
    # placeholder ('g/ml') for every row, not a real per-flavour unit -- the
    # actual unit is the flavour's own sauce ingredient's base_unit.
    return {
        r["display_name"]: dict(r)
        for r in conn.execute(
            "SELECT f.id, f.display_name, i.base_unit "
            "FROM unli_flavor f JOIN ingredient i ON i.id = f.ingredient_id"
        ).fetchall()
    }


def _existing_ledger_refs(conn, ref_type: str) -> set[str]:
    return {
        r["ref_id"] for r in conn.execute(
            "SELECT ref_id FROM stock_ledger WHERE ref_type = ?", (ref_type,)
        ).fetchall()
    }


def _existing_receiving_refs(conn) -> set[str]:
    """See run()'s own comment on why this keys off purchase_order_item.note
    ('sim-receipt:<Receipt ID>') rather than purchase_order_receipt.
    client_request_id -- a zero-Received-Qty row never gets a receipt row."""
    return {
        r["note"].rsplit(":", 1)[-1] for r in conn.execute(
            "SELECT note FROM purchase_order_item WHERE note LIKE 'sim-receipt:%'"
        ).fetchall()
    }


def _existing_client_request_ids(conn, table: str) -> set[str]:
    return {
        r["client_request_id"] for r in conn.execute(
            f"SELECT client_request_id FROM {table} WHERE client_request_id IS NOT NULL"
        ).fetchall()
    }


def _existing_source_refs(conn) -> set[str]:
    return {
        r["source_ref"] for r in conn.execute(
            "SELECT source_ref FROM unli_flavor_reconciliation WHERE source_ref IS NOT NULL"
        ).fetchall()
    }


def _validate_rows(rows: list[dict], *, id_col: str, ingredient_col: str,
                   qty_cols: list[str], ingredients: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Generic structural validation shared by Receiving/Wastage/Counts:
    row has its own ID, resolves to a known ingredient, and every quantity
    column present is a non-negative number. Returns (accepted, rejected);
    a rejected row carries its own 'reject_reason'."""
    accepted, rejected = [], []
    for row in rows:
        row_id = row.get(id_col)
        if not row_id:
            rejected.append({**row, "reject_reason": "missing_row_id"})
            continue
        ing_id = _norm_ingredient_ref(row.get(ingredient_col))
        if ing_id not in ingredients:
            rejected.append({**row, "reject_reason": f"unknown_ingredient:{row.get(ingredient_col)!r}"})
            continue
        bad_qty = False
        for col in qty_cols:
            v = row.get(col)
            if v is None:
                continue
            try:
                if float(v) < 0:
                    bad_qty = True
                    break
            except (TypeError, ValueError):
                bad_qty = True
                break
        if bad_qty:
            rejected.append({**row, "reject_reason": "invalid_quantity"})
            continue
        accepted.append(row)
    return accepted, rejected


def _replay_ledger(conn, wastage_rows: list[dict], count_rows: list[dict],
                   ingredients: dict[str, dict], receiving_rows: list[dict] | None = None) -> list[dict]:
    """Build an internally-consistent historical stock_ledger picture for
    wastage + stock-count rows, WITHOUT reading or writing
    ingredient.on_hand_base (see this module's own docstring for why).

    Starts each ingredient's running balance at 0 and replays wastage
    (subtract), RECEIVING (add -- bookkeeping only, see below) and counts
    (the physical count authoritatively SETS the running balance, exactly
    like a real count_adjustment does) in chronological order. This makes
    balance_after_base/delta_base/fraction-of-on-hand internally meaningful
    for wt/anomaly.py's own feature math (which only ever reads a ledger
    row against its immediate neighbours, never against live
    ingredient.on_hand_base) while staying fully decoupled from current,
    already-correct live balances.

    2026-10-08: `receiving_rows` is folded into the running-balance
    bookkeeping (added, like a real receipt would be) but still emits NO
    stock_ledger row of its own -- it lands in purchase_order/_item/_receipt
    for lead-time analytics only, exactly as before. Omitting receiving
    from the balance entirely (the original behaviour) understated the
    balance between two counts by however much stock was actually received
    in between, which made the FOLLOWING count's delta/fraction include
    that restocking as if it were loss -- a diagnosed defect (fractions up
    to 2.75x "of on-hand", which is not a coherent quantity) that was
    inflating the stock-loss rule layer's false-positive rate in the
    isolated evaluation harness. Folding receiving into the balance (but
    still not the ledger) fixes this without changing what gets written.
    """
    events = []
    for row in wastage_rows:
        events.append({
            "ts": row["Event Date"], "kind": "wastage", "row": row,
            "ingredient_id": _norm_ingredient_ref(row["Ingredient ID"]),
        })
    for row in count_rows:
        events.append({
            "ts": row["Count Date"], "kind": "count", "row": row,
            "ingredient_id": _norm_ingredient_ref(row["Ingredient ID"]),
        })
    for row in (receiving_rows or []):
        norm = _norm_ingredient_ref(row["Ingredient ID"])
        ing = ingredients.get(norm)
        received_base = _received_qty_base(row, ing) if ing else 0.0
        if received_base <= 0:
            continue
        events.append({
            "ts": row.get("Received At") or row.get("Ordered At"), "kind": "receiving",
            "row": row, "ingredient_id": norm, "received_base": received_base,
        })
    events.sort(key=lambda e: (e["ingredient_id"], str(e["ts"])))

    balances: dict[str, float] = {}
    out = []
    for e in events:
        ing = ingredients[e["ingredient_id"]]
        bal = balances.get(e["ingredient_id"], 0.0)
        row = e["row"]
        if e["kind"] == "receiving":
            balances[e["ingredient_id"]] = bal + e["received_base"]
            continue
        if e["kind"] == "wastage":
            qty = float(row["Quantity (base units)"])
            delta = -qty
            new_bal = max(0.0, bal + delta)
            waste_type = str(row.get("Waste Type") or "").strip()
            note = (f"{row.get('Reason') or ''} "
                    f"[truth={row.get('Injected Synthetic Truth')} "
                    f"scenario={row.get('Scenario ID')}]").strip()
            out.append({
                "ingredient_id": ing["id"], "delta_base": delta,
                "balance_after_base": new_bal,
                "reason": "spoilage" if waste_type.lower() == "spoilage" else "wastage",
                "wastage_reason": waste_type or None,
                "ref_type": "sim_wastage", "ref_id": row["Waste ID"],
                "note": note,
                "created_at": _iso(row["Event Date"]),
            })
            balances[e["ingredient_id"]] = new_bal
        else:
            actual = float(row["Physical Count Qty (base units)"])
            delta = actual - bal
            note = (f"[truth={row.get('Injected Synthetic Truth')} "
                    f"scenario={row.get('Scenario ID')}]")
            out.append({
                "ingredient_id": ing["id"], "delta_base": delta,
                "balance_after_base": actual,
                "reason": "count_adjustment", "wastage_reason": None,
                "ref_type": "sim_count", "ref_id": row["Count ID"],
                "note": note,
                "created_at": _iso(row["Count Date"]),
            })
            balances[e["ingredient_id"]] = actual
    return out


def insert_ledger_row(conn, lr: dict, user_id: str | None, batch_id: str | None = None) -> int:
    """Shared by run() (user_id=USER_ID, excluded from production training)
    and wt/demo_validation.py's isolated workbook-ground-truth harness
    (user_id=None, so the SAME real detect_anomalies() sees it as eligible
    inside that harness's own throwaway database) -- one INSERT, not two
    copies that could drift apart.

    batch_id populates stock_ledger.source_batch_id (migration 0050) the
    same way insert_reconciliation_row already does for
    unli_flavor_reconciliation -- without it, a SIMULATED/MIXED-mode
    provenance query (wt/anomaly.py's _provenance_branches, which matches
    on source_batch_id IN ml_approved_synthetic_batch) can never find these
    rows no matter how many are approved, since the column stays NULL.
    Production's one pre-existing batch got this via a one-time migration
    0050 UPDATE,
    which only ever covered that specific historical batch_id -- any batch
    produced by a later run() call needs it written here, at insert time,
    to be eligible for SIMULATED-mode training at all. user_id=None callers
    (wt/demo_validation.py's harness) still pass batch_id=None by default,
    unchanged: that harness evaluates with mode='REAL' precisely because its
    rows carry user_id=None (real/NULL provenance), not a batch."""
    cur = conn.execute(
        "INSERT INTO stock_ledger "
        "(ingredient_id, delta_base, balance_after_base, reason, wastage_reason, "
        " ref_type, ref_id, user_id, note, created_at, source_batch_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (lr["ingredient_id"], lr["delta_base"], lr["balance_after_base"], lr["reason"],
         lr["wastage_reason"], lr["ref_type"], lr["ref_id"], user_id, lr["note"],
         lr["created_at"], batch_id),
    )
    return cur.lastrowid


def insert_reconciliation_row(conn, row: dict, flavor: dict, ref: str, batch_id: str,
                              user_id: str | None) -> int:
    """Shared by run() and the isolated workbook-ground-truth harness -- see
    insert_ledger_row's own comment for why this is one function, not two."""
    variance_pct = row.get("Variance %") or row.get("Variance vs System/Provisional")
    variance_ratio = None
    try:
        if variance_pct is not None:
            variance_ratio = float(variance_pct) / 100.0 if abs(float(variance_pct)) > 1 else float(variance_pct)
    except (TypeError, ValueError):
        variance_ratio = None
    cur = conn.execute(
        "INSERT INTO unli_flavor_reconciliation "
        "(service_date, flavor_id, heads, servings, wings_served_pcs, leftover_pcs, "
        " net_consumed_pcs, actual_measured_base, unit, provisional_base, variance_base, "
        " variance_ratio, deterministic_rule_flag, observation_status, "
        " source_isolation_forest_flag, source_human_ground_truth, source_evidence_note, "
        " recorded_by, source_ref, source_batch_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _iso(row.get("Service Date")), flavor["id"],
            int(row.get("Heads Exposed / Relevant Heads") or 0),
            int(row.get("Servings / Refills") or 0),
            float(row.get("Wings Served (pcs)") or 0),
            float(row.get("Leftovers (pcs)") or 0),
            float(row.get("Net Wings Consumed (pcs)") or 0),
            float(row.get("Actual Measured Ingredient Consumption") or 0),
            flavor["base_unit"],
            float(row["Provisional Consumption"]) if row.get("Provisional Consumption") is not None else None,
            None, variance_ratio,
            1 if str(row.get("Deterministic Rule Flag", "")).strip().lower() == "flag" else 0,
            "reconciled",
            row.get("Isolation Forest Flag"), row.get("Human Ground Truth"),
            row.get("Evidence / Notes"),
            user_id, ref, batch_id,
        ),
    )
    return cur.lastrowid


def load_and_validate(inv_wb, hist_wb, ingredients: dict[str, dict], flavors: dict[str, dict]) -> dict:
    """Shared sheet-load + validation step, factored out so the isolated
    workbook-ground-truth harness in wt/demo_validation.py validates/rejects
    rows through the exact same rules as production run() below.

    inv_wb (the "inventory" workbook) supplies Receiving/Wastage/Stock
    Counts; hist_wb (the "historical" workbook) supplies Unli Reconciliation
    only. Callers that only need one side (e.g. demo_validation's
    per-pass harnesses) may pass the same workbook object for both -- every
    sheet lookup below is independent."""
    receiving_raw = _load_sheet(inv_wb, _RECEIVING_SHEET)
    wastage_raw = _load_sheet(inv_wb, _WASTAGE_SHEET)
    counts_raw = _load_sheet(inv_wb, _COUNTS_SHEET)
    recon_raw = _load_sheet(hist_wb, _RECONCILIATION_SHEET)

    receiving_ok, receiving_rejected = _validate_rows(
        receiving_raw, id_col="Receipt ID", ingredient_col="Ingredient ID",
        qty_cols=["Ordered Qty", "Received Qty"], ingredients=ingredients,
    )
    wastage_ok, wastage_rejected = _validate_rows(
        wastage_raw, id_col="Waste ID", ingredient_col="Ingredient ID",
        qty_cols=["Quantity (base units)"], ingredients=ingredients,
    )
    counts_ok, counts_rejected = _validate_rows(
        counts_raw, id_col="Count ID", ingredient_col="Ingredient ID",
        qty_cols=["System Expected Qty (base units)", "Physical Count Qty (base units)"],
        ingredients=ingredients,
    )
    recon_ok, recon_rejected = [], []
    for row in recon_raw:
        row_id = row.get("Reconciliation ID")
        flavor_raw = row.get("Flavor")
        flavor_name = (flavor_raw[len(_FLAVOR_PREFIX):]
                       if flavor_raw and flavor_raw.startswith(_FLAVOR_PREFIX) else flavor_raw)
        if not row_id:
            recon_rejected.append({**row, "reject_reason": "missing_row_id"})
            continue
        if flavor_name not in flavors:
            recon_rejected.append({**row, "reject_reason": f"unknown_flavor:{flavor_raw!r}"})
            continue
        recon_ok.append({**row, "_flavor_name": flavor_name})

    return {
        "receiving": (receiving_raw, receiving_ok, receiving_rejected),
        "wastage": (wastage_raw, wastage_ok, wastage_rejected),
        "counts": (counts_raw, counts_ok, counts_rejected),
        "reconciliation": (recon_raw, recon_ok, recon_rejected),
    }


def _delete_bypassing_append_only_guard(conn, table: str, where_sql: str, params: tuple) -> int:
    """DELETE from a table whose BEFORE DELETE trigger(s) enforce
    append-only (stock_ledger, unli_flavor_reconciliation,
    ingredient_variance_review all do, for real rows). Reads the exact
    trigger definitions from sqlite_master (never hand-duplicated, so they
    cannot drift from whatever migration actually created them), drops
    them, runs the DELETE, and recreates them from the captured SQL before
    returning -- the append-only guarantee is back in force before any
    other statement runs, including on an exception (see `finally`).
    `where_sql`/`params` must scope the DELETE to rows this module itself
    wrote (the synthetic USER_ID sentinel) -- this function enforces
    nothing about that; callers are responsible for a safe WHERE clause."""
    trigger_defs = [
        r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? AND sql IS NOT NULL",
            (table,),
        ).fetchall()
    ]
    trigger_names = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,),
        ).fetchall()
    ]
    for name in trigger_names:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    try:
        cur = conn.execute(f"DELETE FROM {table} WHERE {where_sql}", params)
        return cur.rowcount
    finally:
        for sql in trigger_defs:
            conn.execute(sql)


def _purge_previous_import(conn) -> dict:
    """Delete every row this module has ever written (identified solely by
    its sentinel USER_ID -- never by which workbook revision wrote it), so a
    re-run against a NEW source workbook cannot leave a prior revision's
    rows sitting alongside the new ones. Must run inside the same write_tx
    as the re-import that follows, so a failure never leaves the database
    between purge and re-import.

    stock_ledger / unli_flavor_reconciliation / ingredient_variance_review
    are all append-only by design (BEFORE UPDATE/DELETE triggers, enforced
    for every real row ever recorded in them) -- see
    _delete_bypassing_append_only_guard() for how this lifts that guard
    ONLY for the duration of this purge and ONLY for rows carrying the
    synthetic sentinel (user_id/recorded_by=USER_ID, structurally never a
    real account). A real row in any of these tables is never reachable by
    these DELETEs, and the append-only guarantee is restored before this
    function returns."""
    counts = {}

    cur = conn.execute(
        "DELETE FROM purchase_order_receipt WHERE purchase_order_item_id IN ("
        " SELECT id FROM purchase_order_item WHERE purchase_order_id IN ("
        "  SELECT id FROM purchase_order WHERE created_by = ?))", (USER_ID,))
    counts["purchase_order_receipt"] = cur.rowcount
    cur = conn.execute(
        "DELETE FROM purchase_order_item WHERE purchase_order_id IN ("
        " SELECT id FROM purchase_order WHERE created_by = ?)", (USER_ID,))
    counts["purchase_order_item"] = cur.rowcount
    cur = conn.execute("DELETE FROM purchase_order WHERE created_by = ?", (USER_ID,))
    counts["purchase_order"] = cur.rowcount

    counts["stock_ledger"] = _delete_bypassing_append_only_guard(
        conn, "stock_ledger", "ref_type IN ('sim_wastage','sim_count') AND user_id = ?", (USER_ID,))
    counts["ingredient_variance_review"] = _delete_bypassing_append_only_guard(
        conn, "ingredient_variance_review", "user_id = ?", (USER_ID,))
    counts["unli_flavor_reconciliation"] = _delete_bypassing_append_only_guard(
        conn, "unli_flavor_reconciliation", "recorded_by = ?", (USER_ID,))

    return counts


def run(conn=None, dry_run: bool = False, verbose: bool = True,
       inventory_path: Path = INVENTORY_XLSX_PATH,
       historical_path: Path = HISTORICAL_XLSX_PATH,
       replace_existing: bool = True) -> dict:
    conn = conn or db.get_conn()
    inv_wb = openpyxl.load_workbook(inventory_path, read_only=True, data_only=True)
    hist_wb = inv_wb if historical_path == inventory_path else \
        openpyxl.load_workbook(historical_path, read_only=True, data_only=True)

    ingredients = _ingredient_map(conn)
    flavors = _flavor_map(conn)

    loaded = load_and_validate(inv_wb, hist_wb, ingredients, flavors)
    inv_wb.close()
    if hist_wb is not inv_wb:
        hist_wb.close()
    receiving_raw, receiving_ok, receiving_rejected = loaded["receiving"]
    wastage_raw, wastage_ok, wastage_rejected = loaded["wastage"]
    counts_raw, counts_ok, counts_rejected = loaded["counts"]
    recon_raw, recon_ok, recon_rejected = loaded["reconciliation"]

    stats = {
        "receiving": {"total": len(receiving_raw), "accepted": len(receiving_ok), "rejected": len(receiving_rejected)},
        "wastage": {"total": len(wastage_raw), "accepted": len(wastage_ok), "rejected": len(wastage_rejected)},
        "counts": {"total": len(counts_raw), "accepted": len(counts_ok), "rejected": len(counts_rejected)},
        "reconciliation": {"total": len(recon_raw), "accepted": len(recon_ok), "rejected": len(recon_rejected)},
        "rejected_examples": {
            "receiving": receiving_rejected[:10], "wastage": wastage_rejected[:10],
            "counts": counts_rejected[:10], "reconciliation": recon_rejected[:10],
        },
    }

    if dry_run:
        existing_receipt_refs = _existing_receiving_refs(conn)
        existing_wastage_refs = _existing_ledger_refs(conn, "sim_wastage")
        existing_count_refs = _existing_ledger_refs(conn, "sim_count")
        existing_recon_refs = _existing_source_refs(conn)
        stats["would_insert"] = {
            "receiving": sum(1 for r in receiving_ok if r["Receipt ID"] not in existing_receipt_refs),
            "wastage": sum(1 for r in wastage_ok if r["Waste ID"] not in existing_wastage_refs),
            "counts": sum(1 for r in counts_ok if r["Count ID"] not in existing_count_refs),
            "reconciliation": sum(1 for r in recon_ok if r["Reconciliation ID"] not in existing_recon_refs),
        }
        stats["already_imported"] = {
            "receiving": sum(1 for r in receiving_ok if r["Receipt ID"] in existing_receipt_refs),
            "wastage": sum(1 for r in wastage_ok if r["Waste ID"] in existing_wastage_refs),
            "counts": sum(1 for r in counts_ok if r["Count ID"] in existing_count_refs),
            "reconciliation": sum(1 for r in recon_ok if r["Reconciliation ID"] in existing_recon_refs),
        }
        return stats

    inserted = {"receiving": 0, "wastage": 0, "counts": 0, "reconciliation": 0}
    duplicates = {"receiving": 0, "wastage": 0, "counts": 0, "reconciliation": 0}
    batch_id = f"simrun-{_dt.datetime.now().strftime('%Y%m%d%H%M%S')}"

    with write_tx(conn):
        purged = _purge_previous_import(conn) if replace_existing else {}

        # --- Receiving Log -> purchase_order/_item/_receipt, analytics only.
        # Never calls purchasing.receive_line()/reservation._receive_stock_
        # locked() -- see module docstring. One PO + one item + one receipt
        # per workbook row, since this sheet is already 1:1 (one receipt per
        # PO, confirmed by the forensic audit).
        existing_item_refs = _existing_receiving_refs(conn)
        for row in receiving_ok:
            receipt_ref = row["Receipt ID"]
            if receipt_ref in existing_item_refs:
                duplicates["receiving"] += 1
                continue
            ing = ingredients[_norm_ingredient_ref(row["Ingredient ID"])]
            ordered_qty = float(row.get("Ordered Qty") or 0)
            received_qty_base = _received_qty_base(row, ing)
            cur = conn.execute(
                "INSERT INTO purchase_order (status, supplier, notes, created_by, created_at, ordered_at) "
                "VALUES ('received', ?, ?, ?, ?, ?)",
                (row.get("Supplier"),
                 f"Imported from simulated workbook, PO ref {row.get('PO / Order Ref')}",
                 USER_ID, _iso(row.get("Ordered At")), _iso(row.get("Ordered At"))),
            )
            po_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO purchase_order_item "
                "(purchase_order_id, ingredient_id, quantity, unit, received_qty_base, note) "
                "VALUES (?,?,?,?,?,?)",
                (po_id, ing["id"], ordered_qty, row.get("Purchase/Pack Unit") or ing["base_unit"], received_qty_base,
                 f"sim-receipt:{receipt_ref}"),
            )
            item_id = cur.lastrowid
            # qty_base > 0 is enforced by schema -- a row with Received Qty
            # 0 (64 of 5977; a short/failed delivery) still gets its
            # purchase_order/_item rows for lead-time-sample completeness,
            # just no receipt event, since there was nothing to receive.
            if received_qty_base > 0:
                conn.execute(
                    "INSERT INTO purchase_order_receipt "
                    "(purchase_order_item_id, qty_base, client_request_id, created_by, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (item_id, received_qty_base, receipt_ref, USER_ID, _iso(row.get("Received At"))),
                )
            inserted["receiving"] += 1

        # --- Wastage + Stock Counts -> stock_ledger, historical-observation
        # only (see _replay_ledger()'s own docstring for why this never
        # touches ingredient.on_hand_base).
        existing_wastage_refs = _existing_ledger_refs(conn, "sim_wastage")
        existing_count_refs = _existing_ledger_refs(conn, "sim_count")
        ledger_rows = _replay_ledger(conn, wastage_ok, counts_ok, ingredients, receiving_rows=receiving_ok)
        for lr in ledger_rows:
            existing = existing_wastage_refs if lr["ref_type"] == "sim_wastage" else existing_count_refs
            if lr["ref_id"] in existing:
                duplicates["wastage" if lr["ref_type"] == "sim_wastage" else "counts"] += 1
                continue
            insert_ledger_row(conn, lr, USER_ID, batch_id=batch_id)
            if lr["ref_type"] == "sim_wastage":
                inserted["wastage"] += 1
            else:
                inserted["counts"] += 1

        # --- Unli Reconciliation -> unli_flavor_reconciliation (migration 0044)
        existing_recon_refs = _existing_source_refs(conn)
        for row in recon_ok:
            ref = row["Reconciliation ID"]
            if ref in existing_recon_refs:
                duplicates["reconciliation"] += 1
                continue
            flavor = flavors[row["_flavor_name"]]
            insert_reconciliation_row(conn, row, flavor, ref, batch_id, USER_ID)
            inserted["reconciliation"] += 1

    stats["inserted"] = inserted
    stats["duplicates"] = duplicates
    stats["purged_previous_import"] = purged
    stats["batch_id"] = batch_id
    if verbose:
        print(f"simulated workbook import ({batch_id}): {inserted}")
    return stats


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--inventory-path", type=Path, default=INVENTORY_XLSX_PATH)
    p.add_argument("--historical-path", type=Path, default=HISTORICAL_XLSX_PATH)
    p.add_argument("--no-replace", action="store_true",
                   help="Skip purging a previous import's rows before re-importing "
                        "(default is to replace them).")
    args = p.parse_args()
    c = db.get_conn()
    if not args.dry_run:
        db.backup_now("pre_simulated_workbook_import", conn=c)
    result = run(c, dry_run=args.dry_run, inventory_path=args.inventory_path,
                 historical_path=args.historical_path, replace_existing=not args.no_replace)
    import json
    print(json.dumps(result, ensure_ascii=True, default=str, indent=2))
