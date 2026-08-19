"""HTTP surface.

Run:  uvicorn wt.api:app --host 0.0.0.0 --port 8000

Route groups map to the three clients: /pos/* is what a cashier tablet may
reach, /kds/* the kitchen, /console/* the owner. Enforcement is per-capability
in auth.CAPABILITIES rather than per-prefix, so a mis-filed route still cannot
leak a capability the role does not hold.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path as _Path
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db, refills, timeutil, unli, verification
from .anomaly import detect_anomalies, detect_consumption_anomalies
from .anomaly import consumption_model_details
from .anomaly import model_details as anomaly_model_details
from .associations import product_combinations
from .associations import model_details as association_model_details
from .errors import AuthenticationError, NotFoundError, ValidationError, WTError
from .forecasting import (
    evaluate_demand_model, ingredient_depletion, menu_item_demand, menu_item_demand_prophet,
)
from .reservation import (
    availability_for_menu_item, commit_order, receive_stock, record_count, record_usage,
    record_wastage, release_order, reserve_order, void_after_commit,
)
from .resolver import expand_recipe, recipe_cost, validate_recipe
from .units import format_base, normalise_label, purchase_to_base, to_base

app = FastAPI(title="Wings Theory", version="0.2.0",
              description="Inventory & POS backend — Phase 2 (data model & deduction)")
# Phase 09A section 9 audit: this app serves its own frontend (see the
# FileResponse routes for /pos, /kds, /console near the bottom of this file)
# on the exact same host:port every API call goes to, so the real POS/KDS/
# console clients never make a cross-origin request in the first place --
# CORS is not what stands between them and the API. allow_origins=["*"] is
# left as-is rather than narrowed to a guessed value: start_server.ps1's own
# comments explain why there is no one fixed production origin to restrict
# to yet (the LAN IP a tablet reaches this on changes with the network, e.g.
# a phone hotspot reassigning it) -- inventing one here would be exactly the
# "blind change" this audit was told not to make. See
# docs/GO_LIVE_CHECKLIST.md for this as an open deployment-configuration
# item: if this ever moves behind a fixed domain or reverse proxy,
# allow_origins should be narrowed to it then, not guessed now.
#
# allow_methods/allow_headers ARE narrowed here, to what the API actually
# uses (GET/POST/PATCH; Authorization + Content-Type) -- unlike the origin,
# this is not a guess, it's what grepping every @app.get/post/patch/put/
# delete in this file actually shows, and narrowing it costs nothing any
# real caller currently uses.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.on_event("startup")
def _startup() -> None:
    conn = db.get_conn()
    db.migrate(conn)
    # No-op unless WT_ENV=production is explicitly set — see
    # db.validate_production_startup for why that's the gate.
    db.validate_production_startup(conn)


@app.exception_handler(WTError)
def _wt_error(_request, exc: WTError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=exc.http_status, content=exc.as_dict())


def conn():
    """A connection scoped to this one HTTP request, not to a thread.

    FastAPI resolves a sync dependency and the sync endpoint body as two
    separate threadpool dispatches — they are not guaranteed to land on the
    same worker thread. db.get_conn()'s one-connection-per-thread cache
    assumes creation-thread == usage-thread; that assumption held under
    light, sequential traffic but broke as soon as the console started
    firing genuinely parallel requests (e.g. the dashboard's Promise.all),
    surfacing as "SQLite objects created in a thread can only be used in
    that same thread" 500s. Opening a connection here, scoped to this one
    request and closed when it ends, sidesteps the mismatch: check_same_
    thread=False lets it safely cross the dependency/endpoint thread
    boundary, and it's SQLite's own BEGIN IMMEDIATE + busy_timeout — not
    Python-level thread affinity — that actually serializes concurrent
    writers (see db.py's module docstring)."""
    c = db.connect(check_same_thread=False)
    try:
        yield c
    finally:
        c.close()


@app.get("/health", tags=["ops"], include_in_schema=False)
def health(c: sqlite3.Connection = Depends(conn)):
    """Unauthenticated on purpose — this is what a launcher script or an
    external monitor checks before assuming the server is actually usable,
    not a console page, so it can't require a login token. Reports nothing
    that isn't already visible to anyone on the LAN who can reach the
    server at all."""
    return db.integrity_report(c)


def principal(
    authorization: str = Header(default=""), c: sqlite3.Connection = Depends(conn),
) -> dict:
    """Identity check. Beyond the signature/expiry auth.decode_token already
    verifies, this also confirms the account is still active and that the
    token was issued after the account's current password_changed_at --
    see auth.issue_token's 'pwd' claim. Without the second check, changing
    or resetting a password (P0-2) would leave every token already issued
    for that account valid until its own 12-hour expiry, which defeats the
    point of an emergency reset. Both failure modes are 401, same as every
    other identity failure here: the client still just needs to sign in
    again, not be told it lacks permission."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    payload = auth.decode_token(authorization.split(" ", 1)[1])
    row = c.execute(
        "SELECT password_changed_at, is_active FROM app_user WHERE username=?",
        (payload.get("sub"),),
    ).fetchone()
    if not row or not row["is_active"]:
        raise AuthenticationError("account no longer active — please sign in again")
    if payload.get("pwd") != row["password_changed_at"]:
        raise AuthenticationError("session invalidated by a password change — please sign in again")
    return payload


def needs(capability: str):
    def _dep(p: dict = Depends(principal)) -> dict:
        auth.require(p, capability)
        return p
    return _dep


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ===========================================================================
# auth
# ===========================================================================
class Login(BaseModel):
    username: str
    password: str


@app.post("/auth/login", tags=["auth"])
def login(body: Login, c: sqlite3.Connection = Depends(conn)):
    row = c.execute(
        "SELECT * FROM app_user WHERE username=? AND is_active=1", (body.username,)
    ).fetchone()
    if not row or not auth.verify_password(body.password, row["password_hash"], row["password_salt"]):
        raise HTTPException(401, "invalid credentials")
    return {
        "token": auth.issue_token(row["username"], row["role"], row["password_changed_at"]),
        "role": row["role"],
        "display_name": row["display_name"],
        # Data only -- nothing blocks this login or any later request on it.
        # Lets a future console/POS screen prompt a change; today it's what
        # the go-live checklist queries to see which seeded accounts still
        # carry the shared placeholder password.
        "must_change_password": bool(row["must_change_password"]),
    }


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str
    confirm_new_password: str


@app.post("/auth/change_password", tags=["auth"])
def change_password(
    body: ChangePasswordIn, c: sqlite3.Connection = Depends(conn), p: dict = Depends(principal),
):
    """Self-service password change. Always requires the caller's own
    current password -- a valid bearer token alone is not enough to change
    what unlocks the account -- rejects a mismatched confirmation and
    anything auth.validate_new_password refuses, and immediately
    invalidates every token issued before this change by advancing
    password_changed_at (see auth.issue_token's 'pwd' claim and
    api.principal's freshness check). Returns a fresh token for the new
    password so the caller isn't logged out by their own change."""
    row = c.execute(
        "SELECT * FROM app_user WHERE username=? AND is_active=1", (p["sub"],)
    ).fetchone()
    if not row:
        raise AuthenticationError("account no longer active — please sign in again")
    if not auth.verify_password(body.current_password, row["password_hash"], row["password_salt"]):
        raise AuthenticationError("current password is incorrect")
    if body.new_password != body.confirm_new_password:
        raise ValidationError("new password and confirmation do not match")
    auth.validate_new_password(body.new_password, username=row["username"])

    h, s = auth.hash_password(body.new_password)
    with db.write_tx(c):
        c.execute(
            "UPDATE app_user SET password_hash=?, password_salt=?, "
            "password_changed_at=datetime('now','subsec'), must_change_password=0 WHERE id=?",
            (h, s, row["id"]),
        )
    new_changed_at = c.execute(
        "SELECT password_changed_at FROM app_user WHERE id=?", (row["id"],)
    ).fetchone()["password_changed_at"]
    return {"ok": True, "token": auth.issue_token(row["username"], row["role"], new_changed_at)}


@app.post("/console/users/{username}/reset_password", tags=["console"])
def reset_password(
    username: str, c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("user.manage")),
):
    """Admin-only emergency reset (auth.CAPABILITIES already scopes
    user.manage to admin). Generates a random password server-side --
    the admin never types, and therefore never has to invent or reuse, the
    credential this hands back -- and returns it exactly once, in this
    response body only. It is never logged and never written anywhere
    else (not even the audit trail this project keeps for inventory
    verification, which is a different domain): if this response is lost,
    the only recovery is to reset again. Flags must_change_password=1 so
    the go-live checklist can tell a still-temporary password apart from
    one the account holder actually chose."""
    row = c.execute("SELECT * FROM app_user WHERE username=?", (username,)).fetchone()
    if not row:
        raise NotFoundError(f"user '{username}' not found")
    temp_password = auth.generate_temp_password()
    h, s = auth.hash_password(temp_password)
    with db.write_tx(c):
        c.execute(
            "UPDATE app_user SET password_hash=?, password_salt=?, "
            "password_changed_at=datetime('now','subsec'), must_change_password=1 WHERE id=?",
            (h, s, row["id"]),
        )
    return {
        "ok": True,
        "username": username,
        "temporary_password": temp_password,
        "note": "Shown once. Give this to the user directly — it is not stored or logged anywhere.",
    }


# ===========================================================================
# POS — menu with the completeness gate applied
# ===========================================================================
@app.get("/pos/menu", tags=["pos"])
def pos_menu(
    include_blocked: bool = Query(True, description="return blocked items too, flagged"),
    c: sqlite3.Connection = Depends(conn),
    _p: dict = Depends(needs("menu.read")),
):
    """The POS menu. Items failing the gate come back with sellable=false and a
    reason so the tablet can grey them out — never silently omitted, or the
    cashier just thinks the menu is broken.

    flavor_slots counts recipe_line rows whose label marks it as a
    customer-choice flavor sauce (see mapping.FLAVOUR_PLACEHOLDER) — read
    straight from the recipe, not a client-side guess, so the POS only ever
    prompts for a flavor on the items that actually have one to pick."""
    data = rows(c.execute(
        """
        SELECT a.*, m.is_unli, m.pos_visible,
               (SELECT COUNT(*) FROM recipe_line rl
                 WHERE rl.recipe_id = m.recipe_id AND rl.display_label LIKE '%flavor%')
                 AS flavor_slots
          FROM v_menu_item_availability a
          JOIN menu_item m ON m.id = a.menu_item_pk
         WHERE m.pos_visible = 1
         ORDER BY a.category, a.name
        """
    ))
    if not include_blocked:
        data = [d for d in data if d["sellable"]]
    return {
        "items": data,
        "sellable": sum(1 for d in data if d["sellable"]),
        "blocked": sum(1 for d in data if not d["sellable"]),
    }


@app.get("/pos/menu/{menu_item_pk}/availability", tags=["pos"])
def pos_availability(
    menu_item_pk: int, qty: int = 1,
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("menu.read")),
):
    return availability_for_menu_item(c, menu_item_pk, qty)


@app.get("/pos/flavors", tags=["pos"])
def pos_flavors(c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("menu.read"))):
    """The real, selectable wing-flavor sauces — house-made components that
    aren't a flavor choice (Cheese Sauce, Simple Syrup, Tartar Sauce — prep
    for other dishes, not something a customer picks for wings) are named
    with a "(house)" suffix precisely so they can be told apart from an
    actual flavor here without guessing. Gated by menu.read, same as the
    menu itself, so a cashier can load this without inventory access."""
    names = rows(c.execute(
        "SELECT name FROM ingredient WHERE kind='sub_recipe' AND is_active=1 "
        "AND name NOT LIKE '%(house)%' ORDER BY name"
    ))
    return {"flavors": [n["name"] for n in names]}


# ===========================================================================
# Orders — two-phase reservation
# ===========================================================================
class OrderLineIn(BaseModel):
    menu_item_pk: int
    qty: int = Field(gt=0)
    note: str | None = None


class OrderIn(BaseModel):
    table_label: str | None = None
    # A negative head count reached order_requirements as a negative multiplier
    # and died on reservation's `qty_base > 0` CHECK as an unhandled 500. It is
    # not a state the floor can be in, so it is refused as input.
    head_count: int = Field(default=0, ge=0)
    lines: list[OrderLineIn]
    cash_received: float | None = Field(default=None, ge=0)
    # One key per checkout attempt, chosen by the till. Replaying it returns
    # the order already created rather than charging the customer twice.
    client_request_id: str | None = Field(default=None, max_length=64)


# Matches the POS cart's own display formula — the cart has always shown this
# 10% line, so charging anything else at the till would contradict what the
# customer was just shown.
SERVICE_CHARGE_RATE = 0.10


class _DuplicateCheckout(Exception):
    """Internal marker: this checkout key already has an order. Raised inside
    the write transaction so it rolls back, then handled outside it."""


def _find_order_by_key(c: sqlite3.Connection, key: str | None):
    if not key:
        return None
    return c.execute(
        "SELECT id, order_no, subtotal, service_charge, total, cash_received, change_due "
        "FROM customer_order WHERE client_request_id = ?",
        (key,),
    ).fetchone()


def _replay_payload(row) -> dict:
    """What a repeated checkout gets back: the original sale, flagged as a
    replay so the till can say so instead of implying a second order."""
    return {
        "order_id": row["id"], "order_no": row["order_no"],
        "subtotal": row["subtotal"], "service_charge": row["service_charge"],
        "total": row["total"], "cash_received": row["cash_received"],
        "change_due": row["change_due"],
        "state": "reserved", "duplicate": True,
    }


@app.post("/pos/orders", tags=["pos"], status_code=201)
def create_order(
    body: OrderIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("order.create")),
):
    """Punch an order and take the soft allocation in one step.

    Creation and reservation are deliberately not separate endpoints: an order
    that exists without holds is exactly the oversell window this phase closes.

    cash_received is optional — an order can still be punched with payment
    settled separately — but when given it is validated against the real,
    server-computed total, never trusted from the client as-is.

    client_request_id makes this endpoint idempotent. A retried or
    double-tapped checkout replays the same key and gets the original order
    back untouched, instead of ringing the customer up a second time.
    """
    # Fast path: this key already produced an order, so replay it untouched.
    if body.client_request_id:
        existing = _find_order_by_key(c, body.client_request_id)
        if existing:
            return _replay_payload(existing)

    try:
        with db.write_tx(c):
            n = c.execute("SELECT COUNT(*) AS n FROM customer_order").fetchone()["n"]
            order_no = f"WT-{n + 1001}"
            try:
                cur = c.execute(
                    "INSERT INTO customer_order (order_no, table_label, head_count, created_by, "
                    "client_request_id) VALUES (?,?,?,?,?)",
                    (order_no, body.table_label, body.head_count, p["sub"], body.client_request_id),
                )
            except sqlite3.IntegrityError:
                # Two genuinely simultaneous requests carrying the same key.
                # Roll back and let the handler outside return the winner.
                raise _DuplicateCheckout()
            order_pk = cur.lastrowid
            subtotal = 0.0
            for ln in body.lines:
                item = c.execute(
                    "SELECT id, price, name, is_unli FROM menu_item WHERE id=?",
                    (ln.menu_item_pk,)
                ).fetchone()
                if not item:
                    raise HTTPException(404, f"menu item {ln.menu_item_pk} not found")
                # An unlimited line is sized by head_count, never by line qty:
                # order_requirements multiplies lambda by heads AND by the unli
                # line quantity, so a cart holding qty=2 alongside heads=2
                # reserved four covers' worth of wings for a table of two and
                # fed that same inflated figure to the reconciliation as its
                # head count. The two controls are not interchangeable, so the
                # ambiguous punch is refused rather than silently resolved one
                # way or the other.
                if item["is_unli"] and ln.qty != 1:
                    raise HTTPException(
                        422,
                        f"'{item['name']}' is sized by head count, not line quantity — "
                        f"add it once and set Heads to {ln.qty * max(body.head_count, 1)} "
                        f"if that is the number of covers",
                    )
                gate = c.execute(
                    "SELECT sellable, block_reason, name FROM v_menu_item_availability "
                    "WHERE menu_item_pk=?",
                    (ln.menu_item_pk,),
                ).fetchone()
                if not gate["sellable"]:
                    raise HTTPException(
                        409, f"'{gate['name']}' is not sellable: {gate['block_reason']}"
                    )
                c.execute(
                    "INSERT INTO order_line (order_id, menu_item_id, qty, unit_price, note) "
                    "VALUES (?,?,?,?,?)",
                    (order_pk, ln.menu_item_pk, ln.qty, item["price"], ln.note),
                )
                subtotal += item["price"] * ln.qty

            service_charge = round(subtotal * SERVICE_CHARGE_RATE, 2)
            total = round(subtotal + service_charge, 2)
            change_due = None
            if body.cash_received is not None:
                if body.cash_received < total:
                    raise HTTPException(
                        422,
                        f"cash received (₱{body.cash_received:.2f}) is less than "
                        f"the total (₱{total:.2f})",
                    )
                change_due = round(body.cash_received - total, 2)
            c.execute(
                "UPDATE customer_order SET subtotal=?, service_charge=?, total=?, "
                "cash_received=?, change_due=? WHERE id=?",
                (round(subtotal, 2), service_charge, total, body.cash_received, change_due, order_pk),
            )
            # This is the last write this order's money columns will ever
            # accept — migration 0011's trigger refuses every later one. An
            # unlimited ticket's rounds therefore need somewhere else to live,
            # so the session opens here, inside the same transaction: an
            # unlimited order that exists without one is a ticket the floor
            # cannot serve.
            if body.head_count > 0 and c.execute(
                "SELECT 1 FROM order_line ol JOIN menu_item m ON m.id = ol.menu_item_id "
                "WHERE ol.order_id = ? AND m.is_unli = 1", (order_pk,)
            ).fetchone():
                c.execute(
                    "INSERT INTO unli_session (order_id, head_count, created_by) VALUES (?,?,?)",
                    (order_pk, body.head_count, p["sub"]),
                )

    except _DuplicateCheckout:
        # Lost a genuine race: the winner committed microseconds earlier.
        # That customer's order exists and is paid for, so return it rather
        # than reporting a failure for a sale that actually succeeded.
        existing = _find_order_by_key(c, body.client_request_id)
        if existing:
            return _replay_payload(existing)
        raise HTTPException(409, "this order is already being processed")

    try:
        result = reserve_order(c, order_pk, user_id=p["sub"])
    except Exception:
        # The order committed above; the allocation that was supposed to be
        # inseparable from it did not. "Creation and reservation are
        # deliberately not separate endpoints" is only true if a failed
        # reservation takes the order with it — otherwise the row survives as a
        # `punched` ticket carrying a total and a cash figure, with no holds
        # behind it. The kitchen cannot start it ("no held reservations to
        # commit"), the till lists it as open forever, and sales_summary counts
        # every non-void order, so it books revenue for a sale that never
        # happened.
        #
        # reserve_order is all-or-nothing inside its own transaction and writes
        # nothing to the ledger, so nothing survives it to reference this order:
        # order_line and unli_session cascade, and no order_charge can exist
        # yet. Deleting also frees client_request_id so the till's retry is a
        # genuine retry rather than a permanent duplicate.
        #
        # The ordinary out-of-stock punch never reaches here — the
        # v_menu_item_availability gate above rejects it inside the write
        # transaction. This is the path for what that gate structurally cannot
        # see: an unlimited line, whose H x lambda requirement belongs to no
        # recipe.
        try:
            with db.write_tx(c):
                c.execute("DELETE FROM customer_order WHERE id=?", (order_pk,))
        except Exception:
            pass          # report the original failure, not the cleanup's
        raise
    return {
        "order_id": order_pk, "order_no": order_no,
        "subtotal": round(subtotal, 2), "service_charge": service_charge, "total": total,
        "cash_received": body.cash_received, "change_due": change_due,
        **result,
    }


@app.post("/kds/orders/{order_pk}/start", tags=["kds"])
def start_order(
    order_pk: int, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("order.status")),
):
    """Kitchen begins preparation — holds convert into ledger deductions."""
    return commit_order(c, order_pk, user_id=p["sub"])


def _advance_status(c: sqlite3.Connection, order_pk: int, frm: str, to: str) -> dict:
    """Plain status advance for the two stages after deduction has already
    happened (in_progress -> ready -> served). No inventory impact — the
    ledger entry was written when the order moved to in_progress; these two
    steps are purely "where is this ticket in the kitchen", so they don't go
    through reservation.py at all."""
    row = c.execute("SELECT status, order_no FROM customer_order WHERE id=?",
                    (order_pk,)).fetchone()
    if not row:
        raise HTTPException(404, "order not found")
    if row["status"] != frm:
        raise HTTPException(409, f"cannot mark '{to}': order is '{row['status']}', not '{frm}'")
    with db.write_tx(c):
        if to == "served":
            c.execute("UPDATE customer_order SET status=?, served_at=datetime('now') WHERE id=?",
                     (to, order_pk))
        else:
            c.execute("UPDATE customer_order SET status=? WHERE id=?", (to, order_pk))
    return {"order_id": order_pk, "order_no": row["order_no"], "state": to}


@app.post("/kds/orders/{order_pk}/ready", tags=["kds"])
def ready_order(
    order_pk: int, c: sqlite3.Connection = Depends(conn),
    _p: dict = Depends(needs("order.status")),
):
    """Kitchen has plated the order."""
    return _advance_status(c, order_pk, "in_progress", "ready")


@app.post("/kds/orders/{order_pk}/served", tags=["kds"])
def served_order(
    order_pk: int, c: sqlite3.Connection = Depends(conn),
    _p: dict = Depends(needs("order.status")),
):
    """Order has left the pass."""
    return _advance_status(c, order_pk, "ready", "served")


class VoidIn(BaseModel):
    reason: str = "voided"
    restock: bool = False


@app.post("/pos/orders/{order_pk}/void", tags=["pos"])
def void_order(
    order_pk: int, body: VoidIn = Body(default=VoidIn()),
    c: sqlite3.Connection = Depends(conn), p: dict = Depends(needs("order.void")),
):
    """Routes by order state: before commit releases holds and writes nothing;
    after commit posts a compensating pair."""
    row = c.execute("SELECT status FROM customer_order WHERE id=?", (order_pk,)).fetchone()
    if not row:
        raise HTTPException(404, "order not found")
    if row["status"] == "punched":
        result = release_order(c, order_pk, body.reason, user_id=p["sub"])
    else:
        result = void_after_commit(c, order_pk, body.reason, body.restock, user_id=p["sub"])

    # The ticket is cancelled, so the table's session goes with it. Left active
    # it stayed in the till's refill list and kept accepting rounds against a
    # void order. set_session_status releases any round still held and unserved,
    # so this cannot strand stock. Done after the void, not inside it, because
    # the void is the financial act and must stand on its own.
    session = c.execute(
        "SELECT id, status FROM unli_session WHERE order_id=?", (order_pk,)).fetchone()
    if session and session["status"] == "active":
        refills.set_session_status(
            c, session["id"], "cancelled",
            note=f"order voided: {body.reason}", user_id=p["sub"])
        result["session_cancelled"] = session["id"]
    return result


@app.get("/pos/orders", tags=["pos"])
def list_orders(
    status: Literal["open", "all"] = "open",
    limit: int = 100,
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("order.read")),
):
    """Backs the till's Current Orders (status=open) and Order History
    (status=all) screens. Same table, same rows a receipt was built from at
    punch time — this is a read of that record, not a second calculation."""
    sql = "SELECT id, order_no, status, table_label, head_count, created_at, " \
          "committed_at, served_at, voided_at, subtotal, service_charge, total, " \
          "cash_received, change_due FROM customer_order o "
    if status == "open":
        sql += "WHERE o.status IN ('punched','in_progress','ready') "
    sql += "ORDER BY o.created_at DESC LIMIT ?"
    orders = rows(c.execute(sql, (limit,)))
    for o in orders:
        o["line_count"] = c.execute(
            "SELECT COUNT(*) AS n FROM order_line WHERE order_id=?", (o["id"],)
        ).fetchone()["n"]
    return {"orders": orders}


@app.get("/pos/orders/{order_pk}", tags=["pos"])
def order_detail(
    order_pk: int, c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("order.read")),
):
    """Full detail for one order — reopening a receipt from Order History,
    or checking what's actually in a still-open ticket before voiding it."""
    order = c.execute(
        "SELECT id, order_no, status, table_label, head_count, created_by, created_at, "
        "committed_at, served_at, voided_at, void_reason, subtotal, service_charge, total, "
        "cash_received, change_due FROM customer_order WHERE id=?",
        (order_pk,),
    ).fetchone()
    if not order:
        raise HTTPException(404, "order not found")
    lines = rows(c.execute(
        """
        SELECT ol.qty, ol.unit_price, ol.note, m.name
          FROM order_line ol JOIN menu_item m ON m.id = ol.menu_item_id
         WHERE ol.order_id = ?
         ORDER BY ol.id
        """,
        (order_pk,),
    ))
    return {"order": dict(order), "lines": lines}


@app.get("/kds/queue", tags=["kds"])
def kds_queue(c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("order.status"))):
    """Active tickets with their line items — a kitchen screen showing order
    counts with no idea what's actually in each one isn't usable.

    Served orders stay visible for 30 minutes after completion (not forever —
    this is a live board, not an order history) so a "wait, did I already
    serve that?" moment doesn't require leaving the screen."""
    orders = rows(c.execute(
        """
        SELECT o.id, o.order_no, o.status, o.table_label, o.head_count, o.created_at
          FROM customer_order o
         WHERE o.status IN ('punched','in_progress','ready')
            OR (o.status = 'served' AND o.served_at >= datetime('now', '-30 minutes'))
         ORDER BY o.created_at
        """
    ))
    for o in orders:
        o["lines"] = rows(c.execute(
            """
            SELECT ol.qty, ol.note, m.name, m.is_unli
              FROM order_line ol JOIN menu_item m ON m.id = ol.menu_item_id
             WHERE ol.order_id = ?
             ORDER BY ol.id
            """,
            (o["id"],),
        ))

    # Rounds waiting on the pass, as first-class tickets rather than something
    # the kitchen has to infer from an order note. A refill is a real unit of
    # preparation — 6 pieces, one named flavour — and it belongs on the board
    # next to the orders, not buried inside one.
    refill_tickets = rows(c.execute(
        """
        SELECT r.id, r.refill_no, r.pieces, r.head_count, r.flavor_name, r.flavor_type,
               r.addon_amount, r.status, r.created_at, r.note,
               s.id AS session_id, o.id AS order_id, o.order_no, o.table_label
          FROM unli_refill r
          JOIN unli_session s ON s.id = r.session_id
          JOIN customer_order o ON o.id = s.order_id
         WHERE r.status = 'pending'
            OR (r.status = 'served' AND r.served_at >= datetime('now', '-30 minutes'))
         ORDER BY r.created_at
        """
    ))
    return {"orders": orders, "refills": refill_tickets}


# ===========================================================================
# Console — ingredient CRUD
# ===========================================================================
class IngredientIn(BaseModel):
    name: str
    category: Literal["wet_food", "dry_food", "packaging", "consumable", "resale"]
    base_unit: Literal["g", "ml", "pc"]
    purchase_unit: str
    kind: Literal["raw", "sub_recipe"] = "raw"
    sub_recipe_mode: Literal["exploded", "stocked"] | None = None
    pack_size: float | None = None
    yield_factor: float = 1.0
    unit_cost: float | None = None
    supplier: str | None = None
    supplier_contact: str | None = None
    storage_location: str | None = None
    lead_time_days: int = 3
    inventory_type: Literal["food_ingredient", "sellable_product", "operational_supply"] = "food_ingredient"
    expiry_mode: Literal["fixed_date", "shelf_life_estimate", "none"] = "fixed_date"
    shelf_life_days: int | None = None
    max_stock_base: float | None = None
    reorder_point_base: float | None = None


@app.get("/console/ingredients", tags=["console"])
def list_ingredients(
    q: str = "", only_blocked: bool = False, limit: int = 200,
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    sql = """
        SELECT i.*, r.is_deductable, r.block_reason, r.unverified_count,
               (i.on_hand_base - i.reserved_base) AS available_base
          FROM ingredient i JOIN v_ingredient_readiness r ON r.id = i.id
         WHERE i.is_active = 1 AND i.name LIKE ?
    """
    params: list[Any] = [f"%{q}%"]
    if only_blocked:
        sql += " AND r.is_deductable = 0"
    sql += " ORDER BY i.name LIMIT ?"
    params.append(limit)
    data = rows(c.execute(sql, params))
    for d in data:
        d["on_hand_display"] = format_base(d["on_hand_base"], d["base_unit"], d["pack_size"],
                                           d["purchase_unit"], d["yield_factor"])
    return {"ingredients": data, "count": len(data)}


@app.post("/console/ingredients", tags=["console"], status_code=201)
def create_ingredient(
    body: IngredientIn, c: sqlite3.Connection = Depends(conn),
    _p: dict = Depends(needs("inventory.write")),
):
    if body.kind == "sub_recipe" and body.sub_recipe_mode is None:
        raise HTTPException(422, "a sub-recipe ingredient must declare sub_recipe_mode")
    if body.kind == "raw" and body.sub_recipe_mode is not None:
        raise HTTPException(422, "sub_recipe_mode is only valid on a sub-recipe ingredient")
    with db.write_tx(c):
        n = c.execute("SELECT COUNT(*) AS n FROM ingredient").fetchone()["n"]
        cur = c.execute(
            """
            INSERT INTO ingredient
              (ingredient_id, name, category, kind, sub_recipe_mode, base_unit,
               purchase_unit, pack_size, pack_size_status, yield_factor,
               yield_factor_status, unit_cost, unit_cost_status, supplier,
               supplier_contact, storage_location, lead_time_days, inventory_type,
               expiry_mode, shelf_life_days, max_stock_base, reorder_point_base)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (f"ING-{n + 1:04d}", body.name, body.category, body.kind, body.sub_recipe_mode,
             body.base_unit, body.purchase_unit,
             body.pack_size, "verified" if body.pack_size else "unset",
             body.yield_factor, "verified",
             body.unit_cost, "verified" if body.unit_cost else "unset",
             body.supplier, body.supplier_contact, body.storage_location, body.lead_time_days,
             body.inventory_type, body.expiry_mode, body.shelf_life_days,
             body.max_stock_base, body.reorder_point_base),
        )
        return {"id": cur.lastrowid, "ingredient_id": f"ING-{n + 1:04d}"}


class VerifyIn(BaseModel):
    field: Literal["pack_size", "yield_factor", "unit_cost"]
    value: float


@app.post("/console/ingredients/{pk}/verify", tags=["console"])
def verify_constant(
    pk: int, body: VerifyIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("inventory.write")),
):
    """Confirm a physical constant. This is the only path that moves a value
    from needs_verification to verified — it cannot happen as a side effect of
    an ordinary edit.

    Validated the same way /console/queues/verification/resolve validates
    the same three columns (verification.validate_numeric_value) — pydantic's
    plain `float` type accepts NaN and infinity by default, which is not
    caught by body: VerifyIn alone."""
    value = verification.validate_numeric_value(f"ingredient.{body.field}", body.value)
    with db.write_tx(c):
        cur = c.execute(
            f"UPDATE ingredient SET {body.field}=?, {body.field}_status='verified', "
            f"updated_at=datetime('now') WHERE id=?",
            (value, pk),
        )
        if cur.rowcount != 1:
            raise HTTPException(404, "ingredient not found")
        if body.field in ("pack_size", "yield_factor"):
            # stock is held in base units, so changing the conversion restates it
            c.execute(
                """
                UPDATE ingredient
                   SET on_hand_base = (SELECT SUM(qty_base) FROM fefo_batch
                                        WHERE ingredient_id=? AND state='open')
                 WHERE id=? AND EXISTS (SELECT 1 FROM fefo_batch
                                         WHERE ingredient_id=? AND state='open')
                """,
                (pk, pk, pk),
            )
    return {"ok": True, "field": body.field, "value": value, "verified_by": p["sub"]}


class ReceiveIn(BaseModel):
    purchase_qty: float = Field(gt=0, description="in the ingredient's own purchase unit")
    lot_no: str | None = None
    expires_at: str | None = Field(default=None, description="YYYY-MM-DD, if known")


@app.get("/console/ingredients/{pk}/receive/preview", tags=["console"])
def receive_preview(
    pk: int, purchase_qty: float = Query(gt=0),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """What a delivery of this size WOULD do, without writing anything --
    Phase 10's receiving flow shows this before the operator confirms.
    Calls the exact same units.purchase_to_base() conversion
    reservation.receive_stock() uses to actually post the delivery, so the
    preview can never drift from what posting really does -- there is no
    second copy of this arithmetic in the frontend. Read-only, gated on
    inventory.read rather than .write: looking at a preview isn't a write."""
    row = c.execute(
        "SELECT name, pack_size, yield_factor, base_unit, on_hand_base "
        "FROM ingredient WHERE id=?", (pk,),
    ).fetchone()
    if not row:
        raise NotFoundError(f"ingredient {pk} not found")
    base_qty = purchase_to_base(purchase_qty, row["pack_size"], row["yield_factor"])
    return {
        "ingredient_id": pk,
        "name": row["name"],
        "purchase_qty": purchase_qty,
        "base_qty": round(base_qty, 3),
        "base_unit": row["base_unit"],
        "current_on_hand_base": row["on_hand_base"],
        "new_on_hand_base": round(row["on_hand_base"] + base_qty, 3),
    }


@app.post("/console/ingredients/{pk}/receive", tags=["console"])
def receive_ingredient(
    pk: int, body: ReceiveIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("inventory.write")),
):
    """Record a delivery. The only endpoint that increases on-hand stock from
    outside the order-void path — see reservation.receive_stock for why that
    matters for the ledger."""
    return receive_stock(c, pk, body.purchase_qty, body.lot_no, body.expires_at,
                         user_id=p["sub"])


class CountIn(BaseModel):
    actual_base: float = Field(ge=0, description="physically counted quantity, in the ingredient's base unit")
    note: str | None = None


@app.post("/console/ingredients/{pk}/count", tags=["console"])
def count_ingredient(
    pk: int, body: CountIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("inventory.write")),
):
    """Reconcile a physical stock count against what the system expects is on
    hand — the recipe-based deduction system assumes every gram used was
    accounted for by a sale; this is how a real count surfaces the gap
    (spillage, an over-generous scoop, anything unlogged) instead of it
    silently accumulating. See reservation.record_count."""
    return record_count(c, pk, body.actual_base, body.note, user_id=p["sub"])


class BulkCountItem(BaseModel):
    ingredient_pk: int
    actual_base: float = Field(ge=0)


class BulkCountIn(BaseModel):
    items: list[BulkCountItem]
    note: str | None = None


@app.post("/console/ingredients/bulk-count", tags=["console"])
def bulk_count(
    body: BulkCountIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("inventory.write")),
):
    """A full physical stock take in one submission, instead of one ingredient
    at a time — each line still goes through record_count individually (its
    own real ledger entry, its own real variance), so a bulk update carries
    exactly the same audit trail as doing it one by one, just faster to enter.
    One bad line does not abort the rest; it is reported back instead."""
    if not body.items:
        raise HTTPException(422, "no items provided")
    results, errors = [], []
    for item in body.items:
        try:
            results.append(record_count(c, item.ingredient_pk, item.actual_base, body.note, user_id=p["sub"]))
        except WTError as e:
            errors.append({"ingredient_pk": item.ingredient_pk, "error": e.message})
    return {"updated": len(results), "failed": len(errors), "results": results, "errors": errors}


class WasteIn(BaseModel):
    qty_base: float = Field(gt=0, description="amount thrown away, in the ingredient's base unit")
    reason: Literal["wastage", "spoilage"] = "wastage"
    note: str | None = None


@app.post("/console/ingredients/{pk}/waste", tags=["console"])
def waste_ingredient(
    pk: int, body: WasteIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("inventory.write")),
):
    """Write stock off as thrown away or spoiled. Closes a real gap: before
    this, a dropped tray had no way into the system except a physical count
    that absorbed it with no stated cause. See reservation.record_wastage."""
    return record_wastage(c, pk, body.qty_base, body.reason, body.note, user_id=p["sub"])


class UsageIn(BaseModel):
    qty_base: float = Field(gt=0, description="amount used, in the ingredient's base unit")
    note: str | None = None


@app.post("/console/ingredients/{pk}/use", tags=["console"])
def use_ingredient(
    pk: int, body: UsageIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("inventory.write")),
):
    """Record consumption of an operational supply (tissue, cleaning
    alcohol, gloves) that never passes through the POS — staff state how
    many were used instead of the system inferring it from a sale. See
    reservation.record_usage."""
    return record_usage(c, pk, body.qty_base, body.note, user_id=p["sub"])


class IngredientEditIn(BaseModel):
    """Every field a manager can change about an ingredient after it exists
    — everything optional, so a PATCH only touches what was actually sent.
    unit_cost is included here as well as on /verify: editing it after it
    was already confirmed is still an edit, not a first-time confirmation,
    but the effect (a verified, current number) is the same either way."""
    name: str | None = None
    category: Literal["wet_food", "dry_food", "packaging", "consumable", "resale"] | None = None
    purchase_unit: str | None = None
    unit_cost: float | None = Field(default=None, ge=0)
    supplier: str | None = None
    supplier_contact: str | None = None
    storage_location: str | None = None
    lead_time_days: int | None = Field(default=None, ge=0)
    reorder_point_base: float | None = Field(default=None, ge=0)
    max_stock_base: float | None = Field(default=None, gt=0)
    inventory_type: Literal["food_ingredient", "sellable_product", "operational_supply"] | None = None
    expiry_mode: Literal["fixed_date", "shelf_life_estimate", "none"] | None = None
    shelf_life_days: int | None = Field(default=None, gt=0)


@app.patch("/console/ingredients/{pk}", tags=["console"])
def edit_ingredient(
    pk: int, body: IngredientEditIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("inventory.write")),
):
    """General field editing for an ingredient that already exists — price
    changes, reorder points, supplier contact, how it's tracked for expiry
    and whether it's sold at all. Column names in the SET clause come only
    from IngredientEditIn's fixed set of declared fields, the same pattern
    already used by /verify, not from anything a caller can supply."""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(422, "no fields to update")
    set_sql = ", ".join(f"{k} = :{k}" for k in fields)
    if "unit_cost" in fields:
        set_sql += ", unit_cost_status = 'verified'"
    params = {**fields, "id": pk}
    with db.write_tx(c):
        cur = c.execute(
            f"UPDATE ingredient SET {set_sql}, updated_at = datetime('now') WHERE id = :id",
            params,
        )
        if cur.rowcount != 1:
            raise HTTPException(404, "ingredient not found")
    return {"ok": True, "updated": list(fields.keys())}


@app.get("/console/ingredients/{pk}", tags=["console"])
def ingredient_detail(
    pk: int, c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Everything the console's ingredient drawer shows, in one call: the
    ingredient with its readiness flags, which recipes consume it (walking
    the same v_recipe_leaf view the deduction path uses, so a sub-recipe's
    parent shows up too), its open FEFO batches in real pick order, and its
    recent stock movements."""
    row = c.execute(
        """
        SELECT i.*, r.is_deductable, r.block_reason, r.unverified_count,
               (i.on_hand_base - i.reserved_base) AS available_base
          FROM ingredient i JOIN v_ingredient_readiness r ON r.id = i.id
         WHERE i.id = ?
        """,
        (pk,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "ingredient not found")
    data = dict(row)
    data["on_hand_display"] = format_base(
        data["on_hand_base"], data["base_unit"], data["pack_size"], data["purchase_unit"],
        data["yield_factor"])

    used_in = rows(c.execute(
        """
        SELECT r.id, r.name, r.kind, l.qty_base,
               (SELECT COUNT(*) FROM menu_item m WHERE m.recipe_id = r.id) AS menu_items
          FROM v_recipe_leaf l JOIN recipe r ON r.id = l.recipe_id
         WHERE l.ingredient_id = ? AND r.is_active = 1
         ORDER BY l.qty_base DESC
        """,
        (pk,),
    ))
    # Same order deplete_fefo actually draws down in — expiry ascending,
    # undated batches last, id breaking ties — so "next to be used" here
    # means exactly what it means to the deduction engine, not a UI-only
    # approximation of it. Unlike /console/expiry, nothing is excluded for
    # lacking a usable date: a batch this ingredient actually holds doesn't
    # stop being real stock just because nobody has confirmed when it turns.
    batches = rows(c.execute(
        """
        SELECT b.id, b.lot_no, b.qty_base, b.received_at, b.expires_at, b.expiry_status,
               i.expiry_mode, i.shelf_life_days
          FROM fefo_batch b JOIN ingredient i ON i.id = b.ingredient_id
         WHERE b.ingredient_id = ? AND b.state = 'open' AND b.qty_base > 0
         ORDER BY (b.expires_at IS NULL), b.expires_at, b.id
        """,
        (pk,),
    ))
    for b in batches:
        if _annotate_batch_expiry(b) is None:
            b["tracking"] = "not_tracked"
            b["days_to_expiry"] = None
            b["expired"] = False
            b["storage_status"] = None

    movements = rows(c.execute(
        """
        SELECT id, delta_base, balance_after_base, reason, wastage_reason,
               ref_type, ref_id, user_id, note, created_at
          FROM stock_ledger WHERE ingredient_id = ? ORDER BY id DESC LIMIT 20
        """,
        (pk,),
    ))
    # Same rate/runway maths the Forecasts page uses, for this one ingredient.
    dep = next((d for d in ingredient_depletion(c, 30) if d["ingredient_id"] == pk), None)
    return {"ingredient": data, "used_in": used_in, "batches": batches,
            "movements": movements, "depletion": dep}


# ===========================================================================
# Console — recipe CRUD, including sub-recipes
# ===========================================================================
class RecipeLineIn(BaseModel):
    ingredient_pk: int = Field(description="FK — never a name")
    display_label: str
    qty_base: float = Field(gt=0)
    is_optional: bool = False
    note: str | None = None


class RecipeIn(BaseModel):
    name: str
    kind: Literal["menu", "sub_recipe"] = "menu"
    produces_ingredient_pk: int | None = None
    yield_qty: float = 1.0
    yield_unit: str = "serving"
    lines: list[RecipeLineIn] = []


@app.get("/console/recipes", tags=["console"])
def list_recipes(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("recipe.read")),
):
    return {"recipes": rows(c.execute(
        """
        SELECT r.*, (SELECT COUNT(*) FROM recipe_line rl WHERE rl.recipe_id=r.id) AS line_count,
               (SELECT COUNT(*) FROM unmapped_recipe_line u
                 WHERE u.recipe_id=r.id AND u.state='open') AS unmapped_count
          FROM recipe r WHERE r.is_active=1 ORDER BY r.kind, r.name
        """
    ))}


@app.get("/console/recipes/{pk}", tags=["console"])
def get_recipe(
    pk: int, c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("recipe.read")),
):
    r = c.execute("SELECT * FROM recipe WHERE id=?", (pk,)).fetchone()
    if not r:
        raise HTTPException(404, "recipe not found")
    return {
        "recipe": dict(r),
        "lines": rows(c.execute(
            """
            SELECT rl.*, i.name AS ingredient_name, i.base_unit, i.kind AS ingredient_kind,
                   i.sub_recipe_mode
              FROM recipe_line rl JOIN ingredient i ON i.id = rl.ingredient_id
             WHERE rl.recipe_id=? ORDER BY rl.sort_order, rl.id
            """, (pk,))),
        "expanded_leaves": [
            {"ingredient_id": l.ingredient_id, "name": l.name,
             "qty_base": round(l.qty_base, 4), "base_unit": l.base_unit,
             "deductable": l.is_deductable, "block_reason": l.block_reason}
            for l in expand_recipe(c, pk)
        ],
        "unmapped": rows(c.execute(
            "SELECT * FROM unmapped_recipe_line WHERE recipe_id=? AND state='open'", (pk,))),
        "problems": validate_recipe(c, pk),
        "cost": recipe_cost(c, pk),
    }


@app.post("/console/recipes", tags=["console"], status_code=201)
def create_recipe(
    body: RecipeIn, c: sqlite3.Connection = Depends(conn),
    _p: dict = Depends(needs("recipe.write")),
):
    if body.kind == "sub_recipe" and body.produces_ingredient_pk is None:
        raise HTTPException(422, "a sub-recipe must declare which ingredient it produces")
    with db.write_tx(c):
        n = c.execute("SELECT COUNT(*) AS n FROM recipe").fetchone()["n"]
        cur = c.execute(
            "INSERT INTO recipe (recipe_id, name, kind, produces_ingredient_id, "
            "yield_qty, yield_unit) VALUES (?,?,?,?,?,?)",
            (f"REC-{n + 1:04d}", body.name, body.kind, body.produces_ingredient_pk,
             body.yield_qty, body.yield_unit),
        )
        pk = cur.lastrowid
        for order, ln in enumerate(body.lines):
            # NOT NULL + FK on ingredient_id means an unmapped line raises here
            # rather than reaching the table.
            c.execute(
                "INSERT INTO recipe_line (recipe_id, ingredient_id, display_label, "
                "qty_base, qty_status, is_optional, sort_order, note) "
                "VALUES (?,?,?,?, 'verified', ?,?,?)",
                (pk, ln.ingredient_pk, ln.display_label, ln.qty_base,
                 int(ln.is_optional), order, ln.note),
            )
    problems = validate_recipe(c, pk)
    return {"id": pk, "problems": problems}


class MenuPriceIn(BaseModel):
    price: float = Field(gt=0)


@app.patch("/console/menu-items/{pk}/price", tags=["console"])
def edit_menu_price(
    pk: int, body: MenuPriceIn, c: sqlite3.Connection = Depends(conn),
    _p: dict = Depends(needs("recipe.write")),
):
    """The only console-writable field on a menu item today. Everything
    else about it (what it's made of) is edited through its recipe."""
    with db.write_tx(c):
        cur = c.execute(
            "UPDATE menu_item SET price = ?, updated_at = datetime('now') WHERE id = ?",
            (body.price, pk),
        )
        if cur.rowcount != 1:
            raise HTTPException(404, "menu item not found")
    row = c.execute("SELECT id, name, price FROM menu_item WHERE id=?", (pk,)).fetchone()
    return dict(row)


# ===========================================================================
# Console — the two admin queues
# ===========================================================================
@app.get("/console/queues/unmapped", tags=["queues"])
def queue_unmapped(
    state: str = "open", c: sqlite3.Connection = Depends(conn),
    _p: dict = Depends(needs("queue.read")),
):
    """Recipe lines with no ingredient mapping. Each entry blocks every menu
    item whose recipe contains it."""
    items = rows(c.execute(
        """
        SELECT u.*, (SELECT COUNT(*) FROM menu_item m WHERE m.recipe_id = u.recipe_id)
                 AS blocks_menu_items
          FROM unmapped_recipe_line u
         WHERE u.state = ?
         ORDER BY u.reason, u.source_recipe_name
        """, (state,)))
    by_reason: dict[str, int] = {}
    for i in items:
        by_reason[i["reason"]] = by_reason.get(i["reason"], 0) + 1
    return {"items": items, "count": len(items), "by_reason": by_reason}


class ResolveIn(BaseModel):
    ingredient_pk: int | None = None
    action: Literal["map", "dismiss"] = "map"
    note: str | None = None


@app.post("/console/queues/unmapped/{pk}/resolve", tags=["queues"])
def resolve_unmapped(
    pk: int, body: ResolveIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("queue.resolve")),
):
    """Map a queued line to a real ingredient, or dismiss it.

    Mapping writes the alias so the next import of the same spreadsheet resolves
    it without a human. There is no auto-guess path — a person decides once.
    """
    row = c.execute("SELECT * FROM unmapped_recipe_line WHERE id=?", (pk,)).fetchone()
    if not row:
        raise HTTPException(404, "queue item not found")

    with db.write_tx(c):
        if body.action == "dismiss":
            c.execute(
                "UPDATE unmapped_recipe_line SET state='dismissed', resolved_by=?, "
                "resolved_at=datetime('now'), note=? WHERE id=?",
                (p["sub"], body.note, pk),
            )
            return {"ok": True, "state": "dismissed"}

        if body.ingredient_pk is None:
            raise HTTPException(422, "mapping requires an ingredient_pk")
        ing = c.execute(
            "SELECT id, name, base_unit FROM ingredient WHERE id=?", (body.ingredient_pk,)
        ).fetchone()
        if not ing:
            raise HTTPException(404, "ingredient not found")

        qty_base = row["parsed_qty"] or 0.0
        if row["parsed_unit"]:
            qty_base = to_base(row["parsed_qty"], row["parsed_unit"], ing["base_unit"])

        if row["recipe_id"] and qty_base > 0:
            c.execute(
                "INSERT OR IGNORE INTO recipe_line (recipe_id, ingredient_id, display_label, "
                "qty_base, qty_status, sort_order) VALUES (?,?,?,?, 'needs_verification', 999)",
                (row["recipe_id"], ing["id"], row["source_ingredient_label"], qty_base),
            )
        c.execute(
            "INSERT OR IGNORE INTO ingredient_alias (ingredient_id, alias_norm, alias_raw, "
            "source, created_by) VALUES (?,?,?, 'queue_resolution', ?)",
            (ing["id"], normalise_label(row["source_ingredient_label"]),
             row["source_ingredient_label"], p["sub"]),
        )
        c.execute(
            "UPDATE unmapped_recipe_line SET state='resolved', resolved_ingredient_id=?, "
            "resolved_by=?, resolved_at=datetime('now'), note=? WHERE id=?",
            (ing["id"], p["sub"], body.note, pk),
        )
    return {"ok": True, "state": "resolved", "mapped_to": ing["name"],
            "qty_base": round(qty_base, 4)}


@app.get("/console/queues/verification", tags=["queues"])
def queue_verification(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("queue.read")),
):
    """Every physical constant still carrying a seeded placeholder, translated
    into plain operational language and grouped by what breaks if it's
    wrong. See wt/verification.py."""
    return verification.list_queue(c)


@app.get("/console/queues/verification/history", tags=["queues"])
def queue_verification_history(
    field: str, row_id: int,
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("queue.read")),
):
    """Every decision ever recorded for one field/row: what it was, what it
    became, who decided, and when — the provenance trail."""
    return {"events": verification.history_for(c, field, row_id)}


class VerificationResolveIn(BaseModel):
    field: str
    row_id: int
    action: Literal["confirm", "change", "keep_estimate"]
    new_value: float | str | None = None
    note: str | None = None


@app.post("/console/queues/verification/resolve", tags=["queues"])
def resolve_verification(
    body: VerificationResolveIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("queue.resolve")),
):
    """Confirm, change, or keep-as-estimate for one verification item. The
    only path that can move one of these fields to 'verified' — see
    wt/verification.py for exactly what each action does and why the
    unlimited-rate field is never edited in place."""
    return verification.resolve(c, body.field, body.row_id, body.action,
                                body.new_value, body.note, p["sub"])


@app.get("/console/readiness", tags=["console"])
def readiness(c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read"))):
    """One call for the console banner: is this database production-ready?"""
    menu = c.execute(
        "SELECT COUNT(*) AS total, SUM(sellable) AS sellable FROM v_menu_item_availability"
    ).fetchone()
    return {
        "menu_items_total": menu["total"],
        "menu_items_sellable": menu["sellable"] or 0,
        "menu_items_blocked": menu["total"] - (menu["sellable"] or 0),
        "unmapped_open": c.execute(
            "SELECT COUNT(*) AS n FROM unmapped_recipe_line WHERE state='open'"
        ).fetchone()["n"],
        "needs_verification": c.execute(
            "SELECT COUNT(*) AS n FROM v_needs_verification"
        ).fetchone()["n"],
        "ingredients_blocked": c.execute(
            "SELECT COUNT(*) AS n FROM v_ingredient_readiness WHERE is_deductable=0"
        ).fetchone()["n"],
        "production_ready": False,
    }


@app.get("/console/ledger", tags=["console"])
def list_ledger(
    ingredient_id: int | None = None,
    reason: str | None = None,
    limit: int = Query(200, le=1000),
    offset: int = 0,
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """The append-only stock ledger — every movement, in base units, with the
    balance it produced. This is the audit trail every number on the
    dashboard is ultimately reconciled against."""
    sql = """
        SELECT l.*, i.name AS ingredient_name, i.base_unit
          FROM stock_ledger l JOIN ingredient i ON i.id = l.ingredient_id
         WHERE 1=1
    """
    params: list[Any] = []
    if ingredient_id:
        sql += " AND l.ingredient_id = ?"
        params.append(ingredient_id)
    if reason:
        sql += " AND l.reason = ?"
        params.append(reason)
    sql += " ORDER BY l.id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    total = c.execute(
        "SELECT COUNT(*) AS n FROM stock_ledger" + (" WHERE ingredient_id=?" if ingredient_id else ""),
        [ingredient_id] if ingredient_id else [],
    ).fetchone()["n"]
    return {"entries": rows(c.execute(sql, params)), "total": total}


def _annotate_batch_expiry(b: dict) -> dict | None:
    """Shared with ingredient_detail's batch list below, so there is exactly
    one place that turns a batch's raw expiry_mode/expires_at/received_at
    into a usable date — not two independently-maintained copies of the
    same arithmetic. Mutates and returns `b` with tracking/days_to_expiry/
    expired/storage_status added, or returns None when this batch has no
    computable expiry at all (not on a shelf-life estimate, and either
    missing or an unparseable expires_at) — the caller decides what that
    means for it: expiry_monitor drops it (it isn't an expiry signal to
    monitor), the drawer keeps it (it's still real stock the owner should
    see, just with nothing to say about when it turns).

    Pops shelf_life_days/expiry_mode off `b` — they're inputs to this
    calculation, not part of what either caller wants back.
    """
    import datetime as _dt
    shelf_life_days = b.pop("shelf_life_days")
    mode = b.pop("expiry_mode")
    if mode == "shelf_life_estimate":
        try:
            # The Manila business date the batch was received on, not the
            # UTC calendar date its timestamp happens to fall on -- a batch
            # received at, say, UTC 20:00 (Manila 04:00 the next day) is
            # "received today" to whoever signed for it.
            received = _dt.date.fromisoformat(timeutil.business_date_of(b["received_at"]))
        except (ValueError, TypeError):
            return None
        exp = received + _dt.timedelta(days=shelf_life_days or 0)
        b["expires_at"] = exp.isoformat()
        b["tracking"] = "estimated_shelf_life"
    else:
        try:
            exp = _dt.date.fromisoformat(b["expires_at"])
        except (ValueError, TypeError):
            return None
        b["tracking"] = "exact_date"
    days = (exp - timeutil.business_today()).days
    b["days_to_expiry"] = days
    b["expired"] = days < 0
    b["storage_status"] = "Expired" if days < 0 else "Use Soon" if days <= 2 else "Normal"
    return b


@app.get("/console/expiry", tags=["console"])
def expiry_monitor(
    within_days: int = Query(90, description="only batches expiring/estimated within this many days"),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Open FEFO batches that carry an actual expiry signal, nearest first.

    Not every batch has a manufacturer date, and per the ingredient's
    expiry_mode that's by design rather than missing data:
      - shelf_life_estimate items (fresh produce, herbs) never get a printed
        date, so they're tracked by estimate — received_at plus the
        ingredient's configured shelf_life_days — full stop, even if some
        stray expires_at is sitting on the batch from an old entry.
      - fixed_date items are tracked by their real recorded date.
      - none-mode items (eggs, by default) are left out entirely unless a
        real date was actually recorded on that specific batch — showing an
        estimate for them would just be inventing a number nobody supplied.
    Days are computed here rather than stored, so they're never stale."""
    batches = rows(c.execute(
        """
        SELECT b.id, b.lot_no, b.qty_base, b.received_at, b.expires_at, b.expiry_status,
               i.id AS ingredient_id, i.name AS ingredient_name, i.base_unit,
               i.expiry_mode, i.shelf_life_days
          FROM fefo_batch b JOIN ingredient i ON i.id = b.ingredient_id
         WHERE b.state = 'open' AND b.qty_base > 0
           AND (b.expires_at IS NOT NULL OR i.expiry_mode = 'shelf_life_estimate')
        """
    ))
    out = []
    for b in batches:
        annotated = _annotate_batch_expiry(b)
        if annotated is None or annotated["days_to_expiry"] > within_days:
            continue
        out.append(annotated)
    out.sort(key=lambda x: x["days_to_expiry"])
    return {"batches": out, "count": len(out)}


@app.get("/console/reports/sales", tags=["console"])
def sales_summary(
    days: int = Query(30, description="look-back window"),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Real numbers only — this reads customer_order/order_line directly, so
    on a system with only a handful of test orders the totals will be small.
    No synthetic history is generated to make the chart look busier.

    days may be negative (a window into the future, matching nothing) — used
    by tests to get a deterministically-empty result. utc_bound_n_manila_days_ago
    handles that the same way the old f"-{days} days" SQLite modifier did: a
    negative or zero `days` pushes the bound into the future, so nothing real
    can be >= it.

    The bound is computed once in Python (Manila calendar-day aligned) rather
    than left to SQLite's own `datetime('now', ?)`, and calendar-day grouping
    below uses the Manila business date, not the UTC one — see wt/timeutil.py.
    """
    bound = timeutil.utc_bound_n_manila_days_ago(days)
    totals = c.execute(
        """
        SELECT COUNT(*) AS orders, COALESCE(SUM(o.head_count), 0) AS covers
          FROM customer_order o
         WHERE o.status != 'void' AND o.created_at >= ?
        """,
        (bound,),
    ).fetchone()
    revenue = c.execute(
        """
        SELECT COALESCE(SUM(ol.qty * ol.unit_price), 0) AS revenue
          FROM order_line ol JOIN customer_order o ON o.id = ol.order_id
         WHERE o.status != 'void' AND o.created_at >= ?
        """,
        (bound,),
    ).fetchone()["revenue"]
    by_item = rows(c.execute(
        """
        SELECT m.name, SUM(ol.qty) AS qty_sold, SUM(ol.qty * ol.unit_price) AS revenue
          FROM order_line ol
          JOIN customer_order o ON o.id = ol.order_id
          JOIN menu_item m ON m.id = ol.menu_item_id
         WHERE o.status != 'void' AND o.created_at >= ?
         GROUP BY m.id ORDER BY revenue DESC
        """,
        (bound,),
    ))
    # Revenue per calendar day, oldest first. Purely additive: the console's
    # dashboard charts this instead of deriving it from recent_orders, which
    # is capped at 50 rows and would silently undercount a busy week.
    day_bd = timeutil.sql_business_date("o.created_at")
    by_day = rows(c.execute(
        f"""
        SELECT {day_bd} AS day,
               COUNT(DISTINCT o.id) AS orders,
               COALESCE(SUM(ol.qty * ol.unit_price), 0) AS revenue
          FROM customer_order o
          LEFT JOIN order_line ol ON ol.order_id = o.id
         WHERE o.status != 'void' AND o.created_at >= ?
         GROUP BY {day_bd} ORDER BY day
        """,
        (bound,),
    ))
    # Voided orders stay in this list on purpose (audit visibility — seeing
    # "this one was voided" is useful in a report) but were missing the same
    # date-window filter as everything else above, so this returned the last
    # 50 orders ever, unconditionally, no matter what `days` was asked for.
    recent = rows(c.execute(
        """
        SELECT o.id, o.order_no, o.status, o.table_label, o.created_at,
               (SELECT COALESCE(SUM(ol2.qty*ol2.unit_price),0) FROM order_line ol2
                 WHERE ol2.order_id=o.id) AS total
          FROM customer_order o
         WHERE o.created_at >= ?
         ORDER BY o.created_at DESC LIMIT 50
        """,
        (bound,),
    ))
    return {
        "window_days": days,
        "orders": totals["orders"],
        "covers": totals["covers"],
        "revenue": round(revenue, 2),
        "by_item": by_item,
        "by_day": by_day,
        "recent_orders": recent,
    }


@app.get("/console/forecasts/depletion", tags=["console"])
def forecast_depletion(
    lookback_days: int = Query(30, ge=1, le=365),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Real per-ingredient runway at the actual recent consumption rate — no
    trained model, stdlib arithmetic over stock_ledger. See wt/forecasting.py
    for why Prophet isn't here.

    Also carries the reorder-point rule (S_k < ROP_k) for every ingredient
    that has one configured. below_reorder_point counts only those — an
    ingredient with no reorder point set is not silently treated as having
    one of zero."""
    data = ingredient_depletion(c, lookback_days)
    return {
        "ingredients": data,
        "below_reorder_point": sum(1 for d in data if d["below_reorder_point"]),
        "reorder_points_configured": sum(1 for d in data if d["reorder_point_base"] is not None),
    }


@app.get("/console/forecasts/demand", tags=["console"])
def forecast_demand(
    menu_item_pk: int | None = None,
    horizon_days: int = Query(7, ge=1, le=30),
    lookback_days: int = Query(60, ge=7, le=365),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """One item's daily sales history plus a real Prophet forecast (with
    uncertainty bounds), or every POS-visible item's quick summary if
    menu_item_pk is omitted. The bulk list intentionally uses the cheap
    trailing-average method — see wt/forecasting.py for why running Prophet
    for 20-30 items on one page load isn't practical."""
    if menu_item_pk is not None:
        return menu_item_demand_prophet(c, menu_item_pk, horizon_days, max(lookback_days, 90))
    items = rows(c.execute("SELECT id FROM menu_item WHERE pos_visible=1 ORDER BY name"))
    return {"items": [menu_item_demand(c, i["id"], horizon_days, lookback_days) for i in items]}


@app.get("/console/anomalies", tags=["console"])
def anomalies(
    lookback_days: int = Query(90, ge=1, le=365),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Both anomaly passes, reported separately rather than merged.

    consumption_anomalies is the specified model: IsolationForest over the
    seven-feature reconciled daily consumption vectors. stock_loss_anomalies
    is the older ledger pass over wastage/spoilage/count_adjustment, which
    answers a question the consumption pass structurally cannot.

    `anomalies` is kept as an alias of the stock-loss list so the existing
    console page and any client built against it keep working unchanged.
    See wt/anomaly.py for both methods."""
    losses = detect_anomalies(c, lookback_days)
    return {
        "consumption_anomalies": detect_consumption_anomalies(c, lookback_days),
        "stock_loss_anomalies": losses,
        "anomalies": losses,
    }


@app.get("/console/combinations", tags=["console"])
def combinations(
    lookback_days: int = Query(90, ge=1, le=365),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Which menu items real customers actually order together, via FP-growth
    (mlxtend) over real order history. See wt/associations.py for method."""
    return product_combinations(c, lookback_days)


@app.get("/console/model-details", tags=["console"])
def model_details_endpoint(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Real technical stats for the three Phase 3 models — a holdout
    backtest for Prophet, live parameters and current pooled-sample counts
    for IsolationForest and FP-growth. Kept separate from the plain-language
    Forecasts/Anomaly/Reports pages; this is the evidence-of-work view."""
    return {
        "demand_forecast": evaluate_demand_model(c),
        "anomaly_detection": consumption_model_details(c),
        "stock_loss_detection": anomaly_model_details(c),
        "product_combinations": association_model_details(c),
    }


# ===========================================================================
# Unlimited-item reconciliation — the three-stage provisional/actual model.
#
# Deliberately NOT under /console: the kitchen is the station that weighs the
# tray, and a kitchen tablet holds no console capability at all. These routes
# are gated on unli.reconcile / unli.approve specifically so the workflow
# works without handing the kitchen inventory.write. See wt/unli.py.
# ===========================================================================
def _today_iso() -> str:
    return timeutil.business_today_iso()


@app.get("/unli/reconciliation/pending", tags=["unli"])
def unli_pending(
    service_date: str | None = Query(None, description="YYYY-MM-DD; defaults to today"),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("unli.reconcile")),
):
    """Per unlimited stream for one service date: covers served, the
    provisional quantity already deducted for them, the coefficient that
    sized it, and the reconciliation if one has been recorded."""
    return unli.pending(c, service_date or _today_iso())


class ReconcileIn(BaseModel):
    service_date: str = Field(description="YYYY-MM-DD")
    item_key: Literal["wings", "rice", "fries", "drinks"]
    opening_base: float = Field(ge=0, description="weighed out at start of service, base units")
    closing_base: float = Field(ge=0, description="leftover weighed at close, base units")
    added_base: float = Field(default=0.0, ge=0, description="top-ups issued during service")
    note: str | None = None


@app.post("/unli/reconciliation", tags=["unli"], status_code=201)
def unli_reconcile(
    body: ReconcileIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("unli.reconcile")),
):
    """Stage 2 + 3 in one transaction: record the measurement, compute actual
    consumption, compare it against the provisional deduction, judge it
    against the configured tolerance, and post the *difference* to stock.

    Never re-deducts the provisional quantity — see wt/unli.py on why that is
    the one thing this endpoint must not do."""
    return unli.record_reconciliation(
        c, body.service_date, body.item_key, body.opening_base, body.closing_base,
        body.added_base, body.note, user_id=p["sub"],
    )


@app.get("/unli/reconciliation", tags=["unli"])
def unli_history(
    limit: int = Query(100, le=500), item_key: str | None = None,
    days: int | None = Query(None, ge=1, le=365),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("unli.reconcile")),
):
    """Reconciliation history and audit trail — the same rows the Isolation
    Forest trains on, read through the same view."""
    return unli.history(c, limit, item_key, days)


class ReviewIn(BaseModel):
    approve: bool
    note: str | None = None


@app.post("/unli/reconciliation/{pk}/review", tags=["unli"])
def unli_review(
    pk: int, body: ReviewIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("unli.approve")),
):
    """Sign off (or reject) a day that fell outside tolerance. Governs only
    whether that day may influence a coefficient; the inventory correction
    was already posted when the measurement was recorded."""
    return unli.review(c, pk, body.approve, body.note, user_id=p["sub"])


class RecalibrateIn(BaseModel):
    item_key: str | None = None
    dry_run: bool = False


@app.post("/unli/coefficients/recalibrate", tags=["unli"])
def unli_recalibrate(
    body: RecalibrateIn = Body(default=RecalibrateIn()),
    c: sqlite3.Connection = Depends(conn), p: dict = Depends(needs("unli.approve")),
):
    """Re-estimate each rolling coefficient from its own validated
    reconciliation history. dry_run returns what would change without
    writing. The frozen baseline is never touched."""
    return unli.recalibrate(c, body.item_key, user_id=p["sub"], dry_run=body.dry_run)


@app.get("/unli/coefficients", tags=["unli"])
def unli_coefficients(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("unli.reconcile")),
):
    """Every stream with its frozen baseline and current rolling lambda."""
    return {"streams": rows(c.execute(
        """
        SELECT ui.id, ui.item_key, ui.label, ui.window_days, i.name AS ingredient_name,
               i.base_unit,
               (SELECT lambda_value FROM unli_coefficient b WHERE b.unli_item_id = ui.id
                 AND b.role='baseline' ORDER BY b.effective_from DESC, b.id DESC LIMIT 1)
                 AS baseline_lambda,
               (SELECT lambda_value FROM unli_coefficient r WHERE r.unli_item_id = ui.id
                 AND r.role='rolling' ORDER BY r.effective_from DESC, r.id DESC LIMIT 1)
                 AS rolling_lambda,
               (SELECT COUNT(*) FROM unli_coefficient h WHERE h.unli_item_id = ui.id
                 AND h.role='rolling') AS rolling_revisions
          FROM unli_item ui JOIN ingredient i ON i.id = ui.ingredient_id
         WHERE ui.is_active = 1 ORDER BY ui.id
        """))}


class ToleranceIn(BaseModel):
    tolerance_ratio: float = Field(gt=0, le=1, description="e.g. 0.10 for 10%")


# ---------------------------------------------------------------------------
# Sessions and refills — the operational layer on top of the immutable order.
# ---------------------------------------------------------------------------
@app.get("/unli/flavors", tags=["unli"])
def unli_flavors(
    all_flavors: bool = Query(False, description="include inactive / non-deductable"),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("menu.read")),
):
    """The selectable flavours with their add-on prices.

    Prices are resolved server-side from the linked menu item or the flavour's
    own configured amount — a client never sends a price and never decides
    one. Gated on menu.read, the same capability the menu itself uses, so a
    till can load this without inventory access."""
    data = refills.list_flavors(c, active_only=not all_flavors)
    return {
        "flavors": data,
        "included": [f for f in data if f["flavor_type"] == "included"],
        "special": [f for f in data if f["flavor_type"] == "special"],
    }


class FlavorConfigIn(BaseModel):
    flavor_type: Literal["included", "special"] | None = None
    addon_price: float | None = Field(default=None, ge=0)
    addon_menu_item_id: int | None = None
    is_active: bool | None = None


@app.patch("/unli/flavors/{pk}", tags=["unli"])
def unli_set_flavor(
    pk: int, body: FlavorConfigIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("recipe.write")),
):
    """Make a flavour premium, price it, or withdraw it.

    Under recipe.write — the same capability that edits a menu price, because
    that is exactly what this is. Marking a flavour 'included' forces its price
    back to zero rather than leaving a stale amount behind it."""
    return refills.set_flavor(c, pk, body.flavor_type, body.addon_price,
                              body.addon_menu_item_id, body.is_active, user_id=p["sub"])


@app.post("/unli/flavors/sync", tags=["unli"], status_code=200)
def unli_sync_flavors(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("recipe.write")),
):
    """Register any customer-selectable sauce that has no flavour row yet.
    Idempotent. Needed on a fresh install, where migrations run before the
    ingredients exist."""
    return {"registered": refills.sync_flavors(c)}


@app.get("/unli/sessions", tags=["unli"])
def unli_sessions(
    status: Literal["active", "completed", "cancelled", "all"] = "active",
    limit: int = Query(100, le=500),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("unli.refill")),
):
    """Tables currently on unlimited, for the till's and the kitchen's
    refill screens."""
    return refills.list_sessions(c, status, limit)


@app.get("/unli/sessions/{pk}", tags=["unli"])
def unli_session_detail(
    pk: int, c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("unli.refill")),
):
    """Everything the refill screen needs: the order it belongs to, head count,
    session state, the next round's number and piece count, every round served
    so far, and the add-ons charged."""
    return refills.session_detail(c, pk)


@app.get("/unli/orders/{order_pk}/session", tags=["unli"])
def unli_session_for_order(
    order_pk: int, c: sqlite3.Connection = Depends(conn),
    _p: dict = Depends(needs("unli.refill")),
):
    session = refills.session_for_order(c, order_pk)
    if session is None:
        raise HTTPException(404, "this order has no unlimited session")
    return session


@app.post("/unli/orders/{order_pk}/session", tags=["unli"], status_code=201)
def unli_open_session(
    order_pk: int, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("unli.session")),
):
    """Open a session for an unlimited order that does not have one — an order
    punched before this feature existed. New orders get theirs automatically at
    checkout. order_id is UNIQUE, so calling this twice returns the same
    session rather than opening a second."""
    return refills.open_session(c, order_pk, user_id=p["sub"])


class SessionStatusIn(BaseModel):
    status: Literal["completed", "cancelled"]
    note: str | None = None


@app.post("/unli/sessions/{pk}/close", tags=["unli"])
def unli_close_session(
    pk: int, body: SessionStatusIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("unli.session")),
):
    """The table has finished. A completed or cancelled session accepts no
    further rounds, and any round still sitting unserved is released — a
    session cannot close with stock held against food nobody will cook."""
    return refills.set_session_status(c, pk, body.status, body.note, user_id=p["sub"])


class ReopenIn(BaseModel):
    note: str | None = None


@app.post("/unli/sessions/{pk}/reopen", tags=["unli"])
def unli_reopen_session(
    pk: int, body: ReopenIn = Body(default=ReopenIn()),
    c: sqlite3.Connection = Depends(conn), p: dict = Depends(needs("unli.approve")),
):
    """Reopen a closed session. Deliberately a manager action and deliberately
    not the same capability that closed it: the whole point of completing a
    session is that the table has gone, so undoing that should not be reachable
    by the same accidental tap."""
    return refills.set_session_status(c, pk, "active", body.note, user_id=p["sub"])


class RefillIn(BaseModel):
    flavor_id: int
    note: str | None = None
    # One key per submit attempt, chosen by the client. Replaying it returns
    # the round already recorded instead of serving the table twice.
    client_request_id: str | None = Field(default=None, max_length=64)


@app.post("/unli/sessions/{pk}/refills", tags=["unli"], status_code=201)
def unli_record_refill(
    pk: int, body: RefillIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("unli.refill")),
):
    """Record a round. Pieces are computed server-side as pieces-per-head x the
    SESSION's head count — never taken from the request — and the selected
    flavour's sauce is held through the existing BOM resolver and reservation
    path. Nothing is deducted until the kitchen serves it."""
    return refills.record_refill(c, pk, body.flavor_id, user_id=p["sub"],
                                 client_request_id=body.client_request_id, note=body.note)


@app.get("/unli/refills/{pk}", tags=["unli"])
def unli_refill_detail(
    pk: int, c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("unli.refill")),
):
    return refills.refill_detail(c, pk)


@app.post("/unli/refills/{pk}/serve", tags=["unli"])
def unli_serve_refill(
    pk: int, c: sqlite3.Connection = Depends(conn), p: dict = Depends(needs("unli.refill")),
):
    """The kitchen has made it. Holds become ledger deductions and FEFO batch
    movements, and a premium flavour's add-on is charged against the order as a
    linked, append-only order_charge — the order's own total is never touched."""
    return refills.serve_refill(c, pk, user_id=p["sub"])


class RefillVoidIn(BaseModel):
    reason: str = "voided"
    restock: bool = False


@app.post("/unli/refills/{pk}/void", tags=["unli"])
def unli_void_refill(
    pk: int, body: RefillVoidIn = Body(default=RefillVoidIn()),
    c: sqlite3.Connection = Depends(conn), p: dict = Depends(needs("unli.refill")),
):
    """Undo a round. Before it was served: holds released, nothing written.
    After: a compensating credit/wastage pair and a negative add-on charge.
    The refill record itself is never deleted."""
    return refills.void_refill(c, pk, body.reason, body.restock, user_id=p["sub"])


class RefillCorrectIn(BaseModel):
    flavor_id: int
    reason: str = "wrong flavour"
    client_request_id: str | None = Field(default=None, max_length=64)


@app.post("/unli/refills/{pk}/correct", tags=["unli"])
def unli_correct_refill(
    pk: int, body: RefillCorrectIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("unli.refill")),
):
    """Wrong flavour. Voids the round and records a replacement pointing back
    at it, both in one transaction. Never edits the original."""
    return refills.correct_refill(c, pk, body.flavor_id, body.reason, user_id=p["sub"],
                                  client_request_id=body.client_request_id)


@app.get("/pos/orders/{order_pk}/receipt", tags=["pos"])
def order_receipt(
    order_pk: int, c: sqlite3.Connection = Depends(conn),
    _p: dict = Depends(needs("order.read")),
):
    """The receipt: the settled financial record exactly as it was paid, plus
    the unlimited session and refill history hanging off it.

    `order.total` is what was charged and never changes. `current_total` is
    that plus any linked add-on charges, computed here — the two are separate
    fields precisely so no client can mistake one for the other."""
    return refills.order_receipt(c, order_pk)


@app.get("/unli/tolerance", tags=["unli"])
def unli_get_tolerance(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("unli.reconcile")),
):
    return {
        "tolerance_ratio": unli.tolerance(c),
        "min_heads_for_recalibration": unli.get_setting(
            c, "unli_recalibration_min_heads", unli.DEFAULT_MIN_HEADS),
    }


@app.patch("/unli/tolerance", tags=["unli"])
def unli_set_tolerance(
    body: ToleranceIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("unli.approve")),
):
    """The tolerance band is a business decision, so it is a stored setting
    rather than a constant. Rows already reconciled keep the tolerance they
    were judged against — changing this does not re-judge history."""
    unli.set_setting(c, "unli_variance_tolerance", body.tolerance_ratio, user_id=p["sub"])
    return {"tolerance_ratio": body.tolerance_ratio}


# ===========================================================================
# Static clients — kiosk pages for the POS and kitchen tablets. Served
# same-origin so a tablet on the shop LAN needs only the server's address:
# http://<lan-ip>:8000/pos and http://<lan-ip>:8000/kds. Same-origin also
# means no CORS negotiation for these two, unlike a client hosted elsewhere.
#
# Registered LAST and by exact path (not a StaticFiles mount at "/") so
# nothing here can shadow an API route defined above.
# ===========================================================================
_STATIC_DIR = _Path(__file__).resolve().parent.parent / "static"

# Brand assets (logo, tagline graphic) — a plain StaticFiles mount rather than
# an explicit route since these are just files to hand back byte-for-byte.
# Mounted under "/img", a prefix nothing else in this file uses, so it can't
# shadow an API route regardless of registration order.
app.mount("/img", StaticFiles(directory=_STATIC_DIR / "img"), name="brand-img")

# Shared brand layer (intro animation CSS/JS) used by all four pages, so it
# lives in one file instead of being duplicated into each HTML client.
app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets")


@app.get("/pos", include_in_schema=False)
def pos_client():
    return FileResponse(_STATIC_DIR / "pos.html")


@app.get("/kds", include_in_schema=False)
def kds_client():
    return FileResponse(_STATIC_DIR / "kds.html")


@app.get("/console", include_in_schema=False)
def console_client():
    return FileResponse(_STATIC_DIR / "console.html")


@app.get("/", include_in_schema=False)
def landing():
    return FileResponse(_STATIC_DIR / "landing.html")
