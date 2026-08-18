"""Export the seeded database as a JSON bundle the console can run on.

The console is a single self-contained page; a strict CSP stops it reaching the
LAN backend. Rather than let it keep its own invented arrays — which is how the
numbers drifted apart in the first place — it gets the real data and runs the
real domain logic client-side against it.

Only facts are exported. Everything derived (available stock, days of supply,
stock status, menu availability, capacity) is computed by the page from these
facts, so there is exactly one definition of each and no opportunity to drift.

Run:  python -m wt.export_ui > ui_data.json
"""
from __future__ import annotations

import json
import sys

from . import db


def build() -> dict:
    conn = db.get_conn()

    ingredients = [
        {
            "id": r["id"],
            "code": r["ingredient_id"],
            "name": r["name"],
            "cat": r["category"],
            "kind": r["kind"],
            "mode": r["sub_recipe_mode"],
            "base": r["base_unit"],
            "pu": r["purchase_unit"],
            "pack": r["pack_size"],
            "packSt": r["pack_size_status"],
            "yield": r["yield_factor"],
            "yieldSt": r["yield_factor_status"],
            "onHand": round(r["on_hand_base"], 4),
            "cost": r["unit_cost"],
            "costSt": r["unit_cost_status"],
            "lead": r["lead_time_days"],
            "sup": r["supplier"],
            "loc": r["storage_location"],
        }
        for r in conn.execute(
            "SELECT * FROM ingredient WHERE is_active=1 ORDER BY id")
    ]

    # expanded leaves: what a sale actually deducts, sub-recipes already resolved
    leaves: dict[int, list] = {}
    for r in conn.execute(
        "SELECT recipe_id, ingredient_id, qty_base FROM v_recipe_leaf ORDER BY recipe_id"
    ):
        leaves.setdefault(r["recipe_id"], []).append(
            [r["ingredient_id"], round(r["qty_base"], 5)])

    # authored lines, for the recipe editor (pre-expansion, keeps sub-recipes)
    lines: dict[int, list] = {}
    for r in conn.execute(
        """SELECT rl.recipe_id, rl.ingredient_id, rl.display_label, rl.qty_base,
                  rl.qty_status, rl.is_optional
             FROM recipe_line rl ORDER BY rl.recipe_id, rl.sort_order, rl.id"""
    ):
        lines.setdefault(r["recipe_id"], []).append({
            "ing": r["ingredient_id"], "label": r["display_label"],
            "qty": round(r["qty_base"], 5), "st": r["qty_status"],
            "opt": bool(r["is_optional"]),
        })

    recipes = [
        {
            "id": r["id"], "code": r["recipe_id"], "name": r["name"],
            "kind": r["kind"], "produces": r["produces_ingredient_id"],
            "yieldQty": r["yield_qty"], "yieldUnit": r["yield_unit"],
            "lines": lines.get(r["id"], []),
            "leaves": leaves.get(r["id"], []),
        }
        for r in conn.execute("SELECT * FROM recipe WHERE is_active=1 ORDER BY id")
    ]

    menu = [
        {
            "id": r["menu_item_pk"], "code": r["menu_item_id"], "name": r["name"],
            "cat": r["category"], "price": r["price"], "recipe": r["recipe_id"],
            "unli": bool(r["is_unli"]), "ready": bool(r["structurally_ready"]),
        }
        for r in conn.execute(
            "SELECT * FROM v_menu_item_availability ORDER BY category, name")
    ]

    batches = [
        {
            "id": r["id"], "ing": r["ingredient_id"], "lot": r["lot_no"],
            "qty": round(r["qty_base"], 3), "recv": r["received_at"],
            "exp": r["expires_at"], "expSt": r["expiry_status"],
        }
        for r in conn.execute(
            "SELECT * FROM fefo_batch WHERE state='open' ORDER BY ingredient_id")
    ]

    unli = [
        {
            "key": r["item_key"], "label": r["label"], "ing": r["ingredient_id"],
            "window": r["window_days"],
            "baseline": conn.execute(
                "SELECT lambda_value FROM unli_coefficient WHERE unli_item_id=? "
                "AND role='baseline' ORDER BY effective_from DESC LIMIT 1",
                (r["id"],)).fetchone()["lambda_value"],
            "rolling": conn.execute(
                "SELECT lambda_value FROM unli_coefficient WHERE unli_item_id=? "
                "AND role='rolling' ORDER BY effective_from DESC LIMIT 1",
                (r["id"],)).fetchone()["lambda_value"],
        }
        for r in conn.execute("SELECT * FROM unli_item WHERE is_active=1")
    ]

    verify = [
        {"field": r["field"], "entity": r["entity_id"], "name": r["entity_name"],
         "value": r["current_value"], "ctx": r["context"], "row": r["row_id"]}
        for r in conn.execute("SELECT * FROM v_needs_verification")
    ]

    queue = [
        {"id": r["id"], "label": r["source_ingredient_label"],
         "recipe": r["source_recipe_name"], "qty": r["source_qty_text"],
         "reason": r["reason"], "state": r["state"],
         "resolved": r["resolved_ingredient_id"], "note": r["note"]}
        for r in conn.execute(
            "SELECT * FROM unmapped_recipe_line ORDER BY state, reason")
    ]

    return {
        "generated": "seeded database export",
        "ingredients": ingredients,
        "recipes": recipes,
        "menu": menu,
        "batches": batches,
        "unli": unli,
        "verify": verify,
        "queue": queue,
        "counts": {
            "ingredients": len(ingredients),
            "recipes": len(recipes),
            "menu": len(menu),
            "recipeLines": sum(len(r["lines"]) for r in recipes),
            "batches": len(batches),
            "verify": len(verify),
            "queueOpen": sum(1 for q in queue if q["state"] == "open"),
        },
    }


if __name__ == "__main__":
    json.dump(build(), sys.stdout, separators=(",", ":"))
