"""Unlimited-item reconciliation: the three-stage provisional/actual/reconciled
model, end to end.

    Stage 1   D  = H x lambda        provisional, at order time
    Stage 2   Au = opening + added - closing
                                     actual, from what the kitchen weighed
    Stage 3   variance = Au - D      compare, judge, correct, recalibrate

Stage 1 already ran
-------------------
This module never re-deducts D. By the time anybody reconciles a service
date, the provisional quantity has *already* left the ledger: an unli order
line has no bill of materials, so resolver.order_requirements() sizes it from
head count x the live rolling coefficient and the ordinary two-phase
reserve/commit path books it as a normal `sale_commit`. Reconciliation's job
is therefore to post the *difference* between what was assumed and what was
measured, and nothing else. Posting Au itself would deduct the same food
twice, which is the single most damaging thing this module could do, so:

  * the correction is always (theoretical - actual), never actual;
  * unli_reconciliation carries UNIQUE (service_date, unli_item_id), so a
    second submission for the same day and stream is refused by the database
    rather than by a Python check that a retry could race past;
  * the measurement, the arithmetic, the ledger row, the stock write and the
    batch write all happen inside one write_tx.

Reproducing D honestly
----------------------
`theoretical_base` is not H x (whatever lambda happens to be current). It is
recomputed per order against the coefficient that was in force when that
order was punched (unli_coefficient.effective_from <= the order's created_at),
which is exactly what order_requirements() read at the time. Recalibrating
lambda on Tuesday must not retroactively change what Monday's deduction was,
or the variance stops being a measurement of anything.

Only orders that actually reached the kitchen count. A `punched` order holds
a reservation but has moved no stock, and a voided one was credited back, so
both are excluded — the theoretical figure has to describe the same set of
movements the physical measurement does.
"""
from __future__ import annotations

import sqlite3

from . import timeutil
from .db import write_tx
from .errors import InvalidStateError, NotFoundError
from .reservation import EPS, _ledger, restore_fefo, deplete_fefo

# Order states whose stock has genuinely left on_hand via commit_order.
COMMITTED_STATES = ("in_progress", "ready", "served")

DEFAULT_TOLERANCE = 0.10
DEFAULT_MIN_HEADS = 20


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def get_setting(conn: sqlite3.Connection, key: str, default: float) -> float:
    row = conn.execute("SELECT value FROM app_setting WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return default


def set_setting(conn: sqlite3.Connection, key: str, value: float, user_id: str | None = None) -> None:
    with write_tx(conn):
        conn.execute(
            "INSERT INTO app_setting (key, value, updated_by, updated_at) "
            "VALUES (?,?,?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_by=excluded.updated_by, updated_at=datetime('now')",
            (key, str(value), user_id),
        )


def tolerance(conn: sqlite3.Connection) -> float:
    return get_setting(conn, "unli_variance_tolerance", DEFAULT_TOLERANCE)


# ---------------------------------------------------------------------------
# Stage 1 — what the provisional deduction actually was
# ---------------------------------------------------------------------------
def _streams(conn: sqlite3.Connection, item_key: str | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT ui.id, ui.item_key, ui.label, ui.window_days,
               ui.ingredient_id, i.name AS ingredient_name, i.base_unit,
               i.on_hand_base
          FROM unli_item ui JOIN ingredient i ON i.id = ui.ingredient_id
         WHERE ui.is_active = 1
    """
    params: list = []
    if item_key:
        sql += " AND ui.item_key = ?"
        params.append(item_key)
    return conn.execute(sql + " ORDER BY ui.id", params).fetchall()


def provisional_for_date(conn: sqlite3.Connection, unli_item_id: int, service_date: str) -> dict:
    """Head count and provisional quantity D for one stream on one service date.

    Walks the individual orders rather than multiplying a daily head total by
    one lambda: the coefficient can change mid-window, and a day that spans a
    recalibration would otherwise be reported against a lambda that was only
    true for part of it.
    """
    orders = conn.execute(
        f"""
        SELECT o.id, o.created_at, o.head_count,
               SUM(ol.qty) AS unli_lines
          FROM customer_order o
          JOIN order_line ol ON ol.order_id = o.id
          JOIN menu_item  m  ON m.id = ol.menu_item_id AND m.is_unli = 1
         WHERE {timeutil.sql_business_date("COALESCE(o.committed_at, o.created_at)")} = ?
           AND o.status IN ({",".join("?" * len(COMMITTED_STATES))})
         GROUP BY o.id
        """,
        (service_date, *COMMITTED_STATES),
    ).fetchall()

    heads = 0
    theoretical = 0.0
    coefficient_id = None
    lambda_used = None
    for o in orders:
        covers = (o["head_count"] or 0) * (o["unli_lines"] or 0)
        if covers <= 0:
            continue
        coeff = conn.execute(
            """
            SELECT id, lambda_value FROM unli_coefficient
             WHERE unli_item_id = ? AND role = 'rolling' AND effective_from <= ?
             ORDER BY effective_from DESC, id DESC LIMIT 1
            """,
            (unli_item_id, o["created_at"]),
        ).fetchone()
        if coeff is None:
            # An order punched before this stream had any rolling coefficient
            # cannot have been sized from one, so it deducted nothing for this
            # stream and contributes nothing here either.
            continue
        heads += covers
        theoretical += coeff["lambda_value"] * covers
        coefficient_id, lambda_used = coeff["id"], coeff["lambda_value"]

    return {
        "heads": heads,
        "theoretical_base": theoretical,
        "coefficient_id": coefficient_id,
        "lambda_used": lambda_used,
        "orders": len(orders),
    }


def refill_context(conn: sqlite3.Connection, service_date: str) -> dict:
    """What the floor actually recorded serving on this date.

    Operational context for the reconciliation screen, NOT a second
    reconciliation and not an input to the variance. The authoritative
    comparison stays H x lambda against a physical weighing; refill counts
    answer the different question a manager asks when a variance looks odd —
    "how many rounds went out, and were they unusually large?".

    Deliberately not folded into theoretical_base. Refills record wings by the
    piece and the provisional deduction sizes wings by the gram from lambda;
    conflating the two would smuggle a second estimate into a figure whose
    whole value is that it reproduces exactly what the ledger did.
    """
    served_bd = timeutil.sql_business_date("r.served_at")
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS refills,
               COALESCE(SUM(r.pieces), 0)     AS pieces,
               COALESCE(SUM(r.sauce_base), 0) AS sauce_base,
               COUNT(DISTINCT r.session_id)   AS sessions,
               COUNT(DISTINCT r.flavor_id)    AS flavors_used
          FROM unli_refill r
         WHERE r.status = 'served' AND {served_bd} = ?
        """,
        (service_date,),
    ).fetchone()
    by_flavor = [dict(r) for r in conn.execute(
        f"""
        SELECT r.flavor_name, COUNT(*) AS refills, SUM(r.pieces) AS pieces
          FROM unli_refill r
         WHERE r.status = 'served' AND {served_bd} = ?
         GROUP BY r.flavor_id ORDER BY refills DESC, r.flavor_name
        """,
        (service_date,),
    ).fetchall()]
    voided_bd = timeutil.sql_business_date("COALESCE(voided_at, created_at)")
    voided = conn.execute(
        f"SELECT COUNT(*) AS n FROM unli_refill WHERE status IN ('voided','cancelled') "
        f"AND {voided_bd} = ?", (service_date,),
    ).fetchone()["n"]
    return {
        "refills_served": row["refills"], "pieces_served": row["pieces"],
        "sauce_base": round(row["sauce_base"], 3), "sessions": row["sessions"],
        "flavors_used": row["flavors_used"], "refills_voided": voided,
        "by_flavor": by_flavor,
    }


def pending(conn: sqlite3.Connection, service_date: str) -> dict:
    """Everything the reconciliation screen needs before anyone weighs
    anything: per stream, how many covers that date served, what was
    provisionally deducted for them, and whether it has been reconciled.

    Carries the day's refill activity alongside as context — see
    refill_context() for why it is context and not an input.
    """
    out = []
    for s in _streams(conn):
        prov = provisional_for_date(conn, s["id"], service_date)
        done = conn.execute(
            "SELECT id, actual_base, variance_base, variance_ratio, status, "
            "validation_state, correction_base FROM unli_reconciliation "
            "WHERE service_date=? AND unli_item_id=?",
            (service_date, s["id"]),
        ).fetchone()
        out.append({
            "unli_item_id": s["id"],
            "item_key": s["item_key"],
            "label": s["label"],
            "ingredient_id": s["ingredient_id"],
            "ingredient_name": s["ingredient_name"],
            "base_unit": s["base_unit"],
            "heads": prov["heads"],
            "theoretical_base": round(prov["theoretical_base"], 3),
            "lambda_used": prov["lambda_used"],
            "orders": prov["orders"],
            "reconciled": dict(done) if done else None,
        })
    return {
        "service_date": service_date,
        "tolerance_ratio": tolerance(conn),
        "streams": out,
        "refill_context": refill_context(conn, service_date),
    }


# ---------------------------------------------------------------------------
# Stage 2 + 3 — measure, judge, correct
# ---------------------------------------------------------------------------
def record_reconciliation(
    conn: sqlite3.Connection,
    service_date: str,
    item_key: str,
    opening_base: float,
    closing_base: float,
    added_base: float = 0.0,
    note: str | None = None,
    user_id: str | None = None,
) -> dict:
    """Reconcile one stream for one service date. All of it, or none of it.

    Au = opening + added - closing, in the stream's base unit. A container
    tare cancels between the two readings, so gross weights are fine as long
    as both were taken the same way.

    The inventory correction is the delta against what was already deducted
    provisionally — theoretical - actual — so it is negative (more stock
    leaves) when the kitchen used more than the coefficient predicted and
    positive (stock comes back) when it used less. It is applied whenever it
    is non-zero, not only when the variance breaches tolerance: the tolerance
    decides whether a human is asked about the day, not whether the balance is
    allowed to stay wrong.
    """
    streams = _streams(conn, item_key)
    if not streams:
        raise NotFoundError(f"no active unlimited stream '{item_key}'")
    s = streams[0]

    for label, value in (("opening", opening_base), ("closing", closing_base),
                         ("added", added_base)):
        if value < 0:
            raise InvalidStateError(f"{label} weight cannot be negative")

    issued = opening_base + added_base
    if closing_base > issued + EPS:
        raise InvalidStateError(
            f"leftover ({closing_base:g} {s['base_unit']}) is more than what was put "
            f"out ({issued:g} {s['base_unit']}) — check the two readings"
        )

    actual = issued - closing_base

    with write_tx(conn):
        if conn.execute(
            "SELECT 1 FROM unli_reconciliation WHERE service_date=? AND unli_item_id=?",
            (service_date, s["id"]),
        ).fetchone():
            raise InvalidStateError(
                f"{s['label']} has already been reconciled for {service_date}"
            )

        prov = provisional_for_date(conn, s["id"], service_date)
        heads = prov["heads"]
        theoretical = prov["theoretical_base"]
        if heads <= 0:
            raise InvalidStateError(
                f"no completed unlimited orders on {service_date} — there is no "
                f"provisional deduction to reconcile against"
            )

        variance = actual - theoretical
        variance_ratio = (variance / theoretical) if theoretical > 0 else None
        tol = tolerance(conn)
        if variance_ratio is None or abs(variance_ratio) <= tol:
            status = "within_tolerance"
            validation_state = "validated"
        else:
            status = "over_tolerance" if variance_ratio > 0 else "under_tolerance"
            # Held back from lambda until a human agrees it is a real portion
            # measurement and not, say, a tray somebody forgot to weigh.
            validation_state = "pending_review"

        # --- the correction ------------------------------------------------
        # Never `actual`: the provisional already left the ledger at commit.
        correction = theoretical - actual
        on_hand = conn.execute(
            "SELECT on_hand_base FROM ingredient WHERE id=?", (s["ingredient_id"],)
        ).fetchone()["on_hand_base"]
        # Clamped the same way record_wastage is: a correction bigger than the
        # remaining balance means the stock was already gone by other means,
        # and a CHECK failure that aborts the whole reconciliation is a worse
        # answer than posting what can actually be posted and saying so.
        applied = max(correction, -on_hand) if correction < 0 else correction
        clamped = abs(applied - correction) > EPS

        ledger_id = None
        if abs(applied) > EPS:
            conn.execute(
                "UPDATE ingredient SET on_hand_base = MAX(0, on_hand_base + :d), "
                "updated_at = datetime('now') WHERE id = :id",
                {"d": applied, "id": s["ingredient_id"]},
            )
            if applied < 0:
                deplete_fefo(conn, s["ingredient_id"], -applied)
            else:
                restore_fefo(conn, s["ingredient_id"], applied)

        cur = conn.execute(
            """
            INSERT INTO unli_reconciliation
              (service_date, unli_item_id, heads, theoretical_base, actual_base,
               variance_base, variance_ratio, coefficient_id, opening_base,
               added_base, closing_base, tolerance_ratio, status, validation_state,
               correction_base, recorded_by, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (service_date, s["id"], heads, theoretical, actual, variance,
             variance_ratio, prov["coefficient_id"], opening_base, added_base,
             closing_base, tol, status, validation_state, applied, user_id, note),
        )
        recon_id = cur.lastrowid

        if abs(applied) > EPS:
            _ledger(
                conn, s["ingredient_id"], applied, "unli_reconcile",
                "unli_reconciliation", str(recon_id), user_id,
                note=f"{s['label']} {service_date}: measured {actual:.1f} vs "
                     f"provisional {theoretical:.1f} {s['base_unit']}",
            )
            ledger_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute("UPDATE unli_reconciliation SET ledger_id=? WHERE id=?",
                         (ledger_id, recon_id))

    return {
        "id": recon_id,
        "service_date": service_date,
        "item_key": s["item_key"],
        "label": s["label"],
        "base_unit": s["base_unit"],
        "heads": heads,
        "opening_base": opening_base,
        "added_base": added_base,
        "closing_base": closing_base,
        "theoretical_base": round(theoretical, 3),
        "actual_base": round(actual, 3),
        "actual_per_head": round(actual / heads, 3) if heads else None,
        "variance_base": round(variance, 3),
        "variance_ratio": round(variance_ratio, 4) if variance_ratio is not None else None,
        "variance_pct": round(variance_ratio * 100, 2) if variance_ratio is not None else None,
        "tolerance_ratio": tol,
        "status": status,
        "validation_state": validation_state,
        "correction_base": round(applied, 3),
        "correction_clamped": clamped,
        "inventory_corrected": abs(applied) > EPS,
        "ledger_id": ledger_id,
    }


def review(conn: sqlite3.Connection, recon_pk: int, approve: bool,
           note: str | None = None, user_id: str | None = None) -> dict:
    """Sign off (or reject) an out-of-tolerance reconciliation.

    Only affects whether the day may influence lambda. The inventory
    correction was posted when the measurement was recorded and is not
    unwound here — rejecting a day says "do not learn a portion size from
    this", not "the scale was lying about how much food is left".
    """
    with write_tx(conn):
        row = conn.execute("SELECT * FROM unli_reconciliation WHERE id=?", (recon_pk,)).fetchone()
        if not row:
            raise NotFoundError(f"reconciliation {recon_pk} not found")
        if row["validation_state"] != "pending_review":
            raise InvalidStateError(
                f"this reconciliation is already '{row['validation_state']}' — "
                f"only a pending_review row can be signed off"
            )
        state = "validated" if approve else "rejected"
        conn.execute(
            "UPDATE unli_reconciliation SET validation_state=?, reviewed_by=?, "
            "reviewed_at=datetime('now'), note=COALESCE(?, note) WHERE id=?",
            (state, user_id, note, recon_pk),
        )
    return {"id": recon_pk, "validation_state": state, "reviewed_by": user_id}


def revision_history(conn: sqlite3.Connection, recon_pk: int) -> list[dict]:
    """Every revision ever recorded against one original reconciliation,
    oldest first — the append-only record a caller walks to see exactly how
    a service-date's reconciled figures evolved as late orders committed."""
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM unli_reconciliation_revision WHERE reconciliation_id=? ORDER BY id",
            (recon_pk,),
        ).fetchall()
    ]


def _cumulative_correction(conn: sqlite3.Connection, recon_pk: int, original_correction: float) -> float:
    row = conn.execute(
        "SELECT cumulative_correction_base FROM unli_reconciliation_revision "
        "WHERE reconciliation_id=? ORDER BY id DESC LIMIT 1",
        (recon_pk,),
    ).fetchone()
    return row["cumulative_correction_base"] if row else original_correction


def _revision_calc(conn: sqlite3.Connection, recon_pk: int,
                   opening_base: float, closing_base: float, added_base: float) -> dict:
    """The arithmetic revise_reconciliation() and preview_revision() both
    need, factored out so the preview a caller reviews is GUARANTEED to be
    the exact same computation the real submission later applies -- never
    two independently-maintained copies that could drift apart. Read-only:
    touches no table, opens no transaction. Raises NotFoundError/
    InvalidStateError exactly as revise_reconciliation() itself documents.
    """
    original = conn.execute(
        "SELECT * FROM unli_reconciliation WHERE id=?", (recon_pk,)
    ).fetchone()
    if not original:
        raise NotFoundError(f"reconciliation {recon_pk} not found")

    s = conn.execute(
        "SELECT ui.id, ui.item_key, ui.label, ui.ingredient_id, i.base_unit "
        "FROM unli_item ui JOIN ingredient i ON i.id = ui.ingredient_id WHERE ui.id=?",
        (original["unli_item_id"],),
    ).fetchone()

    issued = opening_base + added_base
    if closing_base > issued + EPS:
        raise InvalidStateError(
            f"leftover ({closing_base:g} {s['base_unit']}) is more than what was put "
            f"out ({issued:g} {s['base_unit']}) — check the two readings"
        )
    actual = issued - closing_base

    # Fresh, not the original's own stale figure -- this is what picks
    # up a late-committed order automatically, and what lets a caller
    # detect one at all (heads > original["heads"]).
    prov = provisional_for_date(conn, s["id"], original["service_date"])
    heads = prov["heads"]
    theoretical = prov["theoretical_base"]
    if heads <= 0:
        raise InvalidStateError(
            f"no completed unlimited orders on {original['service_date']} — there is no "
            f"provisional deduction to reconcile against"
        )

    variance = actual - theoretical
    variance_ratio = (variance / theoretical) if theoretical > 0 else None
    tol = tolerance(conn)
    if variance_ratio is None or abs(variance_ratio) <= tol:
        status = "within_tolerance"
        validation_state = "validated"
    else:
        status = "over_tolerance" if variance_ratio > 0 else "under_tolerance"
        validation_state = "pending_review"

    full_correction = theoretical - actual
    already_applied = _cumulative_correction(conn, recon_pk, original["correction_base"])
    incremental = full_correction - already_applied

    on_hand = conn.execute(
        "SELECT on_hand_base FROM ingredient WHERE id=?", (s["ingredient_id"],)
    ).fetchone()["on_hand_base"]
    applied = max(incremental, -on_hand) if incremental < 0 else incremental
    clamped = abs(applied - incremental) > EPS

    return {
        "original": original, "stream": s, "actual": actual, "heads": heads,
        "theoretical": theoretical, "variance": variance, "variance_ratio": variance_ratio,
        "tolerance_ratio": tol, "status": status, "validation_state": validation_state,
        "already_applied": already_applied, "applied": applied, "clamped": clamped,
    }


def preview_revision(
    conn: sqlite3.Connection, recon_pk: int,
    opening_base: float, closing_base: float, added_base: float = 0.0,
) -> dict:
    """Read-only dry run of revise_reconciliation() -- lets a caller see the
    freshly recomputed head count (so a late-committed order is visible
    BEFORE anything is written), the measured variance, and the exact
    incremental correction that would post, without touching stock, the
    ledger, or the revision table at all. 'lateOrdersDetected' is simply
    whether the fresh head count differs from what the original
    reconciliation recorded -- the plain, honest signal that at least one
    order has committed (or been voided) against this service date since
    it was last reconciled."""
    for label, value in (("opening", opening_base), ("closing", closing_base),
                         ("added", added_base)):
        if value < 0:
            raise InvalidStateError(f"{label} weight cannot be negative")
    calc = _revision_calc(conn, recon_pk, opening_base, closing_base, added_base)
    original, s = calc["original"], calc["stream"]
    cumulative = calc["already_applied"] + calc["applied"]
    return {
        "reconciliationId": recon_pk,
        "serviceDate": original["service_date"],
        "label": s["label"],
        "baseUnit": s["base_unit"],
        "headsAtOriginal": original["heads"],
        "heads": calc["heads"],
        "lateOrdersDetected": calc["heads"] != original["heads"],
        "theoreticalBase": round(calc["theoretical"], 3),
        "actualBase": round(calc["actual"], 3),
        "varianceBase": round(calc["variance"], 3),
        "varianceRatio": round(calc["variance_ratio"], 4) if calc["variance_ratio"] is not None else None,
        "variancePct": round(calc["variance_ratio"] * 100, 2) if calc["variance_ratio"] is not None else None,
        "toleranceRatio": calc["tolerance_ratio"],
        "status": calc["status"],
        "validationState": calc["validation_state"],
        "alreadyAppliedBase": round(calc["already_applied"], 3),
        "incrementalCorrectionBase": round(calc["applied"], 3),
        "cumulativeCorrectionBase": round(cumulative, 3),
        "correctionClamped": calc["clamped"],
    }


def revise_reconciliation(
    conn: sqlite3.Connection, recon_pk: int,
    opening_base: float, closing_base: float, added_base: float = 0.0,
    note: str | None = None, user_id: str | None = None,
    client_request_id: str | None = None,
) -> dict:
    """Bring an already-reconciled service-date/stream up to date — the
    2026-10 fix for the case record_reconciliation() itself explicitly
    refuses: a reconciliation run before every one of that day's orders had
    committed. The ORIGINAL unli_reconciliation row is never mutated (its
    own UNIQUE(service_date, unli_item_id) and every existing reader of it
    keep meaning exactly what they always meant) — this inserts a new,
    separate unli_reconciliation_revision row instead, see migration 0049.

    theoretical_base is always recomputed fresh via provisional_for_date(),
    so a late-committed order (or a correction/void that landed since the
    original run) is picked up automatically. The correction POSTED here is
    only the INCREMENTAL difference between the freshly computed
    theoretical-vs-actual gap and whatever has already been applied across
    the original row and any earlier revisions — never the full gap again,
    which is exactly the double-deduction this module's own module
    docstring warns against. Idempotent via client_request_id, same pattern
    as every other mutating write in this codebase (e.g.
    purchasing.receive_line). See preview_revision() for the read-only dry
    run a caller should show before calling this."""
    if client_request_id:
        existing = conn.execute(
            "SELECT * FROM unli_reconciliation_revision WHERE client_request_id=?",
            (client_request_id,),
        ).fetchone()
        if existing:
            return _revision_row(conn, dict(existing))

    for label, value in (("opening", opening_base), ("closing", closing_base),
                         ("added", added_base)):
        if value < 0:
            raise InvalidStateError(f"{label} weight cannot be negative")

    with write_tx(conn):
        calc = _revision_calc(conn, recon_pk, opening_base, closing_base, added_base)
        original, s = calc["original"], calc["stream"]
        heads, theoretical, actual = calc["heads"], calc["theoretical"], calc["actual"]
        variance, variance_ratio = calc["variance"], calc["variance_ratio"]
        tol, status, validation_state = calc["tolerance_ratio"], calc["status"], calc["validation_state"]
        already_applied, applied, clamped = calc["already_applied"], calc["applied"], calc["clamped"]

        ledger_id = None
        if abs(applied) > EPS:
            conn.execute(
                "UPDATE ingredient SET on_hand_base = MAX(0, on_hand_base + :d), "
                "updated_at = datetime('now') WHERE id = :id",
                {"d": applied, "id": s["ingredient_id"]},
            )
            if applied < 0:
                deplete_fefo(conn, s["ingredient_id"], -applied)
            else:
                restore_fefo(conn, s["ingredient_id"], applied)

        cumulative = already_applied + applied
        cur = conn.execute(
            """
            INSERT INTO unli_reconciliation_revision
              (reconciliation_id, heads, theoretical_base, actual_base, variance_base,
               variance_ratio, opening_base, added_base, closing_base, tolerance_ratio,
               status, validation_state, correction_base, cumulative_correction_base,
               client_request_id, recorded_by, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (recon_pk, heads, theoretical, actual, variance, variance_ratio,
             opening_base, added_base, closing_base, tol, status, validation_state,
             applied, cumulative, client_request_id, user_id, note),
        )
        revision_id = cur.lastrowid

        if abs(applied) > EPS:
            # reason stays 'unli_reconcile' -- stock_ledger.reason's CHECK
            # constraint (migration 0005) is a fixed, deliberately short
            # enum that would need a full table rebuild to extend (the one
            # place append-only really matters, per that migration's own
            # comment), which is more schema risk than this fix needs.
            # ref_type/ref_id already point at this specific revision row,
            # so provenance is fully traceable without a new reason value.
            _ledger(
                conn, s["ingredient_id"], applied, "unli_reconcile",
                "unli_reconciliation_revision", str(revision_id), user_id,
                note=f"{s['label']} {original['service_date']} (revision): measured "
                     f"{actual:.1f} vs freshly computed provisional {theoretical:.1f} "
                     f"{s['base_unit']}, {applied:+.1f} incremental correction",
            )
            ledger_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute("UPDATE unli_reconciliation_revision SET ledger_id=? WHERE id=?",
                         (ledger_id, revision_id))

        row = conn.execute(
            "SELECT * FROM unli_reconciliation_revision WHERE id=?", (revision_id,)
        ).fetchone()

    return _revision_row(conn, dict(row), label=s["label"], base_unit=s["base_unit"],
                         service_date=original["service_date"], clamped=clamped)


def _revision_row(conn: sqlite3.Connection, r: dict, *, label: str | None = None,
                  base_unit: str | None = None, service_date: str | None = None,
                  clamped: bool = False) -> dict:
    if label is None or base_unit is None or service_date is None:
        original = conn.execute(
            "SELECT service_date, unli_item_id FROM unli_reconciliation WHERE id=?",
            (r["reconciliation_id"],),
        ).fetchone()
        s = conn.execute(
            "SELECT label, base_unit FROM unli_item ui JOIN ingredient i ON i.id = ui.ingredient_id "
            "WHERE ui.id=?", (original["unli_item_id"],),
        ).fetchone()
        label, base_unit, service_date = s["label"], s["base_unit"], original["service_date"]
    return {
        "id": r["id"],
        "reconciliation_id": r["reconciliation_id"],
        "service_date": service_date,
        "label": label,
        "base_unit": base_unit,
        "heads": r["heads"],
        "theoretical_base": round(r["theoretical_base"], 3),
        "actual_base": round(r["actual_base"], 3),
        "variance_base": round(r["variance_base"], 3),
        "variance_ratio": round(r["variance_ratio"], 4) if r["variance_ratio"] is not None else None,
        "variance_pct": round(r["variance_ratio"] * 100, 2) if r["variance_ratio"] is not None else None,
        "status": r["status"],
        "validation_state": r["validation_state"],
        "correction_base": round(r["correction_base"], 3),
        "cumulative_correction_base": round(r["cumulative_correction_base"], 3),
        "correction_clamped": clamped,
        "inventory_corrected": abs(r["correction_base"]) > EPS,
        "ledger_id": r["ledger_id"],
        "created_at": r["created_at"],
    }


def history(conn: sqlite3.Connection, limit: int = 100, item_key: str | None = None,
            days: int | None = None) -> dict:
    sql = "SELECT * FROM v_unli_daily_consumption WHERE 1=1"
    params: list = []
    if item_key:
        sql += " AND item_key = ?"
        params.append(item_key)
    if days:
        sql += " AND service_date >= ?"
        params.append(timeutil.business_date_n_days_ago(days))
    sql += " ORDER BY service_date DESC, unli_item_id LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        r["variance_pct"] = round(r["variance_ratio"] * 100, 2) if r["variance_ratio"] is not None else None
    return {"reconciliations": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Stage 3 (tail) — lambda recalibration
# ---------------------------------------------------------------------------
def recalibrate(conn: sqlite3.Connection, item_key: str | None = None,
                user_id: str | None = None, dry_run: bool = False) -> dict:
    """Re-estimate each stream's rolling lambda from its own validated
    reconciliation history.

        lambda = SUM(actual_base) / SUM(heads)   over the stream's window

    A pooled ratio, not the mean of per-day rates: a 4-cover Tuesday should
    not carry the same weight as a 60-cover Saturday when the question is
    "how much does one cover eat".

    Only `validated` rows count, and only days at or above the configured
    minimum head count — the same two filters that keep a fryer-line accident
    or a single table's leftovers from becoming the shop's portion constant.
    The baseline coefficient is never touched: it is the frozen reference the
    variance is *measured against*, and a baseline that moves with the data
    measures nothing.

    Writes a new unli_coefficient row rather than updating the current one, so
    every lambda the system ever deducted against stays recoverable — which is
    precisely what provisional_for_date() relies on to reproduce an old day's
    provisional figure.
    """
    min_heads = get_setting(conn, "unli_recalibration_min_heads", DEFAULT_MIN_HEADS)
    results = []
    for s in _streams(conn, item_key):
        agg = conn.execute(
            """
            SELECT COUNT(*) AS days, SUM(heads) AS heads, SUM(actual_base) AS actual,
                   MIN(service_date) AS window_start, MAX(service_date) AS window_end
              FROM unli_reconciliation
             WHERE unli_item_id = ?
               AND validation_state = 'validated'
               AND heads >= ?
               AND service_date >= ?
            """,
            (s["id"], min_heads, timeutil.business_date_n_days_ago(s["window_days"])),
        ).fetchone()

        current = conn.execute(
            "SELECT id, lambda_value FROM unli_coefficient WHERE unli_item_id=? "
            "AND role='rolling' ORDER BY effective_from DESC, id DESC LIMIT 1",
            (s["id"],),
        ).fetchone()

        entry = {
            "unli_item_id": s["id"], "item_key": s["item_key"], "label": s["label"],
            "base_unit": s["base_unit"],
            "current_lambda": current["lambda_value"] if current else None,
            "validated_days": agg["days"] or 0,
            "total_heads": agg["heads"] or 0,
            "total_actual_base": round(agg["actual"], 3) if agg["actual"] else 0.0,
            "window_days": s["window_days"],
            "new_lambda": None, "applied": False,
        }
        if not agg["days"] or not agg["heads"]:
            entry["reason"] = (
                f"no validated reconciliation of at least {min_heads:g} covers in the "
                f"last {s['window_days']} days"
            )
            results.append(entry)
            continue

        new_lambda = agg["actual"] / agg["heads"]
        entry["new_lambda"] = round(new_lambda, 4)
        entry["change_pct"] = (
            round((new_lambda - current["lambda_value"]) / current["lambda_value"] * 100, 2)
            if current and current["lambda_value"] else None
        )
        if new_lambda <= 0:
            entry["reason"] = "computed coefficient is not positive — not applied"
            results.append(entry)
            continue

        if not dry_run:
            with write_tx(conn):
                conn.execute(
                    """
                    INSERT INTO unli_coefficient
                      (unli_item_id, lambda_value, role, window_start, window_end,
                       total_actual_base, total_heads, status, effective_from)
                    VALUES (?,?, 'rolling', ?,?,?,?, 'verified', datetime('now'))
                    """,
                    (s["id"], new_lambda, agg["window_start"], agg["window_end"],
                     agg["actual"], agg["heads"]),
                )
            entry["applied"] = True
        results.append(entry)

    return {"streams": results, "min_heads": min_heads, "dry_run": dry_run}
