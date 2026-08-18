"""Phase 3 — product-combination analysis via FP-growth (mlxtend).

Finds menu items that real customers actually order together often enough
to be a real pattern — not "these two are both popular so they look
correlated," but lift-filtered: the pair co-occurs more than random chance
would predict given how often each item sells on its own. Built on the same
real order_line/customer_order history everything else in Phase 3 reads,
with the same rule as forecasting.py and anomaly.py: report "insufficient
data" honestly below MIN_TRANSACTIONS rather than mining noise.
"""
from __future__ import annotations

import sqlite3

import pandas as pd
from mlxtend.frequent_patterns import association_rules, fpgrowth
from mlxtend.preprocessing import TransactionEncoder

from . import timeutil

MIN_TRANSACTIONS = 30     # orders needed before a frequent-itemset pass is trustworthy
MIN_SUPPORT = 0.02        # a pair must appear in at least 2% of orders
MIN_CONFIDENCE = 0.3      # given item A, item B follows at least 30% of the time
MIN_LIFT = 1.2            # co-occurs 20%+ more than random chance predicts
COMBO_DISCOUNT = 0.92     # suggested bundle price = 8% off the sum of both items


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def product_combinations(conn: sqlite3.Connection, lookback_days: int = 90, top_n: int = 15) -> dict:
    lines = _rows(conn.execute(
        """
        SELECT ol.order_id, m.name
          FROM order_line ol
          JOIN customer_order o ON o.id = ol.order_id
          JOIN menu_item m ON m.id = ol.menu_item_id
         WHERE o.status != 'void' AND o.created_at >= ?
        """,
        (timeutil.utc_bound_n_manila_days_ago(lookback_days),),
    ))

    baskets: dict[int, set] = {}
    for r in lines:
        baskets.setdefault(r["order_id"], set()).add(r["name"])
    basket_list = list(baskets.values())
    transaction_count = len(basket_list)

    shape = {"insufficient_data": True, "transaction_count": transaction_count,
             "required": MIN_TRANSACTIONS, "combos": []}
    if transaction_count < MIN_TRANSACTIONS:
        return shape

    te = TransactionEncoder()
    arr = te.fit(basket_list).transform(basket_list)
    df = pd.DataFrame(arr, columns=te.columns_)

    freq = fpgrowth(df, min_support=MIN_SUPPORT, use_colnames=True)
    if freq.empty:
        return {**shape, "insufficient_data": False}

    rules = association_rules(freq, metric="lift", min_threshold=MIN_LIFT,
                              num_itemsets=len(freq))
    rules = rules[
        (rules["confidence"] >= MIN_CONFIDENCE)
        & (rules["antecedents"].apply(len) == 1)
        & (rules["consequents"].apply(len) == 1)
    ].sort_values("lift", ascending=False)

    prices = {r["name"]: r["price"] for r in _rows(conn.execute("SELECT name, price FROM menu_item"))}

    combos = []
    seen_pairs = set()
    for _, row in rules.iterrows():
        a, b = next(iter(row["antecedents"])), next(iter(row["consequents"]))
        pair_key = tuple(sorted((a, b)))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        a_price, b_price = prices.get(a), prices.get(b)
        individual_total = round(a_price + b_price, 2) if a_price is not None and b_price is not None else None
        combos.append({
            "item_a": a, "item_b": b,
            "support": round(float(row["support"]), 4),
            "confidence": round(float(row["confidence"]), 4),
            "lift": round(float(row["lift"]), 3),
            "individual_total": individual_total,
            "suggested_combo_price": round(individual_total * COMBO_DISCOUNT, 2) if individual_total else None,
        })
        if len(combos) >= top_n:
            break

    return {"insufficient_data": False, "transaction_count": transaction_count,
            "required": MIN_TRANSACTIONS, "combos": combos}


def model_details(conn: sqlite3.Connection, lookback_days: int = 90) -> dict:
    """Real parameters and current stats for the FP-growth pass — backs the
    technical panel under System Check, not the main Reports page."""
    result = product_combinations(conn, lookback_days)
    return {
        "model": "FP-growth (mlxtend)",
        "min_transactions": MIN_TRANSACTIONS,
        "min_support": MIN_SUPPORT,
        "min_confidence": MIN_CONFIDENCE,
        "min_lift": MIN_LIFT,
        "lookback_days": lookback_days,
        "transactions_evaluated": result["transaction_count"],
        "combos_found": len(result["combos"]),
    }
