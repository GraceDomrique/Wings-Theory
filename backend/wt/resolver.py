"""Recipe expansion: menu item -> recipe -> deductable leaf ingredients.

A recipe line may point at an ingredient that is itself produced in-house — the
fourteen flavour sauces, Ranch Dip, simple syrup. Those carry kind='sub_recipe'.

Two consumption modes, and the difference is a real operational choice:

  exploded  The sub-recipe holds no stock. Selling a Wings Platter deducts
            buffalo hot sauce, butter and honey directly. Correct when the
            kitchen makes sauce to order or in untracked batches, which is
            what Wings Theory does today.

  stocked   The sub-recipe is batch-prepped and counted as its own item.
            Selling deducts prepared sauce; a separate production entry
            converts raw goods into it. Correct once the kitchen starts
            logging prep batches, and it is a better model then — it separates
            production variance from service variance.

Default is 'exploded'. Switching an ingredient to 'stocked' later is a data
change, not a code change: expansion stops at it automatically.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .errors import InvalidStateError, NotFoundError, RecipeCycleError

MAX_DEPTH = 8


@dataclass(frozen=True)
class Leaf:
    ingredient_id: int
    qty_base: float
    name: str
    base_unit: str
    is_deductable: bool
    block_reason: str | None


def expand_recipe(conn: sqlite3.Connection, recipe_pk: int, multiplier: float = 1.0) -> list[Leaf]:
    """Deductable leaves for one recipe, scaled by `multiplier`.

    Backed by v_recipe_leaf, which does the recursion in SQL. Keeping it there
    means the POS availability gate and the deduction path cannot disagree
    about what a recipe contains — they read the same view.
    """
    rows = conn.execute(
        """
        SELECT l.ingredient_id, l.qty_base, i.name, i.base_unit,
               r.is_deductable, r.block_reason
          FROM v_recipe_leaf l
          JOIN ingredient i             ON i.id = l.ingredient_id
          JOIN v_ingredient_readiness r ON r.id = l.ingredient_id
         WHERE l.recipe_id = ?
         ORDER BY l.ingredient_id
        """,
        (recipe_pk,),
    ).fetchall()
    return [
        Leaf(
            ingredient_id=r["ingredient_id"],
            qty_base=r["qty_base"] * multiplier,
            name=r["name"],
            base_unit=r["base_unit"],
            is_deductable=bool(r["is_deductable"]),
            block_reason=r["block_reason"],
        )
        for r in rows
    ]


def order_requirements(conn: sqlite3.Connection, order_pk: int) -> dict[int, float]:
    """Total base-unit requirement per ingredient for a whole order.

    Returned sorted by ingredient_id. That ordering is load-bearing: acquiring
    row locks in a consistent order across concurrent transactions is what
    prevents deadlock once this moves to a row-locking engine. SQLite
    serialises writers anyway, so it costs nothing here and buys portability.

    Unlimited items have no recipe_id — by design, their per-serving
    consumption is unknown at order time, which is the entire reason the
    unli_item/unli_coefficient vector exists (see the manuscript's three-stage
    provisional/actual/reconciled model). An order line for one of them
    therefore CANNOT be picked up by the ordinary recipe-leaf join below: that
    join is keyed on menu_item.recipe_id, which is NULL for every unli item.
    Missing this case doesn't raise or warn — the join simply returns no rows
    for that line — so an earlier version of this function silently deducted
    nothing at all for an Unlimited Wings order while still charging the
    customer full price. Every unli order_line is handled separately below,
    using the order's head_count against each stream's ROLLING coefficient
    (the live, trend-adjusted estimate — the frozen baseline is reserved for
    variance comparison, never for sizing an actual deduction).
    """
    need: dict[int, float] = {}
    for r in conn.execute(
        """
        SELECT l.ingredient_id, SUM(l.qty_base * ol.qty) AS need
          FROM order_line ol
          JOIN menu_item  m ON m.id = ol.menu_item_id
          JOIN v_recipe_leaf l ON l.recipe_id = m.recipe_id
         WHERE ol.order_id = ?
         GROUP BY l.ingredient_id
        """,
        (order_pk,),
    ).fetchall():
        need[r["ingredient_id"]] = need.get(r["ingredient_id"], 0.0) + r["need"]

    unli_qty = conn.execute(
        """
        SELECT SUM(ol.qty) AS n
          FROM order_line ol JOIN menu_item m ON m.id = ol.menu_item_id
         WHERE ol.order_id = ? AND m.is_unli = 1
        """,
        (order_pk,),
    ).fetchone()["n"]

    if unli_qty:
        heads = conn.execute(
            "SELECT head_count FROM customer_order WHERE id=?", (order_pk,)
        ).fetchone()["head_count"]
        if not heads:
            raise InvalidStateError(
                "an unlimited item was ordered with no head count — cannot size "
                "the deduction; head_count must be set on the order"
            )
        for stream in conn.execute(
            """
            SELECT ui.ingredient_id, uc.lambda_value
              FROM unli_item ui
              JOIN unli_coefficient uc ON uc.unli_item_id = ui.id
             WHERE ui.is_active = 1 AND uc.role = 'rolling'
               AND uc.effective_from = (
                     SELECT MAX(uc2.effective_from) FROM unli_coefficient uc2
                      WHERE uc2.unli_item_id = ui.id AND uc2.role = 'rolling')
            """
        ).fetchall():
            qty = stream["lambda_value"] * heads * unli_qty
            need[stream["ingredient_id"]] = need.get(stream["ingredient_id"], 0.0) + qty

    return need


def validate_recipe(conn: sqlite3.Connection, recipe_pk: int) -> list[str]:
    """Structural check run before a recipe is saved or activated.

    Walks the graph in Python rather than SQL so a cycle can be reported as the
    actual path that closed it. The SQL view guards against cycles too, but it
    guards by truncating — useful at runtime, useless in an error message.
    """
    row = conn.execute("SELECT id, name FROM recipe WHERE id=?", (recipe_pk,)).fetchone()
    if not row:
        raise NotFoundError(f"recipe {recipe_pk} not found")

    problems: list[str] = []
    seen_path: list[str] = []

    def walk(rid: int, depth: int, path: tuple[int, ...]) -> None:
        if depth > MAX_DEPTH:
            problems.append(f"expansion exceeded {MAX_DEPTH} levels")
            return
        lines = conn.execute(
            """
            SELECT rl.ingredient_id, rl.display_label, rl.qty_base,
                   i.name, i.kind, i.sub_recipe_mode,
                   sub.id AS sub_recipe_pk, sub.name AS sub_name, sub.yield_qty
              FROM recipe_line rl
              JOIN ingredient i ON i.id = rl.ingredient_id
              LEFT JOIN recipe sub ON sub.produces_ingredient_id = i.id AND sub.is_active = 1
             WHERE rl.recipe_id = ?
            """,
            (rid,),
        ).fetchall()

        if not lines:
            problems.append(f"recipe {rid} has no lines")

        for ln in lines:
            if ln["kind"] == "sub_recipe" and ln["sub_recipe_mode"] == "exploded":
                if ln["sub_recipe_pk"] is None:
                    problems.append(
                        f"'{ln['name']}' is an exploded sub-recipe but has no active "
                        f"producing recipe"
                    )
                    continue
                if ln["sub_recipe_pk"] in path:
                    chain = " -> ".join(seen_path + [ln["sub_name"]])
                    raise RecipeCycleError(f"sub-recipe cycle: {chain}")
                seen_path.append(ln["sub_name"])
                walk(ln["sub_recipe_pk"], depth + 1, path + (ln["sub_recipe_pk"],))
                seen_path.pop()

    seen_path.append(row["name"])
    walk(recipe_pk, 0, (recipe_pk,))

    for leaf in expand_recipe(conn, recipe_pk):
        if not leaf.is_deductable:
            problems.append(f"'{leaf.name}': {leaf.block_reason}")

    unmapped = conn.execute(
        "SELECT COUNT(*) AS n FROM unmapped_recipe_line WHERE recipe_id=? AND state='open'",
        (recipe_pk,),
    ).fetchone()["n"]
    if unmapped:
        problems.append(f"{unmapped} ingredient line(s) still unmapped")

    return problems


def recipe_cost(conn: sqlite3.Connection, recipe_pk: int) -> dict:
    """Costed expansion. Any leaf without a verified unit cost is reported
    separately rather than folded in at zero — a recipe cost that silently
    omits ingredients is worse than no recipe cost."""
    total = 0.0
    priced, unpriced = [], []
    for leaf in expand_recipe(conn, recipe_pk):
        row = conn.execute(
            "SELECT unit_cost, unit_cost_status, pack_size, yield_factor, purchase_unit "
            "FROM ingredient WHERE id=?",
            (leaf.ingredient_id,),
        ).fetchone()
        if row["unit_cost"] is None or row["pack_size"] is None:
            unpriced.append(leaf.name)
            continue
        per_base = row["unit_cost"] / (row["pack_size"] * row["yield_factor"])
        cost = per_base * leaf.qty_base
        total += cost
        priced.append(
            {
                "ingredient": leaf.name,
                "qty_base": round(leaf.qty_base, 4),
                "base_unit": leaf.base_unit,
                "cost": round(cost, 4),
                "cost_confirmed": row["unit_cost_status"] == "verified",
            }
        )
    return {
        "total_cost": round(total, 2),
        "lines": priced,
        "unpriced_ingredients": unpriced,
        "complete": not unpriced,
    }
