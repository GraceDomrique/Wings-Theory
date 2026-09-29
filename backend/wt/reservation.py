"""Two-phase inventory reservation.

    Punch order -> RESERVE (soft) -> Send to kitchen -> COMMIT -> Cook

Why two phases rather than deducting once
-----------------------------------------
Deducting at punch-in books stock against orders that may never be cooked, so
every void leaves a phantom deduction the ledger has to be corrected for later.
Deducting only when the kitchen starts fixes that but opens a worse hole: from
punch-in until the kitchen touches the ticket, the stock is neither deducted nor
held, so two cashiers can both sell the last portion. The second customer finds
out at the pass.

Reserving on punch and committing on kitchen start closes both. Availability
everywhere reads `on_hand_base - reserved_base`.

Atomicity
---------
The reservation is a single guarded UPDATE:

    UPDATE ingredient
       SET reserved_base = reserved_base + :qty
     WHERE id = :id AND (on_hand_base - reserved_base) >= :qty

The test and the write are one statement, so no interleaving can put two
callers past the guard. `rowcount == 0` means the guard rejected it. This is
deliberately not a SELECT-then-UPDATE in Python: that is the exact read-then-
write race the design has to prevent, and it is invisible in single-threaded
testing.

The same statement is race-safe on PostgreSQL and MySQL, so moving off SQLite
does not require rewriting this module.

Batches
-------
Every path here that moves `on_hand_base` also moves the FEFO batches behind
it, in the same transaction, via deplete_fefo / restore_fefo. The invariant is

    SUM(fefo_batch.qty_base WHERE state='open') == ingredient.on_hand_base

for any ingredient whose stock arrived through receive_stock. It is asserted
directly in TestFEFO rather than left as a comment, because it held silently
for exactly as long as nothing consumed anything: batches were opened by
receiving and never closed by anything at all.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import audit, timeutil
from .db import write_tx
from .errors import InsufficientStockError, InvalidStateError, NotFoundError
from .resolver import order_requirements
from .units import purchase_to_base

# Rounding guard for float accumulation in base units. Sub-milligram noise
# should never fail a reservation.
EPS = 1e-6


@dataclass(frozen=True)
class Shortfall:
    ingredient_id: int
    name: str
    needed_base: float
    available_base: float
    base_unit: str

    def as_dict(self) -> dict:
        return {
            "ingredient_id": self.ingredient_id,
            "name": self.name,
            "needed": round(self.needed_base, 3),
            "available": round(self.available_base, 3),
            # Computed here, not by the caller -- a POS rendering "Available
            # X, Required Y, Shortfall Z" (Phase 09A section 7) should never
            # have to subtract two numbers itself.
            "shortfall": round(max(self.needed_base - self.available_base, 0), 3),
            "unit": self.base_unit,
        }


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _order(conn: sqlite3.Connection, order_pk: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM customer_order WHERE id=?", (order_pk,)).fetchone()
    if not row:
        raise NotFoundError(f"order {order_pk} not found")
    return row


def _ledger(
    conn: sqlite3.Connection,
    ingredient_id: int,
    delta: float,
    reason: str,
    ref_type: str,
    ref_id: str,
    user_id: str | None,
    wastage_reason: str | None = None,
    note: str | None = None,
) -> None:
    """Append one ledger row. Balance is read inside the caller's transaction so
    balance_after_base is the true post-write figure."""
    bal = conn.execute(
        "SELECT on_hand_base FROM ingredient WHERE id=?", (ingredient_id,)
    ).fetchone()["on_hand_base"]
    conn.execute(
        """
        INSERT INTO stock_ledger
          (ingredient_id, delta_base, balance_after_base, reason, wastage_reason,
           ref_type, ref_id, user_id, note)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (ingredient_id, delta, bal, reason, wastage_reason, ref_type, ref_id, user_id, note),
    )


def deplete_fefo(conn: sqlite3.Connection, ingredient_id: int, qty_base: float) -> float:
    """Take `qty_base` out of the open batches, earliest expiry first.

    Until now the only code that touched fefo_batch was receive_stock, which
    opened one. Nothing ever closed one: on_hand_base fell with every sale
    while the batch rows stayed at their received quantity, so the expiry
    monitor kept reporting stock that had physically been eaten weeks ago,
    and the sum of open batches drifted permanently above the real balance.

    Ordering is `expires_at` ascending — the FEFO promise — with NULLs last,
    since a batch with no expiry has no urgency to be used first. `id` breaks
    ties so the pick is deterministic between two batches expiring the same
    day.

    Returns how much was actually drawn from batches, which is normally
    `qty_base` and is less only when batch coverage is short of on_hand_base.
    That is not an error: a database seeded (or unit-tested) by writing
    on_hand_base directly has stock with no batch behind it, and refusing the
    sale for a bookkeeping gap would be a worse answer than depleting what
    batches exist. Never goes negative and never takes more than is there —
    both enforced by the guarded UPDATE, not by arithmetic done in Python.

    Caller must already hold the write transaction; this writes no ledger row
    of its own, because a batch movement is not a stock movement — it is the
    detail of one the caller is already recording.
    """
    remaining = qty_base
    if remaining <= EPS:
        return 0.0
    batches = conn.execute(
        """
        SELECT id, qty_base FROM fefo_batch
         WHERE ingredient_id = ? AND state = 'open' AND qty_base > 0
         ORDER BY (expires_at IS NULL), expires_at, id
        """,
        (ingredient_id,),
    ).fetchall()

    drawn = 0.0
    for b in batches:
        if remaining <= EPS:
            break
        take = min(b["qty_base"], remaining)
        # Guarded the same way a hold is: the test and the write are one
        # statement, so two writers cannot both draw the last of a batch.
        cur = conn.execute(
            "UPDATE fefo_batch SET qty_base = qty_base - :take, "
            "state = CASE WHEN qty_base - :take <= :eps THEN 'depleted' ELSE 'open' END "
            "WHERE id = :id AND state = 'open' AND qty_base >= :take - :eps",
            {"id": b["id"], "take": take, "eps": EPS},
        )
        if cur.rowcount != 1:
            continue
        drawn += take
        remaining -= take
    return drawn


def restore_fefo(conn: sqlite3.Connection, ingredient_id: int, qty_base: float,
                 lot_no: str | None = None) -> None:
    """Put stock back into batch tracking after a credit, an upward count
    correction, or a reconciliation that found less was eaten than the
    provisional deduction assumed.

    Goes back into the batch that would have been issued next (earliest
    expiry, the mirror of deplete_fefo) rather than opening a fresh one, so a
    void-and-restock does not silently reset the expiry clock on stock that
    never left the walk-in. Only when there is no open batch at all does this
    reopen the most recently depleted one, and only when there is none of
    those either does it open a new batch — with no expiry date and status
    'unset', because nobody measured one and inventing a date here would put
    a fabricated constant into the very queue that exists to catch them.
    """
    if qty_base <= EPS:
        return
    target = conn.execute(
        """
        SELECT id FROM fefo_batch
         WHERE ingredient_id = ? AND state = 'open'
         ORDER BY (expires_at IS NULL), expires_at, id LIMIT 1
        """,
        (ingredient_id,),
    ).fetchone()
    if target is None:
        target = conn.execute(
            "SELECT id FROM fefo_batch WHERE ingredient_id = ? AND state = 'depleted' "
            "ORDER BY id DESC LIMIT 1",
            (ingredient_id,),
        ).fetchone()
    if target is not None:
        conn.execute(
            "UPDATE fefo_batch SET qty_base = qty_base + ?, state = 'open' WHERE id = ?",
            (qty_base, target["id"]),
        )
        return
    conn.execute(
        """
        INSERT INTO fefo_batch
          (ingredient_id, lot_no, qty_base, received_at, expires_at, expiry_status, state)
        VALUES (?,?,?, datetime('now'), NULL, 'unset', 'open')
        """,
        (ingredient_id, lot_no, qty_base),
    )


def _sync_fefo(conn: sqlite3.Connection, ingredient_id: int, delta_base: float) -> None:
    """Apply a signed stock movement to the batches. One call site shape for
    every path that moves on_hand_base, so a new movement type cannot forget
    which of the two functions above it needed."""
    if delta_base < -EPS:
        deplete_fefo(conn, ingredient_id, -delta_base)
    elif delta_base > EPS:
        restore_fefo(conn, ingredient_id, delta_base)


def _try_hold(conn: sqlite3.Connection, ingredient_id: int, qty: float) -> bool:
    """The guarded UPDATE. True if the hold was taken."""
    cur = conn.execute(
        """
        UPDATE ingredient
           SET reserved_base = reserved_base + :qty,
               updated_at    = datetime('now')
         WHERE id = :id
           AND (on_hand_base - reserved_base) >= :qty - :eps
        """,
        {"id": ingredient_id, "qty": qty, "eps": EPS},
    )
    return cur.rowcount == 1


def _blocked_ingredients(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT id, name, block_reason FROM v_ingredient_readiness "
        f"WHERE id IN ({marks}) AND is_deductable = 0",
        ids,
    ).fetchall()


# ---------------------------------------------------------------------------
# phase 1 — reserve
# ---------------------------------------------------------------------------
def reserve_order(conn: sqlite3.Connection, order_pk: int, user_id: str | None = None) -> dict:
    """Soft-allocate everything an order needs. All-or-nothing.

    Nothing is written to the ledger here. A reservation is a promise about
    stock, not a movement of it.
    """
    with write_tx(conn):
        order = _order(conn, order_pk)
        if order["status"] != "punched":
            raise InvalidStateError(
                f"cannot reserve an order in state '{order['status']}'",
                order_no=order["order_no"],
            )
        if conn.execute(
            "SELECT 1 FROM reservation WHERE order_id=? AND state='held'", (order_pk,)
        ).fetchone():
            raise InvalidStateError("order already has held reservations")

        needs = order_requirements(conn, order_pk)
        if not needs:
            raise InvalidStateError("order has no deductable lines")

        blocked = _blocked_ingredients(conn, list(needs))
        if blocked:
            raise InsufficientStockError(
                "order contains ingredients that cannot be deducted",
                shortfalls=[
                    {"ingredient_id": b["id"], "name": b["name"], "reason": b["block_reason"]}
                    for b in blocked
                ],
            )

        held: list[int] = []
        shortfalls: list[Shortfall] = []

        # ascending ingredient_id — deterministic acquisition order
        for ingredient_id in sorted(needs):
            qty = needs[ingredient_id]
            if _try_hold(conn, ingredient_id, qty):
                held.append(ingredient_id)
                conn.execute(
                    """
                    INSERT INTO reservation (order_id, ingredient_id, qty_base, state)
                    VALUES (?,?,?,'held')
                    ON CONFLICT(order_id, ingredient_id)
                    DO UPDATE SET qty_base=excluded.qty_base, state='held', resolved_at=NULL
                    """,
                    (order_pk, ingredient_id, qty),
                )
            else:
                row = conn.execute(
                    "SELECT name, base_unit, on_hand_base, reserved_base "
                    "FROM ingredient WHERE id=?",
                    (ingredient_id,),
                ).fetchone()
                shortfalls.append(
                    Shortfall(
                        ingredient_id=ingredient_id,
                        name=row["name"],
                        needed_base=qty,
                        available_base=row["on_hand_base"] - row["reserved_base"],
                        base_unit=row["base_unit"],
                    )
                )

        if shortfalls:
            # write_tx rolls back on the raise, undoing every hold taken above.
            raise InsufficientStockError(
                f"insufficient stock for {len(shortfalls)} ingredient(s)",
                shortfalls=[s.as_dict() for s in shortfalls],
            )

        return {
            "order_id": order_pk,
            "order_no": order["order_no"],
            "state": "reserved",
            "ingredients_held": len(held),
        }


# ---------------------------------------------------------------------------
# phase 2 — commit
# ---------------------------------------------------------------------------
def commit_order(conn: sqlite3.Connection, order_pk: int, user_id: str | None = None) -> dict:
    """Kitchen has started. Turn every held reservation into a real deduction.

    Stock leaves `on_hand` and the hold is released in the same statement, so
    the pair can never be observed half-applied.
    """
    with write_tx(conn):
        order = _order(conn, order_pk)
        if order["status"] not in ("punched",):
            raise InvalidStateError(
                f"cannot commit an order in state '{order['status']}'",
                order_no=order["order_no"],
            )

        holds = conn.execute(
            "SELECT * FROM reservation WHERE order_id=? AND state='held' ORDER BY ingredient_id",
            (order_pk,),
        ).fetchall()
        if not holds:
            raise InvalidStateError("order has no held reservations to commit")

        for h in holds:
            # _try_hold allows a hold when on_hand - reserved is short of qty
            # by up to EPS (float drift, not a real shortage) — MAX(0, ...)
            # here absorbs that same epsilon slack so it can't turn into a
            # CHECK (on_hand_base >= 0) failure at commit time. Without it,
            # a hold taken legitimately at reserve time could still fail to
            # commit purely from accumulated floating-point rounding, which
            # is exactly what happened stress-testing this against several
            # thousand sequential orders.
            cur = conn.execute(
                """
                UPDATE ingredient
                   SET on_hand_base  = MAX(0, on_hand_base - :qty),
                       reserved_base = MAX(0, reserved_base - :qty),
                       updated_at    = datetime('now')
                 WHERE id = :id AND reserved_base >= :qty - :eps
                """,
                {"id": h["ingredient_id"], "qty": h["qty_base"], "eps": EPS},
            )
            if cur.rowcount != 1:
                # Only reachable if a reservation was tampered with out of band.
                raise InvalidStateError(
                    "reservation no longer matches held stock",
                    ingredient_id=h["ingredient_id"],
                )
            # The sale physically leaves the earliest-expiring batch. Same
            # transaction as the on_hand write above, so the two can never be
            # observed disagreeing.
            deplete_fefo(conn, h["ingredient_id"], h["qty_base"])
            _ledger(
                conn,
                h["ingredient_id"],
                -h["qty_base"],
                "sale_commit",
                "order",
                order["order_no"],
                user_id,
            )
            conn.execute(
                "UPDATE reservation SET state='committed', resolved_at=datetime('now') WHERE id=?",
                (h["id"],),
            )

        conn.execute(
            "UPDATE customer_order SET status='in_progress', committed_at=datetime('now') "
            "WHERE id=?",
            (order_pk,),
        )
        return {
            "order_id": order_pk,
            "order_no": order["order_no"],
            "state": "in_progress",
            "ingredients_committed": len(holds),
        }


# ---------------------------------------------------------------------------
# voids
# ---------------------------------------------------------------------------
def release_order(
    conn: sqlite3.Connection, order_pk: int, reason: str = "voided before preparation",
    user_id: str | None = None,
) -> dict:
    """Void before commit. Drop the holds; write nothing to the ledger.

    There is no ledger entry precisely because no stock moved. Recording a
    movement here would put phantom activity into the very dataset the forecast
    and anomaly models are trained on.
    """
    with write_tx(conn):
        order = _order(conn, order_pk)
        if order["status"] not in ("punched",):
            raise InvalidStateError(
                f"cannot release an order in state '{order['status']}' — "
                f"use void_after_commit once the kitchen has started"
            )
        holds = conn.execute(
            "SELECT * FROM reservation WHERE order_id=? AND state='held'", (order_pk,)
        ).fetchall()
        for h in holds:
            conn.execute(
                "UPDATE ingredient SET reserved_base = reserved_base - :qty, "
                "updated_at = datetime('now') WHERE id=:id",
                {"id": h["ingredient_id"], "qty": h["qty_base"]},
            )
            conn.execute(
                "UPDATE reservation SET state='released', resolved_at=datetime('now') WHERE id=?",
                (h["id"],),
            )
        conn.execute(
            "UPDATE customer_order SET status='void', voided_at=datetime('now'), void_reason=? "
            "WHERE id=?",
            (reason, order_pk),
        )
        audit.log(
            conn, "order", order["order_no"], "void", user_id,
            field="status", previous_value=order["status"], new_value="void", note=reason,
        )
        return {
            "order_id": order_pk,
            "order_no": order["order_no"],
            "state": "void",
            "reservations_released": len(holds),
            "ledger_entries": 0,
        }


def void_after_commit(
    conn: sqlite3.Connection,
    order_pk: int,
    wastage_reason: str = "voided after preparation",
    restock: bool = False,
    user_id: str | None = None,
) -> dict:
    """Void once stock has already left the ledger. Never a delete.

    Two outcomes, and the default is the pessimistic one:

      restock=False (default) — the food was cooked. A `void_credit` reverses
        the sale so the order nets to zero, and a `wastage` entry immediately
        books the same quantity out again. Physical stock is unchanged, because
        the food really is gone, but the audit trail now says *why* it is gone.
        Booking only the credit would silently inflate stock by the size of
        every voided order.

      restock=True — the commit fired in error and nothing was prepared. A
        single `void_credit` returns the stock.
    """
    with write_tx(conn):
        order = _order(conn, order_pk)
        if order["status"] == "void":
            raise InvalidStateError("order is already void")
        committed = conn.execute(
            "SELECT * FROM reservation WHERE order_id=? AND state='committed' "
            "ORDER BY ingredient_id",
            (order_pk,),
        ).fetchall()
        if not committed:
            raise InvalidStateError(
                "order has no committed reservations — use release_order instead"
            )

        entries = 0
        for r in committed:
            conn.execute(
                "UPDATE ingredient SET on_hand_base = on_hand_base + :qty, "
                "updated_at = datetime('now') WHERE id=:id",
                {"id": r["ingredient_id"], "qty": r["qty_base"]},
            )
            # The credit puts the stock back where the sale took it from, so
            # batch quantities track the two legs of a void exactly the way
            # on_hand does — including the restock=False case below, where the
            # net effect on both is zero because the food really is gone.
            restore_fefo(conn, r["ingredient_id"], r["qty_base"])
            _ledger(
                conn, r["ingredient_id"], r["qty_base"], "void_credit",
                "order", order["order_no"], user_id, note=wastage_reason,
            )
            entries += 1

            if not restock:
                conn.execute(
                    "UPDATE ingredient SET on_hand_base = on_hand_base - :qty, "
                    "updated_at = datetime('now') WHERE id=:id",
                    {"id": r["ingredient_id"], "qty": r["qty_base"]},
                )
                deplete_fefo(conn, r["ingredient_id"], r["qty_base"])
                _ledger(
                    conn, r["ingredient_id"], -r["qty_base"], "wastage",
                    "order", order["order_no"], user_id, wastage_reason=wastage_reason,
                )
                entries += 1

        conn.execute(
            "UPDATE customer_order SET status='void', voided_at=datetime('now'), void_reason=? "
            "WHERE id=?",
            (wastage_reason, order_pk),
        )
        audit.log(
            conn, "order", order["order_no"], "void", user_id,
            field="status", previous_value=order["status"], new_value="void",
            note=f"{wastage_reason} (restocked={restock})",
        )
        return {
            "order_id": order_pk,
            "order_no": order["order_no"],
            "state": "void",
            "restocked": restock,
            "ledger_entries": entries,
        }


def receive_stock(
    conn: sqlite3.Connection,
    ingredient_pk: int,
    purchase_qty: float,
    lot_no: str | None = None,
    expires_at: str | None = None,
    user_id: str | None = None,
) -> dict:
    """Record a delivery: the only legitimate way on-hand stock increases
    outside of a void-with-restock. Public entry point — opens its own
    transaction and delegates to _receive_stock_locked(). See that function
    for the actual logic; this wrapper exists only so purchasing.receive_line
    can run the identical logic inside ITS OWN already-open transaction
    (write_tx forbids nesting), keeping a PO's received_qty_base/status
    update and the resulting ledger/FEFO write atomic — see wt/purchasing.py.
    """
    with write_tx(conn):
        return _receive_stock_locked(conn, ingredient_pk, purchase_qty, lot_no, expires_at, user_id)


def _receive_stock_locked(
    conn: sqlite3.Connection,
    ingredient_pk: int,
    purchase_qty: float,
    lot_no: str | None = None,
    expires_at: str | None = None,
    user_id: str | None = None,
) -> dict:
    """The actual receiving logic — assumes the caller already holds an open
    write transaction (same contract audit.log() uses). Every other mutation
    in this module only ever decreases stock or corrects a previous entry —
    this is the one path that adds real, newly-purchased stock, so it is
    also the one path that opens a new FEFO batch.

    purchase_qty is in the ingredient's own purchase unit (packs, bottles,
    kilos — whatever purchase_unit says); it is converted to base units via
    the ingredient's recorded pack_size and yield_factor, the same conversion
    the importer uses. An ingredient with no pack_size set cannot be received
    — the console's Needs Verification queue exists precisely to catch this
    before someone tries to fix it by pushing more real stock through it.
    """
    row = conn.execute(
        "SELECT name, pack_size, yield_factor, base_unit FROM ingredient WHERE id=?",
        (ingredient_pk,),
    ).fetchone()
    if not row:
        raise NotFoundError(f"ingredient {ingredient_pk} not found")
    if purchase_qty <= 0:
        raise InvalidStateError("received quantity must be greater than zero")
    if row["pack_size"] is None:
        raise InvalidStateError(
            f"{row['name']} has no pack size set — set it first so a purchase "
            f"quantity can be converted into base units"
        )

    base_qty = purchase_to_base(purchase_qty, row["pack_size"], row["yield_factor"])
    previous_on_hand = conn.execute(
        "SELECT on_hand_base FROM ingredient WHERE id=?", (ingredient_pk,)
    ).fetchone()["on_hand_base"]

    conn.execute(
        "UPDATE ingredient SET on_hand_base = on_hand_base + :qty, "
        "updated_at = datetime('now') WHERE id = :id",
        {"qty": base_qty, "id": ingredient_pk},
    )
    _ledger(conn, ingredient_pk, base_qty, "purchase_receipt", "purchase",
           lot_no or f"RECV-{ingredient_pk}-{purchase_qty}", user_id, note=lot_no)
    conn.execute(
        """
        INSERT INTO fefo_batch
          (ingredient_id, lot_no, qty_base, received_at, expires_at, expiry_status, state)
        VALUES (?,?,?, datetime('now'), ?, ?, 'open')
        """,
        (ingredient_pk, lot_no, base_qty, expires_at,
         "needs_verification" if expires_at else "unset"),
    )
    new_on_hand = conn.execute(
        "SELECT on_hand_base FROM ingredient WHERE id=?", (ingredient_pk,)
    ).fetchone()["on_hand_base"]
    audit.log(
        conn, "ingredient", ingredient_pk, "receive", user_id,
        field="on_hand_base", previous_value=round(previous_on_hand, 4),
        new_value=round(new_on_hand, 4), note=lot_no,
    )

    return {
        "ingredient_id": ingredient_pk,
        "name": row["name"],
        "received_base": round(base_qty, 4),
        "base_unit": row["base_unit"],
        "on_hand_base": round(new_on_hand, 4),
    }


def record_count(
    conn: sqlite3.Connection,
    ingredient_pk: int,
    actual_base: float,
    note: str | None = None,
    user_id: str | None = None,
) -> dict:
    """Reconcile what the system expects is on hand against a real physical
    count. This is the "compare expected vs. actual" half of the recipe-based
    deduction system — the deduction side assumes every gram used was
    accounted for by a recipe; this is how the difference (spillage, an
    over-generous scoop, an unlogged loss) gets surfaced and corrected,
    without silently editing on_hand_base and losing the discrepancy.

    Always writes a ledger entry, even when the count matches exactly (delta
    0) — a confirmed count is itself worth a record, not just a correction.

    A count states physical reality and is never refused for landing below
    on_hand alone. But it IS refused when it would land below what is
    currently reserved for active orders: the schema enforces
    reserved_base <= on_hand_base as a hard invariant (see
    migrations/0001_ingredient_master.sql), because every other order path
    (commit_order, availability checks) relies on a held reservation always
    being physically fulfillable. A count that would break that is a real
    operational conflict — stock promised to an order the count did not
    find — and it must surface as a named shortfall for a person to resolve
    (release or void the order) rather than either being hidden behind
    MAX(0, ...) or crashing the request as a raw database error.
    """
    with write_tx(conn):
        row = conn.execute(
            "SELECT name, base_unit, on_hand_base, reserved_base FROM ingredient WHERE id=?",
            (ingredient_pk,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"ingredient {ingredient_pk} not found")
        if actual_base < 0:
            raise InvalidStateError("a physical count cannot be negative")
        if actual_base < row["reserved_base"] - EPS:
            raise InsufficientStockError(
                f"physical count of {actual_base:g} {row['base_unit']} for {row['name']} is "
                f"below the {row['reserved_base']:g} {row['base_unit']} already reserved for "
                f"active orders — release or resolve those orders before applying this count",
                shortfalls=[{
                    "ingredient_id": ingredient_pk, "name": row["name"],
                    "needed_base": row["reserved_base"], "available_base": actual_base,
                    "base_unit": row["base_unit"],
                }],
            )

        expected = row["on_hand_base"]
        delta = actual_base - expected

        conn.execute(
            "UPDATE ingredient SET on_hand_base = :qty, updated_at = datetime('now') WHERE id = :id",
            {"qty": actual_base, "id": ingredient_pk},
        )
        # A count is the one movement that can go either way, so the batches
        # follow the sign of the variance: a short count draws the missing
        # amount off the oldest batches, a long count puts it back.
        _sync_fefo(conn, ingredient_pk, delta)
        _ledger(conn, ingredient_pk, delta, "count_adjustment", "manual_count", None,
               user_id, note=note)
        audit.log(
            conn, "ingredient", ingredient_pk, "count", user_id,
            field="on_hand_base", previous_value=round(expected, 4),
            new_value=round(actual_base, 4), note=note,
        )

    return {
        "ingredient_id": ingredient_pk,
        "name": row["name"],
        "base_unit": row["base_unit"],
        "expected_base": round(expected, 4),
        "actual_base": round(actual_base, 4),
        "variance_base": round(delta, 4),
    }


def record_wastage(
    conn: sqlite3.Connection,
    ingredient_pk: int,
    qty_base: float,
    reason: str = "wastage",
    note: str | None = None,
    user_id: str | None = None,
) -> dict:
    """Book stock out as thrown away or spoiled.

    Until now the only paths that reduced stock were a sale and a void — so a
    dropped tray or a spoiled batch had nowhere to go, and the only way to
    reflect it was a physical count that silently absorbed the loss with no
    stated cause. This records the loss with its reason, which is also what
    makes it visible to the anomaly check (that only ever reads wastage,
    spoilage and count_adjustment entries).

    Deliberately clamped at zero rather than allowed to go negative: you
    cannot throw away more than you have, and a CHECK-constraint crash here
    would be a worse answer than refusing the input.

    Checked against on_hand MINUS what is already reserved for active
    orders, not on_hand alone: on_hand=100 with reserved=80 means 20 is
    actually free to write off, not 100. Letting a correction spend stock a
    held reservation is already counting on would silently break the
    invariant commit_order relies on (reserved_base <= on_hand_base) and let
    an order's kitchen ticket commit against ingredients that no longer
    physically exist.
    """
    if reason not in ("wastage", "spoilage"):
        raise InvalidStateError("reason must be 'wastage' or 'spoilage'")

    with write_tx(conn):
        row = conn.execute(
            "SELECT name, base_unit, on_hand_base, reserved_base FROM ingredient WHERE id=?",
            (ingredient_pk,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"ingredient {ingredient_pk} not found")
        if qty_base <= 0:
            raise InvalidStateError("the amount wasted must be greater than zero")
        available = row["on_hand_base"] - row["reserved_base"]
        if qty_base > available + EPS:
            reserved_note = (
                f" ({row['reserved_base']:g} {row['base_unit']} is reserved for active orders)"
                if row["reserved_base"] > EPS else ""
            )
            raise InvalidStateError(
                f"cannot write off {qty_base:g} {row['base_unit']} — only "
                f"{max(available, 0):g} {row['base_unit']} is available{reserved_note}"
            )

        conn.execute(
            "UPDATE ingredient SET on_hand_base = MAX(0, on_hand_base - :qty), "
            "updated_at = datetime('now') WHERE id = :id",
            {"qty": qty_base, "id": ingredient_pk},
        )
        deplete_fefo(conn, ingredient_pk, qty_base)
        _ledger(conn, ingredient_pk, -qty_base, reason, "manual_writeoff", None,
               user_id, wastage_reason=note or reason, note=note)
        new_on_hand = conn.execute(
            "SELECT on_hand_base FROM ingredient WHERE id=?", (ingredient_pk,)
        ).fetchone()["on_hand_base"]
        audit.log(
            conn, "ingredient", ingredient_pk, reason, user_id,
            field="on_hand_base", previous_value=round(row["on_hand_base"], 4),
            new_value=round(new_on_hand, 4), note=note,
        )

    return {
        "ingredient_id": ingredient_pk,
        "name": row["name"],
        "base_unit": row["base_unit"],
        "wasted_base": round(qty_base, 4),
        "reason": reason,
        "on_hand_base": round(new_on_hand, 4),
    }


# 2026-08 audit: unli.py's three-stage model (provisional deduction, actual
# measurement, theoretical-vs-actual variance with a tolerance/review
# status) only ever ran for unlimited-refill streams. Regular recipe-driven
# ingredients had the SAME underlying comparison — record_count above
# already writes expected-vs-actual as a count_adjustment ledger entry,
# every time a physical count happens — but no periodic report pulled it
# together the way unli's does. This is that report: a read-only rollup
# over stock_ledger, not a new deduction or adjustment mechanism, and it
# writes nothing.
#
# A physical count's own delta (actual - expected at that instant) already
# IS the "measured vs. theoretical" gap: on_hand_base is maintained
# transaction-by-transaction from every sale, receipt and write-off in
# between, so the count's variance already nets out everything that
# happened since the last count. This report re-groups those same ledger
# rows by reason over a chosen window so a manager can see, per ingredient,
# how much of the period's stock movement was recipe/wastage consumption,
# how much was received, and how much of a net gap the physical counts in
# that window actually found — without re-deriving anything the ledger
# doesn't already record.
CONSUMPTION_REASONS = ("sale_commit", "wastage", "spoilage", "operational_use")
# A variance below this fraction of the period's own consumption is normal
# measurement noise (scoop-to-scoop variation, scale rounding) — flagging
# it would just be noise dressed up as a finding. Chosen to match the same
# order of magnitude as unli's own default tolerance band (unli.py), not
# derived from this dataset.
VARIANCE_FLAG_RATIO = 0.10

# Minimum independent physical-count dates before a repeated-direction
# variance is treated as a pattern worth surfacing rather than one unusual
# count or ordinary counting noise. Three counts landing on the same
# calendar date are one observation, not three -- see
# _repeated_variance_signals()'s per-date collapse below.
MIN_INDEPENDENT_COUNT_DATES = 3


def _repeated_variance_signals(conn: sqlite3.Connection, since: str) -> dict[int, dict]:
    """Per ingredient: has the same DIRECTION of count variance shown up on
    at least MIN_INDEPENDENT_COUNT_DATES distinct Manila business dates,
    each individually at or past the existing VARIANCE_FLAG_RATIO (the same
    10% figure ingredient_reconciliation_report's own per-window aggregate
    already uses -- reused here, not re-derived)?

    This is deliberately a SEPARATE pass from the aggregate SUM(...) query
    above: that query collapses every count_adjustment row in the window
    into one lump total, which is exactly right for "how much variance
    landed in this window" but cannot answer "did the same thing happen
    on three separate occasions" -- a single huge one-off count and three
    small-but-consistent counts can sum to the same total and must not
    read as the same kind of evidence.

    For any single count_adjustment row, record_count() (this module)
    posted delta_base = actual_base - expected (the on_hand figure at that
    exact moment) and _ledger() stamped balance_after_base = actual_base in
    the same transaction. So, per row, with no new column and no
    reconstruction of anything not already stored:
        physical  = balance_after_base
        expected  = balance_after_base - delta_base
        variance  = delta_base                    (signed: physical - expected)
    This is real per-event evidence ("a difference was observed between
    expected and physical stock at this count"), never "the kitchen used
    X extra" -- no cause is inferred here, only the fact and its direction.
    """
    rows = conn.execute(
        f"""
        SELECT l.id, l.ingredient_id, l.delta_base, l.balance_after_base, l.created_at,
               {timeutil.sql_business_date('l.created_at')} AS count_date
          FROM stock_ledger l
         WHERE l.reason = 'count_adjustment' AND l.created_at >= ?
         ORDER BY l.ingredient_id, l.created_at
        """,
        (since,),
    ).fetchall()

    by_ingredient: dict[int, dict[str, sqlite3.Row]] = {}
    for r in rows:
        # Later rows overwrite earlier ones for the same date (ORDER BY
        # created_at above) -- if an ingredient was counted twice on one
        # date, the later count is the physical reality that date actually
        # settled on; the two counts are not two independent observations.
        by_ingredient.setdefault(r["ingredient_id"], {})[r["count_date"]] = r

    out: dict[int, dict] = {}
    for ing_id, by_date in by_ingredient.items():
        observations = []
        for count_date in sorted(by_date):
            r = by_date[count_date]
            physical = r["balance_after_base"]
            delta = r["delta_base"]
            expected = physical - delta
            ratio_signed = (delta / expected) if expected > EPS else None
            observations.append({
                "date": count_date,
                "ledger_id": r["id"],
                "expected_base": round(expected, 4),
                "physical_base": round(physical, 4),
                "variance_base": round(delta, 4),
                "variance_ratio": round(ratio_signed, 4) if ratio_signed is not None else None,
            })

        if len(observations) < MIN_INDEPENDENT_COUNT_DATES:
            continue  # not enough independent evidence yet -- no signal either way

        # Only the most recent MIN_INDEPENDENT_COUNT_DATES independent dates
        # count as "recent physical checks" -- a pattern from a month ago
        # that two clean recent counts have since resolved should not keep
        # showing as an active signal today.
        recent = observations[-MIN_INDEPENDENT_COUNT_DATES:]
        qualifying_ratios = [o["variance_ratio"] for o in recent
                             if o["variance_ratio"] is not None
                             and abs(o["variance_ratio"]) >= VARIANCE_FLAG_RATIO]
        directions = {("below" if v < 0 else "above") for v in qualifying_ratios}

        # Active only when EVERY one of the most recent required dates
        # individually clears the threshold AND they all agree on
        # direction. One or two qualifying counts out of three is not yet
        # a pattern; a mix of "below" and "above" is evidence of noise or
        # measurement error, not a consistent drift -- see spec: no false
        # signal on mixed directions.
        active = len(qualifying_ratios) == MIN_INDEPENDENT_COUNT_DATES and len(directions) == 1

        # Signed mean, e.g. -0.18 for "18% consistently short" -- kept
        # signed (never abs()) so a caller can never accidentally print a
        # magnitude without knowing which way it points. Only meaningful
        # when every one of the recent observations had a valid ratio.
        recent_ratios = [o["variance_ratio"] for o in recent if o["variance_ratio"] is not None]
        avg_ratio = round(sum(recent_ratios) / len(recent_ratios), 4) if len(recent_ratios) == len(recent) else None

        out[ing_id] = {
            "active": active,
            "observation_count": len(recent),
            "dates": [o["date"] for o in recent],
            "observations": recent,
            "direction": (next(iter(directions)) if active else None),
            "average_variance_ratio": avg_ratio,
            "threshold_ratio": VARIANCE_FLAG_RATIO,
            "min_observations": MIN_INDEPENDENT_COUNT_DATES,
            # The most recent count's ledger row — real, permanent evidence a
            # variance review (see record_variance_review) can point at.
            # Exposed here only, never used in the active/direction math above.
            "evidence_ledger_id": recent[-1]["ledger_id"],
        }
    return out


# ---------------------------------------------------------------------------
# Variance review — a human's suspected operational cause for a physical-
# count discrepancy. Deliberately separate from verification.py's
# calibration workflow (pack_size/yield_factor/unit_cost/recipe_line.
# qty_base/unli_coefficient.lambda/fefo_batch.expires_at): those confirm or
# correct a MODEL INPUT the deduction math reads live; this records a
# HYPOTHESIS about why a count came out short or long. Recording one never
# touches on_hand_base, never touches stock_ledger, never edits a recipe or
# ingredient value, and never backdates a conclusion onto historical
# variance the system had no evidence for at the time. See migration 0033.
# ---------------------------------------------------------------------------
VARIANCE_REASONS = (
    "waste", "portion_variance", "counting_error",
    "recipe_yield_issue", "receiving_issue", "other",
)
VARIANCE_REASON_LABEL = {
    "waste":              "Unrecorded waste",
    "portion_variance":   "Preparation or portion variance",
    "counting_error":     "Counting error",
    "recipe_yield_issue": "Recipe or yield issue",
    "receiving_issue":    "Receiving or stock issue",
    "other":              "Other",
}


def _variance_review_view(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "reason": row["reason"],
        "reason_label": VARIANCE_REASON_LABEL[row["reason"]],
        "note": row["note"],
        "user_id": row["user_id"],
        "created_at": row["created_at"],
    }


def record_variance_review(
    conn: sqlite3.Connection,
    ingredient_pk: int,
    reason: str,
    note: str | None = None,
    user_id: str | None = None,
    client_request_id: str | None = None,
) -> dict:
    """Record a human's suspected cause for this ingredient's physical-count
    variance. This is an annotation of a signal, not a correction of it:
    nothing here writes to stock_ledger, ingredient, recipe_line, or any
    other operational column, and the original count_adjustment row(s) this
    review responds to are never touched — append-only, exactly like that
    row itself.

    A review requires real evidence to point at: this ingredient must
    already have at least one count_adjustment ledger row, i.e. a physical
    count actually happened. Reviewing an ingredient that has never been
    counted would have nothing real to annotate.

    Idempotent via client_request_id, same convention as purchase receipts
    and order creation: a retried submission of the same request replays the
    original row rather than creating a second one. A genuinely new
    conclusion (the reviewer changed their mind) is a new call with a new
    (or no) client_request_id, and appends a new row — the earlier review is
    never edited or deleted.
    """
    if reason not in VARIANCE_REASONS:
        raise InvalidStateError(f"unknown variance review reason '{reason}'")

    if client_request_id:
        existing = conn.execute(
            "SELECT * FROM ingredient_variance_review WHERE client_request_id=?",
            (client_request_id,),
        ).fetchone()
        if existing:
            return _variance_review_row(existing)

    with write_tx(conn):
        ing = conn.execute(
            "SELECT id, name FROM ingredient WHERE id=?", (ingredient_pk,)
        ).fetchone()
        if not ing:
            raise NotFoundError(f"ingredient {ingredient_pk} not found")

        evidence = conn.execute(
            "SELECT id FROM stock_ledger WHERE ingredient_id=? AND reason='count_adjustment' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (ingredient_pk,),
        ).fetchone()
        if not evidence:
            raise InvalidStateError(
                f"{ing['name']} has never had a physical count recorded — "
                "there is no variance evidence to review yet"
            )

        cur = conn.execute(
            "INSERT INTO ingredient_variance_review "
            "(ingredient_id, evidence_ledger_id, reason, note, user_id, client_request_id) "
            "VALUES (?,?,?,?,?,?)",
            (ingredient_pk, evidence["id"], reason, note, user_id, client_request_id),
        )
        row = conn.execute(
            "SELECT * FROM ingredient_variance_review WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return _variance_review_row(row)


def _variance_review_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "ingredient_id": row["ingredient_id"],
        "evidence_ledger_id": row["evidence_ledger_id"],
        "reason": row["reason"],
        "reason_label": VARIANCE_REASON_LABEL[row["reason"]],
        "note": row["note"],
        "user_id": row["user_id"],
        "created_at": row["created_at"],
    }


def variance_review_history(conn: sqlite3.Connection, ingredient_pk: int) -> list[dict]:
    """Every review ever recorded for this ingredient, oldest first — the
    same provenance-trail shape as verification.history_for()."""
    return [
        _variance_review_row(r) for r in conn.execute(
            "SELECT * FROM ingredient_variance_review WHERE ingredient_id=? ORDER BY id",
            (ingredient_pk,),
        ).fetchall()
    ]


def ingredient_reconciliation_report(conn: sqlite3.Connection, days: int = 30) -> dict:
    """Expected (recipe/wastage) consumption vs. what physical counts
    actually found, per ingredient, over the last `days` days. A flagged
    ingredient is a variance worth a look, never an accusation — same
    framing as anomaly.py's own descriptive-only rule (see its docstring):
    this only ever surfaces a fact and a ratio, it does not name a cause.
    """
    since = timeutil.utc_bound_n_manila_days_ago(days)
    rows = conn.execute(
        """
        SELECT i.id, i.name, i.base_unit,
               SUM(CASE WHEN l.reason IN ('sale_commit','wastage','spoilage','operational_use')
                        THEN -l.delta_base ELSE 0 END) AS consumed_base,
               SUM(CASE WHEN l.reason = 'purchase_receipt' THEN l.delta_base ELSE 0 END) AS received_base,
               SUM(CASE WHEN l.reason = 'void_credit' THEN l.delta_base ELSE 0 END) AS void_credit_base,
               SUM(CASE WHEN l.reason = 'count_adjustment' THEN l.delta_base ELSE 0 END) AS count_variance_base,
               SUM(CASE WHEN l.reason = 'count_adjustment' THEN 1 ELSE 0 END) AS count_events
          FROM ingredient i
          JOIN stock_ledger l ON l.ingredient_id = i.id
         WHERE l.created_at >= ? AND i.is_active = 1
         GROUP BY i.id, i.name, i.base_unit
        """,
        (since,),
    ).fetchall()

    # Repeated-direction signal, same window, same threshold -- see
    # _repeated_variance_signals()'s own docstring for why this needs a
    # separate per-count-date pass rather than reading off the aggregate
    # SUM(...) above. Advisory only: never changes flagged/variance_ratio/
    # count_variance_base above, never writes anything, never infers a
    # cause.
    signals = _repeated_variance_signals(conn, since)

    # Latest human review per ingredient, read-only — this never changes any
    # of the figures above and never decides `flagged` on its own behalf; it
    # only lets the caller show "Reviewed — <reason>" next to a signal that
    # a human has already looked at. See record_variance_review().
    latest_reviews: dict[int, sqlite3.Row] = {}
    for rev in conn.execute(
        """
        SELECT v.ingredient_id, v.reason, v.note, v.user_id, v.created_at
          FROM ingredient_variance_review v
          JOIN (SELECT ingredient_id, MAX(id) AS max_id
                  FROM ingredient_variance_review GROUP BY ingredient_id) m
            ON m.ingredient_id = v.ingredient_id AND m.max_id = v.id
        """
    ).fetchall():
        latest_reviews[rev["ingredient_id"]] = rev

    items = []
    for r in rows:
        consumed = round(r["consumed_base"] or 0.0, 4)
        variance = round(r["count_variance_base"] or 0.0, 4)
        count_events = r["count_events"] or 0
        has_count_data = count_events > 0
        variance_ratio = (
            round(abs(variance) / consumed, 4) if has_count_data and consumed > EPS else None
        )
        flagged = has_count_data and variance_ratio is not None and variance_ratio >= VARIANCE_FLAG_RATIO
        items.append({
            "ingredient_id": r["id"],
            "name": r["name"],
            "base_unit": r["base_unit"],
            "consumed_base": consumed,
            "received_base": round(r["received_base"] or 0.0, 4),
            "void_credit_base": round(r["void_credit_base"] or 0.0, 4),
            "count_variance_base": variance,
            "count_events": count_events,
            "has_count_data": has_count_data,
            "variance_ratio": variance_ratio,
            "flagged": flagged,
            "repeated_variance_signal": signals.get(r["id"]),
            "latest_variance_review": _variance_review_view(latest_reviews.get(r["id"])),
        })

    items.sort(key=lambda x: (not x["flagged"], -(x["variance_ratio"] or 0)))
    return {
        "days": days,
        "since": since,
        "flag_ratio": VARIANCE_FLAG_RATIO,
        "min_independent_count_dates": MIN_INDEPENDENT_COUNT_DATES,
        "items": items,
    }


def inventory_valuation(conn: sqlite3.Connection) -> dict:
    """Stock on hand valued only where unit_cost is a CONFIRMED figure
    (unit_cost_status='verified') -- most ingredients still carry an
    unverified estimate (see the owner-verification queue), and folding
    those into the same total as confirmed costs would present a
    partly-invented number as a real one.

    unit_cost is stored PER PURCHASE UNIT (see the column comment in
    migrations/0001_ingredient_master.sql), not per base unit, so it must be
    divided by pack_size * yield_factor before being multiplied against
    on_hand_base (which is always in base units) -- exactly the conversion
    resolver.recipe_cost() already applies for menu costing. Multiplying
    on_hand_base directly by unit_cost skips that conversion and overstates
    value by a factor of pack_size: e.g. 10,000 g of rice at PHP 1,000/25kg
    sack is PHP 400 (10,000 * (1000 / 25000)), not PHP 10,000,000. An
    ingredient priced but missing pack_size cannot be safely converted, so
    it is excluded from the total rather than valued as if pack_size were 1.

    priced_items/total_items lets a caller show exactly how much of the
    catalog this total actually covers, the same "no data is not zero"
    discipline the console applies everywhere else. Shared by
    GET /reports/valuation and the Today dashboard's stock-value KPI so both
    ever compute this one way."""
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN unit_cost_status='verified'
                              AND pack_size IS NOT NULL AND pack_size > 0
                             THEN on_hand_base * (unit_cost / (pack_size * yield_factor))
                             END), 0) AS total_value,
          SUM(CASE WHEN unit_cost_status='verified'
                     AND pack_size IS NOT NULL AND pack_size > 0 THEN 1 ELSE 0 END) AS priced_items,
          COUNT(*) AS total_items
          FROM ingredient WHERE is_active=1
        """
    ).fetchone()
    return {
        "total_value": round(row["total_value"], 2),
        "priced_items": row["priced_items"],
        "total_items": row["total_items"],
    }


def record_usage(
    conn: sqlite3.Connection,
    ingredient_pk: int,
    qty_base: float,
    note: str | None = None,
    user_id: str | None = None,
) -> dict:
    """Record consumption of an operational supply — tissue, cleaning
    alcohol, gloves, garbage bags. These never pass through the POS, so
    there is no sale to deduct against; a staff member states how many were
    used, the same way a kitchen states how much of an ingredient it wasted,
    just without implying anything went wrong.

    Deliberately its own ledger reason ('operational_use') rather than
    reusing 'wastage' — a used roll of tissue is not a loss to explain, and
    conflating the two would make the anomaly check (which watches wastage/
    spoilage for a *problem*) flag routine supply use as one.
    """
    with write_tx(conn):
        row = conn.execute(
            "SELECT name, base_unit, on_hand_base FROM ingredient WHERE id=?",
            (ingredient_pk,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"ingredient {ingredient_pk} not found")
        if qty_base <= 0:
            raise InvalidStateError("the amount used must be greater than zero")
        if qty_base > row["on_hand_base"] + EPS:
            raise InvalidStateError(
                f"cannot use {qty_base:g} {row['base_unit']} — only "
                f"{row['on_hand_base']:g} {row['base_unit']} is on hand"
            )

        conn.execute(
            "UPDATE ingredient SET on_hand_base = MAX(0, on_hand_base - :qty), "
            "updated_at = datetime('now') WHERE id = :id",
            {"qty": qty_base, "id": ingredient_pk},
        )
        deplete_fefo(conn, ingredient_pk, qty_base)
        _ledger(conn, ingredient_pk, -qty_base, "operational_use", "manual_usage", None,
               user_id, note=note)
        new_on_hand = conn.execute(
            "SELECT on_hand_base FROM ingredient WHERE id=?", (ingredient_pk,)
        ).fetchone()["on_hand_base"]
        audit.log(
            conn, "ingredient", ingredient_pk, "operational_use", user_id,
            field="on_hand_base", previous_value=round(row["on_hand_base"], 4),
            new_value=round(new_on_hand, 4), note=note,
        )

    return {
        "ingredient_id": ingredient_pk,
        "name": row["name"],
        "base_unit": row["base_unit"],
        "used_base": round(qty_base, 4),
        "on_hand_base": round(new_on_hand, 4),
    }


def availability_for_menu_item(conn: sqlite3.Connection, menu_item_pk: int, qty: int = 1) -> dict:
    """How many of this menu item current stock could still cover, and — for
    the specific case of "there just isn't enough of one ingredient" — the
    real numbers behind that, so a POS can tell a cashier something more
    useful than the word "Unavailable" (Phase 09A section 7).

    Read-only and advisory — the authoritative check is the guarded UPDATE in
    reserve_order. A POS that trusted this number alone would be back to
    read-then-write.

    Two different kinds of "blocked" are told apart on purpose:

      structurally blocked -- no recipe linked, an unmapped ingredient, an
      ingredient with no pack size configured. There is no meaningful
      Available/Required number for "the recipe doesn't fully exist yet";
      `shortfall` stays None and `block_reason` (already plain language,
      see v_menu_item_availability) is the whole story.

      blocked purely on physical stock -- the recipe is complete and every
      ingredient it needs is deductable; there just isn't enough of the one
      that runs out first. This is the case with real numbers to show, and
      the one this function used to short-circuit before ever computing.
    """
    row = conn.execute(
        "SELECT * FROM v_menu_item_availability WHERE menu_item_pk=?", (menu_item_pk,)
    ).fetchone()
    if not row:
        raise NotFoundError(f"menu item {menu_item_pk} not found")
    if not row["structurally_ready"]:
        return {
            "menu_item_id": row["menu_item_id"],
            "name": row["name"],
            "sellable": False,
            "block_reason": row["block_reason"],
            "max_qty": 0,
            "shortfall": None,
        }

    leaves = conn.execute(
        """
        SELECT l.ingredient_id, l.qty_base, i.name, i.base_unit,
               (i.on_hand_base - i.reserved_base) AS available
          FROM v_recipe_leaf l JOIN ingredient i ON i.id = l.ingredient_id
         WHERE l.recipe_id = ?
        """,
        (row["recipe_id"],),
    ).fetchall()

    max_qty = None
    limiting = None
    limiting_row = None
    for l in leaves:
        if l["qty_base"] <= 0:
            continue
        possible = int((l["available"] + EPS) // l["qty_base"])
        if max_qty is None or possible < max_qty:
            max_qty, limiting, limiting_row = possible, l["name"], l

    sellable = (max_qty or 0) >= qty
    shortfall = None
    if not sellable and limiting_row is not None:
        required = limiting_row["qty_base"] * qty
        shortfall = {
            "ingredient": limiting_row["name"],
            "available": round(limiting_row["available"], 3),
            "required": round(required, 3),
            "shortfall": round(max(required - limiting_row["available"], 0), 3),
            "unit": limiting_row["base_unit"],
        }

    return {
        "menu_item_id": row["menu_item_id"],
        "name": row["name"],
        "sellable": sellable,
        "block_reason": None if sellable else "insufficient stock",
        "max_qty": max_qty or 0,
        "limiting_ingredient": limiting,
        "shortfall": shortfall,
    }
