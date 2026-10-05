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

import bisect
import datetime as _dt
import sqlite3
from collections import Counter
from itertools import combinations

import pandas as pd
from mlxtend.frequent_patterns import association_rules, fpgrowth
from mlxtend.preprocessing import TransactionEncoder

from . import timeutil

MIN_TRANSACTIONS = 30     # real transactions needed before a frequent-itemset pass is trustworthy

# Primary threshold tier.
#
# 2026-10 recalibration for the 365-day default (was 90-day): MIN_SUPPORT
# used to be a flat 0.002 (0.2%), chosen when this restaurant had ~5,200
# real transactions/90d -- at that volume 0.2% support and
# MIN_PAIR_COUNT/transaction_count (10/5242 = 0.0019) were nearly the same
# number, so which one "won" in _effective_min_support()'s max() rarely
# mattered. At 365 days' ~21,700 real transactions, the SAME flat 0.2%
# demands ~43 raw occurrences of a pair -- 4x stricter in absolute terms
# than the 90-day calibration intended, and it silently became the
# dominant (wrong) term precisely because transaction volume grew. That is
# backwards: MIN_PAIR_COUNT is the scale-invariant, business-meaningful
# criterion here ("this exact pair has really happened N times this year",
# true regardless of how many total transactions that represents as a
# percentage) -- a flat support percentage is only a rough proxy for "has
# enough raw occurrences," and now that MIN_PAIR_COUNT states that
# requirement directly, the flat term should never be the one deciding
# anything at a realistic transaction count. MIN_SUPPORT below is kept only
# as a degenerate-case safety floor (guards a pathological min_support=0
# call), set low enough that MIN_PAIR_COUNT/transaction_count already
# exceeds it at every real transaction count this restaurant has ever had
# (30 up through its full ~33,600-transaction history) -- see
# _effective_min_support() and threshold_sensitivity(), which still runs
# the OLD flat-0.02 configuration too (legacy_strict_2026_08, unchanged) as
# a labeled historical comparison point.
MIN_SUPPORT = 0.0003
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
# FALLBACK_MIN_SUPPORT: same 2026-10 fix as MIN_SUPPORT above, scaled to
# this tier's own (lower) pair-count floor.
FALLBACK_MIN_SUPPORT = 0.0001
FALLBACK_MIN_CONFIDENCE = 0.03
FALLBACK_MIN_LIFT = 1.10
FALLBACK_MIN_PAIR_COUNT = 5

# --- chronological holdout validation (2026-10) -----------------------------
# A candidate pair clearing the in-sample thresholds above is a CANDIDATE,
# not yet a "real" recommendation -- see product_combinations()'s
# `holdout_validate` path. The candidate pool is mined on the earliest
# (1 - HOLDOUT_FRACTION) share of real transactions only; each candidate is
# then checked against the later HOLDOUT_FRACTION share the miner never
# saw, using the SAME MIN_LIFT floor as in-sample (never loosened for the
# holdout check -- a pair that cannot clear the same bar on unseen data is
# not validated, full stop). This runs automatically only when both the
# train and holdout slices are themselves large enough to be trustworthy
# samples in their own right (see product_combinations()); otherwise the
# response says so and falls back to in-sample-only reporting rather than
# fabricating a split too thin to mean anything.
HOLDOUT_FRACTION = 0.20
MIN_HOLDOUT_TRANSACTIONS = 2 * MIN_TRANSACTIONS   # the holdout slice alone needs a safety margin above
                                                   # the bare "is mining even meaningful" floor, not just
                                                   # barely clear it
MIN_HOLDOUT_PAIR_COUNT = 5        # the holdout slice is deliberately the smaller share of the data;
                                  # this is intentionally the same floor the fallback tier uses in-sample

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


def _transactions(conn: sqlite3.Connection, lookback_days: int) -> tuple[list[set], list[str], int, dict]:
    """One basket per REAL completed transaction -- not one per
    customer_order row (see module docstring for why those differ on this
    database). A transaction's key is COALESCE(source_order_id, its own
    row id): two customer_order rows sharing one source_order_id are two
    halves of a single historical mixed order and are merged into one
    basket; every other row (every live POS order, and every historical
    order that was never split) is already exactly one transaction on its
    own. Void orders are excluded -- they were never a completed sale.

    Returns (basket_list, basket_dates, raw_order_row_count,
    category_by_item_name). basket_dates[i] is basket_list[i]'s own
    created_at (the EARLIEST row's, for a merged two-row historical
    basket) -- added 2026-10 for expanding_window_validation(), which must
    place each basket on one side of a REAL calendar-date fold boundary,
    not an index-proportion guess. Parallel to basket_list by construction
    (both built from the same single pass over `lines` in the same order;
    Python dicts preserve insertion order).
    raw_order_row_count is the number of distinct customer_order rows found
    in the window (what the pre-audit code mistakenly reported as the
    transaction count) -- kept so the API can show both numbers and explain
    the difference rather than silently changing what "orders analysed"
    means.

    2026-10: baskets are now returned in chronological order (ORDER BY
    created_at, id below) so a caller doing a chronological train/holdout
    split (see HOLDOUT_FRACTION) gets a real temporal split, not
    insertion-order noise. `status != 'void'` is unchanged: this schema's
    customer_order.status enum is ('punched','in_progress','ready','served',
    'void') -- there is no separate 'cancelled' state, void IS the
    cancellation state, confirmed directly against production (every real
    row is currently 'served'; zero 'void' rows exist at all, so nothing in
    the live data is excluded by this clause today, but it stays in place
    for whenever a real void is recorded).
    """
    lines = _rows(conn.execute(
        """
        SELECT o.id AS order_pk, o.source_order_id, o.created_at, m.name, m.category
          FROM order_line ol
          JOIN customer_order o ON o.id = ol.order_id
          JOIN menu_item m ON m.id = ol.menu_item_id
         WHERE o.status != 'void' AND o.created_at >= ?
         ORDER BY o.created_at, o.id
        """,
        (timeutil.utc_bound_n_manila_days_ago(lookback_days),),
    ))
    raw_order_ids: set[int] = set()
    baskets: dict[str, set] = {}
    basket_dates: dict[str, str] = {}
    categories: dict[str, str] = {}
    for r in lines:
        raw_order_ids.add(r["order_pk"])
        txn_key = r["source_order_id"] or f"row:{r['order_pk']}"
        baskets.setdefault(txn_key, set()).add(r["name"])
        basket_dates.setdefault(txn_key, r["created_at"])  # first (earliest) row wins
        categories[r["name"]] = r["category"]
    return list(baskets.values()), list(basket_dates.values()), len(raw_order_ids), categories


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


# 2026-10 fix: mlxtend computes a rule's confidence as support(itemset) /
# support(antecedent) -- two divisions through intermediate "support"
# fractions (each itself count/transaction_count), not the single direct
# pair_n/count_hi division this module's own _qualifying_pairs() uses to
# DISPLAY confidence. For a pair whose true confidence is mathematically
# exactly MIN_CONFIDENCE (e.g. 29/580 = 0.05 exactly), that extra division
# chain can round the computed value a few ULPs below 0.05, so the plain
# `>=` comparison below used to reject a pair that is not actually below
# the threshold -- confirmed directly (29/580 real-data case, see the
# 2026-10 FP-Growth audit). CONFIDENCE_FLOAT_TOLERANCE absorbs exactly that
# class of rounding error (double-precision rounding over a couple of
# chained divisions is on the order of 1e-15 relative; 1e-9 is ~6 orders of
# magnitude more headroom than that ever needs) without moving the
# threshold itself -- a pair at 0.0499, meaningfully below 0.05, still
# fails; only a pair whose TRUE confidence already clears 0.05 can be
# rescued from a same-pair floating-point rounding artifact.
CONFIDENCE_FLOAT_TOLERANCE = 1e-9


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
        (rules["confidence"] >= min_confidence - CONFIDENCE_FLOAT_TOLERANCE)
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


def _split_chronological(basket_list: list[set], holdout_fraction: float) -> tuple[list[set], list[set]]:
    """The earliest (1 - holdout_fraction) share of transactions (train),
    then the later holdout_fraction share (holdout) -- basket_list MUST
    already be in chronological order (see _transactions()'s own ORDER BY).
    A chronological split, never random: the question this exists to answer
    is "does a pattern mined from the past still hold on transactions the
    miner has not seen," which a random split cannot test (a random holdout
    row can sit chronologically BEFORE training rows the model effectively
    learned from, which is not the real deployment scenario -- see
    forecasting.py/depletion.py's own rolling-backtest docstrings for the
    same reasoning applied to those models)."""
    n = len(basket_list)
    split = int(round(n * (1 - holdout_fraction)))
    return basket_list[:split], basket_list[split:]


def _holdout_validate(candidates: list[dict], holdout_basket_list: list[set],
                      min_holdout_pair_count: int, min_lift: float) -> tuple[list[dict], list[dict]]:
    """Checks each TRAIN-mined candidate pair against the holdout slice the
    miner never saw, using the SAME min_lift floor already used in-sample
    (never loosened here -- loosening it for the holdout check would just
    be moving the goalposts after seeing the answer). Returns
    (validated, rejected); every candidate ends up in exactly one list, each
    rejected entry carrying a `rejection_reason` and whatever holdout
    numbers were actually computable, so "why wasn't this surfaced" is
    always answerable from the response, never silent.

    Direction (item_a/item_b = hi/lo) is carried over from the TRAIN-side
    candidate unchanged -- which item sold more often is itself re-decided
    per holdout window, and flipping which side is "hi" between train and
    holdout would make the two confidence/lift numbers describe different
    questions.

    2026-10: `status` is "holdout_validated"/"rejected" (was the bare
    "validated") and every record also carries "validation_method" --
    this check answers ONE specific question (did this pair repeat on the
    most recent 20% of real transactions), and a pair passing it is not
    thereby "validated" in any broader sense. expanding_window_validation()
    answers the separate question of multi-window temporal stability; a
    caller wanting to know whether a pair cleared BOTH must look at both
    fields explicitly, never infer one from the other's presence."""
    holdout_item_counts, holdout_pair_counts = _item_and_pair_counts(holdout_basket_list)
    holdout_transaction_count = len(holdout_basket_list)

    validated: list[dict] = []
    rejected: list[dict] = []
    for c in candidates:
        key = tuple(sorted((c["item_a"], c["item_b"])))
        pair_n_oos = holdout_pair_counts.get(key, 0)
        base = {**c, "status": "rejected", "validation_method": "chronological_80_20_holdout",
               "holdout_pair_count": pair_n_oos}

        if pair_n_oos < min_holdout_pair_count:
            rejected.append({**base, "rejection_reason": "insufficient_holdout_occurrences",
                             "rejection_detail": (f"seen together only {pair_n_oos}x in the holdout "
                                                  f"window, below the required {min_holdout_pair_count}")})
            continue

        count_hi_oos = holdout_item_counts.get(c["item_a"], 0)
        count_lo_oos = holdout_item_counts.get(c["item_b"], 0)
        if count_hi_oos == 0 or count_lo_oos == 0 or holdout_transaction_count == 0:
            rejected.append({**base, "rejection_reason": "item_absent_from_holdout",
                             "rejection_detail": "one or both items never appeared at all in the "
                                                 "holdout window"})
            continue

        confidence_oos = pair_n_oos / count_hi_oos
        support_oos = pair_n_oos / holdout_transaction_count
        support_lo_oos = count_lo_oos / holdout_transaction_count
        lift_oos = (confidence_oos / support_lo_oos) if support_lo_oos else 0.0

        if lift_oos <= 1.0 or lift_oos < min_lift:
            rejected.append({**base, "rejection_reason": "failed_holdout_lift",
                             "holdout_confidence": round(confidence_oos, 5),
                             "holdout_support": round(support_oos, 5),
                             "holdout_lift": round(lift_oos, 3),
                             "rejection_detail": (f"holdout lift {lift_oos:.3f} did not clear the "
                                                  f"required {min_lift} (same floor used in-sample)")})
            continue

        validated.append({
            **c, "status": "holdout_validated", "validation_method": "chronological_80_20_holdout",
            "holdout_pair_count": pair_n_oos,
            "holdout_confidence": round(confidence_oos, 5),
            "holdout_support": round(support_oos, 5),
            "holdout_lift": round(lift_oos, 3),
        })

    return validated, rejected


# ===========================================================================
# Expanding-window (rolling) temporal-stability validation -- 2026-10.
#
# The single 80/20 chronological holdout above (_holdout_validate) answers
# "did this candidate repeat on the most recent slice of real data." It
# does NOT answer "is this association stable over time, or did it just
# happen to survive the one slice checked." expanding_window_validation()
# answers that second question with THREE independent, nested train/test
# folds over the SAME 365-day real dataset used everywhere else in this
# module -- never a second dataset, never different thresholds:
#
#   fold_1: train on the earliest ~180 real days, test on the next ~60
#   fold_2: train on the earliest ~240 real days, test on the next ~60
#   fold_3: train on the earliest ~300 real days, test on the remaining ~65
#
# Boundaries are placed against the REAL calendar dates present in the
# data (see _fold_cutoff_index), not a naive 1/365th-per-day assumption --
# this restaurant's real order volume is not perfectly uniform day to day.
#
# A pair can be newly IDENTIFIED (first clears the primary/fallback
# thresholds -- the same MIN_PAIR_COUNT/MIN_CONFIDENCE/MIN_LIFT already
# used everywhere, never retuned here) in fold_1, fold_2, or fold_3's own
# training window. Once identified, it is checked against every fold
# whose TEST window is not already inside the data that identified it --
# fold_1-identified pairs are checked against fold_1, fold_2, AND fold_3's
# test windows (all three are genuinely future relative to a 180-day
# training cut); a pair that only clears the threshold at fold_2's larger
# training window is checked against fold_2 and fold_3 only (fold_1's test
# window is already inside fold_2's training data -- checking it would not
# be an out-of-sample test).
#
# STABILITY RULE -- fixed here, BEFORE any fold is evaluated, and never
# changed afterward to make any particular pair (Sprite + Creamy Sausage
# Pasta or any other) pass:
#
#   For each of a candidate's applicable folds, that fold's test window is
#   EVIDENCED if the pair's count there is >= MIN_HOLDOUT_PAIR_COUNT (the
#   same floor the single-holdout check already uses). An evidenced fold
#   PASSES if its lift there is >= MIN_LIFT (same floor, same comparison
#   as every other lift check in this module -- never loosened).
#
#   "validated_stable"      at least 2 of the applicable folds are both
#                            evidenced AND passing (or, if only 1 fold is
#                            applicable at all, that 1 fold is evidenced
#                            and passing), AND no applicable fold is
#                            evidenced-but-failing.
#   "unstable_failed"        at least one applicable, evidenced fold has
#                            lift < MIN_LIFT -- a real, measured
#                            counterexample, which disqualifies "stable"
#                            regardless of how any other fold looks.
#   "insufficient_evidence"  everything else: no evidenced fold ever
#                            failed, but there were not enough evidenced-
#                            and-passing folds to call the pattern stable
#                            (zero evidenced folds, or exactly one
#                            evidenced-and-passing fold when more than one
#                            fold was applicable -- one good fold alone is
#                            not "stability").
EXPANDING_FOLD_DEFS = (
    {"name": "fold_1", "train_days": 180, "test_days": 60},
    {"name": "fold_2", "train_days": 240, "test_days": 60},
    {"name": "fold_3", "train_days": 300, "test_days": None},  # None = remaining real data
)


def _fold_cutoff_index(basket_dates: list[str], cutoff_date_iso: str) -> int:
    """First index i such that basket_dates[i] (a 'YYYY-MM-DD HH:MM:SS'
    string, already chronological) falls on or after cutoff_date_iso (a
    bare 'YYYY-MM-DD' date) -- bisects on the date-only prefix so a
    same-day time component never pushes a row to the wrong side."""
    prefixes = [d[:10] for d in basket_dates]
    return bisect.bisect_left(prefixes, cutoff_date_iso)


def _evaluate_pair_in_basket_list(item_a: str, item_b: str, basket_list: list[set],
                                  min_pair_count: int, min_lift: float) -> dict:
    """One pair's real pair count/support/confidence/lift inside
    `basket_list`, with an explicit evidenced/verdict classification --
    the single-pair building block expanding_window_validation() runs
    once per candidate per applicable fold. Direction (item_a=hi) is
    whatever the candidate was identified with; it is not re-decided here,
    so confidence/lift describe the same question in every fold."""
    n = len(basket_list)
    if n == 0:
        return {"pair_count": 0, "transactions_in_fold": 0, "evidenced": False,
               "verdict": "insufficient_evidence", "detail": "fold has no transactions"}
    item_counts, pair_counts = _item_and_pair_counts(basket_list)
    pair_n = pair_counts.get(tuple(sorted((item_a, item_b))), 0)
    if pair_n < min_pair_count:
        return {"pair_count": pair_n, "transactions_in_fold": n, "evidenced": False,
               "verdict": "insufficient_evidence",
               "detail": f"seen together {pair_n}x in this fold, below the required {min_pair_count}"}
    count_a, count_b = item_counts.get(item_a, 0), item_counts.get(item_b, 0)
    if count_a == 0 or count_b == 0:
        return {"pair_count": pair_n, "transactions_in_fold": n, "evidenced": False,
               "verdict": "insufficient_evidence", "detail": "one of the two items never appears alone in this fold"}
    confidence = pair_n / count_a
    support = pair_n / n
    support_b = count_b / n
    lift = (confidence / support_b) if support_b else 0.0
    passed = lift > 1.0 and lift >= min_lift
    return {
        "pair_count": pair_n, "transactions_in_fold": n,
        "support": round(support, 5), "confidence": round(confidence, 5), "lift": round(lift, 3),
        "evidenced": True, "verdict": "passed" if passed else "failed_lift",
    }


def expanding_window_validation(conn: sqlite3.Connection, lookback_days: int = 365) -> dict:
    """Runs the three nested EXPANDING_FOLD_DEFS folds over the real
    365-day dataset and reports, for every candidate pair ever identified
    in any fold's training window, its fold-by-fold pair count/support/
    confidence/lift and the fixed stability verdict (see this section's
    own module-level comment for the rule, decided before this function
    ever ran). This is a SEPARATE analysis from product_combinations()'s
    80/20 holdout gate -- it does not change what that function returns,
    and the live recommendation engine keeps training on the complete
    latest lookback_days exactly as already fixed; this exists to show
    whether a pattern that survives one holdout slice also survives being
    checked three different ways.
    """
    basket_list, basket_dates, _raw_order_count, categories = _transactions(conn, lookback_days)
    transaction_count = len(basket_list)
    if transaction_count < MIN_TRANSACTIONS:
        return {"available": False,
               "reason": f"only {transaction_count} real transactions in the window, below "
                         f"MIN_TRANSACTIONS={MIN_TRANSACTIONS}"}

    anchor = _dt.date.fromisoformat(basket_dates[0][:10])
    data_end = _dt.date.fromisoformat(basket_dates[-1][:10])
    data_end_exclusive = (data_end + _dt.timedelta(days=1)).isoformat()

    folds = []
    for fd in EXPANDING_FOLD_DEFS:
        train_end = min(anchor + _dt.timedelta(days=fd["train_days"]), data_end + _dt.timedelta(days=1))
        if fd["test_days"] is None:
            test_end = data_end + _dt.timedelta(days=1)
        else:
            test_end = min(train_end + _dt.timedelta(days=fd["test_days"]), data_end + _dt.timedelta(days=1))
        train_idx = _fold_cutoff_index(basket_dates, train_end.isoformat())
        test_idx = _fold_cutoff_index(basket_dates, test_end.isoformat())
        folds.append({
            "name": fd["name"],
            "train_end_date": train_end.isoformat(),
            "test_end_date": test_end.isoformat(),
            "train_baskets": basket_list[:train_idx],
            "test_baskets": basket_list[train_idx:test_idx],
        })

    seen_pairs: set[tuple[str, str]] = set()
    candidates: dict[tuple[str, str], dict] = {}
    fold_reports = []

    for fold_i, fold in enumerate(folds):
        train_baskets = fold["train_baskets"]
        report = {
            "name": fold["name"], "train_end_date": fold["train_end_date"],
            "test_end_date": fold["test_end_date"],
            "train_transactions": len(train_baskets), "test_transactions": len(fold["test_baskets"]),
        }
        if len(train_baskets) < MIN_TRANSACTIONS:
            report["available"] = False
            report["reason"] = (f"this fold's training window has only {len(train_baskets)} real "
                                f"transactions, below MIN_TRANSACTIONS={MIN_TRANSACTIONS}")
            report["newly_identified_candidates"] = 0
            fold_reports.append(report)
            continue

        item_counts, pair_counts = _item_and_pair_counts(train_baskets)
        mined = _mine_with_fallback(train_baskets, item_counts, pair_counts, categories)
        report["available"] = True
        report["threshold_tier"] = mined["tier"]
        report["min_support"] = round(mined["min_support"], 5)
        report["candidates_in_training"] = len(mined["combos"])

        newly_identified = 0
        for c in mined["combos"]:
            key = tuple(sorted((c["item_a"], c["item_b"])))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            newly_identified += 1
            candidates[key] = {
                "item_a": c["item_a"], "item_b": c["item_b"],
                "category_a": c["category_a"], "category_b": c["category_b"],
                "identified_in_fold": fold["name"],
                "identified_at_train_transactions": len(train_baskets),
                "identification_pair_count": c["pair_count"],
                "identification_support": c["support"],
                "identification_confidence": c["confidence"],
                "identification_lift": c["lift"],
                "applicable_fold_indices": list(range(fold_i, len(folds))),
            }
        report["newly_identified_candidates"] = newly_identified
        fold_reports.append(report)

    results = []
    for key, cand in candidates.items():
        fold_results = []
        for fi in cand["applicable_fold_indices"]:
            fold = folds[fi]
            ev = _evaluate_pair_in_basket_list(cand["item_a"], cand["item_b"], fold["test_baskets"],
                                               MIN_HOLDOUT_PAIR_COUNT, MIN_LIFT)
            fold_results.append({"fold": fold["name"], **ev})

        evidenced = [r for r in fold_results if r["evidenced"]]
        passing = [r for r in evidenced if r["verdict"] == "passed"]
        failing = [r for r in evidenced if r["verdict"] == "failed_lift"]
        n_applicable = len(fold_results)

        if failing:
            stability_verdict = "unstable_failed"
        elif n_applicable == 1:
            stability_verdict = "validated_stable" if len(passing) == 1 else "insufficient_evidence"
        else:
            stability_verdict = "validated_stable" if len(passing) >= 2 else "insufficient_evidence"

        results.append({
            **{k: v for k, v in cand.items() if k != "applicable_fold_indices"},
            "applicable_folds": [folds[fi]["name"] for fi in cand["applicable_fold_indices"]],
            "fold_results": fold_results,
            "evidenced_fold_count": len(evidenced),
            "passing_fold_count": len(passing),
            "failing_fold_count": len(failing),
            "stability_verdict": stability_verdict,
        })

    results.sort(key=lambda r: (
        {"validated_stable": 0, "unstable_failed": 1, "insufficient_evidence": 2}[r["stability_verdict"]],
        -r["passing_fold_count"],
    ))

    return {
        "available": True,
        "methodology": (
            "Three nested expanding-window folds over the same real 365-day dataset "
            "(~180d train / next ~60d test, ~240d train / next ~60d test, ~300d train / "
            "remaining ~65d test), boundaries placed against real calendar dates present "
            "in the data. A pair is checked against every fold whose test window was not "
            "already part of the training data that first identified it. See this "
            "module's EXPANDING_FOLD_DEFS / stability-rule comment for the fixed "
            "validated_stable / unstable_failed / insufficient_evidence rule, decided "
            "before any fold was evaluated."
        ),
        "transaction_count": transaction_count,
        "data_start_date": basket_dates[0][:10],
        "data_end_date": basket_dates[-1][:10],
        "folds": fold_reports,
        "candidates": results,
        "summary": {
            "total_candidates_ever_identified": len(results),
            "validated_stable": sum(1 for r in results if r["stability_verdict"] == "validated_stable"),
            "unstable_failed": sum(1 for r in results if r["stability_verdict"] == "unstable_failed"),
            "insufficient_evidence": sum(1 for r in results if r["stability_verdict"] == "insufficient_evidence"),
        },
    }


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


def threshold_sensitivity(conn: sqlite3.Connection, lookback_days: int = 365) -> dict:
    """Runs the SAME real transaction data through every configuration in
    THRESHOLD_CONFIGS and reports how many qualifying combinations each one
    finds. Exists to answer, with real numbers rather than a guess: does
    loosening support/confidence within a still-defensible range actually
    surface anything, or is the transaction data itself simply too sparse
    for co-occurrence mining regardless of threshold? Both are legitimate
    answers; this reports whichever one the data gives."""
    basket_list, _basket_dates, _raw_order_count, categories = _transactions(conn, lookback_days)
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


def product_combinations(conn: sqlite3.Connection, lookback_days: int = 365, top_n: int = 15,
                         holdout_validate: bool = True) -> dict:
    """The operational combo-recommendation surface. lookback_days defaults
    to 365 (2026-10: was 90 -- see MIN_SUPPORT's own comment on why the old
    90-day calibration does not transfer to a longer window as-is).

    holdout_validate=True (the default for every real caller) additionally
    requires each in-sample candidate to hold up on a later, chronologically
    separate slice of real transactions the miner never trained on -- see
    _split_chronological()/_holdout_validate() and HOLDOUT_FRACTION. It is
    automatically skipped (never silently run on too little data) when the
    transaction count cannot support a trustworthy split on BOTH sides; the
    response's `mining_basis` always says which path actually ran. Passing
    False restores plain in-sample mining only -- used by tests that are
    exercising the miner itself, not the holdout gate, and want a result
    that does not depend on how temporally spread a small test fixture
    happens to be.
    """
    basket_list, _basket_dates, raw_order_count, categories = _transactions(conn, lookback_days)
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
    avg_basket_size = (sum(len(b) for b in basket_list) / transaction_count) if basket_list else 0.0

    holdout_size = transaction_count * HOLDOUT_FRACTION
    train_size = transaction_count * (1 - HOLDOUT_FRACTION)
    can_holdout = (holdout_validate and holdout_size >= MIN_HOLDOUT_TRANSACTIONS
                   and train_size >= MIN_TRANSACTIONS)

    rejected_combinations: list[dict] = []
    if can_holdout:
        train_baskets, holdout_baskets = _split_chronological(basket_list, HOLDOUT_FRACTION)
        train_item_counts, train_pair_counts = _item_and_pair_counts(train_baskets)
        mined = _mine_with_fallback(train_baskets, train_item_counts, train_pair_counts, categories)
        validated, rejected_combinations = _holdout_validate(
            mined["combos"], holdout_baskets, MIN_HOLDOUT_PAIR_COUNT, MIN_LIFT)
        validated.sort(key=lambda c: (-c["lift"], -c["pair_count"]))
        final_combos = validated
        mining_basis = {
            "method": "chronological_train_holdout",
            "train_transactions": len(train_baskets),
            "holdout_transactions": len(holdout_baskets),
            "holdout_fraction": HOLDOUT_FRACTION,
            "candidates_considered": len(mined["combos"]),
            "candidates_validated": len(validated),
            "candidates_rejected": len(rejected_combinations),
        }
    else:
        mined = _mine_with_fallback(basket_list, item_counts, pair_counts, categories)
        final_combos = [{**c, "status": "validated_in_sample_only", "validation_method": "none_in_sample_only"}
                       for c in mined["combos"]]
        mining_basis = {
            "method": "in_sample_only",
            "reason": (
                "holdout_validate=False" if not holdout_validate else
                f"transaction_count={transaction_count} cannot support a trustworthy "
                f"chronological split: needs >= {MIN_TRANSACTIONS} train-side and "
                f">= {MIN_HOLDOUT_TRANSACTIONS} holdout-side at a "
                f"{HOLDOUT_FRACTION:.0%} holdout fraction (would be "
                f"{int(train_size)}/{int(holdout_size)})"
            ),
        }

    menu_rows = _rows(conn.execute("SELECT id, name, price FROM menu_item"))
    prices = {r["name"]: r["price"] for r in menu_rows}
    menu_item_ids = {r["name"]: r["id"] for r in menu_rows}

    def _enrich(c: dict) -> dict:
        a_price, b_price = prices.get(c["item_a"]), prices.get(c["item_b"])
        individual_total = round(a_price + b_price, 2) if a_price is not None and b_price is not None else None
        return {
            **c,
            "item_a_menu_item_id": menu_item_ids.get(c["item_a"]),
            "item_b_menu_item_id": menu_item_ids.get(c["item_b"]),
            "individual_total": individual_total,
            "discount_reference_price": round(individual_total * COMBO_DISCOUNT, 2) if individual_total else None,
        }

    combos = [_enrich(c) for c in final_combos[:top_n]]
    rejected_combinations = [_enrich(c) for c in rejected_combinations]

    result = {
        "insufficient_data": False,
        "transaction_count": transaction_count,
        "total_orders_analyzed": raw_order_count,
        "valid_transactions": transaction_count,
        "excluded_transactions": excluded,
        "unique_products": unique_products,
        "avg_items_per_transaction": round(avg_basket_size, 3),
        "required": MIN_TRANSACTIONS,
        "combos": combos,
        "rejected_combinations": rejected_combinations,
        "mining_basis": mining_basis,
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
        if can_holdout and mining_basis["candidates_considered"] > 0:
            result["empty_reason"] = (
                f"{mining_basis['candidates_considered']} candidate(s) cleared the in-sample "
                f"thresholds on the training window, but none held up on the later, unseen "
                f"holdout window -- see rejected_combinations for exactly why each one failed. "
                f"This is a real finding, not a gap: a pattern that only exists in-sample is not "
                f"a repeatable one."
            )
        else:
            result["empty_reason"] = (
                "No association met the minimum transaction-count, confidence, and lift "
                "requirements, even after relaxing thresholds within a safe, bounded fallback "
                "tier (support/confidence loosened; the pair-count and lift floors were not)."
            )
    return result


def _combined_validation_table(product_combinations_result: dict, temporal_stability_result: dict) -> list[dict]:
    """2026-10: a single, explicit per-pair join of the two SEPARATE
    validation checks -- the live chronological 80/20 holdout
    (product_combinations' combos/rejected_combinations) and the
    expanding-window temporal-stability analysis (expanding_window_
    validation) -- so neither check's result is ever read as standing in
    for the other. A pair that only cleared one of the two is represented
    exactly as such; nothing here is "promoted" based on the other check,
    and nothing here changes which pairs product_combinations() actually
    returns as live recommendations (still decided by the holdout check
    alone, unchanged). Covers every pair either check ever looked at."""
    holdout_by_pair: dict[tuple[str, str], dict] = {}
    for c in (product_combinations_result.get("combos", []) + product_combinations_result.get("rejected_combinations", [])):
        key = tuple(sorted((c["item_a"], c["item_b"])))
        holdout_by_pair[key] = c

    stability_by_pair: dict[tuple[str, str], dict] = {}
    if temporal_stability_result.get("available"):
        for c in temporal_stability_result.get("candidates", []):
            key = tuple(sorted((c["item_a"], c["item_b"])))
            stability_by_pair[key] = c

    rows = []
    for key in sorted(set(holdout_by_pair) | set(stability_by_pair)):
        h = holdout_by_pair.get(key)
        s = stability_by_pair.get(key)
        item_a, item_b = (h["item_a"], h["item_b"]) if h else (s["item_a"], s["item_b"])
        rows.append({
            "item_a": item_a, "item_b": item_b,
            "holdout_status": h["status"] if h else "not_a_holdout_candidate",
            "holdout_lift": h.get("holdout_lift") if h else None,
            "temporal_stability_verdict": s["stability_verdict"] if s else "not_evaluated",
            "temporal_stability_evidenced_folds": s["evidenced_fold_count"] if s else 0,
            "temporal_stability_passing_folds": s["passing_fold_count"] if s else 0,
            "is_live_recommendation": bool(h and h["status"] == "holdout_validated"),
        })
    rows.sort(key=lambda r: (not r["is_live_recommendation"],
                             r["temporal_stability_verdict"] != "validated_stable"))
    return rows


def model_details(conn: sqlite3.Connection, lookback_days: int = 365) -> dict:
    """Real parameters and current stats for the FP-growth pass -- backs the
    technical panel under System Check, not the main Reports page. Includes
    the threshold-sensitivity table so the panel can answer "did we choose
    these thresholds to force a result, or is 0 combos what the data
    actually gives across a defensible range" with real numbers."""
    result = product_combinations(conn, lookback_days)
    temporal_stability = expanding_window_validation(conn, lookback_days)
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
        "mining_basis": result.get("mining_basis"),
        "rejected_combinations": result.get("rejected_combinations", []),
        "threshold_sensitivity": threshold_sensitivity(conn, lookback_days),
        "temporal_stability": temporal_stability,
        "combined_validation": _combined_validation_table(result, temporal_stability),
    }
