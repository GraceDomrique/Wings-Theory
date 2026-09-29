"""HTTP surface.

Run:  uvicorn wt.api:app --host 0.0.0.0 --port 8000

Route groups map to the three clients: /pos/* is what a cashier tablet may
reach, /kds/* the kitchen, /console/* the owner. Enforcement is per-capability
in auth.CAPABILITIES rather than per-prefix, so a mis-filed route still cannot
leak a capability the role does not hold.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sqlite3
from datetime import date as _date, timedelta as _timedelta
from pathlib import Path as _Path
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    audit, auth, db, instructions, permissions, purchasing, refills, timeutil, unli, updates,
    verification,
)
from .cache import TTLCache
from .anomaly import detect_anomalies, detect_consumption_anomalies
from .anomaly import consumption_model_details
from .anomaly import model_details as anomaly_model_details
from .associations import product_combinations
from .associations import model_details as association_model_details
from .combo_pricing import (
    create_draft_combo, estimate_combo_cost, get_combo_pricing_config, list_draft_combos,
    review_draft_combo, set_combo_pricing_config, suggest_price_range,
)
from .errors import (
    AuthenticationError,
    NotFoundError,
    PasswordChangeRequiredError,
    PermissionError_,
    ValidationError,
    WTError,
)
from .depletion import (
    HORIZON_DAYS as DEPLETION_HORIZON_DAYS,
    LOOKBACK_DAYS as DEPLETION_LOOKBACK_DAYS,
    ingredient_detail as ingredient_depletion_detail,
    ingredient_forecast,
    prophet_refresh_generation,
    refresh_prophet_baselines,
    replenishment_backtest,
)
from .forecasting import (
    MIN_DEMAND_SAMPLE_DAYS, STORE_METRICS, evaluate_demand_model, evaluate_store_demand_model,
    ingredient_depletion, menu_item_demand, menu_item_demand_prophet, store_demand_prophet,
)
from .anomaly import anomaly_review_history, record_anomaly_review
from .data_quality import data_quality_report
from .reservation import (
    availability_for_menu_item, commit_order, ingredient_reconciliation_report,
    inventory_valuation, receive_stock, record_count, record_usage,
    record_variance_review, record_wastage, release_order, reserve_order,
    variance_review_history, void_after_commit,
)
from .resolver import expand_recipe, recipe_cost, validate_recipe
from .units import format_base, normalise_label, to_base

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

# 2026-08 audit: forecasting.py's single-item Prophet fit and both of
# anomaly.py's Isolation Forest passes ran fresh on every request with no
# caching. See wt/cache.py's module docstring for the full reasoning --
# short version: each cache key includes a cheap fingerprint of the table
# the computation actually reads (MAX(id)), so a genuinely new order or
# ledger row invalidates it immediately; TTL/max_entries only bound memory
# over a long-running process. Two separate instances because the two
# computations have unrelated data-change triggers (orders vs. ledger/
# reconciliation rows) and unrelated cost profiles.
_forecast_cache = TTLCache()
_anomaly_cache = TTLCache()
# Rolling backtest (evaluate_demand_model) fits Prophet up to
# ROLLING_MAX_ITEMS x ROLLING_MAX_FOLDS times per call -- far too slow to
# redo on every technical-panel page load. Longer TTL than the other two
# since a new order does not change which folds/items get evaluated in any
# way that matters for a technical-metrics panel refreshed occasionally.
_model_details_cache = TTLCache(ttl_seconds=1800)


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


_BACKEND_DIR = _Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _BACKEND_DIR.parent


def _git_commit() -> str | None:
    """Best-effort, computed once at import time rather than per-request —
    a checkout with no git history (or git missing) just reports None
    instead of failing health checks over it."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(_PROJECT_ROOT),
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


_GIT_COMMIT = _git_commit()


@app.get("/health", tags=["ops"], include_in_schema=False)
def health(c: sqlite3.Connection = Depends(conn)):
    """Unauthenticated on purpose — this is what a launcher script or an
    external monitor checks before assuming the server is actually usable,
    not a console page, so it can't require a login token. Reports nothing
    that isn't already visible to anyone on the LAN who can reach the
    server at all.

    The project_root/backend_dir/git_commit/pid fields exist specifically
    so two checkouts of this project can never be confused for each other
    again by eye — see the 2026-09 consolidation: a stale second checkout
    (C:\\Users\\PLPASIG\\wings-theory) was, for a time, indistinguishable
    from this one except by which port answered first. No secrets here —
    same threat model as the rest of this endpoint."""
    report = db.integrity_report(c)
    report.update({
        "app": "Wings Theory",
        "environment": os.environ.get("WT_ENV"),
        "project_root": str(_PROJECT_ROOT),
        "backend_dir": str(_BACKEND_DIR),
        "cwd": os.getcwd(),
        "git_commit": _GIT_COMMIT,
        "pid": os.getpid(),
    })
    return report


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
        "SELECT password_changed_at, is_active, must_change_password FROM app_user WHERE username=?",
        (payload.get("sub"),),
    ).fetchone()
    if not row or not row["is_active"]:
        raise AuthenticationError("account no longer active — please sign in again")
    if payload.get("pwd") != row["password_changed_at"]:
        raise AuthenticationError("session invalidated by a password change — please sign in again")
    # Stapled onto the decoded payload, not looked up again by needs() below,
    # so the must_change_password gate costs zero extra queries — this SELECT
    # already runs on every request.
    payload["_must_change_password"] = bool(row["must_change_password"])
    return payload


def needs(capability: str):
    def _dep(p: dict = Depends(principal)) -> dict:
        # Gated here, not in principal() itself: principal() also backs
        # /auth/change_password (bare Depends(principal), never needs()), and
        # that is the one endpoint a flagged account must still be able to
        # reach. Every other capability-gated route blocks until the account
        # sets its own password.
        if p.get("_must_change_password"):
            raise PasswordChangeRequiredError(
                "sign-in still uses a temporary password — set a new password to continue"
            )
        auth.require(p, capability)
        return p
    return _dep


def needs_page(page_key: str):
    """A second, narrower gate meant to sit ALONGSIDE a needs(capability)
    dependency on the same route, not replace it: the capability governs
    whether the ACTION is allowed at all (wt/auth.py's CAPABILITIES, code-
    only, unchanged by this); this governs whether the caller's role
    currently has admin-granted visibility into this whole PAGE
    (wt/permissions.py, admin-editable at runtime via /console/admin/
    permissions). Admin is always granted and never queried."""
    def _dep(p: dict = Depends(principal), c: sqlite3.Connection = Depends(conn)) -> dict:
        if not permissions.get_effective_grants(c, p.get("role"))[page_key]:
            raise PermissionError_(
                f"role '{p.get('role')}' does not have the '{page_key}' page enabled",
                page=page_key,
            )
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
    """auth.py's threat model is explicit: this system does not defend a
    public endpoint. It still defends a lost/compromised device, or a LAN
    guest guessing a still-active seeded password -- all four real
    accounts share one password until an owner rotates them (see
    GO_LIVE_CHECKLIST.md) -- against unlimited guesses. Locks the account
    for LOCKOUT_MINUTES after MAX_LOGIN_ATTEMPTS consecutive failures;
    any successful login clears the counter."""
    row = c.execute(
        "SELECT * FROM app_user WHERE username=? AND is_active=1", (body.username,)
    ).fetchone()
    if row and row["locked_until"] and row["locked_until"] > timeutil.utc_now_sql():
        with db.write_tx(c):
            audit.log_login(c, body.username, row["role"], success=False, reason="locked_out")
        raise HTTPException(
            423, f"account locked until {row['locked_until']} (too many failed attempts)"
        )
    if not row or not auth.verify_password(body.password, row["password_hash"], row["password_salt"]):
        with db.write_tx(c):
            if row:
                count = row["failed_login_count"] + 1
                if count >= auth.MAX_LOGIN_ATTEMPTS:
                    locked_until = timeutil.utc_sql_after_minutes(auth.LOCKOUT_MINUTES)
                    c.execute(
                        "UPDATE app_user SET failed_login_count=0, locked_until=? WHERE id=?",
                        (locked_until, row["id"]),
                    )
                else:
                    c.execute(
                        "UPDATE app_user SET failed_login_count=? WHERE id=?",
                        (count, row["id"]),
                    )
            audit.log_login(c, body.username, row["role"] if row else None, success=False,
                             reason="invalid_credentials" if row else "unknown_user")
        raise HTTPException(401, "invalid credentials")
    with db.write_tx(c):
        if row["failed_login_count"] or row["locked_until"]:
            c.execute(
                "UPDATE app_user SET failed_login_count=0, locked_until=NULL WHERE id=?",
                (row["id"],),
            )
        audit.log_login(c, row["username"], row["role"], success=True)
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
    username: str, c: sqlite3.Connection = Depends(conn), p: dict = Depends(needs("user.manage")),
):
    """Admin-only emergency reset (auth.CAPABILITIES already scopes
    user.manage to admin). Generates a random password server-side --
    the admin never types, and therefore never has to invent or reuse, the
    credential this hands back -- and returns it exactly once, in this
    response body only. The password itself is never logged and never
    written anywhere else: if this response is lost, the only recovery is
    to reset again. The event itself (which admin reset which account,
    when) IS logged to admin_audit_log -- attribution for a sensitive
    account action does not require persisting the credential it produced.
    Flags must_change_password=1 so the go-live checklist can tell a
    still-temporary password apart from one the account holder actually
    chose."""
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
        audit.log(c, "user", username, "password_reset", p["sub"])
    return {
        "ok": True,
        "username": username,
        "temporary_password": temp_password,
        "note": "Shown once. Give this to the user directly — it is not stored or logged anywhere.",
    }


@app.get("/console/users", tags=["console"])
def list_users(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("user.manage")),
):
    """Every account and its role — never password_hash/password_salt.
    Admin-only, same capability as every other account-management action."""
    return {"users": rows(c.execute(
        "SELECT username, display_name, role, must_change_password, locked_until, is_active "
        "FROM app_user ORDER BY username"
    ))}


class CreateUserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    role: str


@app.post("/console/users", tags=["console"], status_code=201)
def create_user(
    body: CreateUserIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("user.manage")),
):
    """Provision a new operational staff account: cashier or kitchen (admin
    may also provision another admin). Mirrors reset_password's exact
    contract: a random, server-generated temp password, hashed the same way,
    returned exactly once in this response and never stored or logged
    anywhere else, with must_change_password=1 so the account holder must set
    their own password before reaching anything capability-gated.

    role='owner' is refused unconditionally, regardless of whether one
    already exists — OWNER is the business owner/CEO, not a staff role ADMIN
    provisions through account management. The only way an OWNER account is
    created is the deployment-only wt/owner_provisioning.py CLI, run once by
    whoever controls the deployment; there is deliberately no HTTP path to it
    at all, so no crafted request against this endpoint can reach it either.

    role='manager' is likewise refused: the current business roles are
    OWNER/ADMIN/CASHIER/KITCHEN, and ADMIN is now the sole administrative
    role. A legacy 'manager' account that already exists in the database
    keeps working (auth.CAPABILITIES and auth.ROLES both still recognize the
    role — nothing here revokes or converts it) — this endpoint only closes
    off creating any *new* one."""
    if body.role == "owner":
        raise PermissionError_(
            "OWNER accounts cannot be created through account management — "
            "OWNER is provisioned once, at deployment, outside this endpoint",
            capability="user.manage",
        )
    if body.role == "manager":
        raise PermissionError_(
            "'manager' is a legacy role and can no longer be assigned to a new "
            "account — create an admin, cashier, or kitchen account instead",
            capability="user.manage",
        )
    if body.role not in auth.ROLES:
        raise ValidationError(f"unknown role '{body.role}' — expected one of {auth.ROLES}")
    existing = c.execute(
        "SELECT 1 FROM app_user WHERE username=?", (body.username,)
    ).fetchone()
    if existing:
        raise HTTPException(409, f"username '{body.username}' is already in use")
    temp_password = auth.generate_temp_password()
    h, s = auth.hash_password(temp_password)
    with db.write_tx(c):
        c.execute(
            "INSERT INTO app_user "
            "(username, display_name, role, password_hash, password_salt, "
            "password_changed_at, must_change_password) "
            "VALUES (?,?,?,?,?, datetime('now','subsec'), 1)",
            (body.username, body.display_name, body.role, h, s),
        )
        audit.log(c, "user", body.username, "create", p["sub"], new_value=body.role)
    return {
        "ok": True,
        "username": body.username,
        "role": body.role,
        "temporary_password": temp_password,
        "note": "Shown once. Give this to the user directly — it is not stored or logged anywhere.",
    }


@app.get("/console/audit/logins", tags=["console"])
def audit_logins(
    limit: int = Query(100, ge=1, le=500),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("user.manage")),
):
    """Recent login attempts, successful and failed — migration 0017's
    login_event, written on every /auth/login call. Admin-only (same
    capability as user administration): who signed in, when, and whether
    it succeeded is account-security data, not general operational
    reading."""
    rows = c.execute(
        "SELECT username, role, success, reason, created_at FROM login_event "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {"events": [dict(r) for r in rows]}


@app.get("/console/audit/actions", tags=["console"])
def audit_actions(
    limit: int = Query(100, ge=1, le=500),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("user.manage")),
):
    """Recent sensitive mutations — migration 0016's admin_audit_log
    (ingredient edits, recipe creation, menu price changes, admin
    password resets). Admin-only, same reasoning as audit_logins above."""
    rows = c.execute(
        "SELECT entity_type, entity_id, action, field, previous_value, new_value, "
        "user_id, note, created_at FROM admin_audit_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {"events": [dict(r) for r in rows]}


# ===========================================================================
# owner instructions
# ===========================================================================
@app.get("/console/instructions", tags=["console"])
def list_instructions(
    status: str | None = Query(default=None),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("instruction.read")),
):
    """Owner and admin see the same shared task board."""
    return instructions.list_for_role(c, status)


class InstructionCreateIn(BaseModel):
    title: str
    body: str
    priority: Literal["normal", "important", "urgent"] = "normal"
    assigned_to: str | None = None
    due_date: str | None = None


@app.post("/console/instructions", tags=["console"], status_code=201)
def create_instruction(
    body: InstructionCreateIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("instruction.create")),
):
    return instructions.create(
        c, body.title, body.body, body.priority, body.assigned_to, body.due_date, p["sub"],
    )


@app.post("/console/instructions/{pk}/acknowledge", tags=["console"])
def acknowledge_instruction(
    pk: int, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("instruction.update_status")),
):
    return instructions.acknowledge(c, pk, p["sub"])


@app.post("/console/instructions/{pk}/start", tags=["console"])
def start_instruction(
    pk: int, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("instruction.update_status")),
):
    return instructions.start(c, pk, p["sub"])


class InstructionCompleteIn(BaseModel):
    completion_note: str


@app.post("/console/instructions/{pk}/complete", tags=["console"])
def complete_instruction(
    pk: int, body: InstructionCompleteIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("instruction.update_status")),
):
    return instructions.complete(c, pk, body.completion_note, p["sub"])


class InstructionCancelIn(BaseModel):
    reason: str | None = None


@app.post("/console/instructions/{pk}/cancel", tags=["console"])
def cancel_instruction(
    pk: int, body: InstructionCancelIn = Body(default=InstructionCancelIn()),
    c: sqlite3.Connection = Depends(conn), p: dict = Depends(needs("instruction.cancel")),
):
    """Reachable by both owner and admin (instruction.cancel lists both) —
    the finer rule (owner: only their own, only while 'open') is enforced
    inside instructions.cancel() itself, same shape as order.void."""
    return instructions.cancel(c, pk, body.reason, p["sub"], p["role"])


# ===========================================================================
# operational updates — the free-text, bidirectional half of Owner/Admin
# communication instructions' fixed state machine doesn't cover.
# ===========================================================================
@app.get("/console/updates", tags=["console"])
def list_updates(
    owner_instruction_id: int | None = Query(default=None),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("update.read")),
):
    return updates.list_all(c, owner_instruction_id)


class UpdateCreateIn(BaseModel):
    body: str
    owner_instruction_id: int | None = None


@app.post("/console/updates", tags=["console"], status_code=201)
def create_update(
    body: UpdateCreateIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("update.create")),
):
    return updates.create(c, body.body, body.owner_instruction_id, p["sub"], p["role"])


@app.post("/console/updates/{pk}/acknowledge", tags=["console"])
def acknowledge_update(
    pk: int, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("update.acknowledge")),
):
    """Reachable by both roles — updates.acknowledge() itself refuses when
    the caller's role matches whoever posted the update, same shape as
    instruction cancellation above."""
    return updates.acknowledge(c, pk, p["sub"], p["role"])


# ===========================================================================
# purchase orders
# ===========================================================================
@app.get("/console/purchase-orders", tags=["console"])
def list_purchase_orders(
    status: str | None = Query(default=None),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("purchase_order.read")),
):
    return purchasing.list_all(c, status)


@app.get("/console/purchase-orders/{pk}", tags=["console"])
def get_purchase_order(
    pk: int, c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("purchase_order.read")),
):
    return purchasing.get(c, pk)


class PurchaseOrderItemIn(BaseModel):
    ingredient_id: int
    quantity: float = Field(gt=0)
    recommended_qty_base: float | None = None
    note: str | None = None


class PurchaseOrderCreateIn(BaseModel):
    supplier: str | None = None
    notes: str | None = None
    expected_delivery_date: str | None = None
    items: list[PurchaseOrderItemIn]


@app.post("/console/purchase-orders", tags=["console"], status_code=201)
def create_purchase_order(
    body: PurchaseOrderCreateIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("purchase_order.create")),
):
    return purchasing.create(
        c, body.supplier, body.notes, body.expected_delivery_date,
        [item.model_dump() for item in body.items], p["sub"],
    )


@app.post("/console/purchase-orders/{pk}/order", tags=["console"])
def order_purchase_order(
    pk: int, c: sqlite3.Connection = Depends(conn), p: dict = Depends(needs("purchase_order.manage")),
):
    return purchasing.mark_ordered(c, pk, p["sub"])


class PurchaseOrderCancelIn(BaseModel):
    reason: str | None = None


@app.post("/console/purchase-orders/{pk}/cancel", tags=["console"])
def cancel_purchase_order(
    pk: int, body: PurchaseOrderCancelIn = Body(default=PurchaseOrderCancelIn()),
    c: sqlite3.Connection = Depends(conn), p: dict = Depends(needs("purchase_order.cancel")),
):
    """Reachable by both owner and admin — see purchasing.cancel() for the
    finer per-role rule, same shape as instruction cancellation above."""
    return purchasing.cancel(c, pk, body.reason, p["sub"], p["role"])


class ReceiveLineIn(BaseModel):
    qty_base: float = Field(gt=0)
    lot_no: str | None = None
    expires_at: str | None = None
    client_request_id: str | None = Field(default=None, max_length=64)


@app.post("/console/purchase-orders/items/{item_pk}/receive", tags=["console"])
def receive_purchase_order_line(
    item_pk: int, body: ReceiveLineIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("purchase_order.manage")),
):
    """The only endpoint in the purchasing module that ever touches
    inventory — see purchasing.receive_line for the transaction/idempotency
    design. client_request_id makes a retried submission safe to repeat."""
    return purchasing.receive_line(
        c, item_pk, body.qty_base, p["sub"], body.client_request_id,
        body.lot_no, body.expires_at,
    )


@app.get("/console/capabilities", tags=["console"])
def capabilities(p: dict = Depends(principal)):
    """A live read of auth.CAPABILITIES -- the actual policy the needs()
    gate enforces on every other route -- for System & Settings' "Who can
    do what" reference table.

    2026-09 contract fix: this table used to be hand-transcribed in the
    frontend (lib/api/system.js's old CAPABILITY_REFERENCE), with its own
    comment recording that a PRIOR transcription had already drifted out of
    step with CAPABILITIES and asking for exactly this endpoint so the
    table could stop being copied by hand. No signed-in role is excluded --
    everyone benefits from seeing the real policy, not just admin -- so
    this only requires a valid session, not a capability of its own.

    Returned keyed by the raw capability string (e.g. "order.create"), not
    a hand-picked human label -- inventing prose grouping here would be
    the exact kind of second copy of the policy this endpoint exists to
    stop maintaining. Presentation is the frontend's job."""
    return {
        "roles": list(auth.ROLES),
        "capabilities": {cap: list(allowed) for cap, allowed in auth.CAPABILITIES.items()},
    }


@app.get("/console/permissions/pages", tags=["console"])
def my_page_permissions(c: sqlite3.Connection = Depends(conn), p: dict = Depends(principal)):
    """What the CALLING role can currently see — no capability required
    beyond being signed in, since every role needs to be able to ask this
    about itself to build its own sidebar. Distinct from the admin-only
    endpoints below, which read/write every role's grants."""
    return {"role": p["role"], "grants": permissions.get_effective_grants(c, p["role"])}


@app.get("/console/admin/permissions", tags=["console"])
def admin_permissions(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("permissions.manage")),
):
    """The full editable matrix for the Role & Permissions section of System
    & Settings — every role wt/permissions.DEFAULT_GRANTS knows how to
    configure (today, just 'manager')."""
    return {"pageKeys": list(permissions.PAGE_KEYS), "grants": permissions.get_all_grants(c)}


class SetPagePermissionIn(BaseModel):
    role: str
    page_key: str
    allowed: bool


@app.put("/console/admin/permissions", tags=["console"])
def set_page_permission(
    body: SetPagePermissionIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("permissions.manage")),
):
    """Grant or revoke one role's access to one console page. Audited the
    same way every other admin mutation is (admin_audit_log via audit.log,
    see wt/permissions.set_grant) — who changed what a role can see, and
    when, is exactly the kind of record this table already keeps for
    ingredient edits and password resets."""
    with db.write_tx(c):
        grants = permissions.set_grant(c, body.role, body.page_key, body.allowed, p["sub"])
    return {"role": body.role, "grants": grants}


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
    svc_enabled, svc_rate = _effective_service_charge(c)
    enabled_discounts = _enabled_discount_types(c)
    return {
        "items": data,
        "sellable": sum(1 for d in data if d["sellable"]),
        "blocked": sum(1 for d in data if not d["sellable"]),
        # The POS shows a running total before checkout (subtotal, service
        # charge, change preview) — it needs this rate to do that, and it
        # must be this one, not a second copy hardcoded in JavaScript that
        # could silently drift from what checkout actually charges. Menu
        # load is the one call every POS session makes before any of that
        # math happens, so it rides along here rather than needing its own
        # endpoint. The rate is already 0 when service charge is disabled —
        # the till never needs to know *why*, only what to charge.
        "service_charge_rate": svc_rate,
        "service_charge_enabled": svc_enabled,
        # Same reasoning as service_charge_rate above: the discount buttons on
        # the till preview an amount before checkout, and that preview has to
        # use the exact rate checkout will charge, not a second copy that
        # could drift from DISCOUNT_RATES. Only types an admin has left
        # enabled are offered — see _enabled_discount_types().
        "discount_rates": {t: r for t, r in DISCOUNT_RATES.items() if t in enabled_discounts},
        "discount_labels": {t: l for t, l in DISCOUNT_LABELS.items() if t in enabled_discounts},
        # The restaurant's configured seating — same reasoning as the two
        # rates above, read once at menu load rather than a copy the till
        # could drift from. Defaults to DEFAULT_TABLE_COUNT (9) until an
        # admin configures it via PATCH /console/pos-config.
        "table_count": int(unli.get_setting(c, "pos_table_count", DEFAULT_TABLE_COUNT)),
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
    # The till only ever sends which discount card the customer presented —
    # never a rate or an amount. The percentage that means is decided here,
    # server-side, from DISCOUNT_RATES, so a compromised or buggy client
    # cannot charge a rate it made up.
    discount_type: Literal["senior", "pwd", "staff"] | None = None


# Fallback rate an admin gets the first time they flip service charge on
# without also choosing a rate. Wings Theory currently runs with NO service
# charge at all — see _effective_service_charge() below, which is what
# create_order and GET /pos/menu actually use. This constant is never
# applied on its own any more.
SERVICE_CHARGE_RATE = 0.10

# RA 9994 (senior citizens) and RA 10754 (PWD) both mandate 20% off the sale
# price before service charge/VAT; staff is a house courtesy discount at half
# that rate. Applied to the subtotal, then service charge is computed on
# what's left — a discounted order is never charged service on the amount it
# didn't actually pay. These are the rates in effect when an admin has not
# disabled a discount type — see _enabled_discount_types() below.
DISCOUNT_RATES = {"senior": 0.20, "pwd": 0.20, "staff": 0.10}
DISCOUNT_LABELS = {"senior": "SC 20%", "pwd": "PWD 20%", "staff": "Staff 10%"}


# ---------------------------------------------------------------------------
# POS billing configuration — service charge on/off + rate, and which
# discount types a cashier may select. Stored in app_setting (wt/unli.py's
# get_setting/set_setting, already used for depletion_demand_model and
# unli_variance_tolerance) rather than a new table. Defaults preserve the
# actual current business rule: service charge OFF, every discount type
# enabled at its existing rate — installing this changes nothing until an
# admin deliberately opts in via PATCH /console/pos-config.
# ---------------------------------------------------------------------------
def _effective_service_charge(c: sqlite3.Connection) -> tuple[bool, float]:
    enabled = unli.get_setting(c, "service_charge_enabled", 0.0) >= 1.0
    rate = unli.get_setting(c, "service_charge_rate", SERVICE_CHARGE_RATE) if enabled else 0.0
    return enabled, rate


def _enabled_discount_types(c: sqlite3.Connection) -> set[str]:
    return {
        t for t in DISCOUNT_RATES
        if unli.get_setting(c, f"discount_{t}_enabled", 1.0) >= 1.0
    }


class _DuplicateCheckout(Exception):
    """Internal marker: this checkout key already has an order. Raised inside
    the write transaction so it rolls back, then handled outside it."""


def _find_order_by_key(c: sqlite3.Connection, key: str | None):
    if not key:
        return None
    return c.execute(
        "SELECT id, order_no, subtotal, discount_type, discount_amount, service_charge, "
        "total, cash_received, change_due FROM customer_order WHERE client_request_id = ?",
        (key,),
    ).fetchone()


def _replay_payload(row) -> dict:
    """What a repeated checkout gets back: the original sale, flagged as a
    replay so the till can say so instead of implying a second order."""
    return {
        "order_id": row["id"], "order_no": row["order_no"],
        "subtotal": row["subtotal"],
        "discount_type": row["discount_type"], "discount_amount": row["discount_amount"],
        "service_charge": row["service_charge"],
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
            prefix = f"WTRORD-{_date.today().year}"
            # MAX over the existing numeric suffix, not COUNT(*): gap-safe the
            # same way the old WT-<n> scheme was (see demo_seed._new_order),
            # and immune to any row from an earlier year still being present.
            n = c.execute(
                "SELECT COALESCE(MAX(CAST(SUBSTR(order_no, LENGTH(?) + 1) AS INTEGER)), 0) AS n "
                "FROM customer_order WHERE order_no LIKE ?",
                (prefix, prefix + "%"),
            ).fetchone()["n"]
            order_no = f"{prefix}{n + 1:06d}"
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
                # Unlimited Wings is priced per head (₱399/head, not a flat
                # ₱399 regardless of how many are eating) — head_count=0 would
                # silently punch a ₱0 unlimited line and open no session (the
                # session guard just below only fires when head_count > 0), so
                # it is refused here rather than producing a free ticket.
                if item["is_unli"] and body.head_count < 1:
                    raise HTTPException(
                        422,
                        f"'{item['name']}' is priced per head — set Heads to at least 1",
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
                # An unlimited line's commercial quantity is always 1 (guarded
                # above) — what it's actually charged for is the number of
                # diners it covers, so price is per head_count, not per qty.
                # An ordinary line is charged per its own qty as always.
                subtotal += item["price"] * (body.head_count if item["is_unli"] else ln.qty)

            if body.discount_type and body.discount_type not in _enabled_discount_types(c):
                raise HTTPException(
                    422, f"'{body.discount_type}' discount is not currently enabled"
                )
            discount_amount = (
                round(subtotal * DISCOUNT_RATES[body.discount_type], 2)
                if body.discount_type else 0.0
            )
            discounted_subtotal = round(subtotal - discount_amount, 2)
            _svc_enabled, svc_rate = _effective_service_charge(c)
            service_charge = round(discounted_subtotal * svc_rate, 2)
            total = round(discounted_subtotal + service_charge, 2)
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
                "UPDATE customer_order SET subtotal=?, discount_type=?, discount_amount=?, "
                "service_charge=?, total=?, cash_received=?, change_due=? WHERE id=?",
                (round(subtotal, 2), body.discount_type, discount_amount,
                 service_charge, total, body.cash_received, change_due, order_pk),
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
        "subtotal": round(subtotal, 2),
        "discount_type": body.discount_type, "discount_amount": discount_amount,
        "service_charge": service_charge, "total": total,
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
    row = c.execute("SELECT status, source FROM customer_order WHERE id=?", (order_pk,)).fetchone()
    if not row:
        raise HTTPException(404, "order not found")
    if row["source"] == "historical":
        raise HTTPException(
            409, "a historical sale cannot be voided — it is a read-only record of a completed past sale"
        )
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
    punch time — this is a read of that record, not a second calculation.

    total stays the frozen, settled figure (migration 0011's
    trg_order_financials_immutable never lets it move). current_total adds
    in whatever has posted to order_charge since — served refill add-ons —
    the same arithmetic v_unli_session_summary and refills.order_receipt()
    already use for the receipt/"View order" path. Without this, a table
    that took a ₱50 special-flavor add-on after checkout showed the correct
    ₱489 on the receipt but a stale ₱439 in this list — the exact
    History/Current-Orders inconsistency this join closes.

    status="all" (the till's Order History screen) is additionally capped
    to a rolling last-3-Manila-calendar-months window -- a query-level
    restriction only, not a data deletion; the full ledger stays in the
    database and is reachable without this cap from the admin console's
    own order-history route."""
    sql = ("SELECT o.id, o.order_no, o.status, o.table_label, o.head_count, o.created_at, "
           "o.committed_at, o.served_at, o.voided_at, o.subtotal, o.discount_type, "
           "o.discount_amount, o.service_charge, o.total, "
           "o.total + COALESCE(ch.addon_total, 0) AS current_total, "
           "o.cash_received, o.change_due, o.source, o.source_order_id, "
           "(SELECT GROUP_CONCAT(m.name, ', ') FROM order_line ol "
           "   JOIN menu_item m ON m.id = ol.menu_item_id WHERE ol.order_id = o.id) AS item_names "
           "FROM customer_order o "
           "LEFT JOIN (SELECT order_id, SUM(amount) AS addon_total FROM order_charge "
           "           GROUP BY order_id) ch ON ch.order_id = o.id ")
    params: list = []
    if status == "open":
        sql += "WHERE o.status IN ('punched','in_progress','ready') "
    else:
        sql += "WHERE o.created_at >= ? "
        params.append(timeutil.utc_bound_n_manila_months_ago(3))
    sql += "ORDER BY o.created_at DESC LIMIT ?"
    params.append(limit)
    orders = rows(c.execute(sql, params))
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
        "committed_at, served_at, voided_at, void_reason, subtotal, discount_type, "
        "discount_amount, service_charge, total, "
        "cash_received, change_due, source, source_order_id FROM customer_order WHERE id=?",
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
            SELECT ol.qty, ol.note, m.name, m.is_unli, m.category
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


class ReceiveIn(BaseModel):
    purchase_qty: float = Field(gt=0, description="in the ingredient's own purchase unit")
    lot_no: str | None = None
    expires_at: str | None = Field(default=None, description="YYYY-MM-DD, if known")


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


class VarianceReviewIn(BaseModel):
    reason: Literal[
        "waste", "portion_variance", "counting_error",
        "recipe_yield_issue", "receiving_issue", "other",
    ]
    note: str | None = None
    client_request_id: str | None = Field(default=None, max_length=64)


@app.post("/console/ingredients/{pk}/variance-review", tags=["console"])
def review_ingredient_variance(
    pk: int, body: VarianceReviewIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("inventory.write")),
):
    """Record a human's suspected cause for a repeated physical-count
    variance — waste, portion drift, a miscount, a recipe/yield issue, or a
    receiving problem. This is a REVIEW, not a correction: it never touches
    on_hand_base, stock_ledger, or any recipe/ingredient value. Pack size,
    yield factor, and recipe quantity corrections stay on /verify, exactly
    where they already are — see reservation.record_variance_review."""
    return record_variance_review(
        c, pk, body.reason, body.note, user_id=p["sub"],
        client_request_id=body.client_request_id,
    )


@app.get("/console/ingredients/{pk}/variance-review", tags=["console"])
def ingredient_variance_review_history(
    pk: int, c: sqlite3.Connection = Depends(conn),
    _p: dict = Depends(needs("inventory.read")),
):
    """Every review ever recorded for this ingredient, oldest first — the
    provenance trail behind whatever "Reviewed — ..." line the reconciliation
    view is currently showing for it."""
    return {"ingredient_id": pk, "reviews": variance_review_history(c, pk)}


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
    # Pydantic's plain `float` accepts Infinity (ge=0/gt=0 alone lets it
    # through -- inf >= 0 and inf > 0 are both true; only NaN fails those
    # comparisons) -- the same gap verification.validate_numeric_value
    # guards elsewhere. Checked explicitly rather than as a pydantic
    # field_validator: FastAPI's default RequestValidationError handler
    # echoes the raw rejected value back in the response body, and
    # Starlette's JSONResponse refuses to serialize a non-finite float
    # (allow_nan=False) -- raising ValidationError here instead reaches
    # this project's own WTError handler, which never echoes raw input.
    for field in ("unit_cost", "reorder_point_base", "max_stock_base"):
        if field in fields and not math.isfinite(fields[field]):
            raise ValidationError(f"{field} must be a finite number")
    set_sql = ", ".join(f"{k} = :{k}" for k in fields)
    if "unit_cost" in fields:
        set_sql += ", unit_cost_status = 'verified'"
    params = {**fields, "id": pk}
    with db.write_tx(c):
        before = c.execute(
            f"SELECT {', '.join(fields)} FROM ingredient WHERE id=?", (pk,)
        ).fetchone()
        if not before:
            raise HTTPException(404, "ingredient not found")
        # Millisecond precision -- see verification.resolve()'s identical
        # note. This edit (reorder_point_base, lead_time_days, pack_size,
        # etc.) writes no stock_ledger/customer_order row, so
        # _depletion_fingerprint() keys on ingredient.updated_at alone to
        # detect it; datetime('now')'s bare-second resolution can collide
        # with a same-second commit_order write (which also bumps this
        # column, at far higher frequency) and leave the forecast cache
        # stale.
        cur = c.execute(
            f"UPDATE ingredient SET {set_sql}, "
            f"updated_at = strftime('%Y-%m-%d %H:%M:%f','now') WHERE id = :id",
            params,
        )
        if cur.rowcount != 1:
            raise HTTPException(404, "ingredient not found")
        for k, new_value in fields.items():
            audit.log(c, "ingredient", pk, "edit", p["sub"],
                       field=k, previous_value=before[k], new_value=new_value)
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
    p: dict = Depends(needs("recipe.write")),
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
        audit.log(c, "recipe", pk, "create", p["sub"],
                   new_value=body.name, note=f"{len(body.lines)} line(s)")
    problems = validate_recipe(c, pk)
    return {"id": pk, "problems": problems}


class MenuPriceIn(BaseModel):
    price: float = Field(gt=0)


@app.patch("/console/menu-items/{pk}/price", tags=["console"])
def edit_menu_price(
    pk: int, body: MenuPriceIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("recipe.write")),
):
    """The only console-writable field on a menu item today. Everything
    else about it (what it's made of) is edited through its recipe."""
    with db.write_tx(c):
        before = c.execute("SELECT price FROM menu_item WHERE id=?", (pk,)).fetchone()
        if not before:
            raise HTTPException(404, "menu item not found")
        cur = c.execute(
            "UPDATE menu_item SET price = ?, updated_at = datetime('now') WHERE id = ?",
            (body.price, pk),
        )
        if cur.rowcount != 1:
            raise HTTPException(404, "menu item not found")
        audit.log(c, "menu_item", pk, "price_change", p["sub"],
                   field="price", previous_value=before["price"], new_value=body.price)
    row = c.execute("SELECT id, name, price FROM menu_item WHERE id=?", (pk,)).fetchone()
    return dict(row)


# ===========================================================================
# Console — the two admin queues
# ===========================================================================
@app.get("/console/queues/unmapped", tags=["queues"])
def queue_unmapped(
    state: str = "open", c: sqlite3.Connection = Depends(conn),
    _p: dict = Depends(needs("queue.read")), _pg: dict = Depends(needs_page("verify")),
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
    _pg: dict = Depends(needs_page("verify")),
):
    """Every physical constant still carrying a seeded placeholder, translated
    into plain operational language and grouped by what breaks if it's
    wrong. See wt/verification.py."""
    return verification.list_queue(c)


@app.get("/console/queues/verification/history", tags=["queues"])
def queue_verification_history(
    field: str, row_id: int,
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("queue.read")),
    _pg: dict = Depends(needs_page("verify")),
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
def readiness(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
    _pg: dict = Depends(needs_page("system")),
):
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
    # Same WHERE conditions as the row query above (minus LIMIT/OFFSET) --
    # a `reason` filter must narrow the page-count total exactly as it
    # narrows the rows themselves, or "Page X of Y" undercounts the pages
    # whenever a reason filter is active.
    count_sql = "SELECT COUNT(*) AS n FROM stock_ledger WHERE 1=1"
    count_params: list[Any] = []
    if ingredient_id:
        count_sql += " AND ingredient_id = ?"
        count_params.append(ingredient_id)
    if reason:
        count_sql += " AND reason = ?"
        count_params.append(reason)
    total = c.execute(count_sql, count_params).fetchone()["n"]
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


@app.get("/console/reconciliation/ingredients", tags=["console"])
def ingredient_reconciliation(
    days: int = Query(30, ge=1, le=365),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Recipe/wastage-driven consumption vs. what physical counts actually
    found, per ingredient, over the window. See
    reservation.ingredient_reconciliation_report — this is the general-
    ingredient counterpart to /unli/reconciliation/pending, which already
    does the same expected-vs-actual comparison for unlimited-refill
    streams specifically."""
    return ingredient_reconciliation_report(c, days)


@app.get("/console/reports/sales", tags=["console"])
def sales_summary(
    days: int = Query(30, description="look-back window (ignored when start_date or all_time is given)"),
    start_date: str | None = Query(default=None, description="Manila business date YYYY-MM-DD, inclusive"),
    end_date: str | None = Query(
        default=None, description="Manila business date YYYY-MM-DD, inclusive; defaults to today when start_date is given",
    ),
    all_time: bool = Query(default=False, description="every order ever recorded — no lower date bound"),
    preset: Literal["today", "yesterday", "this_week", "this_month"] | None = Query(
        default=None, description="A named Manila calendar window — an alternative to start_date/end_date/days "
        "so a client never has to reimplement 'what day is today in Manila' itself",
    ),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Real numbers only — this reads customer_order/order_line directly, so
    on a system with only a handful of test orders the totals will be small.
    No synthetic history is generated to make the chart look busier.

    Four mutually exclusive ways to pick the window, checked in this order:
      1. all_time=true            — every order ever recorded, no lower bound.
      2. start_date (+ end_date)  — an explicit Manila-business-date range,
         inclusive on both ends. end_date defaults to today when omitted.
      3. preset                   — a named Manila calendar window:
         'today'/'yesterday' (a single closed business day, both bounds set —
         NOT the same as days=1, which is open-ended through the current
         instant) or 'this_week'/'this_month' (Monday-of-this-Manila-week, or
         the 1st of this Manila month, through today). Kept server-side
         (never left to the client to derive from a preset id) so "what day
         is today in Manila" has exactly the one home timeutil.py's own
         docstring already claims for every other business-date calculation.
      4. days (default 30)        — the original trailing-window behaviour,
         unchanged for every existing caller that only ever passed `days`.
         May be negative (a window into the future, matching nothing) — used
         by tests to get a deterministically-empty result.
    response echoes back the resolved range as `start_date`/`end_date` (or
    null/null for all_time) so a client can label a preset or custom range
    without re-deriving the same Manila-calendar arithmetic client-side.

    Every bound is computed once in Python (Manila calendar-day aligned)
    rather than left to SQLite's own `datetime('now', ?)`, and calendar-day
    grouping below uses the Manila business date, not the UTC one — see
    wt/timeutil.py.
    """
    upper_bound: str | None = None
    if all_time:
        bound = "0001-01-01 00:00:00"
        window_days_out: int | None = None
        range_start, range_end = None, timeutil.business_today_iso()
    elif start_date:
        try:
            start_d = _date.fromisoformat(start_date)
        except ValueError:
            raise ValidationError("start_date must be an ISO date (YYYY-MM-DD)")
        if end_date:
            try:
                end_d = _date.fromisoformat(end_date)
            except ValueError:
                raise ValidationError("end_date must be an ISO date (YYYY-MM-DD)")
        else:
            end_d = timeutil.business_today()
        if end_d < start_d:
            raise ValidationError("end_date cannot be before start_date")
        bound = timeutil.utc_bound_for_manila_date(start_d.isoformat())
        upper_bound = timeutil.utc_bound_for_manila_date((end_d + _timedelta(days=1)).isoformat())
        window_days_out = (end_d - start_d).days + 1
        range_start, range_end = start_d.isoformat(), end_d.isoformat()
    elif preset:
        today = timeutil.business_today()
        if preset == "today":
            start_d = end_d = today
        elif preset == "yesterday":
            start_d = end_d = today - _timedelta(days=1)
        elif preset == "this_week":
            start_d, end_d = today - _timedelta(days=today.weekday()), today
        else:  # this_month
            start_d, end_d = today.replace(day=1), today
        bound = timeutil.utc_bound_for_manila_date(start_d.isoformat())
        # today/yesterday are single closed days and need a real upper bound
        # too (an order from today must not leak into 'yesterday'); the
        # week/month presets stay open-ended through the current instant,
        # matching every other "...to date" window in this endpoint.
        if preset in ("today", "yesterday"):
            upper_bound = timeutil.utc_bound_for_manila_date((end_d + _timedelta(days=1)).isoformat())
        window_days_out = (end_d - start_d).days + 1
        range_start, range_end = start_d.isoformat(), end_d.isoformat()
    else:
        bound = timeutil.utc_bound_n_manila_days_ago(days)
        window_days_out = days
        range_end = timeutil.business_today_iso()
        range_start = timeutil.business_date_n_days_ago(days - 1) if days > 0 else None

    upper_clause = " AND o.created_at < :upper" if upper_bound is not None else ""
    params: dict[str, Any] = {"bound": bound, "upper": upper_bound}
    totals = c.execute(
        f"""
        SELECT COUNT(*) AS orders, COALESCE(SUM(o.head_count), 0) AS covers
          FROM customer_order o
         WHERE o.status != 'void' AND o.created_at >= :bound{upper_clause}
        """,
        params,
    ).fetchone()
    revenue = c.execute(
        f"""
        SELECT COALESCE(SUM(ol.qty * ol.unit_price), 0) AS revenue
          FROM order_line ol JOIN customer_order o ON o.id = ol.order_id
         WHERE o.status != 'void' AND o.created_at >= :bound{upper_clause}
        """,
        params,
    ).fetchone()["revenue"]
    by_item = rows(c.execute(
        f"""
        SELECT m.name, SUM(ol.qty) AS qty_sold, SUM(ol.qty * ol.unit_price) AS revenue
          FROM order_line ol
          JOIN customer_order o ON o.id = ol.order_id
          JOIN menu_item m ON m.id = ol.menu_item_id
         WHERE o.status != 'void' AND o.created_at >= :bound{upper_clause}
         GROUP BY m.id ORDER BY revenue DESC
        """,
        params,
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
         WHERE o.status != 'void' AND o.created_at >= :bound{upper_clause}
         GROUP BY {day_bd} ORDER BY day
        """,
        params,
    ))
    # Voided orders stay in this list on purpose (audit visibility — seeing
    # "this one was voided" is useful in a report) but were missing the same
    # date-window filter as everything else above, so this returned the last
    # 50 orders ever, unconditionally, no matter what `days` was asked for.
    recent = rows(c.execute(
        f"""
        SELECT o.id, o.order_no, o.status, o.table_label, o.created_at,
               (SELECT COALESCE(SUM(ol2.qty*ol2.unit_price),0) FROM order_line ol2
                 WHERE ol2.order_id=o.id) AS total
          FROM customer_order o
         WHERE o.created_at >= :bound{upper_clause}
         ORDER BY o.created_at DESC LIMIT 50
        """,
        params,
    ))
    return {
        "window_days": window_days_out,
        "start_date": range_start,
        "end_date": range_end,
        "all_time": all_time,
        "orders": totals["orders"],
        "covers": totals["covers"],
        "revenue": round(revenue, 2),
        "by_item": by_item,
        "by_day": by_day,
        "recent_orders": recent,
    }


@app.get("/reports/valuation", tags=["console"])
def reports_valuation(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """2026-08 audit: the console's Reports page and Inventory On Hand page
    both already had a fully-wired client waiting on this exact endpoint,
    falling back to a labeled "not available" state in the meantime. See
    reservation.inventory_valuation — only unit_cost_status='verified'
    ingredients are summed."""
    return inventory_valuation(c)


def _forecast_preview(c: sqlite3.Connection, horizon_days: int = 7, lookback_days: int = 60,
                       top_n: int = 5) -> dict:
    """Today's 'Demand Forecast' mini-panel: total forecasted units/day,
    summed across the shop's top-selling menu items, for the next
    `horizon_days`. Restored 2026-09 after a dashboard_summary() rewrite
    dropped this field while Today.js still read it (contract-audit
    finding) -- reuses menu_item_demand()'s existing cheap trailing-average
    method unmodified (the same one /console/forecasts/demand's bulk list
    already runs for every POS-visible item), so this adds no new
    forecasting logic, just a small, bounded consumer of it."""
    top_items = rows(c.execute(
        """
        SELECT m.id AS id
          FROM order_line ol
          JOIN customer_order o ON o.id = ol.order_id
          JOIN menu_item m ON m.id = ol.menu_item_id
         WHERE o.status != 'void' AND m.pos_visible = 1
         GROUP BY m.id
         ORDER BY SUM(ol.qty) DESC
         LIMIT ?
        """,
        (top_n,),
    ))
    if not top_items:
        return {
            "insufficientData": True, "daysOfHistory": 0, "requiredDays": MIN_DEMAND_SAMPLE_DAYS,
            "labels": [], "values": [], "note": "No completed orders yet.",
        }

    results = [menu_item_demand(c, it["id"], horizon_days, lookback_days) for it in top_items]
    usable = [r for r in results if not r["insufficient_data"]]

    if not usable:
        best_days = max((r["active_days"] for r in results), default=0)
        return {
            "insufficientData": True, "daysOfHistory": best_days, "requiredDays": MIN_DEMAND_SAMPLE_DAYS,
            "labels": [], "values": [],
            "note": f"The closest menu item has {best_days} day(s) on file.",
        }

    labels = [p["date"] for p in usable[0]["forecast"]]
    values = [round(sum(r["forecast"][i]["qty"] for r in usable), 2) for i in range(len(labels))]
    return {
        "insufficientData": False,
        "daysOfHistory": max(r["active_days"] for r in usable),
        "requiredDays": MIN_DEMAND_SAMPLE_DAYS,
        "labels": labels, "values": values,
        "note": None,
    }


@app.get("/dashboard/summary", tags=["console"])
def dashboard_summary(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """The Today screen's single aggregation payload.

    Composes entirely from functions/queries every other endpoint already
    uses (ingredient_forecast, _annotate_batch_expiry, v_menu_item_
    availability, inventory_valuation, verification.list_queue). It derives
    no business meaning of its own -- it aggregates already-authoritative
    sources into the one shaped payload the console's Today route reads.

    2026-09 frontend rebuild. Three things changed here, all additive to the
    data model and none of them touching an algorithm:

    1. THE 'BELOW REORDER POINT' KPI IS GONE, and is not replaced by a
       silent equivalent. It read "0 -- every item has a buffer, severity
       good" against a catalog where reorder_point_base is NULL for 131 of
       131 ingredients. Nothing was below its reorder point because nothing
       HAS one; the KPI turned an unconfigured field into a clean bill of
       health on the first screen anyone sees. The replacement, "Need
       restock", counts the forward depletion engine's out/critical/at_risk
       rows -- a real signal that exists today -- and `coverage.
       reorderPointsConfigured` is reported alongside it so the UI can say
       out loud which basis it is using and that the other one is unset.

    2. DATA FRESHNESS IS REPORTED. Every demand-derived number here is built
       from orders that stop at MAX(customer_order.created_at), which on the
       live database is several days behind the reader's today. The payload
       now carries that timestamp (plus the ledger's and the last receiving)
       so a purchasing decision cannot be taken from this screen without the
       age of its evidence being visible.

    3. QUANTITIES ARE STRUCTURED. Every quantity that used to ship only as a
       pre-formatted string ("4000 g") now also ships as {qty, unit}. The
       display string is kept for compatibility, but the console reads the
       structured pair, so unit presentation is a frontend decision made
       once rather than a string parsed back apart at each call site.
    """
    # ---- expiry (same query/annotation expiry_monitor uses) ----
    raw_batches = rows(c.execute(
        """
        SELECT b.id, b.lot_no, b.qty_base, b.received_at, b.expires_at, b.expiry_status,
               i.id AS ingredient_id, i.name AS ingredient_name, i.base_unit,
               i.expiry_mode, i.shelf_life_days
          FROM fefo_batch b JOIN ingredient i ON i.id = b.ingredient_id
         WHERE b.state = 'open' AND b.qty_base > 0
           AND (b.expires_at IS NOT NULL OR i.expiry_mode = 'shelf_life_estimate')
        """
    ))
    batches = [a for a in (_annotate_batch_expiry(b) for b in raw_batches) if a is not None]
    batches.sort(key=lambda x: x["days_to_expiry"])
    expired = [b for b in batches if b["expired"]]
    expiring_soon = [b for b in batches if not b["expired"] and b["days_to_expiry"] <= 7]

    def expiry_severity(days: int) -> str:
        if days <= 2: return "crit"
        if days <= 7: return "warn"
        if days <= 30: return "info"
        return "good"

    def expiry_label(days: int) -> str:
        if days < 0: return "expired"
        if days < 1: return "today"
        return f"{days}d"

    def qty_pair(value, unit) -> dict:
        """The structured half of point 3 above. `display` stays for any
        client that still wants a ready-made string; `qty`/`unit` are what
        the console actually formats from."""
        return {"qty": value, "unit": unit, "display": f"{value:g} {unit}"}

    # ---- forward depletion engine (same source + cache key Purchasing reads,
    # so the two screens cannot disagree about which ingredients need buying) --
    fc_key = _depletion_fingerprint(c) + ("ingredients", DEPLETION_HORIZON_DAYS, DEPLETION_LOOKBACK_DAYS)
    ing_forecast = _forecast_cache.get_or_compute(
        fc_key, lambda: ingredient_forecast(c, DEPLETION_HORIZON_DAYS, DEPLETION_LOOKBACK_DAYS,
                                            demand_model=_demand_model(c))
    )
    fc_rows = ing_forecast["ingredients"]
    buckets = ing_forecast["buckets"]
    # "Needs restock" = the engine's own urgent tiers. Not a reorder-point
    # comparison: see point 1 above for why that KPI could not be honest.
    need_restock = sum(buckets.get(k, 0) for k in ("out", "critical", "at_risk"))
    running_low = buckets.get("at_risk", 0)

    # ---- unsellable menu items (same view /pos/menu reads) ----
    menu_avail = rows(c.execute(
        "SELECT name, sellable, block_reason FROM v_menu_item_availability"
    ))
    unsellable = [m for m in menu_avail if not m["sellable"]]

    # ---- stock value (shared with GET /reports/valuation) ----
    valuation = inventory_valuation(c)

    # ---- verification queue (same source the Data Quality page reads) ----
    verify_queue = verification.list_queue(c)
    verify_all = verify_queue["items"]

    # ---- master-data coverage: what share of the catalog each figure rests on --
    cov = c.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN reorder_point_base IS NOT NULL THEN 1 ELSE 0 END) AS rop,
               SUM(CASE WHEN supplier IS NOT NULL AND TRIM(supplier) <> '' THEN 1 ELSE 0 END) AS supplier,
               SUM(CASE WHEN pack_size IS NULL OR pack_size <= 0 THEN 1 ELSE 0 END) AS no_pack,
               SUM(CASE WHEN unit_cost IS NULL THEN 1 ELSE 0 END) AS no_cost,
               COUNT(DISTINCT TRIM(purchase_unit)) AS purchase_unit_spellings
          FROM ingredient WHERE is_active=1
        """
    ).fetchone()

    # ---- freshness: the age of the evidence behind everything above -------
    latest_order = c.execute(
        "SELECT MAX(created_at) AS t FROM customer_order WHERE status != 'void'"
    ).fetchone()["t"]
    latest_ledger = c.execute("SELECT MAX(created_at) AS t FROM stock_ledger").fetchone()["t"]
    latest_receipt = c.execute(
        "SELECT MAX(created_at) AS t FROM stock_ledger WHERE reason='purchase_receipt'"
    ).fetchone()["t"]

    freshness = {
        # Demand, and therefore every runway and purchase quantity on this
        # screen, cannot be more current than this.
        "latestOrderAt": latest_order,
        "latestLedgerAt": latest_ledger,
        "latestReceivingAt": latest_receipt,
        "businessDate": ing_forecast["businessDate"],
        "computedAt": ing_forecast["asOf"],
        "demandLookbackDays": ing_forecast["lookbackDays"],
        "tradingDaysInWindow": ing_forecast["tradingDaysInWindow"],
        "demandModel": ing_forecast["demandModel"],
        "prophetDrivenIngredients": ing_forecast["prophetDrivenIngredients"],
    }

    coverage = {
        "ingredientsTotal": cov["total"],
        "ingredientsForecast": len(fc_rows),
        "reorderPointsConfigured": ing_forecast["reorderPointsConfigured"],
        "reorderPointsTotal": cov["total"],
        "suppliersConfigured": cov["supplier"],
        "missingPackSize": cov["no_pack"],
        "missingUnitCost": cov["no_cost"],
        "purchaseUnitSpellings": cov["purchase_unit_spellings"],
        "costVerified": valuation["priced_items"],
        "costEstimated": max(0, cov["total"] - valuation["priced_items"] - cov["no_cost"]),
        "costUnset": cov["no_cost"],
        "verificationOpen": verify_queue["count"],
        "verificationByPriority": verify_queue["by_priority"],
    }

    # ---- kpis, fixed order: soonest-to-hurt first -------------------------
    # `basis` is new and is the point of the rewrite: every count says what
    # it was actually derived from, so a reader can tell a measured signal
    # from an unconfigured field without leaving the screen.
    rop_note = (
        "from the demand forecast — reorder points are not configured"
        if coverage["reorderPointsConfigured"] == 0 else
        f"{coverage['reorderPointsConfigured']} of {coverage['reorderPointsTotal']} "
        "ingredients have a reorder point"
    )
    kpis = [
        {"key": "expired", "value": len(expired), "display": None, "label": "Expired",
         "foot": "discard or credit today" if expired else "nothing has expired",
         "severity": "crit" if expired else "good", "icon": "⏱", "route": "expiry",
         "basis": "open FEFO batches past their expiry date"},
        {"key": "expiringSoon", "value": len(expiring_soon), "display": None,
         "label": "Expiring soon",
         "foot": "use first, oldest first" if expiring_soon else "nothing due in the next 7 days",
         "severity": "warn" if expiring_soon else "good", "icon": "◔", "route": "expiry",
         "basis": "open batches expiring within 7 days"},
        {"key": "needRestock", "value": need_restock, "display": None, "label": "Need restock",
         "foot": rop_note,
         "severity": "crit" if need_restock else "good",
         "icon": "⚠" if need_restock else "✓", "route": "purchasing",
         "basis": "forward demand forecast: projected to run out or fall below "
                  "safety stock inside the lead time"},
         # 2026-09 contract fix: this KPI used to be keyed "belowReorderPoint",
         # which collided in name (though not in meaning) with
         # wt/depletion.ingredient_forecast()'s own belowReorderPoint field --
         # that one is a literal count of ingredients under a configured
         # reorder_point_base; this one counts forward-forecast risk tiers.
         # Same key, two different numbers was a real contract bug (see
         # TestReplenishmentEndToEnd), not a naming preference -- renamed
         # rather than documented around.
        {"key": "unsellable", "value": len(unsellable), "display": None,
         "label": "Blocked menu items",
         "foot": "missing ingredient stock" if unsellable
                 else f"all {len(menu_avail)} items can be sold",
         "severity": "crit" if unsellable else "good",
         "icon": "⨯" if unsellable else "✓", "route": "menu",
         "basis": "v_menu_item_availability — the same view the POS sells from"},
        {"key": "stockValue", "value": valuation["total_value"],
         "display": f"₱{valuation['total_value']/1000:.1f}k" if valuation["total_value"] >= 1000
                     else f"₱{valuation['total_value']:.0f}",
         "label": "Stock value",
         "foot": f"{valuation['priced_items']} of {valuation['total_items']} items priced",
         "severity": "info", "icon": "₱", "route": "inventory",
         "basis": "confirmed unit costs only; estimated costs are excluded from this total"},
    ]

    # ---- action required, worst first ------------------------------------
    attention = []
    for b in expired[:6]:
        attention.append({
            "kind": "expired", "severity": "crit",
            "headline": f"{b['ingredient_name']} — expired {abs(b['days_to_expiry'])} days ago",
            "detail": "still counted as on hand until it is written off",
            "qty": qty_pair(b["qty_base"], b["base_unit"]),
            "ingredientId": b["ingredient_id"], "route": "expiry",
        })
    for r in [x for x in fc_rows if x["status"] in ("out", "critical")][:6]:
        attention.append({
            "kind": "restock", "severity": "crit",
            "headline": f"{r['name']} — "
                        + ("out of stock" if r["status"] == "out"
                           else "runs out today" if (r["daysOfSupply"] or 0) < 1
                           else f"about {round(r['daysOfSupply'])} day(s) left"),
            "detail": f"lead time {r['leadTimeDays']} days"
                      if r["leadTimeDays"] is not None else "lead time not set",
            "qty": qty_pair(r["available"], r["baseUnit"]),
            "ingredientId": r["ingredientId"], "route": "purchasing",
        })
    for b in [x for x in expiring_soon if x["days_to_expiry"] >= 0][:4]:
        attention.append({
            "kind": "expiring", "severity": expiry_severity(b["days_to_expiry"]),
            "headline": f"{b['ingredient_name']} — expires "
                        + ("today" if b["days_to_expiry"] < 1
                           else f"in {b['days_to_expiry']} days"),
            "detail": "use before it turns",
            "qty": qty_pair(b["qty_base"], b["base_unit"]),
            "ingredientId": b["ingredient_id"], "route": "expiry",
        })
    for m in unsellable[:4]:
        attention.append({
            "kind": "blocked", "severity": "crit",
            "headline": f"{m['name']} cannot be sold",
            "detail": m["block_reason"] or "an ingredient it needs is out of stock",
            "qty": None, "ingredientId": None, "route": "menu",
        })
    order = {"crit": 0, "warn": 1, "info": 2, "good": 3, "flat": 4}
    attention.sort(key=lambda a: order.get(a["severity"], 5))
    attention = attention[:12]

    # ---- buy today: the ingredients with a real restock recommendation ----
    # suggestedOrderQty is the depletion engine's own figure, in the
    # ingredient's own purchase unit. Rows without one are omitted rather
    # than shown with an invented quantity.
    buy_today = [
        {
            "ingredientId": r["ingredientId"], "name": r["name"], "status": r["status"],
            "available": qty_pair(r["available"], r["baseUnit"]),
            "daysOfSupply": r["daysOfSupply"],
            "depletionDate": r["depletionDate"],
            "leadTimeDays": r["leadTimeDays"],
            "purchaseUnit": r["purchaseUnit"],
            "packSize": r["packSize"],
            "suggestedOrderQty": r["suggestedOrderQty"],
            "suggestedOrderUnit": r["suggestedOrderUnit"],
            "expectedBaseQty": (r["suggestedOrderQty"] * r["packSize"])
                               if (r["suggestedOrderQty"] is not None and r["packSize"]) else None,
            "baseUnit": r["baseUnit"],
            "demandSource": r["demandSource"],
        }
        for r in fc_rows
        if r["status"] in ("out", "critical", "at_risk")
    ]

    # ---- use first: oldest stock, FEFO order ------------------------------
    use_first = [
        {"ingredientId": b["ingredient_id"], "name": b["ingredient_name"],
         "qty": qty_pair(b["qty_base"], b["base_unit"]),
         "label": expiry_label(b["days_to_expiry"]),
         "daysToExpiry": b["days_to_expiry"],
         "tracking": b["tracking"],
         "severity": expiry_severity(b["days_to_expiry"])}
        for b in batches[:8]
    ]

    return {
        "asOf": ing_forecast["asOf"],
        "businessDate": ing_forecast["businessDate"],
        "freshness": freshness,
        "coverage": coverage,
        "valuation": valuation,
        "kpis": kpis,
        "alerts": attention,
        "forecastPreview": _forecast_preview(c),
        "buyToday": buy_today,
        "useFirst": use_first,
        "buckets": buckets,
        "runningLow": running_low,
        "menuItemsTotal": len(menu_avail),
        "blockedMenuItems": [
            {"name": m["name"], "reason": m["block_reason"]} for m in unsellable[:10]
        ],
        # Not-yet-reviewed items lead (stable sort preserves list_queue's own
        # priority/group/name ordering within each half) -- a value an admin
        # has already looked at and knowingly kept as an estimate shouldn't
        # crowd a genuinely untouched one out of this 6-item preview.
        "verifyPreview": [
            {"label": f"{i['entity_name']} — {i['field_label']}", "priority": i["priority"],
             "entityName": i["entity_name"], "fieldLabel": i["field_label"]}
            for i in sorted(verify_all, key=lambda i: i["reviewed"])[:6]
        ],
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


@app.get("/console/data-quality", tags=["console"])
def data_quality_endpoint(
    lookback_days: int = Query(90, ge=1, le=365),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs_page("system")),
):
    """Machine-readable data-quality report over the real tables the ML
    layer reads — see wt/data_quality.py. Read-only: every check is a plain
    SQL query, nothing here alters a row."""
    return data_quality_report(c, lookback_days)


@app.get("/console/replenishment/backtest", tags=["console"])
def replenishment_backtest_endpoint(
    lookback_days: int = Query(60, ge=37, le=365),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Historical simulation of the replenishment early-warning signal
    (ingredient_forecast()'s critical/out status) against what the real
    ledger shows actually happened afterward — see
    wt/depletion.replenishment_backtest() for the full methodology and
    exactly what this does and does not claim to evaluate. Not cached: this
    is an on-demand technical-panel report, not something read on every
    dashboard load."""
    return replenishment_backtest(c, lookback_days)


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
    for 20-30 items on one page load isn't practical.

    The single-item Prophet fit is cached (wt/cache.py) — several seconds
    per fit, and a manager re-opening the same item's forecast a minute
    later would otherwise pay that cost again for an answer that could not
    have changed. Cache key includes MAX(customer_order.id), so a new order
    invalidates it immediately; never serves a result that predates an
    order actually in the database."""
    if menu_item_pk is not None:
        bound = max(lookback_days, 90)
        max_order = c.execute("SELECT MAX(id) AS m FROM customer_order").fetchone()["m"] or 0
        key = (str(db.db_path()), menu_item_pk, horizon_days, bound, max_order)
        return _forecast_cache.get_or_compute(
            key, lambda: menu_item_demand_prophet(c, menu_item_pk, horizon_days, bound)
        )
    items = rows(c.execute("SELECT id FROM menu_item WHERE pos_visible=1 ORDER BY name"))
    return {"items": [menu_item_demand(c, i["id"], horizon_days, lookback_days) for i in items]}


@app.get("/console/forecasts/store-demand", tags=["console"])
def forecast_store_demand(
    metric: str = Query("items", description="orders | items | sales"),
    horizon_days: int = Query(7, ge=1, le=30),
    lookback_days: int = Query(90, ge=7, le=365),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Store-wide daily demand (orders taken, items sold, or gross sales)
    plus a real Prophet forecast — the store-level analogue of
    /console/forecasts/demand, aggregated across the whole menu instead of
    one item. See wt/forecasting.store_demand_prophet(). Cached the same way
    as the per-item fit, and invalidated the same way — MAX(customer_order.id)
    in the cache key."""
    if metric not in STORE_METRICS:
        raise ValidationError(f"metric must be one of {STORE_METRICS}")
    max_order = c.execute("SELECT MAX(id) AS m FROM customer_order").fetchone()["m"] or 0
    key = (str(db.db_path()), "store-demand", metric, horizon_days, lookback_days, max_order)
    return _forecast_cache.get_or_compute(
        key, lambda: store_demand_prophet(c, metric, horizon_days, lookback_days)
    )


def _demand_model(c: sqlite3.Connection) -> str:
    """'prophet' or 'ewma' -- which demand model wt/depletion.py's forward
    chain uses for future-day demand (see build_demand()'s docstring).
    Backed by the app_setting 'depletion_demand_model' (0=ewma, 1=prophet;
    app_setting is float-only, see unli.get_setting), defaulting to ewma so
    installing this code changes nothing until someone deliberately opts in.
    One setting, one authoritative answer at a time -- never both engines
    live simultaneously for the same request the way the old
    forecasting.ingredient_depletion() purchase-quantity duplicate was."""
    return "prophet" if unli.get_setting(c, "depletion_demand_model", 0.0) >= 1.0 else "ewma"


def _depletion_fingerprint(c: sqlite3.Connection) -> tuple:
    """What the ingredient forecast actually reads, cheaply summarised.

    A new order (demand + today's observation), a new ledger row (stock,
    receipts, wastage), a recipe edit, or an ingredient-record edit all
    change the answer, so all four are in the key — the instant any of them
    lands, the next request recomputes for real rather than serving a number
    that predates it. The Manila hour is in there too because the intraday
    nowcast is explicitly a function of how much of today has elapsed; it
    must not be frozen at whatever hour the first caller happened to ask.

    2026-09 audit finding: ingredient.updated_at was missing from this key.
    on_hand_base/reserved_base changes are covered indirectly (every path
    that moves them -- commit, receive, count, wastage -- also writes a
    stock_ledger row), but PATCH /console/ingredients/{pk} changes
    reorder_point_base, lead_time_days, pack_size, yield_factor and
    unit_cost with no ledger/order/recipe_line row at all. An owner
    resetting an ingredient's reorder point or lead time (both read
    directly by ingredient_forecast) would silently keep serving the
    pre-edit forecast/replenishment numbers until some unrelated order or
    recipe change happened to invalidate the cache for an entirely
    different reason. Caught by
    TestReplenishmentEndToEnd.test_served_orders_flow_through_forecast_and_replenishment_to_the_api,
    which sets a reorder point mid-test and expects the very next request
    to see it.
    """
    return (
        str(db.db_path()),
        c.execute("SELECT MAX(id) AS m FROM customer_order").fetchone()["m"] or 0,
        c.execute("SELECT MAX(id) AS m FROM stock_ledger").fetchone()["m"] or 0,
        c.execute("SELECT MAX(id) AS m FROM recipe_line").fetchone()["m"] or 0,
        c.execute("SELECT MAX(updated_at) AS m FROM ingredient").fetchone()["m"] or "",
        timeutil.to_manila(timeutil.utc_now()).hour,
        # 2026-09: which demand model is active, and which generation of
        # Prophet fits is cached -- either changing must invalidate exactly
        # like any other input this forecast reads, or a demand_model
        # toggle or a completed refresh could serve pre-change numbers
        # until an unrelated order happened to invalidate the cache too.
        _demand_model(c),
        prophet_refresh_generation(),
    )


@app.get("/console/forecasts/ingredients", tags=["console"])
def forecast_ingredients(
    horizon_days: int = Query(DEPLETION_HORIZON_DAYS, ge=3, le=60),
    lookback_days: int = Query(DEPLETION_LOOKBACK_DAYS, ge=14, le=365),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Ingredient-level depletion forecasting — the whole chain, forward.

    POS order -> ordered menu quantity -> recipe quantities -> ingredient
    consumption -> current on-hand -> historical demand -> today's observed
    demand -> future demand -> projected consumption -> projected remaining
    stock -> projected depletion date -> days of supply -> lead-time warning.

    Distinct from /console/forecasts/depletion, which is the *backward*
    runway number (trailing ledger rate) that powers the Replenishment
    table. Both are kept: they answer different questions and neither is a
    substitute for the other. See wt/depletion.py for the model.
    """
    key = _depletion_fingerprint(c) + ("ingredients", horizon_days, lookback_days)
    return _forecast_cache.get_or_compute(
        key, lambda: ingredient_forecast(c, horizon_days, lookback_days, demand_model=_demand_model(c))
    )


@app.get("/console/forecasts/ingredients/{ingredient_pk}", tags=["console"])
def forecast_ingredient_detail(
    ingredient_pk: int,
    horizon_days: int = Query(DEPLETION_HORIZON_DAYS, ge=3, le=60),
    lookback_days: int = Query(DEPLETION_LOOKBACK_DAYS, ge=14, le=365),
    history_days: int = Query(30, ge=7, le=180),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """One ingredient's full picture: reconstructed stock history, forward
    trajectory with the demand model's own uncertainty band, the menu items
    driving consumption (measured, from order_line x v_recipe_leaf), and the
    specific observations behind the forecast. Every figure traces to a row
    in this database — nothing here is generated to fill a gap."""
    key = _depletion_fingerprint(c) + ("detail", ingredient_pk, horizon_days,
                                       lookback_days, history_days)
    return _forecast_cache.get_or_compute(
        key,
        lambda: ingredient_depletion_detail(c, ingredient_pk, horizon_days,
                                            lookback_days, history_days,
                                            demand_model=_demand_model(c)),
    )


@app.post("/console/forecasts/refresh-prophet-baselines", tags=["console"])
def refresh_prophet_baselines_endpoint(
    horizon_days: int = Query(DEPLETION_HORIZON_DAYS, ge=3, le=60),
    lookback_days: int = Query(DEPLETION_LOOKBACK_DAYS, ge=14, le=365),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.write")),
):
    """Fit Prophet for every POS-visible menu item and cache the result --
    the only place ingredient replenishment's Prophet model is ever fit. Not
    triggered automatically by any request: measured at ~1.8s per item, so a
    realistic menu is tens of seconds to a couple of minutes, which is fine
    for an explicit call (an admin action, or a scheduled task hitting this
    endpoint on a cadence such as nightly) and not fine to block a live page
    load on. Only takes effect for demand_model='prophet' -- see the
    'depletion_demand_model' app_setting; with it left at the default
    ('ewma'), calling this is harmless but has no visible effect. See
    wt/depletion.refresh_prophet_baselines() for the full reasoning."""
    return refresh_prophet_baselines(c, horizon_days, lookback_days)


@app.get("/console/menu/margins", tags=["console"])
def menu_margins(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Every menu price against its recipe's estimated ingredient cost.

    ESTIMATED is the operative word and it is reported, not buried: almost no
    ingredient in this database carries a supplier-confirmed unit cost yet
    (unit_cost_status / price_status), so a margin here is a margin against a
    benchmark, and `costConfidence` says so per row. Nothing on this screen
    changes a price — a negative margin is a flag for a human, and the two
    plausible causes (a wrong benchmark cost vs. a genuinely underpriced
    item) are not distinguishable from inside this system.
    """
    out = []
    for m in rows(c.execute(
        "SELECT id, menu_item_id, name, category, price, recipe_id, is_unli "
        "FROM menu_item ORDER BY category, name"
    )):
        row = {**m, "cost": None, "marginPct": None, "unpricedIngredients": [],
               "unverifiedCostLines": 0, "costConfidence": "none", "flags": []}
        if m["price"] is None or m["price"] <= 0:
            row["flags"].append("price is zero or unset")
        if m["is_unli"]:
            # An unlimited cover's consumption is the reconciled lambda vector,
            # not a recipe — costing it from a recipe it does not have would be
            # a fabrication. wt/unli.py owns that number.
            row["costConfidence"] = "not_recipe_costed"
            out.append(row)
            continue
        if m["recipe_id"] is None:
            row["flags"].append("no recipe linked")
            out.append(row)
            continue
        rc = recipe_cost(c, m["recipe_id"])
        row["cost"] = rc["total_cost"]
        row["unpricedIngredients"] = rc["unpriced_ingredients"]
        unverified = sum(1 for ln in rc["lines"] if not ln["cost_confirmed"])
        row["unverifiedCostLines"] = unverified
        row["costConfidence"] = (
            "incomplete" if rc["unpriced_ingredients"]
            else "estimated" if unverified else "confirmed"
        )
        if m["price"]:
            row["marginPct"] = round((m["price"] - rc["total_cost"]) / m["price"] * 100, 1)
            if row["marginPct"] < 0:
                row["flags"].append("estimated ingredient cost exceeds the menu price")
            elif row["marginPct"] < 15:
                row["flags"].append("estimated gross margin under 15%")
        out.append(row)

    return {
        "items": out,
        "negativeMargin": sum(1 for r in out if r["marginPct"] is not None and r["marginPct"] < 0),
        "thinMargin": sum(1 for r in out
                          if r["marginPct"] is not None and 0 <= r["marginPct"] < 15),
        "confirmedCostItems": sum(1 for r in out if r["costConfidence"] == "confirmed"),
        "note": ("Costs are computed from each recipe's expansion times the ingredient's "
                 "recorded unit cost. Ingredient costs in this database are benchmarks "
                 "pending supplier confirmation, so treat every margin below as an "
                 "estimate — see Needs Verification."),
    }


@app.get("/console/anomalies", tags=["console"])
def anomalies(
    lookback_days: int = Query(90, ge=1, le=365),
    ingredient_id: int | None = None,
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Both anomaly passes, reported separately rather than merged.

    `ingredient_id`, when given, narrows stock_loss_anomalies (and its
    `anomalies` alias) to one ingredient's own flags -- applied AFTER
    detection, so the model still scores against the whole pooled
    population; only the response list is filtered (see lib/api/
    intelligence.js's getAnomalies(), which documents exactly this and was
    already sending the param before this route accepted it). consumption_
    anomalies is keyed by unlimited-item stream, not a raw ingredient, so
    it is intentionally not narrowed by this parameter.

    consumption_anomalies is the specified model: IsolationForest over the
    seven-feature reconciled daily consumption vectors. stock_loss_anomalies
    is the older ledger pass over wastage/spoilage/count_adjustment, which
    answers a question the consumption pass structurally cannot.

    `anomalies` is kept as an alias of the stock-loss list so the existing
    console page and any client built against it keep working unchanged.
    See wt/anomaly.py for both methods.

    Both passes are cached (wt/cache.py) on a key that includes MAX(id)
    from the table each one actually reads (stock_ledger for the loss
    pass, unli_reconciliation for the consumption pass) — a new wastage
    entry or a newly reconciled day invalidates only the pass it actually
    affects, immediately, never a stale result masquerading as current."""
    max_ledger = c.execute("SELECT MAX(id) AS m FROM stock_ledger").fetchone()["m"] or 0
    max_recon = c.execute("SELECT MAX(id) AS m FROM unli_reconciliation").fetchone()["m"] or 0
    path = str(db.db_path())
    losses = _anomaly_cache.get_or_compute(
        (path, "stock_loss", lookback_days, max_ledger),
        lambda: detect_anomalies(c, lookback_days),
    )
    consumption = _anomaly_cache.get_or_compute(
        (path, "consumption", lookback_days, max_recon),
        lambda: detect_consumption_anomalies(c, lookback_days),
    )
    if ingredient_id is not None:
        losses = [a for a in losses if a["ingredient_id"] == ingredient_id]
    return {
        "consumption_anomalies": consumption,
        "stock_loss_anomalies": losses,
        "anomalies": losses,
    }


class AnomalyReviewIn(BaseModel):
    status: Literal["flagged", "under_review", "confirmed_anomaly", "false_positive", "resolved"]
    note: str | None = None
    client_request_id: str | None = Field(default=None, max_length=64)


@app.post("/console/anomalies/{ledger_id}/review", tags=["console"])
def review_anomaly(
    ledger_id: int, body: AnomalyReviewIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("inventory.write")),
):
    """Record a human reviewer's classification of one stock-loss anomaly
    flag (or, to catch a false negative, of a ledger row the model never
    flagged at all). This is the ground truth wt/anomaly.py's
    validation_metrics() computes Precision/Recall/F1 from — nothing is
    ever auto-labeled confirmed_anomaly, and there is no accusation implied
    by a flag alone. See wt/anomaly.py's record_anomaly_review()."""
    return record_anomaly_review(
        c, ledger_id, body.status, body.note, user_id=p["sub"],
        client_request_id=body.client_request_id,
    )


@app.get("/console/anomalies/{ledger_id}/reviews", tags=["console"])
def anomaly_review_history_endpoint(
    ledger_id: int, c: sqlite3.Connection = Depends(conn),
    _p: dict = Depends(needs("inventory.read")),
):
    """Every review ever recorded for this ledger row, oldest first."""
    return {"ledger_id": ledger_id, "reviews": anomaly_review_history(c, ledger_id)}


@app.get("/console/combinations", tags=["console"])
def combinations(
    lookback_days: int = Query(90, ge=1, le=365),
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    """Which menu items real customers actually order together, via FP-growth
    (mlxtend) over real order history. See wt/associations.py for method."""
    return product_combinations(c, lookback_days)


class ComboPricingPreviewIn(BaseModel):
    item_a_menu_item_id: int
    item_b_menu_item_id: int
    portion_multiplier_a: float = Field(default=1.0, gt=0, le=5)
    portion_multiplier_b: float = Field(default=1.0, gt=0, le=5)


@app.post("/console/combo-pricing/preview", tags=["console"])
def combo_pricing_preview(
    body: ComboPricingPreviewIn, c: sqlite3.Connection = Depends(conn),
    _p: dict = Depends(needs("inventory.read")),
):
    """Read-only cost + suggested price range for a potential combo of two
    menu items — never writes anything. See wt/combo_pricing.py for the
    ingredient/packaging/labor/overhead breakdown and price-range logic;
    portion multipliers default to each item's real recipe portion as sold
    (1.0) and are only ever scaled down on an explicit request, never
    invented (Part 16)."""
    cost = estimate_combo_cost(
        c, body.item_a_menu_item_id, body.item_b_menu_item_id,
        body.portion_multiplier_a, body.portion_multiplier_b,
    )
    price = suggest_price_range(c, cost)
    return {"cost_estimate": cost, "price_suggestion": price}


class ComboPricingConfigIn(BaseModel):
    target_food_cost_pct: float | None = Field(default=None, ge=1, le=100)
    labor_method: Literal["fixed_per_serving", "pct_of_ingredient_cost"] | None = None
    labor_fixed_amount: float | None = Field(default=None, ge=0)
    labor_pct: float | None = Field(default=None, ge=0, le=100)
    overhead_pct: float | None = Field(default=None, ge=0, le=100)
    preferred_min_price: float | None = Field(default=None, ge=0)
    preferred_max_price: float | None = Field(default=None, ge=0)
    max_discount_pct: float | None = Field(default=None, ge=0, le=100)


@app.get("/console/combo-pricing-config", tags=["console"])
def combo_pricing_config(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    return get_combo_pricing_config(c)


@app.patch("/console/combo-pricing-config", tags=["console"])
def set_combo_pricing_config_endpoint(
    body: ComboPricingConfigIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("inventory.write")),
):
    """Admin-only, same capability level as pos-config. Every field left
    out of the request body (Python None on a field that was never sent
    at all is indistinguishable from an explicit null in a plain dict, so
    the frontend only ever sends fields the admin actually touched) is left
    exactly as it was — sending a field explicitly as null clears it back
    to 'not configured'."""
    provided = body.model_dump(exclude_unset=True)
    return set_combo_pricing_config(c, provided, user_id=p["sub"])


class DraftComboIn(BaseModel):
    item_a_menu_item_id: int
    item_b_menu_item_id: int
    name: str | None = Field(default=None, max_length=120)
    portion_multiplier_a: float = Field(default=1.0, gt=0, le=5)
    portion_multiplier_b: float = Field(default=1.0, gt=0, le=5)
    chosen_price: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=2000)
    pair_stats: dict[str, Any] | None = None
    client_request_id: str | None = Field(default=None, max_length=64)


@app.post("/console/combo-pricing/drafts", tags=["console"])
def create_draft_combo_endpoint(
    body: DraftComboIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("combo.draft")),
):
    """Saves a combo idea as a draft — never publishes to menu_item/POS
    (Part 38). Recomputes cost/price server-side; the pricing_snapshot this
    writes is the audit trail for what was used at the moment of saving
    (Part 50/51)."""
    return create_draft_combo(
        c, body.item_a_menu_item_id, body.item_b_menu_item_id,
        name=body.name, portion_multiplier_a=body.portion_multiplier_a,
        portion_multiplier_b=body.portion_multiplier_b, chosen_price=body.chosen_price,
        note=body.note, pair_stats=body.pair_stats, user_id=p["sub"],
        client_request_id=body.client_request_id,
    )


@app.get("/console/combo-pricing/drafts", tags=["console"])
def list_draft_combos_endpoint(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
):
    return {"drafts": list_draft_combos(c)}


class DraftComboReviewIn(BaseModel):
    status: Literal["draft", "approved", "rejected"]
    note: str | None = Field(default=None, max_length=2000)
    client_request_id: str | None = Field(default=None, max_length=64)


@app.post("/console/combo-pricing/drafts/{draft_id}/review", tags=["console"])
def review_draft_combo_endpoint(
    draft_id: int, body: DraftComboReviewIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("combo.draft")),
):
    """Records an owner/admin review decision on a saved draft combo.
    Approving here is a record-keeping decision only — it does NOT create a
    real menu_item or POS product; publishing a combo to the live menu is a
    separate action outside this endpoint's scope (Part 62)."""
    return review_draft_combo(
        c, draft_id, body.status, note=body.note, user_id=p["sub"],
        client_request_id=body.client_request_id,
    )


@app.get("/console/model-details", tags=["console"])
def model_details_endpoint(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("inventory.read")),
    _pg: dict = Depends(needs_page("system")),
):
    """Real technical stats for the three Phase 3 models — a holdout
    backtest for Prophet, live parameters and current pooled-sample counts
    for IsolationForest and FP-growth. Kept separate from the plain-language
    Forecasts/Anomaly/Reports pages; this is the evidence-of-work view."""
    max_order = c.execute("SELECT MAX(id) AS m FROM customer_order").fetchone()["m"] or 0
    demand_forecast = _model_details_cache.get_or_compute(
        (str(db.db_path()), "demand_forecast", max_order),
        lambda: evaluate_demand_model(c),
    )
    store_demand_forecast = _model_details_cache.get_or_compute(
        (str(db.db_path()), "store_demand_forecast", max_order),
        lambda: evaluate_store_demand_model(c),
    )
    return {
        "demand_forecast": demand_forecast,
        "store_demand_forecast": store_demand_forecast,
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
    p: dict = Depends(needs("unli.reconcile.write")),
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


class HeadFlavorsIn(BaseModel):
    flavors: list[str | None] = Field(default_factory=list, max_length=50)


@app.patch("/unli/sessions/{pk}/head-flavors", tags=["unli"])
def unli_set_head_flavors(
    pk: int, body: HeadFlavorsIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("unli.refill")),
):
    """Which flavour each head at this table initially chose — repeats
    allowed, capped server-side at the session's own head_count (see
    refills.set_head_flavors). Purely informational: never touches a
    refill, a reservation, or the stock ledger — record_refill still sizes
    every actual served round off head_count alone, completely unchanged."""
    return refills.set_head_flavors(c, pk, body.flavors, user_id=p["sub"])


class SessionTableIn(BaseModel):
    table_label: str = Field(min_length=1, max_length=40)


@app.patch("/unli/sessions/{pk}/table", tags=["unli"])
def unli_set_session_table(
    pk: int, body: SessionTableIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("unli.refill")),
):
    """Assign (or change) the physical table for an already-open unlimited
    session — the table is picked from the refill workflow, not at order
    creation, so a busy cashier can ring up the first serve before the floor
    even knows which table it's going to. See refills.set_session_table."""
    return refills.set_session_table(c, pk, body.table_label, user_id=p["sub"])


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


# The restaurant's actual seating: how many numbered tables the POS offers
# a cashier when taking a Dine In order. 9 until an admin says otherwise —
# installing this changes nothing about an existing deployment.
DEFAULT_TABLE_COUNT = 9


class PosConfigIn(BaseModel):
    service_charge_enabled: bool | None = None
    service_charge_rate: float | None = Field(default=None, ge=0, le=1)
    # Only present for a discount type the admin wants to flip; omitted
    # types are left exactly as they were. Wings Theory's default (nothing
    # ever sent here) is every type enabled at its existing statutory/house
    # rate — this never invents a new rate, only whether the till may offer
    # a type at all.
    discount_enabled: dict[Literal["senior", "pwd", "staff"], bool] | None = None
    table_count: int | None = Field(default=None, ge=1, le=200)


@app.get("/console/pos-config", tags=["console"])
def get_pos_config(
    c: sqlite3.Connection = Depends(conn), _p: dict = Depends(needs("recipe.read")),
):
    """The admin-configurable half of order pricing that isn't a per-item
    price or a per-flavour add-on: whether a service charge applies at all,
    and which of the Senior/PWD/Staff discount cards a cashier may select.
    Both default to "current business rule, unchanged" — service charge
    off, every discount type enabled — until an admin explicitly touches
    this. See _effective_service_charge()/_enabled_discount_types(), the
    same functions create_order() and GET /pos/menu actually use, so this
    screen can never drift from what checkout charges."""
    enabled, rate = _effective_service_charge(c)
    enabled_discounts = _enabled_discount_types(c)
    return {
        "service_charge_enabled": enabled,
        "service_charge_rate": rate if enabled else unli.get_setting(
            c, "service_charge_rate", SERVICE_CHARGE_RATE),
        "discounts": {
            t: {"enabled": t in enabled_discounts, "rate": DISCOUNT_RATES[t],
                "label": DISCOUNT_LABELS[t]}
            for t in DISCOUNT_RATES
        },
        "table_count": int(unli.get_setting(c, "pos_table_count", DEFAULT_TABLE_COUNT)),
    }


@app.patch("/console/pos-config", tags=["console"])
def set_pos_config(
    body: PosConfigIn, c: sqlite3.Connection = Depends(conn),
    p: dict = Depends(needs("pos_config.write")),
):
    """Admin-only. Cashiers read the effective result through GET /pos/menu
    (service_charge_enabled/service_charge_rate/discount_rates) but cannot
    reach this endpoint — see auth.CAPABILITIES' pos_config.write."""
    if body.service_charge_enabled is not None:
        unli.set_setting(c, "service_charge_enabled",
                          1.0 if body.service_charge_enabled else 0.0, user_id=p["sub"])
    if body.service_charge_rate is not None:
        unli.set_setting(c, "service_charge_rate", body.service_charge_rate, user_id=p["sub"])
    if body.discount_enabled:
        for t, on in body.discount_enabled.items():
            unli.set_setting(c, f"discount_{t}_enabled", 1.0 if on else 0.0, user_id=p["sub"])
    if body.table_count is not None:
        unli.set_setting(c, "pos_table_count", float(body.table_count), user_id=p["sub"])
    return get_pos_config(c, p)


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
# lives in one file instead of being duplicated into each HTML client. This
# is also where the entire console SPA's JS tree lives (assets/console/js).
#
# 2026-09 fix: StaticFiles emits ETag/Last-Modified but no Cache-Control, so
# a browser is free to serve a console module byte-for-byte from disk cache
# on a later visit without ever asking the server whether it changed --
# discovered when a console bugfix kept failing to show up in an already-
# cached browser tab even after a full page reload. Exactly the same failure
# mode /pos's own "no-store" comment above already describes for pos.html,
# just one layer down (the JS the HTML shell imports, not the shell itself).
# no-cache (not no-store) still lets the browser keep a local copy and
# revalidate it with a conditional GET against the ETag StaticFiles already
# sends -- a 304 on every unchanged asset, a real fetch only when something
# actually changed -- rather than re-downloading every asset on every load.
@app.middleware("http")
async def _no_cache_console_assets(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets")


@app.get("/pos", include_in_schema=False)
def pos_client():
    # no-store: this file gets patched frequently mid-shift, and a stale
    # cached copy silently running last week's JS in an already-open tab is
    # a much worse failure mode than one extra fetch on every reload.
    return FileResponse(_STATIC_DIR / "pos.html", headers={"Cache-Control": "no-store"})


@app.get("/kds", include_in_schema=False)
def kds_client():
    # no-store, same reasoning as /pos above — this shell was missing the
    # same protection despite being exactly as susceptible: found 2026-09
    # when a console.html fix (below) kept silently not showing up in an
    # already-open tab, for this same reason.
    return FileResponse(_STATIC_DIR / "kds.html", headers={"Cache-Control": "no-store"})


@app.get("/console", include_in_schema=False)
def console_client():
    # no-store — same reasoning as /pos/ /kds above. Console changes land
    # less often mid-shift than POS/KDS, but an owner reopening an
    # already-open console tab expecting a just-shipped fix and silently
    # getting yesterday's page is the same failure, not a smaller one.
    return FileResponse(_STATIC_DIR / "console.html", headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def landing():
    return FileResponse(_STATIC_DIR / "landing.html", headers={"Cache-Control": "no-store"})
