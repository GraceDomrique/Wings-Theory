"""Cost-based combo pricing — turns a real FP-growth purchasing pattern
(wt/associations.py) into an auditable cost estimate and a suggested customer
price range, built from the SAME recipe/BOM data every other costed screen
in this system reads (resolver.expand_recipe, ingredient.unit_cost).

This module never invents a number. Every cost line carries a status:
  - "confirmed"        every ingredient has a verified unit cost
  - "estimated"        priced, but at least one ingredient's cost is
                        unverified (same meaning as menu_margins.costConfidence)
  - "incomplete"        at least one ingredient has no recorded cost at all
  - "not_recipe_costed" the menu item has no recipe (or is an unlimited
                        item, whose consumption is a reconciled lambda
                        vector, not a recipe — see wt/unli.py)
  - "not_configured"    a labor/overhead/pricing SETTING (not a recipe fact)
                        has not been given a value yet

app_setting (wt/unli.get_setting/set_setting) only stores floats, so "not
configured" is represented on disk by the sentinel -1 — the same
enum-as-float convention 'depletion_demand_model' already uses — and
translated to None the moment it crosses back into Python.
"""
from __future__ import annotations

import json
import sqlite3

from . import audit, unli
from .db import write_tx
from .errors import NotFoundError, ValidationError
from .resolver import expand_recipe

# ---------------------------------------------------------------------------
# Settings — app_setting keys, and the friendly<->stored mapping.
# ---------------------------------------------------------------------------
NOT_CONFIGURED = -1.0

_CFG_KEYS = {
    "target_food_cost_pct": "combo_target_food_cost_pct",
    "labor_method": "combo_labor_method",
    "labor_fixed_amount": "combo_labor_fixed_amount",
    "labor_pct": "combo_labor_pct",
    "overhead_pct": "combo_overhead_pct",
    "preferred_min_price": "combo_preferred_min_price",
    "preferred_max_price": "combo_preferred_max_price",
    "max_discount_pct": "combo_max_discount_pct",
}

_LABOR_METHODS = {0: "fixed_per_serving", 1: "pct_of_ingredient_cost"}
_LABOR_METHOD_CODES = {v: k for k, v in _LABOR_METHODS.items()}


def get_combo_pricing_config(conn: sqlite3.Connection) -> dict:
    """The restaurant's current combo-pricing settings — every field is
    None ("not configured") until an admin sets it via
    PATCH /console/combo-pricing-config, exactly like every other
    app_setting-backed config in this system (see wt/api.py's
    _effective_service_charge)."""
    raw = {k: unli.get_setting(conn, v, NOT_CONFIGURED) for k, v in _CFG_KEYS.items()}

    def opt(x: float) -> float | None:
        return None if x is None or x < 0 else x

    labor_code = raw["labor_method"]
    return {
        "target_food_cost_pct": opt(raw["target_food_cost_pct"]),
        "labor_method": _LABOR_METHODS.get(int(labor_code)) if labor_code is not None and labor_code >= 0 else None,
        "labor_fixed_amount": opt(raw["labor_fixed_amount"]),
        "labor_pct": opt(raw["labor_pct"]),
        "overhead_pct": opt(raw["overhead_pct"]),
        "preferred_min_price": opt(raw["preferred_min_price"]),
        "preferred_max_price": opt(raw["preferred_max_price"]),
        "max_discount_pct": opt(raw["max_discount_pct"]),
    }


def set_combo_pricing_config(conn: sqlite3.Connection, updates: dict, user_id: str | None = None) -> dict:
    """Only the fields present in `updates` are written — same partial-update
    convention as PATCH /console/pos-config. `None` explicitly clears a field
    back to "not configured" rather than being ignored, so an admin can
    un-set a value once entered."""
    for field, setting_key in _CFG_KEYS.items():
        if field not in updates:
            continue
        value = updates[field]
        if field == "labor_method":
            stored = NOT_CONFIGURED if value is None else float(_LABOR_METHOD_CODES[value])
        else:
            stored = NOT_CONFIGURED if value is None else float(value)
        # unli.set_setting() opens and commits its own write_tx (it is the
        # same helper every other app_setting write in this codebase uses),
        # so the audit row is written in its own immediately-following
        # transaction rather than nested inside it -- write_tx() rejects
        # nesting outright (wt/db.py) to avoid a partial rollback leaving
        # inconsistent state.
        unli.set_setting(conn, setting_key, stored, user_id=user_id)
        with write_tx(conn):
            audit.log(
                conn, "combo_pricing_config", setting_key, "update", user_id,
                field=field, new_value=value,
            )
    return get_combo_pricing_config(conn)


# ---------------------------------------------------------------------------
# Per-item cost — reuses resolver.expand_recipe (the recursive sub-recipe
# walk) and the exact per_base = unit_cost / (pack_size * yield_factor)
# formula resolver.recipe_cost() uses, extended to also read
# ingredient.category so ingredient cost and packaging cost can be reported
# as two separate lines (Part 15/20) instead of resolver.recipe_cost()'s
# single blended total.
# ---------------------------------------------------------------------------
_CONFIDENCE_RANK = {"not_recipe_costed": 0, "incomplete": 1, "estimated": 2, "confirmed": 3}


def menu_item_cost_breakdown(conn: sqlite3.Connection, menu_item_pk: int) -> dict:
    row = conn.execute(
        "SELECT id, name, price, recipe_id, is_unli FROM menu_item WHERE id=?",
        (menu_item_pk,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"menu item {menu_item_pk} not found")

    base = {"menu_item_id": row["id"], "name": row["name"], "price": row["price"]}

    if row["is_unli"]:
        # An unlimited item's real consumption is the reconciled lambda
        # vector (wt/unli.py), not a bill of materials — costing it from a
        # recipe it does not have would be a fabrication, same rule
        # menu_margins() already applies.
        return {**base, "ingredient_cost": None, "packaging_cost": None,
                "cost_confidence": "not_recipe_costed", "unpriced_ingredients": []}
    if row["recipe_id"] is None:
        return {**base, "ingredient_cost": None, "packaging_cost": None,
                "cost_confidence": "not_recipe_costed", "unpriced_ingredients": []}

    ingredient_cost = 0.0
    packaging_cost = 0.0
    unpriced: list[str] = []
    any_unverified = False
    for leaf in expand_recipe(conn, row["recipe_id"]):
        ing = conn.execute(
            "SELECT unit_cost, unit_cost_status, pack_size, yield_factor, category "
            "FROM ingredient WHERE id=?",
            (leaf.ingredient_id,),
        ).fetchone()
        if ing["unit_cost"] is None or ing["pack_size"] is None:
            unpriced.append(leaf.name)
            continue
        per_base = ing["unit_cost"] / (ing["pack_size"] * ing["yield_factor"])
        cost = per_base * leaf.qty_base
        if ing["unit_cost_status"] != "verified":
            any_unverified = True
        if ing["category"] in ("packaging", "consumable"):
            packaging_cost += cost
        else:
            ingredient_cost += cost

    confidence = "incomplete" if unpriced else ("estimated" if any_unverified else "confirmed")
    return {
        **base,
        "ingredient_cost": round(ingredient_cost, 2),
        "packaging_cost": round(packaging_cost, 2),
        "cost_confidence": confidence,
        "unpriced_ingredients": unpriced,
    }


# ---------------------------------------------------------------------------
# Combo cost estimate — both items, real portions unless the caller
# explicitly overrides (Part 16: a smaller combo portion is never invented,
# only ever applied when a human sends a multiplier).
# ---------------------------------------------------------------------------
def estimate_combo_cost(
    conn: sqlite3.Connection, item_a_id: int, item_b_id: int,
    portion_multiplier_a: float = 1.0, portion_multiplier_b: float = 1.0,
) -> dict:
    a = menu_item_cost_breakdown(conn, item_a_id)
    b = menu_item_cost_breakdown(conn, item_b_id)
    cfg = get_combo_pricing_config(conn)

    def scale(v: float | None, m: float) -> float | None:
        return None if v is None else round(v * m, 2)

    a_ing, b_ing = scale(a["ingredient_cost"], portion_multiplier_a), scale(b["ingredient_cost"], portion_multiplier_b)
    a_pkg, b_pkg = scale(a["packaging_cost"], portion_multiplier_a), scale(b["packaging_cost"], portion_multiplier_b)

    item_confidence = min(a["cost_confidence"], b["cost_confidence"], key=_CONFIDENCE_RANK.get)
    # A partial sum (e.g. one unpriced ingredient skipped) is not a real
    # cost — reported as unavailable rather than as an understated total,
    # same rule resolver.recipe_cost() applies (unpriced_ingredients are
    # never folded in at zero).
    reliable = item_confidence in ("confirmed", "estimated")
    ingredient_cost = round(a_ing + b_ing, 2) if reliable and a_ing is not None and b_ing is not None else None
    packaging_cost = round(a_pkg + b_pkg, 2) if reliable and a_pkg is not None and b_pkg is not None else None

    # Labor
    if cfg["labor_method"] is None:
        labor_cost, labor_status = None, "not_configured"
    elif cfg["labor_method"] == "fixed_per_serving":
        if cfg["labor_fixed_amount"] is None:
            labor_cost, labor_status = None, "not_configured"
        else:
            labor_cost, labor_status = round(cfg["labor_fixed_amount"], 2), "estimated"
    else:  # pct_of_ingredient_cost
        if cfg["labor_pct"] is None:
            labor_cost, labor_status = None, "not_configured"
        elif ingredient_cost is None:
            labor_cost, labor_status = None, "incomplete"
        else:
            labor_cost, labor_status = round(ingredient_cost * cfg["labor_pct"] / 100, 2), "estimated"

    # Overhead — the only method the current architecture can safely
    # support without real prep-time or utility-usage data: a configured
    # percentage of ingredient + packaging cost (Part 19).
    if cfg["overhead_pct"] is None:
        overhead_cost, overhead_status = None, "not_configured"
    elif ingredient_cost is None or packaging_cost is None:
        overhead_cost, overhead_status = None, "incomplete"
    else:
        overhead_cost = round((ingredient_cost + packaging_cost) * cfg["overhead_pct"] / 100, 2)
        overhead_status = "estimated"

    components = [ingredient_cost, packaging_cost, labor_cost, overhead_cost]
    if ingredient_cost is None:
        total_cost, total_status = None, "incomplete"
    else:
        total_cost = round(sum(v for v in components if v is not None), 2)
        total_status = "complete" if all(v is not None for v in components) else "partial"

    return {
        "item_a": a, "item_b": b,
        "portion_multiplier_a": portion_multiplier_a, "portion_multiplier_b": portion_multiplier_b,
        "ingredient_cost": ingredient_cost, "ingredient_status": item_confidence,
        "packaging_cost": packaging_cost,
        "labor_cost": labor_cost, "labor_status": labor_status,
        "overhead_cost": overhead_cost, "overhead_status": overhead_status,
        "total_cost": total_cost, "total_status": total_status,
        "unpriced_ingredients": sorted(set(a["unpriced_ingredients"] + b["unpriced_ingredients"])),
        "current_menu_total": round(a["price"] + b["price"], 2) if a["price"] is not None and b["price"] is not None else None,
        "config": cfg,
    }


# ---------------------------------------------------------------------------
# Price range — Part 21/22/23/24: a range, not one magic number; balances
# the cost-based price against the restaurant's own preferred customer
# range and its max-discount-off-list-price ceiling, and reports an "issue"
# instead of forcing an unrealistic number when those constraints conflict.
# ---------------------------------------------------------------------------
def suggest_price_range(conn: sqlite3.Connection, cost_estimate: dict) -> dict:
    cfg = cost_estimate["config"]
    total_cost = cost_estimate["total_cost"]
    individual_total = cost_estimate["current_menu_total"]

    if total_cost is None:
        return {"range": None, "cost_based_price": None,
                "issue": "Both items need a complete, priced recipe before a combo price can be estimated."}
    target_pct = cfg["target_food_cost_pct"]
    if target_pct is None:
        return {"range": None, "cost_based_price": None,
                "issue": "Configure a target food-cost percentage in System settings to see a suggested price range."}

    cost_based_price = total_cost / (target_pct / 100)

    upper_candidates = [v for v in [
        cfg["preferred_max_price"],
        (individual_total * (1 - cfg["max_discount_pct"] / 100)) if individual_total and cfg["max_discount_pct"] is not None else None,
    ] if v is not None]
    upper_bound = min(upper_candidates) if upper_candidates else None

    if upper_bound is not None and cost_based_price > upper_bound:
        return {"range": None, "cost_based_price": round(cost_based_price, 2),
                "issue": "Current costs make this combination difficult to price within the preferred range."}

    low = cost_based_price
    high = upper_bound if upper_bound is not None else round(cost_based_price * 1.15, 2)
    if cfg["preferred_min_price"] is not None:
        low = max(low, cfg["preferred_min_price"])
        if low > high:
            return {"range": None, "cost_based_price": round(cost_based_price, 2),
                    "issue": "The configured preferred minimum price is above what current costs and the "
                             "preferred maximum allow."}

    return {"range": [round(low, 2), round(high, 2)], "cost_based_price": round(cost_based_price, 2), "issue": None}


# ---------------------------------------------------------------------------
# Draft combos — Part 38/39/50/51: saving an idea never touches menu_item or
# POS data. append-only, same convention as anomaly_review (migration 0035):
# the original pricing snapshot is never rewritten; a status change is a new
# row in the companion draft_combo_review table.
# ---------------------------------------------------------------------------
def _draft_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "item_a_menu_item_id": r["item_a_menu_item_id"],
        "item_b_menu_item_id": r["item_b_menu_item_id"],
        "name": r["name"],
        "portion_multiplier_a": r["portion_multiplier_a"],
        "portion_multiplier_b": r["portion_multiplier_b"],
        "pricing_snapshot": json.loads(r["pricing_snapshot"]),
        "chosen_price": r["chosen_price"],
        "note": r["note"],
        "created_by": r["created_by"],
        "created_at": r["created_at"],
    }


def create_draft_combo(
    conn: sqlite3.Connection, item_a_id: int, item_b_id: int, *,
    name: str | None = None, portion_multiplier_a: float = 1.0, portion_multiplier_b: float = 1.0,
    chosen_price: float | None = None, note: str | None = None,
    pair_stats: dict | None = None, user_id: str | None = None, client_request_id: str | None = None,
) -> dict:
    """Recomputes the cost breakdown and price range server-side (never
    trusts a client-supplied cost or price) and freezes that result into
    `pricing_snapshot` — the audit trail Part 50 requires. `pair_stats`, if
    given, is the FP-growth pair's own observed numbers (pair_count/support/
    confidence/lift) as already returned by GET /console/combinations moments
    earlier — informational context for the snapshot, not re-derived here,
    since those figures are read-only and deterministic for a given
    lookback window."""
    if client_request_id:
        existing = conn.execute(
            "SELECT * FROM draft_combo WHERE client_request_id=?", (client_request_id,),
        ).fetchone()
        if existing:
            return _draft_row(existing)

    cost = estimate_combo_cost(conn, item_a_id, item_b_id, portion_multiplier_a, portion_multiplier_b)
    price = suggest_price_range(conn, cost)
    snapshot = {"cost_estimate": cost, "price_suggestion": price, "pair_stats": pair_stats or {}}

    with write_tx(conn):
        cur = conn.execute(
            "INSERT INTO draft_combo (item_a_menu_item_id, item_b_menu_item_id, name, "
            "portion_multiplier_a, portion_multiplier_b, pricing_snapshot, chosen_price, note, "
            "created_by, client_request_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (item_a_id, item_b_id, name, portion_multiplier_a, portion_multiplier_b,
             json.dumps(snapshot), chosen_price, note, user_id, client_request_id),
        )
        draft_pk = cur.lastrowid
        conn.execute(
            "INSERT INTO draft_combo_review (draft_combo_id, status, user_id) VALUES (?, 'draft', ?)",
            (draft_pk, user_id),
        )
        audit.log(
            conn, "draft_combo", draft_pk, "create", user_id,
            note=f"items {item_a_id}+{item_b_id}" + (f" '{name}'" if name else ""),
        )
    return _draft_row(conn.execute("SELECT * FROM draft_combo WHERE id=?", (draft_pk,)).fetchone())


def _latest_draft_statuses(conn: sqlite3.Connection) -> dict[int, str]:
    rows = conn.execute(
        """
        SELECT dr.draft_combo_id, dr.status FROM draft_combo_review dr
         WHERE dr.id = (SELECT MAX(dr2.id) FROM draft_combo_review dr2
                          WHERE dr2.draft_combo_id = dr.draft_combo_id)
        """
    ).fetchall()
    return {r["draft_combo_id"]: r["status"] for r in rows}


def list_draft_combos(conn: sqlite3.Connection) -> list[dict]:
    statuses = _latest_draft_statuses(conn)
    out = []
    for r in conn.execute("SELECT * FROM draft_combo ORDER BY id DESC").fetchall():
        row = _draft_row(r)
        row["status"] = statuses.get(row["id"], "draft")
        out.append(row)
    return out


REVIEW_STATUSES = ("draft", "approved", "rejected")


def review_draft_combo(
    conn: sqlite3.Connection, draft_combo_id: int, status: str,
    note: str | None = None, user_id: str | None = None, client_request_id: str | None = None,
) -> dict:
    if status not in REVIEW_STATUSES:
        raise ValidationError(f"unknown draft combo status '{status}'")
    if not conn.execute("SELECT 1 FROM draft_combo WHERE id=?", (draft_combo_id,)).fetchone():
        raise NotFoundError(f"draft combo {draft_combo_id} not found")

    if client_request_id:
        existing = conn.execute(
            "SELECT * FROM draft_combo_review WHERE client_request_id=?", (client_request_id,),
        ).fetchone()
        if existing:
            return dict(existing)

    with write_tx(conn):
        cur = conn.execute(
            "INSERT INTO draft_combo_review (draft_combo_id, status, note, user_id, client_request_id) "
            "VALUES (?,?,?,?,?)",
            (draft_combo_id, status, note, user_id, client_request_id),
        )
        audit.log(
            conn, "draft_combo", draft_combo_id, "review", user_id,
            field="status", new_value=status, note=note,
        )
    return dict(conn.execute("SELECT * FROM draft_combo_review WHERE id=?", (cur.lastrowid,)).fetchone())
