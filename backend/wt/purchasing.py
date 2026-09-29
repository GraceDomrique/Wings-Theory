"""Purchase orders (migration 0026): Draft -> Ordered -> Partially Received -> Received.

The owner reviews an existing replenishment recommendation
(depletion.ingredient_forecast, untouched by this module) and creates a
Draft PO; admin marks it Ordered once a supplier has actually been
contacted, then receives it line by line as stock physically arrives.

Creating or ordering a PO never touches ingredient/stock_ledger/fefo_batch —
only receive_line() does, and it does so by calling the existing
reservation._receive_stock_locked() (the same logic reservation.
receive_stock() has always used — see that module for why it was split into
a locked inner function and a transactional public wrapper). This module
never re-implements receiving, FEFO, or the ledger.
"""
from __future__ import annotations

import sqlite3

from . import audit
from .db import write_tx
from .errors import InvalidStateError, NotFoundError, PermissionError_, ValidationError
from .reservation import _receive_stock_locked
from .units import base_to_purchase, purchase_to_base

EPS = 1e-6
CANCELLABLE_FROM = ("draft", "ordered")


class _ReceiveRace(Exception):
    """Two receive requests collided on the same idempotency key. Raised
    inside the write transaction so it rolls back; converted to a replay
    outside it — same shape as refills._RefillRace."""


def _po_row(conn: sqlite3.Connection, po_pk: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM purchase_order WHERE id=?", (po_pk,)).fetchone()
    if not row:
        raise NotFoundError(f"purchase order {po_pk} not found")
    return row


def _item_row(conn: sqlite3.Connection, item_pk: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM purchase_order_item WHERE id=?", (item_pk,)).fetchone()
    if not row:
        raise NotFoundError(f"purchase order item {item_pk} not found")
    return row


def get(conn: sqlite3.Connection, po_pk: int) -> dict:
    po = dict(_po_row(conn, po_pk))
    po["items"] = [dict(r) for r in conn.execute(
        "SELECT poi.*, i.name AS ingredient_name, i.base_unit FROM purchase_order_item poi "
        "JOIN ingredient i ON i.id = poi.ingredient_id WHERE poi.purchase_order_id=? ORDER BY poi.id",
        (po_pk,),
    )]
    return po


def list_all(conn: sqlite3.Connection, status: str | None = None) -> dict:
    """Every purchase order, each with its line items attached — the admin
    Purchase Orders tab and Owner's My Orders both need the items to show
    what's on the order and (for admin) to receive against a line, not just
    the header row. One extra query for every matching PO's items, grouped
    in Python, rather than get() called once per row."""
    sql = "SELECT * FROM purchase_order"
    params: tuple = ()
    if status:
        sql += " WHERE status=?"
        params = (status,)
    sql += " ORDER BY created_at DESC"
    orders = [dict(r) for r in conn.execute(sql, params)]

    items_by_po: dict[int, list[dict]] = {po["id"]: [] for po in orders}
    if orders:
        placeholders = ",".join("?" * len(orders))
        rows = conn.execute(
            f"SELECT poi.*, i.name AS ingredient_name, i.base_unit FROM purchase_order_item poi "
            f"JOIN ingredient i ON i.id = poi.ingredient_id "
            f"WHERE poi.purchase_order_id IN ({placeholders}) ORDER BY poi.id",
            tuple(po["id"] for po in orders),
        ).fetchall()
        for r in rows:
            items_by_po[r["purchase_order_id"]].append(dict(r))
    for po in orders:
        po["items"] = items_by_po[po["id"]]

    return {"items": orders, "count": len(orders)}


def create(
    conn: sqlite3.Connection, supplier: str | None, notes: str | None,
    expected_delivery_date: str | None, items: list[dict], created_by: str | None,
) -> dict:
    """items: [{ingredient_id, quantity, recommended_qty_base?, note?}, ...].
    quantity is in the ingredient's own purchase unit — validated against a
    real ingredient row, never trusted blindly, but no inventory table is
    written here."""
    if not items:
        raise ValidationError("a purchase order needs at least one item")
    with write_tx(conn):
        cur = conn.execute(
            "INSERT INTO purchase_order (supplier, notes, expected_delivery_date, created_by) "
            "VALUES (?,?,?,?)",
            (supplier, notes, expected_delivery_date, created_by),
        )
        po_pk = cur.lastrowid
        for item in items:
            ing = conn.execute(
                "SELECT id, purchase_unit, pack_size, yield_factor FROM ingredient WHERE id=?",
                (item["ingredient_id"],),
            ).fetchone()
            if not ing:
                raise NotFoundError(f"ingredient {item['ingredient_id']} not found")
            qty = item.get("quantity")
            if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0:
                raise ValidationError("each line's quantity must be a number greater than zero")
            conn.execute(
                "INSERT INTO purchase_order_item "
                "(purchase_order_id, ingredient_id, recommended_qty_base, quantity, unit, "
                "pack_size, yield_factor, note) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (po_pk, ing["id"], item.get("recommended_qty_base"), qty,
                 ing["purchase_unit"], ing["pack_size"], ing["yield_factor"], item.get("note")),
            )
        audit.log(
            conn, "purchase_order", po_pk, "create", created_by,
            field="status", previous_value=None, new_value="draft",
            note=f"{len(items)} item(s)",
        )
        return get(conn, po_pk)


def mark_ordered(conn: sqlite3.Connection, po_pk: int, user_id: str | None) -> dict:
    with write_tx(conn):
        po = _po_row(conn, po_pk)
        if po["status"] != "draft":
            raise InvalidStateError(
                f"cannot mark ordered a purchase order in status '{po['status']}' (expected 'draft')"
            )
        conn.execute(
            "UPDATE purchase_order SET status='ordered', ordered_at=datetime('now','subsec'), "
            "updated_at=datetime('now','subsec') WHERE id=?", (po_pk,),
        )
        audit.log(
            conn, "purchase_order", po_pk, "order", user_id,
            field="status", previous_value="draft", new_value="ordered",
        )
        return get(conn, po_pk)


def cancel(conn: sqlite3.Connection, po_pk: int, reason: str | None, user_id: str | None, role: str) -> dict:
    """Admin may cancel a draft or ordered PO. Owner may cancel only their
    own, and only while it is still 'draft' — once admin has ordered it,
    retracting it is an admin decision."""
    with write_tx(conn):
        po = _po_row(conn, po_pk)
        if po["status"] not in CANCELLABLE_FROM:
            raise InvalidStateError(f"cannot cancel a purchase order in status '{po['status']}'")
        if role == "owner":
            if po["created_by"] != user_id:
                raise PermissionError_("owner may only cancel their own purchase order")
            if po["status"] != "draft":
                raise InvalidStateError(
                    "owner may only cancel a purchase order that is still 'draft' — "
                    "ask admin to cancel it once ordered"
                )
        previous_status = po["status"]
        conn.execute(
            "UPDATE purchase_order SET status='cancelled', cancelled_at=datetime('now','subsec'), "
            "cancelled_by=:user, cancel_reason=:reason, updated_at=datetime('now','subsec') WHERE id=:id",
            {"user": user_id, "reason": reason, "id": po_pk},
        )
        audit.log(
            conn, "purchase_order", po_pk, "cancel", user_id,
            field="status", previous_value=previous_status, new_value="cancelled", note=reason,
        )
        return get(conn, po_pk)


def _replay_receipt(conn: sqlite3.Connection, client_request_id: str | None) -> dict | None:
    if not client_request_id:
        return None
    row = conn.execute(
        "SELECT purchase_order_item_id FROM purchase_order_receipt WHERE client_request_id=?",
        (client_request_id,),
    ).fetchone()
    if not row:
        return None
    item = _item_row(conn, row["purchase_order_item_id"])
    out = get(conn, item["purchase_order_id"])
    out["duplicate"] = True
    return out


def _line_fully_received(item: sqlite3.Row) -> bool:
    """Uses the line's own pack_size/yield_factor snapshot (frozen at PO
    creation, migration 0028) rather than the ingredient's current values, so
    a later conversion correction can never change what an already-placed
    order is considered to have ordered."""
    if not item["pack_size"]:
        return False
    ordered_base = purchase_to_base(item["quantity"], item["pack_size"], item["yield_factor"])
    return item["received_qty_base"] >= ordered_base - EPS


def receive_line(
    conn: sqlite3.Connection, item_pk: int, qty_base: float, user_id: str | None,
    client_request_id: str | None, lot_no: str | None = None, expires_at: str | None = None,
) -> dict:
    """Receive physical stock against one PO line — the only place this
    module ever touches inventory. Runs reservation._receive_stock_locked()
    (unmodified receiving logic) inside THIS function's own transaction, so
    the ledger/FEFO write and the PO's received_qty_base/status update
    commit or roll back together; they can never disagree.

    Idempotent via client_request_id (same pattern as refills.record_refill
    and order creation): a retried request with the same key replays the
    original result instead of crediting stock twice. Over-receipt beyond
    the line's ordered quantity is refused outright — no allowance policy is
    implemented in this pass.
    """
    replayed = _replay_receipt(conn, client_request_id)
    if replayed:
        return replayed
    if not isinstance(qty_base, (int, float)) or isinstance(qty_base, bool) or qty_base <= 0:
        raise ValidationError("received quantity must be a number greater than zero")

    try:
        with write_tx(conn):
            item = _item_row(conn, item_pk)
            po = _po_row(conn, item["purchase_order_id"])
            if po["status"] not in ("ordered", "partially_received"):
                raise InvalidStateError(
                    f"cannot receive against a purchase order in status '{po['status']}' "
                    "(expected 'ordered' or 'partially_received')"
                )
            if not item["pack_size"]:
                raise InvalidStateError(
                    f"purchase order item {item_pk} has no recorded pack size — "
                    "this order predates conversion snapshots and cannot be received safely"
                )
            ordered_base = purchase_to_base(item["quantity"], item["pack_size"], item["yield_factor"])
            remaining_base = ordered_base - item["received_qty_base"]
            if qty_base > remaining_base + EPS:
                raise InvalidStateError(
                    f"cannot receive {qty_base:g} base units — only {max(remaining_base, 0):g} "
                    "remains on this line before it is fully received"
                )

            try:
                conn.execute(
                    "INSERT INTO purchase_order_receipt "
                    "(purchase_order_item_id, qty_base, client_request_id, created_by) "
                    "VALUES (?,?,?,?)",
                    (item_pk, qty_base, client_request_id, user_id),
                )
            except sqlite3.IntegrityError as e:
                raise _ReceiveRace(str(e))

            # Deliberately the ingredient's CURRENT pack_size/yield_factor, not
            # the line's frozen snapshot: this only round-trips qty_base into
            # the purchase-unit quantity _receive_stock_locked immediately
            # converts back to base units with the same live values, so the
            # base units actually credited always equal qty_base exactly,
            # regardless of any conversion drift since the PO was placed.
            ing = conn.execute(
                "SELECT pack_size, yield_factor FROM ingredient WHERE id=?",
                (item["ingredient_id"],),
            ).fetchone()
            purchase_qty = base_to_purchase(qty_base, ing["pack_size"], ing["yield_factor"])
            _receive_stock_locked(conn, item["ingredient_id"], purchase_qty, lot_no, expires_at, user_id)

            previous_received = item["received_qty_base"]
            new_received = previous_received + qty_base
            conn.execute(
                "UPDATE purchase_order_item SET received_qty_base=? WHERE id=?",
                (new_received, item_pk),
            )

            all_items = conn.execute(
                "SELECT id, ingredient_id, quantity, received_qty_base, pack_size, yield_factor "
                "FROM purchase_order_item WHERE purchase_order_id=?", (po["id"],),
            ).fetchall()
            fully_received = all(_line_fully_received(r) for r in all_items)
            po_status = "received" if fully_received else "partially_received"
            conn.execute(
                "UPDATE purchase_order SET status=?, updated_at=datetime('now','subsec') WHERE id=?",
                (po_status, po["id"]),
            )
            audit.log(
                conn, "purchase_order", po["id"], "receive", user_id,
                field="received_qty_base", previous_value=round(previous_received, 4),
                new_value=round(new_received, 4), note=f"item {item_pk}: +{qty_base:g} base units",
            )
            result = get(conn, po["id"])
    except _ReceiveRace:
        replayed = _replay_receipt(conn, client_request_id)
        if replayed:
            return replayed
        raise InvalidStateError("that receipt is already being recorded — refresh and check")
    return result
