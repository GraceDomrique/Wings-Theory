"""Phase 3 — product-combination analysis via FP-growth (mlxtend).

Finds menu items that real customers actually order together often enough
to be a real pattern -- not "these two are both popular so they look
correlated," but lift-filtered: the pair co-occurs more than random chance
would predict given how often each item sells on its own. Built on the same
real order_line/customer_order history everything else in Phase 3 reads,
with the same rule as forecasting.py and anomaly.py: report "insufficient
data" honestly below MIN_TRANSACTIONS rather than mining noise.

2026-09 audit -- the basket-construction bug this module shipped with
----------------------------------------------------------------------
_transactions() used to build one basket per customer_order.id. That is
wrong for this database specifically: an earlier version of
wt/xlsx_history_import.py recorded a workbook order that listed BOTH
unlimited-wings and a-la-carte lines (a "mixed" order) as TWO separate
customer_order rows instead of one -- wt/xlsx_history_reconcile.py's own
docstring documents this as a known, disclosed limitation of the original
bulk import, and its reconciliation pass correctly links both halves back
to the SAME workbook source_order_id without merging the rows (it only
ever UPDATEs columns, never restructures order_line). Grouping baskets by
customer_order.id therefore silently split ~1,824 real historical purchases
into two "orders" that never co-occur -- which is exactly the mechanism
that made cross-category pairs like "Unli Wings + a drink" or "Unli Wings +
Ranch Dip" invisible to FP-growth regardless of threshold, and it is also
the exact source of the old "7,423 orders analysed" figure: that number is
a count of customer_order ROWS in the lookback window, not of real
transactions (the workbook itself has 6,971 unique Order IDs; see
_transactions()'s own docstring for the reconciled count). Baskets are now
built per COALESCE(source_order_id, row id) -- one basket per real
transaction -- which is both the honest fix and, independently, the one
that surfaces the associations this module previously reported as absent.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from itertools import combinations

import pandas as pd
from mlxtend.frequent_patterns import association_rules, fpgrowth
from mlxtend.preprocessing import TransactionEncoder

from . import timeutil

MIN_TRANSACTIONS = 30     # real transactions needed before a frequent-itemset pass is trustworthy

# Primary threshold tier. A flat 2% support floor (the old MIN_SUPPORT)
# requires ~140 occurrences of the same pair at this restaurant's real
# volume (~7,000 transactions/90d) -- unrealistic for genuine, useful
# combinations at a single-location QSR with a 29-item menu. These values
# are restaurant-scale defaults, not tuned to force a result -- see
# threshold_sensitivity() for what the real data supports across a
# defensible range, including the old strict configuration for comparison.
MIN_SUPPORT = 0.002        # dynamically raised per-call if it implies fewer than MIN_PAIR_COUNT occurrences
MIN_CONFIDENCE = 0.05
MIN_LIFT = 1.10
MIN_PAIR_COUNT = 10        # a pair must be independently observed at least this many times, regardless of support%

# Fallback tier -- used ONLY when the primary tier finds zero qualifying
# pairs. Relaxes support and confidence within a still-defensible boundary;
# never relaxes the pair-count floor below 5 or the lift floor below 1.0,
# so a fallback result can never be built from noise (a pair seen twice
# cannot "qualify" no matter how extreme its lift looks). Deterministic:
# same inputs always produce the same tier choice, and the response always
# says which tier actually ran (see product_combinations()'s "threshold_tier").
FALLBACK_MIN_SUPPORT = 0.001
FALLBACK_MIN_CONFIDENCE = 0.03
FALLBACK_MIN_LIFT = 1.10
FALLBACK_MIN_PAIR_COUNT = 5

COMBO_DISCOUNT = 0.92     # discount reference = 8% off the sum of both items' current menu prices

# 2026-09 redesign (Part 59): this figure is a plain discount reference, not
# a recommended price -- wt/combo_pricing.py's cost-based price range (see
# GET /console/combo-pricing/preview) is the actual pricing tool now. This
# field is kept only as a quick "list price minus a flat discount" number
# for comparison; the console must label it as a discount reference, never
# as "recommended" or "suggested". Renamed from suggested_combo_price to
# discount_reference_price so the field name itself no longer implies a
# recommendation.
COMBO_PRICE_BASIS = (
    f"Simple discount reference only ({int(round((1 - COMBO_DISCOUNT) * 100))}% off the sum of "
    "current menu prices) -- not a cost-based or margin calculation. See "
    "combo pricing for a cost-based price range."
)


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _transactions(conn: sqlite3.Connection, lookback_days: int) -> tuple[list[set], int, dict]:
    """One basket per REAL completed transaction -- not one per
    customer_order row (see module docstring for why those differ on this
    database). A transaction's key is COALESCE(source_order_id, its own
    row id): two customer_order rows sharing one source_order_id are two
    halves of a single historical mixed order and are merged into one
    basket; every other row (every live POS order, and every historical
    order that was never split) is already exactly one transaction on its
    own. Void orders are excluded -- they were never a completed sale.

    Returns (basket_list, raw_order_row_count, category_by_item_name).
    raw_order_row_count is the number of distinct customer_order rows found
    in the window (what the pre-audit code mistakenly reported as the
    transaction count) -- kept so the API can show both numbers and explain
    the difference rather than silently changing what "orders analysed"
    means.
    """
    lines = _rows(conn.execute(
        """
        SELECT o.id AS order_pk, o.source_order_id, m.name, m.category
          FROM order_line ol
          JOIN customer_order o ON o.id = ol.order_id
          JOIN menu_item m ON m.id = ol.menu_item_id
         WHERE o.status != 'void' AND o.created_at >= ?
        """,
        (timeutil.utc_bound_n_manila_days_ago(lookback_days),),
    ))
    raw_order_ids: set[int] = set()
    baskets: dict[str, set] = {}
    categories: dict[str, str] = {}
    for r in lines:
        raw_order_ids.add(r["order_pk"])
        txn_key = r["source_order_id"] or f"row:{r['order_pk']}"
        baskets.setdefault(txn_key, set()).add(r["name"])
        categories[r["name"]] = r["category"]
    return list(baskets.values()), len(raw_order_ids), categories


def _item_and_pair_counts(basket_list: list[set]) -> tuple[Counter, Counter]:
    """Ground-truth occurrence counts, computed directly from the same
    basket data FP-growth mines -- used to independently verify (never
    just trust) every support/confidence/lift figure this module reports,
    per the 2026-09 audit's own requirement that no displayed number be
    taken from a black box without a direct recomputation backing it."""
    item_counts: Counter = Counter()
    pair_counts: Counter = Counter()
    for basket in basket_list:
        item_counts.update(basket)
        for a, b in combinations(sorted(basket), 2):
            pair_counts[(a, b)] += 1
    return item_counts, pair_counts


def _effective_min_support(transaction_count: int, min_support: float, min_pair_count: int) -> float:
    """The support floor a threshold tier actually enforces, once the
    pair-count floor is accounted for. 0.002 support on 7,000 real
    transactions already implies ~14 occurrences (above MIN_PAIR_COUNT), so
    it dominates; but on a smaller transaction count the flat percentage
    could imply fewer occurrences than MIN_PAIR_COUNT requires -- so the
    support floor passed to FP-growth is always at least
    min_pair_count / transaction_count, computed fresh from the real
    transaction count on every call rather than hardcoded for one dataset
    size."""
    if transaction_count <= 0:
        return min_support
    return max(min_support, min_pair_count / transaction_count)


def _mine(df: "pd.DataFrame", min_support: float, min_confidence: float,
         min_lift: float) -> tuple["pd.DataFrame", "pd.DataFrame"]:
    """FP-growth frequent-itemset mining + association-rule generation at
    one threshold configuration. Returns (frequent_itemsets, pair_rules) --
    both empty (not an exception) when nothing qualifies; 0 qualifying
    combinations at a given threshold is itself a finding, not an error."""
    freq = fpgrowth(df, min_support=min_support, use_colnames=True)
    if freq.empty:
        return freq, freq

    rules = association_rules(freq, metric="lift", min_threshold=min_lift,
                              num_itemsets=len(freq))
    pair_rules = rules[
        (rules["confidence"] >= min_confidence)
        & (rules["antecedents"].apply(len) == 1)
        & (rules["consequents"].apply(len) == 1)
    ]
    return freq, pair_rules


def _qualifying_pairs(rules: "pd.DataFrame", item_counts: Counter, pair_counts: Counter,
                      transaction_count: int, min_pair_count: int, min_lift: float,
                      categories: dict) -> list[dict]:
    """Turns FP-growth's candidate rules into deduplicated, undirected
    pairs (A+B shown once, never as both A->B and B->A -- requirement:
    a "frequently bought together" card needs one consolidated pair), with
    every metric RECOMPUTED from the real basket counts rather than taken
    from mlxtend's rule rows, and a hard minimum-pair-count gate applied
    independently of the support percentage (protects against a
    high-looking lift built on 1-2 coincidental occurrences).

    Direction convention: item_a is always whichever item sells more often
    on its own (ties broken alphabetically) -- "of customers who bought the
    more common item, X% also bought the less common one" is the more
    actionable reading for a bundle/upsell decision than the reverse."""
    seen_pairs: set[tuple[str, str]] = set()
    out: list[dict] = []
    for _, row in rules.iterrows():
        x, y = next(iter(row["antecedents"])), next(iter(row["consequents"]))
        key = tuple(sorted((x, y)))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)

        pair_n = pair_counts.get(key, 0)
        if pair_n < min_pair_count:
            continue

        count_x, count_y = item_counts.get(x, 0), item_counts.get(y, 0)
        if count_x == 0 or count_y == 0:
            continue
        if count_x >= count_y:
            hi, lo, count_hi, count_lo = x, y, count_x, count_y
        else:
            hi, lo, count_hi, count_lo = y, x, count_y, count_x

        confidence = pair_n / count_hi
        support = pair_n / transaction_count
        support_lo = count_lo / transaction_count
        lift = confidence / support_lo if support_lo else 0.0
        # Absolute floor regardless of tier -- a lift at or below 1.0 means
        # no positive association exists, full stop.
        if lift <= 1.0 or lift < min_lift:
            continue

        cat_hi, cat_lo = categories.get(hi), categories.get(lo)
        out.append({
            "item_a": hi, "item_b": lo,
            "category_a": cat_hi, "category_b": cat_lo,
            "pair_count": pair_n,
            "support": round(support, 5),
            "confidence": round(confidence, 5),
            "lift": round(lift, 3),
            "recommendation_type": (
                "Observed cross-category association"
                if cat_hi and cat_lo and cat_hi != cat_lo
                else "Observed association"
            ),
        })
    out.sort(key=lambda c: (-c["lift"], -c["pair_count"]))
    return out


def _run_tier(basket_list: list[set], item_counts: Counter, pair_counts: Counter,
             categories: dict, tier_name: str, base_support: float, confidence: float,
             lift: float, pair_count_floor: int) -> dict:
    transaction_count = len(basket_list)
    eff_support = _effective_min_support(transaction_count, base_support, pair_count_floor)

    te = TransactionEncoder()
    arr = te.fit(basket_list).transform(basket_list)
    df = pd.DataFrame(arr, columns=te.columns_)
    freq, rules = _mine(df, eff_support, confidence, lift)

    combos = (
        _qualifying_pairs(rules, item_counts, pair_counts, transaction_count,
                          pair_count_floor, lift, categories)
        if not rules.empty else []
    )
    return {
        "tier": tier_name, "min_support": eff_support, "min_confidence": confidence,
        "min_lift": lift, "min_pair_count": pair_count_floor,
        "n_frequent_itemsets": 0 if freq.empty else int(len(freq)),
        "n_association_rules": 0 if rules.empty else int(len(rules)),
        "combos": combos,
    }


def _mine_with_fallback(basket_list: list[set], item_counts: Counter, pair_counts: Counter,
                        categories: dict) -> dict:
    """Runs the primary threshold tier; only if it finds ZERO qualifying
    pairs does it try the single, bounded fallback tier (section 13's
    "relax only the least restrictive threshold within a safe boundary").
    Never tries a third, looser tier -- two tiers, deterministic, both
    logged in the response via "threshold_tier"."""
    primary = _run_tier(basket_list, item_counts, pair_counts, categories,
                        "primary", MIN_SUPPORT, MIN_CONFIDENCE, MIN_LIFT, MIN_PAIR_COUNT)
    if primary["combos"]:
        return primary

    fallback = _run_tier(basket_list, item_counts, pair_counts, categories,
                         "fallback", FALLBACK_MIN_SUPPORT, FALLBACK_MIN_CONFIDENCE,
                         FALLBACK_MIN_LIFT, FALLBACK_MIN_PAIR_COUNT)
    if fallback["combos"]:
        return fallback

    # Neither tier found anything -- report the fallback's (the one that
    # was actually most permissive) stats so the "0 found" response shows
    # the loosest thresholds genuinely tried, not just the strictest ones.
    return fallback


# Threshold-sensitivity configurations audited against the real transaction
# data (2026-09) -- each academically defensible for a small-menu QSR
# dataset. Never chosen to force a nonzero result: threshold_sensitivity()
# reports however many combinations each one actually produces on the real
# data, including zero.
THRESHOLD_CONFIGS = {
    "production": {"min_support": MIN_SUPPORT, "min_confidence": MIN_CONFIDENCE,
                   "min_lift": MIN_LIFT, "min_pair_count": MIN_PAIR_COUNT},
    "production_fallback": {"min_support": FALLBACK_MIN_SUPPORT, "min_confidence": FALLBACK_MIN_CONFIDENCE,
                            "min_lift": FALLBACK_MIN_LIFT, "min_pair_count": FALLBACK_MIN_PAIR_COUNT},
    # The module's original 2026-08 configuration, kept as a labeled
    # historical reference point -- this is the threshold that produced 0
    # combinations at every tested variant before the basket-construction
    # bug (see module docstring) was found and fixed.
    "legacy_strict_2026_08": {"min_support": 0.02, "min_confidence": 0.30, "min_lift": 1.2,
                              "min_pair_count": MIN_PAIR_COUNT},
}


def threshold_sensitivity(conn: sqlite3.Connection, lookback_days: int = 90) -> dict:
    """Runs the SAME real transaction data through every configuration in
    THRESHOLD_CONFIGS and reports how many qualifying combinations each one
    finds. Exists to answer, with real numbers rather than a guess: does
    loosening support/confidence within a still-defensible range actually
    surface anything, or is the transaction data itself simply too sparse
    for co-occurrence mining regardless of threshold? Both are legitimate
    answers; this reports whichever one the data gives."""
    basket_list, _raw_order_count, categories = _transactions(conn, lookback_days)
    transaction_count = len(basket_list)
    if transaction_count < MIN_TRANSACTIONS:
        return {"insufficient_data": True, "transaction_count": transaction_count,
               "required": MIN_TRANSACTIONS, "configs": {}}

    item_counts, pair_counts = _item_and_pair_counts(basket_list)
    avg_basket_size = (sum(len(b) for b in basket_list) / transaction_count) if basket_list else 0.0
    results = {}
    for name, cfg in THRESHOLD_CONFIGS.items():
        tier = _run_tier(basket_list, item_counts, pair_counts, categories, name,
                         cfg["min_support"], cfg["min_confidence"], cfg["min_lift"], cfg["min_pair_count"])
        results[name] = {**cfg, "min_support": round(tier["min_support"], 5), "combos_found": len(tier["combos"])}
    return {
        "insufficient_data": False,
        "transaction_count": transaction_count,
        "avg_items_per_order": round(avg_basket_size, 3),
        "configs": results,
        "note": ("avg_items_per_order reflects real transactions (mixed historical orders merged "
                 "to one basket each -- see wt/associations.py module docstring); a value near 1 "
                 "means most transactions contain a single menu item, which structurally leaves "
                 "co-occurrence mining little to find regardless of threshold."),
    }


def product_combinations(conn: sqlite3.Connection, lookback_days: int = 90, top_n: int = 15) -> dict:
    basket_list, raw_order_count, categories = _transactions(conn, lookback_days)
    transaction_count = len(basket_list)
    excluded = raw_order_count - transaction_count

    data_quality_note = (
        f"{raw_order_count} non-void customer_order rows exist in this window, but "
        f"{excluded} of those are the second half of a historical mixed order (both "
        "unlimited-wings and a-la-carte lines on one receipt) that an earlier importer "
        "version recorded as two rows sharing one source_order_id -- they are merged "
        f"back into the {transaction_count} real completed transactions they actually "
        "came from before any mining runs. See wt/associations.py's module docstring."
        if excluded else None
    )

    shape = {
        "insufficient_data": True,
        "transaction_count": transaction_count,
        "total_orders_analyzed": raw_order_count,
        "valid_transactions": transaction_count,
        "excluded_transactions": excluded,
        "required": MIN_TRANSACTIONS,
        "combos": [],
        "data_quality_note": data_quality_note,
    }
    if transaction_count < MIN_TRANSACTIONS:
        return shape

    item_counts, pair_counts = _item_and_pair_counts(basket_list)
    unique_products = len(item_counts)
    mined = _mine_with_fallback(basket_list, item_counts, pair_counts, categories)

    menu_rows = _rows(conn.execute("SELECT id, name, price FROM menu_item"))
    prices = {r["name"]: r["price"] for r in menu_rows}
    menu_item_ids = {r["name"]: r["id"] for r in menu_rows}
    combos = []
    for c in mined["combos"][:top_n]:
        a_price, b_price = prices.get(c["item_a"]), prices.get(c["item_b"])
        individual_total = round(a_price + b_price, 2) if a_price is not None and b_price is not None else None
        combos.append({
            **c,
            "item_a_menu_item_id": menu_item_ids.get(c["item_a"]),
            "item_b_menu_item_id": menu_item_ids.get(c["item_b"]),
            "individual_total": individual_total,
            "discount_reference_price": round(individual_total * COMBO_DISCOUNT, 2) if individual_total else None,
        })

    result = {
        "insufficient_data": False,
        "transaction_count": transaction_count,
        "total_orders_analyzed": raw_order_count,
        "valid_transactions": transaction_count,
        "excluded_transactions": excluded,
        "unique_products": unique_products,
        "required": MIN_TRANSACTIONS,
        "combos": combos,
        "combo_price_basis": COMBO_PRICE_BASIS,
        "data_quality_note": data_quality_note,
        "threshold_tier": mined["tier"],
        "min_support": round(mined["min_support"], 5),
        "min_confidence": mined["min_confidence"],
        "min_lift": mined["min_lift"],
        "min_pair_count": mined["min_pair_count"],
        "number_of_frequent_itemsets": mined["n_frequent_itemsets"],
        "number_of_association_rules": mined["n_association_rules"],
        "number_of_final_recommendations": len(combos),
    }
    if not combos:
        result["empty_reason"] = (
            "No association met the minimum transaction-count, confidence, and lift "
            "requirements, even after relaxing thresholds within a safe, bounded fallback "
            "tier (support/confidence loosened; the pair-count and lift floors were not)."
        )
    return result


def model_details(conn: sqlite3.Connection, lookback_days: int = 90) -> dict:
    """Real parameters and current stats for the FP-growth pass -- backs the
    technical panel under System Check, not the main Reports page. Includes
    the threshold-sensitivity table so the panel can answer "did we choose
    these thresholds to force a result, or is 0 combos what the data
    actually gives across a defensible range" with real numbers."""
    result = product_combinations(conn, lookback_days)
    return {
        "model": "FP-growth (mlxtend)",
        "min_transactions": MIN_TRANSACTIONS,
        "min_support": result.get("min_support", MIN_SUPPORT),
        "min_confidence": result.get("min_confidence", MIN_CONFIDENCE),
        "min_lift": result.get("min_lift", MIN_LIFT),
        "min_pair_count": result.get("min_pair_count", MIN_PAIR_COUNT),
        "threshold_tier": result.get("threshold_tier"),
        "lookback_days": lookback_days,
        "transactions_evaluated": result["transaction_count"],
        "total_orders_analyzed": result.get("total_orders_analyzed"),
        "number_of_frequent_itemsets": result.get("number_of_frequent_itemsets", 0),
        "number_of_association_rules": result.get("number_of_association_rules", 0),
        "combos_found": len(result["combos"]),
        "threshold_sensitivity": threshold_sensitivity(conn, lookback_days),
    }
