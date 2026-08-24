"""Phase 08 — the owner verification workflow.

v_needs_verification (migration 0001) already lists every physical constant
still carrying a seeded placeholder. What this module adds is everything
around that list: a plain-language translation of each technical field so a
non-technical manager can act on it without knowing what pack_size or lambda
means, a priority so 630 rows become three ordered questions instead of one
flat dump, and a durable audit trail (verification_event, migration 0013)
recording every decision the owner actually makes -- confirm, change, or
keep as an estimate for now -- with the previous value preserved alongside
the new one.

Every action here writes through the ordinary column a real order/receiving/
reconciliation already reads live (ingredient.pack_size, recipe_line.qty_base,
fefo_batch.expires_at) -- so confirming a value takes effect for the very
next transaction that reads it, with no separate "apply" step and no second
copy of the number anywhere. The one exception, unli_coefficient.lambda, is
deliberately NOT edited in place: provisional_for_date() re-reads
lambda_value live against whatever row was effective_from <= a given order's
own timestamp, so overwriting a row's value would retroactively change what
an already-reconciled day is judged against. A "change" there inserts a new
row instead -- exactly the pattern recalibrate() already uses -- so history
stays reproducible and only orders from now on see the new rate.
"""
from __future__ import annotations

import datetime as _dt
import math
import sqlite3

from . import timeutil
from .db import write_tx
from .errors import InvalidStateError, NotFoundError, ValidationError

# ---------------------------------------------------------------------------
# Priority: what breaks if this specific field is wrong.
# ---------------------------------------------------------------------------
# P1 -- directly changes how much stock a transaction deducts or credits.
# P2 -- changes costing (dish cost, margin) but never a physical quantity.
# P3 -- changes an expiry/replenishment judgement, never a deduction amount.
PRIORITY = {
    "ingredient.pack_size": 1,
    "ingredient.yield_factor": 1,
    "recipe_line.qty_base": 1,
    "unli_coefficient.lambda": 1,
    "ingredient.unit_cost": 2,
    "fefo_batch.expires_at": 3,
}
PRIORITY_LABEL = {
    1: "Priority 1 — affects how much stock gets deducted",
    2: "Priority 2 — affects cost and pricing, not stock amounts",
    3: "Priority 3 — affects expiry and reorder timing, not stock amounts",
}

# Plain-language translation. No pack_size/yield_factor/qty_base/lambda
# anywhere in the owner-facing text -- see the module docstring.
FIELD_COPY = {
    "ingredient.pack_size": {
        "label": "Pack size",
        "question": "How much is actually in one purchased pack?",
        "why": "This decides how much usable stock the system credits every "
               "time a delivery is received, and how many packs it tells "
               "you to reorder.",
        "check": "Next delivery, weigh or count exactly what is inside one "
                 "purchased unit.",
    },
    "ingredient.yield_factor": {
        "label": "Usable amount after prep",
        "question": "How much usable product does one purchased unit "
                     "produce after preparation or cooking?",
        "why": "This decides how much of what you buy actually turns into "
               "stock the kitchen can use. Too high and the system thinks "
               "you have more than you do; too low and it thinks you have "
               "less.",
        "check": "Weigh the usable amount after cooking, trimming, or prep "
                 "for a typical batch, and compare it to the raw amount you "
                 "started with.",
    },
    "ingredient.unit_cost": {
        "label": "Cost",
        "question": "How much did the restaurant actually pay for one pack?",
        "why": "This drives the cost shown for every recipe and dish that "
               "uses this ingredient.",
        "check": "Check the actual supplier receipt or invoice from the "
                 "last delivery.",
    },
    "recipe_line.qty_base": {
        "label": "Recipe amount",
        "question": "How much of this ingredient is actually used for one "
                     "serving or batch?",
        "why": "This is exactly what gets deducted from stock every time "
               "this item is sold. Wrong here means every sale over- or "
               "under-deducts.",
        "check": "Measure exactly what one real prep of this recipe uses.",
    },
    "unli_coefficient.lambda": {
        "label": "Unlimited consumption rate",
        "question": "How much of this item does one unlimited customer "
                     "normally eat?",
        "why": "This is deducted automatically the moment an unlimited "
               "order is punched in, before anyone weighs anything. The "
               "reconciliation screen already checks this number against "
               "what was actually served.",
        "check": "Look at this stream's reconciliation history — does the "
                 "measured amount usually land close to this rate?",
    },
    "fefo_batch.expires_at": {
        "label": "Expiry date",
        "question": "When does this specific batch of stock actually "
                     "expire?",
        "why": "This decides which stock the system tells the kitchen to "
               "use first, and when it warns about spoilage risk. It never "
               "changes how much gets deducted.",
        "check": "Check the printed date on this delivery, or set a "
                 "realistic estimate for how it is actually stored.",
    },
}

# Coarser-than-one-row-per-item grouping so 269 recipe lines don't read as
# 269 isolated technical rows. "Verify all sauce bottles" in one place
# instead of scattered among 198 unrelated menu-recipe lines.
GROUP_LABEL = {
    "ingredient.pack_size": "Ingredient basics",
    "ingredient.yield_factor": "Ingredient basics",
    "ingredient.unit_cost": "Ingredient basics",
    "unli_coefficient.lambda": "Unlimited consumption rates",
    "fefo_batch.expires_at": "Stock batch expiry dates",
}


# Every numeric field in this workflow represents a strictly positive
# physical or business quantity: a pack always contains more than zero
# units, a yield factor of zero would mean the ingredient never yields
# anything, a recipe line consuming exactly zero of something is a data
# error (an ingredient that's genuinely not used shouldn't have a line at
# all -- see seed.py's import_recipes, which drops qty_base <= 0 lines
# rather than storing them), an unlimited stream deducting zero per head
# defeats the point of tracking it, and a negative cost, quantity, or rate
# is never physically meaningful here. One rule -- new_value > 0 -- covers
# all five; see validate_numeric_value. Public (no leading underscore) --
# api.py's /console/ingredients/{pk}/edit endpoint validates unit_cost the
# same way before writing it, so both paths that can move one of these
# ingredient columns share exactly one validation rule instead of two that
# could drift apart.
NUMERIC_FIELDS = {
    "ingredient.pack_size", "ingredient.yield_factor", "ingredient.unit_cost",
    "recipe_line.qty_base", "unli_coefficient.lambda",
}


def validate_numeric_value(field: str, raw: float | str | None) -> float:
    """Runs before any database write -- see resolve(), which validates
    before entering write_tx at all for a numeric field, so a rejected
    value never opens a transaction in the first place. Never lets a raw
    ValueError/TypeError (from a malformed string, e.g. "500g" instead of
    500) reach the caller -- everything here becomes a clean
    ValidationError (422) with a message safe to show directly."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValidationError(f"{field}: new_value must be a number")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}: {raw!r} is not a valid number")
    if math.isnan(value) or math.isinf(value):
        raise ValidationError(f"{field}: new_value must be a finite number")
    if value <= 0:
        raise ValidationError(f"{field}: new_value must be greater than zero")
    return value


def _validate_date_value(field: str, raw: float | str | None) -> str:
    """fefo_batch.expires_at is the one non-numeric changeable field --
    validates as a real calendar date (YYYY-MM-DD, the format every other
    date in this schema uses) rather than trusting an arbitrary string
    into a column FEFO ordering depends on."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(f"{field}: new_value must be a date")
    cleaned = raw.strip()
    try:
        _dt.date.fromisoformat(cleaned)
    except ValueError:
        raise ValidationError(
            f"{field}: {raw!r} is not a valid date (expected YYYY-MM-DD)"
        )
    return cleaned


def _sub_recipe_ids(conn: sqlite3.Connection) -> set[int]:
    return {r["id"] for r in conn.execute("SELECT id FROM recipe WHERE kind='sub_recipe'")}


def list_queue(conn: sqlite3.Connection) -> dict:
    """Every field in v_needs_verification, translated and prioritized. Still
    real rows from the real view -- this only adds copy and grouping on top,
    it does not filter or reinterpret what needs_verification means."""
    raw = [dict(r) for r in conn.execute(
        "SELECT * FROM v_needs_verification ORDER BY field, entity_name"
    )]
    sub_recipe_ids = _sub_recipe_ids(conn)
    today = timeutil.business_today_iso()

    items = []
    for r in raw:
        field = r["field"]
        copy = FIELD_COPY[field]
        group = GROUP_LABEL.get(field)
        # 2026-08 audit finding: an open FEFO batch's expires_at can need
        # verification (is this recorded date even correct?) AND already
        # be in the past -- two different questions the queue was
        # conflating into one "confirm this value" row with no way to
        # tell them apart. already_expired says so explicitly, computed
        # here (not stored) so it's never stale -- same principle as
        # /console/expiry's own days_to_expiry.
        r["already_expired"] = (
            field == "fefo_batch.expires_at"
            and r["current_value"] not in (None, "(none)")
            and r["current_value"] < today
        )
        if field == "recipe_line.qty_base":
            # entity_id here is the recipe's own recipe_id text (REC-####);
            # row_id is the recipe_line's own id. Split sauce/house-prep
            # recipes from ordinary menu recipes by looking up the parent
            # recipe's kind.
            recipe_pk = conn.execute(
                "SELECT recipe_id FROM recipe_line WHERE id=?", (r["row_id"],)
            ).fetchone()["recipe_id"]
            group = ("Sauce & house-prep recipes" if recipe_pk in sub_recipe_ids
                     else "Menu recipes")
        items.append({
            **r,
            "priority": PRIORITY[field],
            "priority_label": PRIORITY_LABEL[PRIORITY[field]],
            "group_label": group,
            "field_label": copy["label"],
            "question": copy["question"],
            "why_it_matters": copy["why"],
            "what_to_check": copy["check"],
        })

    items.sort(key=lambda i: (i["priority"], i["group_label"], i["entity_name"]))
    by_priority: dict[int, int] = {}
    by_field: dict[str, int] = {}
    for i in items:
        by_priority[i["priority"]] = by_priority.get(i["priority"], 0) + 1
        by_field[i["field"]] = by_field.get(i["field"], 0) + 1
    return {"items": items, "count": len(items), "by_priority": by_priority, "by_field": by_field}


def history_for(conn: sqlite3.Connection, field: str, row_id: int) -> list[dict]:
    """Every decision ever recorded for this exact field/row -- the
    provenance trail: what it was, what it became, who decided, and when.
    Oldest first, so it reads as a timeline."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM verification_event WHERE field=? AND row_id=? ORDER BY id",
        (field, row_id),
    )]


def _log(conn, field, row_id, entity_name, action, previous_value, new_value, note, user_id):
    conn.execute(
        "INSERT INTO verification_event "
        "(field, row_id, entity_name, action, previous_value, new_value, note, user_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (field, row_id, entity_name, action,
         None if previous_value is None else str(previous_value),
         None if new_value is None else str(new_value),
         note, user_id),
    )


def _current_ingredient_row(conn, ingredient_pk):
    row = conn.execute("SELECT * FROM ingredient WHERE id=?", (ingredient_pk,)).fetchone()
    if not row:
        raise NotFoundError(f"ingredient {ingredient_pk} not found")
    return row


def resolve(
    conn: sqlite3.Connection, field: str, row_id: int, action: str,
    new_value: float | str | None, note: str | None, user_id: str | None,
) -> dict:
    """Confirm, change, or keep-as-estimate for one verification item.

    'confirm' marks the current value correct as-is (new_value is read from
    the row itself, never trusted from the caller, so a confirm cannot
    silently smuggle in a different number). 'change' requires new_value and
    replaces it. 'keep_estimate' touches no operational column at all -- only
    the audit trail records that the owner reviewed it and chose not to
    decide yet. Every path is logged.
    """
    if action not in ("confirm", "change", "keep_estimate"):
        raise InvalidStateError(f"unknown verification action '{action}'")
    if action == "change" and new_value is None:
        raise InvalidStateError("a changed value is required for action='change'")
    if field not in PRIORITY:
        raise InvalidStateError(f"unknown verification field '{field}'")

    # Validated BEFORE write_tx opens, not inside it -- a rejected value
    # never starts a transaction, never reaches a database UPDATE, and
    # never reaches _log() (no verification_event row for a change that
    # didn't happen). A malformed new_value (wrong type, NaN/infinite,
    # zero, negative, or an unparseable string) becomes a clean
    # ValidationError here, never a raw ValueError/TypeError escaping to
    # the caller as an opaque 500.
    validated_value: float | str | None = None
    if action == "change":
        if field in NUMERIC_FIELDS:
            validated_value = validate_numeric_value(field, new_value)
        elif field == "fefo_batch.expires_at":
            validated_value = _validate_date_value(field, new_value)

    with write_tx(conn):
        if field in ("ingredient.pack_size", "ingredient.yield_factor", "ingredient.unit_cost"):
            col = field.split(".")[1]
            ing = _current_ingredient_row(conn, row_id)
            current = ing[col]
            if action == "keep_estimate":
                _log(conn, field, row_id, ing["name"], "kept_estimate", current, None, note, user_id)
                return {"field": field, "row_id": row_id, "action": action, "value": current}
            value = current if action == "confirm" else validated_value
            conn.execute(
                f"UPDATE ingredient SET {col}=?, {col}_status='verified', "
                f"updated_at=datetime('now') WHERE id=?",
                (value, row_id),
            )
            if col in ("pack_size", "yield_factor"):
                # Resync the cached on-hand total against the FEFO batches'
                # own (unchanged) base-unit quantities -- the conversion
                # factor just changed, so on_hand_base is restated against
                # the batches' real qty_base rather than left stale.
                conn.execute(
                    """
                    UPDATE ingredient
                       SET on_hand_base = (SELECT SUM(qty_base) FROM fefo_batch
                                            WHERE ingredient_id=? AND state='open')
                     WHERE id=? AND EXISTS (SELECT 1 FROM fefo_batch
                                             WHERE ingredient_id=? AND state='open')
                    """,
                    (row_id, row_id, row_id),
                )
            _log(conn, field, row_id, ing["name"],
                 "confirmed" if action == "confirm" else "changed",
                 current, value, note, user_id)
            return {"field": field, "row_id": row_id, "action": action, "value": value}

        if field == "recipe_line.qty_base":
            line = conn.execute(
                "SELECT rl.*, r.name AS recipe_name FROM recipe_line rl "
                "JOIN recipe r ON r.id = rl.recipe_id WHERE rl.id=?", (row_id,)
            ).fetchone()
            if not line:
                raise NotFoundError(f"recipe line {row_id} not found")
            entity_name = f"{line['recipe_name']} / {line['display_label']}"
            current = line["qty_base"]
            if action == "keep_estimate":
                _log(conn, field, row_id, entity_name, "kept_estimate", current, None, note, user_id)
                return {"field": field, "row_id": row_id, "action": action, "value": current}
            value = current if action == "confirm" else validated_value
            conn.execute(
                "UPDATE recipe_line SET qty_base=?, qty_status='verified' WHERE id=?",
                (value, row_id),
            )
            _log(conn, field, row_id, entity_name,
                 "confirmed" if action == "confirm" else "changed",
                 current, value, note, user_id)
            return {"field": field, "row_id": row_id, "action": action, "value": value}

        if field == "fefo_batch.expires_at":
            batch = conn.execute(
                "SELECT b.*, i.name AS ingredient_name FROM fefo_batch b "
                "JOIN ingredient i ON i.id = b.ingredient_id WHERE b.id=?", (row_id,)
            ).fetchone()
            if not batch:
                raise NotFoundError(f"batch {row_id} not found")
            current = batch["expires_at"]
            if action == "keep_estimate":
                _log(conn, field, row_id, batch["ingredient_name"], "kept_estimate", current, None, note, user_id)
                return {"field": field, "row_id": row_id, "action": action, "value": current}
            value = current if action == "confirm" else validated_value
            conn.execute(
                "UPDATE fefo_batch SET expires_at=?, expiry_status='verified' WHERE id=?",
                (value, row_id),
            )
            _log(conn, field, row_id, batch["ingredient_name"],
                 "confirmed" if action == "confirm" else "changed",
                 current, value, note, user_id)
            return {"field": field, "row_id": row_id, "action": action, "value": value}

        if field == "unli_coefficient.lambda":
            coeff = conn.execute(
                "SELECT c.*, u.label FROM unli_coefficient c "
                "JOIN unli_item u ON u.id = c.unli_item_id WHERE c.id=?", (row_id,)
            ).fetchone()
            if not coeff:
                raise NotFoundError(f"coefficient {row_id} not found")
            current = coeff["lambda_value"]
            if action == "keep_estimate":
                _log(conn, field, row_id, coeff["label"], "kept_estimate", current, None, note, user_id)
                return {"field": field, "row_id": row_id, "action": action, "value": current}
            if action == "confirm":
                # The rate itself is unchanged -- provisional_for_date() reads
                # lambda_value, never status, so flipping status in place
                # here changes nothing about deduction or reconciliation math,
                # historical or future.
                conn.execute("UPDATE unli_coefficient SET status='verified' WHERE id=?", (row_id,))
                _log(conn, field, row_id, coeff["label"], "confirmed", current, current, note, user_id)
                return {"field": field, "row_id": row_id, "action": action, "value": current}
            # action == "change": never overwrite lambda_value in place --
            # provisional_for_date() re-reads it live per past order via
            # effective_from, so an in-place edit would retroactively change
            # what an already-reconciled day is judged against. Insert a new
            # row instead, exactly like recalibrate() does, so only orders
            # from this moment on see the new rate.
            value = validated_value
            conn.execute(
                "INSERT INTO unli_coefficient (unli_item_id, lambda_value, role, status, effective_from) "
                "VALUES (?,?,?, 'verified', datetime('now'))",
                (coeff["unli_item_id"], value, coeff["role"]),
            )
            # The superseded row no longer reflects the stream's current
            # rate -- leaving it 'needs_verification' would show a phantom
            # duplicate in the queue forever. Its own historical
            # reproducibility is untouched: only status changes, never
            # lambda_value or effective_from on this row.
            conn.execute("UPDATE unli_coefficient SET status='verified' WHERE id=?", (row_id,))
            _log(conn, field, row_id, coeff["label"], "changed", current, value, note, user_id)
            return {"field": field, "row_id": row_id, "action": action, "value": value}

        raise InvalidStateError(f"unknown verification field '{field}'")
