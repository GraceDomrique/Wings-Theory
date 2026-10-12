"""Ingredient-level depletion forecasting, driven by menu demand and recipes.

Why this module exists next to forecasting.py rather than inside it
-------------------------------------------------------------------
forecasting.ingredient_depletion() answers a *backward* question: at the
average rate this ingredient has actually left the ledger over the last N
days, how long does what is on hand last? That is a runway number. It is
correct and cheap, and nothing here replaces it for that specific job (see
api.py's forecast_depletion and listIngredients' withRunway). It no longer
powers Replenishment, the Today dashboard's "below reorder point" KPI, or
any purchase-quantity figure -- this module is the sole source of those
(2026-09 audit: the two used to disagree, since the trailing-average
version had its own independent order-quantity formula).

What it structurally cannot do is answer the *forward* question the floor
actually asks:

    POS order -> ordered menu quantity -> recipe quantities -> ingredient
    consumption -> current on-hand -> historical demand pattern -> current
    observed demand -> future demand forecast -> projected ingredient
    consumption -> projected remaining stock -> projected depletion date ->
    days of supply -> lead-time / reorder warning

A trailing ledger average is blind to every link before "current on-hand".
It cannot know that tomorrow is a Saturday, that Rice is consumed by nine
different menu items with nine different demand curves, or that today has
already sold twice a normal Monday's wings by 7pm. This module walks the
whole chain explicitly, from the same real tables the POS writes to.

The model, stated plainly
-------------------------
Per menu item, over `lookback_days`:

  1. TRADING DAYS ONLY. A calendar day on which `customer_order` holds no
     row at all is a day the shop did not trade, and it is excluded from
     every average here. A day the shop traded but this item did not sell
     is a real zero and stays in. This distinction is a fact available in
     the data, not an assumption -- and getting it wrong is the difference
     between "we sell 20 a day" and "we sell 12 a day" on a dataset with a
     six-day gap in it.

  2. BASELINE = exponentially weighted mean of daily sold quantity over
     those trading days, half-life EWMA_HALF_LIFE_DAYS. Recent days weigh
     more, so a genuine trend moves the number without a fitted trend line
     that this much data cannot support.

  3. DAY-OF-WEEK FACTOR = (mean of that weekday) / (overall mean), shrunk
     toward 1.0 by sample size:  (n*f + k) / (n + k). Two Saturdays do not
     get to claim a 1.8x Saturday effect; ten of them mostly do. Applied
     only once MIN_SEASONALITY_WEEKS of history exists.

  4. TODAY'S OBSERVATION (the nowcast). Sales so far today are compared
     against what a normal day of this weekday would have produced by this
     hour, using the pooled intraday profile (what share of a day's units
     have historically landed by hour H). That ratio is the *pace*:

         pace          = today_so_far / (baseline * dow * share_by_now)
         confidence w  = share_by_now          (early = weak, late = strong)
         adjustment    = clamp(1 + w*(pace-1), PACE_CLAMP)

     The remainder of today is then baseline * dow * (1 - share_by_now) *
     adjustment. A double-normal Monday raises today's projected total; a
     dead Monday lowers it. The clamp and the w-weighting together are what
     stop one abnormal ticket at 11:05am from rewriting the week.

  5. MOMENTUM. A damped share of that same adjustment (MOMENTUM_WEIGHT)
     carries into future days, clamped tighter still. Today is evidence
     about tomorrow, but weak evidence.

Then, per ingredient and per future day:

     consumption(ingredient, day) =
         SUM over menu items of  forecast_units(item, day) * recipe_qty_base(item, ingredient)
       + SUM over unlimited streams of  forecast_heads(day) * lambda_rolling(stream)
       + flat operational-use rate            (operational supplies only)

That is read straight out of v_recipe_leaf -- the *same view* the POS
availability gate and the deduction path read, so a forecast can never
disagree with what an order will actually deduct.

Finally the trajectory: start at (on_hand - reserved), subtract each day's
projected consumption, and record where the line crosses zero, where it sits
on the lead-time boundary, and where it sits against a reorder point the
owner actually configured. Day 0 subtracts only the *remainder* of today --
what already sold today has already left the ledger.

What is deliberately NOT claimed
--------------------------------
No annual/holiday seasonality. Ninety days of history contains no December
and no second August; a model claiming to know either would be lying. Weekly
seasonality is supported by this data and is used. That boundary is reported
to the UI (`seasonality`) rather than left implicit.

No fabricated confidence band. The trajectory band here is the demand
model's own spread (the dispersion of trading-day residuals around the
weekday-adjusted baseline), propagated through the recipe arithmetic. When
there is not enough history to compute it, it is omitted, not invented.

Where Prophet actually fits in (2026-09)
-----------------------------------------
The EWMA/day-of-week/nowcast model above is the default and remains fully
in charge of today's remainder, pace and momentum regardless of the
setting below -- those answer "how is today, already partway done, going",
which a periodically-refit model cannot itself know.

For FUTURE days (index 1..horizon_days), setting the 'depletion_demand_model'
app_setting to Prophet switches each POS-visible menu item's contribution to
whatever forecasting.fit_prophet() produced at the last explicit
refresh_prophet_baselines() call, per item, falling back to the EWMA figure
for any one item with no fresh fit (insufficient history, or not refreshed
yet). See refresh_prophet_baselines()'s docstring for why fitting is never
done inline: measured at ~1.8s/item, a live request cannot afford it for
every menu item on every cache invalidation. Purchase quantity is computed
exactly the same way regardless of which model supplied the daily figures --
Prophet (or EWMA) forecasts demand; this module still decides what to buy.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import sqlite3
import statistics
import threading
import time

from . import forecasting, reservation, timeutil
from .db import write_tx

# 2026-10-08: same structural real-vs-synthetic test as wt/anomaly.py's
# _REAL_USER_CLAUSE (see that module's SYNTHETIC_SEED_USER_IDS comment for
# the full reasoning) -- found missing here while diagnosing why
# replenishment_backtest()'s "real outcome" ground truth shifted after an
# otherwise-unrelated simulated-workbook-import fix. Every balance-
# reconstruction query below (_stock_history/_balances_at_cutoff/
# _min_balances_in_window) sums stock_ledger.delta_base with NO reason or
# provenance filter, by design -- a real wastage/spoilage/count_adjustment
# row must count toward a real historical balance exactly like a real sale
# does. But that same unfiltered sum also picks up every wastage/count
# row the simulated-workbook importer writes (reason IN wastage/spoilage/
# count_adjustment, same 108 real ingredients, overlapping real calendar
# dates) -- those rows are tagged user_id='simulated_workbook_import',
# never a real app_user, so this clause excludes them the same way
# anomaly.py already does for its own candidate population. Applied to
# every stock_ledger SUM in this module that reconstructs a historical
# balance (current-state reads already go through ingredient.on_hand_base,
# which an import never touches -- see simulated_workbook_import.py's own
# docstring -- so those paths need no change).
_REAL_LEDGER_USER_CLAUSE = "(user_id IS NULL OR user_id IN (SELECT username FROM app_user))"

# --- demand model constants -------------------------------------------------
LOOKBACK_DAYS = 56          # ~8 weeks: enough weekday repeats, recent enough to be this menu
HORIZON_DAYS = 21           # 3 weeks forward; beyond that this data supports nothing
EWMA_HALF_LIFE_DAYS = 14.0  # a day two weeks old counts half as much as today
MIN_TRADING_DAYS = 7        # below this the residual spread is not measured, so no band is drawn
MIN_SEASONALITY_WEEKS = 2   # matches forecasting.MIN_SEASONALITY_WEEKS
DOW_SHRINK_K = 3.0          # pseudo-observations pulling a weekday factor toward 1.0
PACE_MIN, PACE_MAX = 0.4, 2.5        # today's remainder may not swing further than this
MOMENTUM_WEIGHT = 0.35               # how much of today's pace carries to future days
MOMENTUM_MIN, MOMENTUM_MAX = 0.7, 1.6
REVIEW_CYCLE_DAYS = 3       # matches forecasting.REVIEW_CYCLE_DAYS
EXCESS_DAYS_OF_SUPPLY = 60  # beyond two months of cover with movement = slow-moving capital

# 2026-09 audit: an extreme forward-forecast consumption figure drives an
# equally extreme suggestedOrderQty, and this module has no mechanism that
# questions its own output before handing it to a purchase recommendation.
# FORECAST_DEVIATION_RATIO compares the forward model's steady daily
# consumption against the SAME ingredient's own simple trailing-ledger rate
# (forecasting.ingredient_depletion() -- a different, independent
# calculation over the same stock_ledger) and flags the ratio when they
# disagree by more than this multiple. This never changes the forecast or
# the suggested order -- it is surfaced (forecastDeviationFlag/Ratio) for a
# human to look at, exactly like every other flag in this codebase.
FORECAST_DEVIATION_RATIO = 3.0

# --- optional Prophet-driven baseline (2026-09) -----------------------------
# See refresh_prophet_baselines()'s docstring for the full reasoning. Short
# version: measured at ~1.8s per menu item (backend/wt tests), so fitting
# inline for every item on a live request is not viable -- a realistic
# 30-item menu would be ~55s, a 60-item one ~110s, per request. Prophet is
# instead fit only by an explicit refresh call, cached here, and consulted
# (never fit) by the live request path; an item with no fresh fit yet
# transparently uses the EWMA baseline for that one item.
PROPHET_STALE_AFTER_SECONDS = 48 * 3600
_prophet_store: dict[tuple, tuple[float, dict | None]] = {}
_prophet_lock = threading.Lock()
_prophet_generation = 0  # bumped on every refresh; api.py folds this into its cache fingerprint
                          # so a refresh takes effect on the very next request, not the next
                          # unrelated order/edit that happens to invalidate it otherwise.

# --- optional per-series HYBRID baseline (2026-09, production-integration
# pass) -- Level 2, live -------------------------------------------------
# Mirrors _prophet_store exactly (same key shape, same lock, same TTL) but
# holds whichever method forecasting._backtest_series()'s Level-2 selection
# actually validated as best for that item (naive / trailing-average /
# day-of-week / Prophet-with-its-own-selected-growth), not always Prophet.
# Kept as a SEPARATE store from _prophet_store rather than repurposing it,
# so demand_model='prophet' (today's existing, tested behavior: always
# Prophet when a fresh fit exists) is completely unaffected -- 'hybrid' is
# an additive new value, not a redefinition of an existing one. See
# refresh_prophet_baselines() for how this is populated and build_demand()
# for how demand_model='hybrid' consumes it.
_hybrid_store: dict[tuple, tuple[float, dict | None]] = {}
# Reuses _prophet_lock/_prophet_generation below for both stores -- a single
# refresh call updates both together, so one lock/one generation counter is
# simpler and correct (no risk of the two stores' generations drifting).


def prophet_refresh_generation() -> int:
    with _prophet_lock:
        return _prophet_generation


def snapshot_prophet_stores() -> tuple[dict, dict]:
    """Shallow copy of the live _prophet_store/_hybrid_store, for
    wt/ml_training.py's train_demand_prophet() to persist as a joblib
    artifact and to restore from if a refresh regresses (see
    restore_prophet_stores). Never mutated by anything else -- a fresh dict
    each call."""
    with _prophet_lock:
        return dict(_prophet_store), dict(_hybrid_store)


def restore_prophet_stores(snapshot: tuple[dict, dict]) -> None:
    """Rolls the live stores back to a prior snapshot (ml_training.py calls
    this when a refresh's fleet WAPE regressed, or when the refresh itself
    raised) and bumps the generation counter so api.py's cache fingerprint
    picks up the rollback on the very next request, exactly as it would a
    forward refresh."""
    global _prophet_generation
    prophet_snapshot, hybrid_snapshot = snapshot
    with _prophet_lock:
        _prophet_store.clear()
        _prophet_store.update(prophet_snapshot)
        _hybrid_store.clear()
        _hybrid_store.update(hybrid_snapshot)
        _prophet_generation += 1


def _prophet_scope_key(conn: sqlite3.Connection) -> str:
    """Identifies 'which database' for Prophet-cache scoping, read straight
    from the connection itself via PRAGMA database_list rather than id(conn)
    or a WT_ENV-backed global.

    Two things ruled out id(conn): db.get_conn() hands out one connection
    PER THREAD, and FastAPI/Starlette run sync endpoints in a worker
    threadpool, so a refresh request and the forecast request right after it
    are not guaranteed to land on the same thread -- id(conn) alone made a
    completed refresh invisible to the very next request (caught by
    TestProphetDemandModelSetting). And CPython recycles small objects'
    addresses quickly: across a long test run, a since-closed connection's
    id() got reused by an unrelated later one, serving a stale cross-test
    cached fit (caught by TestDepletionProphetDemandModel, but a real risk
    for any long-running process cycling through short-lived connections).
    sqlite3.Connection supports neither weakref nor arbitrary attributes, so
    neither of the usual fixes for "identity without id() reuse" applies --
    this asks SQLite itself what file the connection is actually attached
    to, which needs no WT_ENV/WT_DB and is correct for exactly the same
    reason db.db_path() would be, without that dependency.
    """
    return conn.execute("PRAGMA database_list").fetchone()[2] or "memory"

# Trading hours used only if there is no intraday history at all to profile.
FALLBACK_OPEN_HOUR, FALLBACK_CLOSE_HOUR = 11, 22

STATUS_ORDER = {
    "out": 0, "critical": 1, "at_risk": 2, "healthy": 3,
    "excess": 4, "idle": 5,
}
# No 'no_data' tier here, unlike forecasting.RISK_ORDER: every ingredient
# reaching ingredient_forecast() always gets a real trajectory computed
# (build_demand's EWMA/Prophet blend always returns *something*, even a
# flat zero series for an item that has never sold), so "idle" -- steady
# demand and depletion are both non-positive -- already covers "nothing
# to project" here. A prior 'no_data' entry in this dict was never once
# assigned by ingredient_forecast() and would have sorted last regardless;
# removed rather than left as a status the API can never actually return.


# ===========================================================================
# Shared data pulls
# ===========================================================================
def _trading_days(conn: sqlite3.Connection, lookback_days: int,
                  now: _dt.datetime | None = None) -> list[str]:
    """Manila calendar days inside the window on which the shop actually took
    an order. Excludes days with no `customer_order` row at all -- see the
    module docstring: that is a closed/no-data day, not a zero-sales day, and
    averaging over it silently understates every rate downstream.

    2026-10 leakage fix: `now` now bounds BOTH ends of the window (was
    lower-bound only, and the lower bound itself was never threaded through
    at all) -- see build_demand()'s own 2026-10 note for the full audit.
    now=None (every caller before replenishment_backtest() started passing
    a historical one) is unchanged: reads up to the live wall clock, same
    as always."""
    bd = timeutil.sql_business_date("created_at")
    upper = " AND created_at < ?" if now is not None else ""
    params = [timeutil.utc_bound_n_manila_days_ago(lookback_days, now)]
    if now is not None:
        params.append(timeutil.utc_upper_bound_sql(now))
    rows = conn.execute(
        f"SELECT DISTINCT {bd} AS d FROM customer_order "
        f"WHERE status != 'void' AND created_at >= ?{upper} ORDER BY d",
        params,
    ).fetchall()
    return [r["d"] for r in rows]


def _item_daily_qty(conn: sqlite3.Connection, lookback_days: int,
                    now: _dt.datetime | None = None) -> dict[int, dict[str, float]]:
    """2026-10 leakage fix: see _trading_days()'s identical note."""
    bd = timeutil.sql_business_date("o.created_at")
    upper = " AND o.created_at < ?" if now is not None else ""
    params = [timeutil.utc_bound_n_manila_days_ago(lookback_days, now)]
    if now is not None:
        params.append(timeutil.utc_upper_bound_sql(now))
    rows = conn.execute(
        f"""
        SELECT ol.menu_item_id AS pk, {bd} AS d, SUM(ol.qty) AS qty
          FROM order_line ol JOIN customer_order o ON o.id = ol.order_id
         WHERE o.status != 'void' AND o.created_at >= ?{upper}
         GROUP BY pk, d
        """,
        params,
    ).fetchall()
    out: dict[int, dict[str, float]] = {}
    for r in rows:
        out.setdefault(r["pk"], {})[r["d"]] = float(r["qty"])
    return out


def _heads_daily(conn: sqlite3.Connection, lookback_days: int,
                 now: _dt.datetime | None = None) -> dict[str, float]:
    """Covers per day on unlimited tickets. Unlimited consumption is sized by
    heads x lambda, never by line quantity (see resolver.order_requirements),
    so heads -- not menu units -- is the series that drives those streams.

    2026-10 leakage fix: see _trading_days()'s identical note -- this was
    the single biggest contributor to the replenishment-backtest leakage,
    since every unli-stream ingredient's (and the new unli-refill demand
    component's) projection scales off this series."""
    bd = timeutil.sql_business_date("o.created_at")
    upper = " AND o.created_at < ?" if now is not None else ""
    params = [timeutil.utc_bound_n_manila_days_ago(lookback_days, now)]
    if now is not None:
        params.append(timeutil.utc_upper_bound_sql(now))
    rows = conn.execute(
        f"""
        SELECT {bd} AS d, SUM(o.head_count * ol.qty) AS heads
          FROM customer_order o
          JOIN order_line ol ON ol.order_id = o.id
          JOIN menu_item  m  ON m.id = ol.menu_item_id
         WHERE m.is_unli = 1 AND o.status != 'void' AND o.created_at >= ?{upper}
         GROUP BY d
        """,
        params,
    ).fetchall()
    return {r["d"]: float(r["heads"] or 0) for r in rows}


def _intraday_share(conn: sqlite3.Connection, lookback_days: int,
                    now: _dt.datetime | None = None) -> list[float]:
    """share[h] = fraction of a normal day's units sold at or before Manila
    hour h, pooled across every item (one item alone is too sparse to profile
    an hour with). This is what makes "40 units by 7pm" comparable to a whole
    day's baseline.

    2026-10 leakage fix: see _trading_days()'s identical note."""
    shift = f"+{timeutil.MANILA_UTC_OFFSET_HOURS} hours"
    upper = " AND o.created_at < ?" if now is not None else ""
    params = [timeutil.utc_bound_n_manila_days_ago(lookback_days, now)]
    if now is not None:
        params.append(timeutil.utc_upper_bound_sql(now))
    rows = conn.execute(
        f"""
        SELECT CAST(strftime('%H', o.created_at, '{shift}') AS INTEGER) AS hr,
               SUM(ol.qty) AS qty
          FROM order_line ol JOIN customer_order o ON o.id = ol.order_id
         WHERE o.status != 'void' AND o.created_at >= ?{upper}
         GROUP BY hr
        """,
        params,
    ).fetchall()
    by_hour = [0.0] * 24
    for r in rows:
        by_hour[int(r["hr"])] += float(r["qty"] or 0)
    total = sum(by_hour)
    if total <= 0:
        # No intraday history: assume units arrive evenly across trading hours.
        share, span = [], max(1, FALLBACK_CLOSE_HOUR - FALLBACK_OPEN_HOUR)
        for h in range(24):
            if h < FALLBACK_OPEN_HOUR:
                share.append(0.0)
            elif h >= FALLBACK_CLOSE_HOUR:
                share.append(1.0)
            else:
                share.append((h - FALLBACK_OPEN_HOUR + 1) / span)
        return share
    cum, running = [], 0.0
    for h in range(24):
        running += by_hour[h]
        cum.append(running / total)
    return cum


def _recipe_leaves(conn: sqlite3.Connection) -> dict[int, list[tuple[int, float]]]:
    """recipe_id -> [(ingredient_id, qty_base per serving)]. Read from
    v_recipe_leaf, the same view the availability gate and the deduction path
    use, so this forecast and an actual order can never disagree."""
    out: dict[int, list[tuple[int, float]]] = {}
    for r in conn.execute("SELECT recipe_id, ingredient_id, qty_base FROM v_recipe_leaf"):
        out.setdefault(r["recipe_id"], []).append((r["ingredient_id"], float(r["qty_base"])))
    return out


def _unli_streams(conn: sqlite3.Connection) -> list[dict]:
    """Active unlimited streams with their ROLLING coefficient -- the live
    trend-adjusted estimate the deduction path actually uses. The frozen
    baseline is for variance comparison and is never used to size anything."""
    rows = conn.execute(
        """
        SELECT ui.ingredient_id, ui.label, uc.lambda_value, uc.status
          FROM unli_item ui
          JOIN unli_coefficient uc ON uc.unli_item_id = ui.id
         WHERE ui.is_active = 1 AND uc.role = 'rolling'
           AND uc.effective_from = (
                 SELECT MAX(uc2.effective_from) FROM unli_coefficient uc2
                  WHERE uc2.unli_item_id = ui.id AND uc2.role = 'rolling')
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ===========================================================================
# Demand model
# ===========================================================================
def _ewma(points: list[tuple[_dt.date, float]], today: _dt.date) -> float:
    """Exponentially weighted mean, weighted by *calendar* age rather than
    position, so a gap in trading days doesn't make an old day look recent."""
    if not points:
        return 0.0
    num = den = 0.0
    for d, v in points:
        age = max(0, (today - d).days)
        w = 0.5 ** (age / EWMA_HALF_LIFE_DAYS)
        num += w * v
        den += w
    return num / den if den > 0 else 0.0


def _weekday_factors(points: list[tuple[_dt.date, float]], overall: float) -> dict[int, float]:
    """Shrunk day-of-week multipliers. `(n*f + k) / (n + k)` pulls a factor
    built from few observations back toward 1.0 -- a weekday seen twice does
    not get to assert a strong effect."""
    if overall <= 0:
        return {}
    by_wd: dict[int, list[float]] = {}
    for d, v in points:
        by_wd.setdefault(d.weekday(), []).append(v)
    out = {}
    for wd, vals in by_wd.items():
        n = len(vals)
        raw = (sum(vals) / n) / overall
        out[wd] = (n * raw + DOW_SHRINK_K * 1.0) / (n + DOW_SHRINK_K)
    return out


def _dispersion(points: list[tuple[_dt.date, float]], baseline: float,
                factors: dict[int, float]) -> float | None:
    """Standard deviation of trading-day residuals around the weekday-adjusted
    baseline -- the model's own observed spread, used for the uncertainty band.
    Returns None when there is not enough history to measure it, in which case
    no band is drawn at all."""
    if len(points) < MIN_TRADING_DAYS or baseline <= 0:
        return None
    resid = [v - baseline * factors.get(d.weekday(), 1.0) for d, v in points]
    n = len(resid)
    mean = sum(resid) / n
    var = sum((r - mean) ** 2 for r in resid) / n
    return math.sqrt(var)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class DemandSeries:
    """One forecastable series (a menu item, or the unlimited head count).

    `daily[k]` is the projected quantity for `today + k`. Index 0 is special:
    it is what is *still to come* today, since what already sold today has
    already been deducted from stock.
    """

    __slots__ = ("key", "name", "baseline", "factors", "sigma", "today_actual",
                 "today_remaining", "pace", "pace_adjustment", "momentum",
                 "trading_days", "seasonality", "daily", "sigma_daily", "demand_source")

    def __init__(self, key, name, points, today, share_by_now, horizon_days):
        self.key = key
        self.name = name
        self.trading_days = len(points)
        today_actual = 0.0
        history = []
        for d, v in points:
            if d == today:
                today_actual = v
            else:
                history.append((d, v))
        self.today_actual = today_actual

        # Baseline is fit on completed days only. Including a half-finished
        # today would drag every average down by however much of the day is
        # left, which is exactly the error the nowcast below exists to avoid.
        self.baseline = _ewma(history, today)
        overall = (sum(v for _, v in history) / len(history)) if history else 0.0
        weeks = len({d.isocalendar()[:2] for d, _ in history})
        self.seasonality = weeks >= MIN_SEASONALITY_WEEKS
        self.factors = _weekday_factors(history, overall) if self.seasonality else {}
        self.sigma = _dispersion(history, self.baseline, self.factors)

        dow_today = self.factors.get(today.weekday(), 1.0)
        normal_today = self.baseline * dow_today
        expected_by_now = normal_today * share_by_now

        if expected_by_now > 0:
            self.pace = today_actual / expected_by_now
            # Confidence in the pace grows with how much of the day is done.
            w = _clamp(share_by_now, 0.0, 1.0)
            self.pace_adjustment = _clamp(1.0 + w * (self.pace - 1.0), PACE_MIN, PACE_MAX)
        else:
            self.pace = None
            self.pace_adjustment = 1.0

        self.today_remaining = max(0.0, normal_today * (1.0 - share_by_now) * self.pace_adjustment)
        self.momentum = _clamp(1.0 + MOMENTUM_WEIGHT * (self.pace_adjustment - 1.0),
                               MOMENTUM_MIN, MOMENTUM_MAX)

        self.daily = [self.today_remaining]
        self.sigma_daily = [(self.sigma or 0.0) * (1.0 - share_by_now)]
        for k in range(1, horizon_days + 1):
            d = today + _dt.timedelta(days=k)
            self.daily.append(max(0.0, self.baseline * self.factors.get(d.weekday(), 1.0)
                                  * self.momentum))
            self.sigma_daily.append(self.sigma or 0.0)
        # Overwritten by build_demand() to "prophet" when a fresh cached fit
        # exists for this series (see refresh_prophet_baselines below) --
        # never computed here, since fitting Prophet is too slow to do
        # inline. today_remaining/pace/momentum above are untouched either
        # way: they answer "how is today, already partway done, actually
        # going", which a periodically-refreshed day-ahead model cannot
        # itself know.
        self.demand_source = "ewma"

    @property
    def today_projected_total(self) -> float:
        return self.today_actual + self.today_remaining

    def as_dict(self) -> dict:
        return {
            "baselineDaily": round(self.baseline, 3),
            "tradingDays": self.trading_days,
            "weeklySeasonality": self.seasonality,
            "todayActual": round(self.today_actual, 3),
            "todayRemaining": round(self.today_remaining, 3),
            "todayProjected": round(self.today_projected_total, 3),
            "pace": round(self.pace, 3) if self.pace is not None else None,
            "paceAdjustment": round(self.pace_adjustment, 3),
            "momentum": round(self.momentum, 3),
            "demandSource": self.demand_source,
        }


# ===========================================================================
# Prophet-driven baseline for future days (opt in, explicitly refreshed)
# ===========================================================================
# Independent of `lookback_days` above (which sizes the actual fit's own
# training window, unchanged since before adaptive growth existed): Level-1
# growth selection (forecasting.select_prophet_growth()) wants as much real
# history as exists to find enough tuning folds -- at this function's own
# horizon_days=HORIZON_DAYS=21, even 2 folds needs 21+2*21=63 real days,
# already past LOOKBACK_DAYS=56. A second, longer _daily_series() query
# (still <30ms per the module's own measurements) is the simplest fix that
# does not touch the existing fit's window or any test asserting it.
GROWTH_SELECTION_LOOKBACK_DAYS = 365


def refresh_prophet_baselines(conn: sqlite3.Connection, horizon_days: int = HORIZON_DAYS,
                              lookback_days: int = LOOKBACK_DAYS) -> dict:
    """Fit Prophet for every POS-visible menu item and cache the result for
    build_demand() to consult when demand_model='prophet'.

    This is the ONLY place ingredient-level replenishment ever fits Prophet.
    Deliberately not run inline by a live request: forecasting.fit_prophet()
    measured at ~1.8s per item on this codebase's own test data, so a
    realistic 30-item menu is ~55s sequential and a 60-item one ~110s --
    both far past any request a person is waiting on. Call this from a
    scheduled task or an admin action (see
    POST /console/forecasts/refresh-prophet-baselines) on whatever cadence
    the deployment wants. Per the spec: forecasts need not retrain after
    every single order, but new real data must eventually reach the next
    forecast -- this is that refresh, on a clearly defined trigger (call it)
    rather than an implicit, unbounded one.

    An item that has too little history to forecast is cached as None
    (insufficient data) rather than left unset, so the live path can tell
    "not enough data yet" apart from "hasn't been refreshed since this item
    started selling" -- both fall back to the EWMA baseline for that one
    item either way, but the counts below make the difference visible to
    whoever is watching this run.

    2026-09 adaptive-growth pass: before fitting, each item now runs a
    Level-1 growth selection (forecasting.select_prophet_growth()) over its
    own history, picking whichever of growth='flat'/'linear' scored lower
    tuning WAPE on that item specifically -- see forecasting.py's own
    "Level 1" section for why a single global default stopped being
    accurate for this restaurant's highest-volume series. No leakage risk
    here: unlike evaluate_demand_model()'s reporting backtest, there is no
    held-out period to protect, because the fit this selection feeds
    targets genuinely future days no fold, anywhere, has ever seen. The
    selection is cached two places: alongside the fit itself in
    _prophet_store (same TTL/generation invalidation already governing that
    cache, read by build_demand()) AND in forecasting's shared
    _growth_selection_store (via cache_growth_selection()), which is what
    menu_item_demand_prophet() (the single-item detail page) now reads too
    -- closing the inconsistency where that page kept using the old global
    PROPHET_DEFAULT_GROWTH while replenishment had already gone adaptive.

    2026-09 (second pass): also runs the identical Level-1 selection for the
    3 store-wide metrics (orders/items/sales) and caches those the same way,
    for store_demand_prophet() to read -- that path fits inline per request
    (it is not part of the per-item _prophet_store/build_demand machinery)
    but needed the same growth consistency fix.
    """
    # 2026-09 second adaptive-growth pass: anchor to the dead-tail-aware
    # "now" (see forecasting.effective_now()'s own section) instead of the
    # live wall clock, which currently sits a few days past a real
    # data-collection gap in this deployment's order history. Computed once
    # per refresh call and reused for every item/metric below, so a single
    # DB round-trip covers the whole run.
    anchor = forecasting.effective_now(conn)
    items = conn.execute("SELECT id, name FROM menu_item WHERE pos_visible = 1").fetchall()
    scope = _prophet_scope_key(conn)
    fitted = insufficient = failed = 0
    growth_flat = growth_linear = growth_fallback = 0
    hybrid_unavailable = 0
    hybrid_method_counts: dict[str, int] = {}
    for it in items:
        rows, series = forecasting._daily_series(conn, it["id"], lookback_days, now=anchor)
        key = (scope, it["id"], lookback_days, horizon_days)
        if len(rows) < forecasting.MIN_DEMAND_SAMPLE_DAYS:
            result = None
            insufficient += 1
            hybrid_unavailable += 1
            with _prophet_lock:
                _hybrid_store[key] = (time.monotonic(), None)
        else:
            growth_rows, growth_series = forecasting._daily_series(
                conn, it["id"], GROWTH_SELECTION_LOOKBACK_DAYS, now=anchor)
            selection = forecasting.select_prophet_growth(
                growth_rows, growth_series, horizon=horizon_days)
            series_key = f"menu_item:{it['id']}"
            cached_selection = {
                **selection,
                "series_key": series_key,
                "selection_date": timeutil.business_today().isoformat(),
                "training_cutoff": selection["tuning_end_date"],
                "flat_validation_wape": selection["mean_wape"].get("flat"),
                "linear_validation_wape": selection["mean_wape"].get("linear"),
            }
            forecasting.cache_growth_selection(conn, series_key, cached_selection)
            if selection["is_fallback"]:
                growth_fallback += 1
            elif selection["selected_growth"] == "linear":
                growth_linear += 1
            else:
                growth_flat += 1

            result = forecasting.fit_prophet(series, rows, horizon_days,
                                             growth=selection["selected_growth"])
            if result is None:
                failed += 1
            else:
                fitted += 1
                result = {**result, "growth_used": selection["selected_growth"],
                         "growth_selection": cached_selection}

            # --- Level 2, live: validate naive/trailing-avg/day-of-week/
            # Prophet on the SAME growth_series/growth_rows the growth
            # selection above just used (reusing selection["selected_growth"]
            # rather than re-deriving it -- _backtest_series's own
            # prophet_growth override exists exactly for this), then apply
            # whichever wins to the full series for a real live forecast.
            # This is what demand_model='hybrid' reads; demand_model=
            # 'prophet' above is completely unaffected (separate cache).
            backtest = forecasting._backtest_series(
                growth_rows, growth_series, horizon_days,
                forecasting.ROLLING_MIN_TRAIN_DAYS, forecasting.ROLLING_MAX_FOLDS,
                prophet_growth=selection["selected_growth"],
            )
            if backtest is None:
                hybrid_result = None
                hybrid_unavailable += 1
            else:
                method = backtest["selected_method"]
                hybrid_forecast = forecasting.forecast_with_method(
                    growth_series, growth_rows, horizon_days, method,
                    growth=selection["selected_growth"],
                )
                if hybrid_forecast is None:
                    hybrid_result = None
                    hybrid_unavailable += 1
                else:
                    hybrid_result = {
                        "forecast": hybrid_forecast,
                        "method_used": method,
                        "growth_used": selection["selected_growth"] if method == "prophet" else None,
                        "method_selection": {
                            "series_key": series_key,
                            "selected_method": method,
                            "selection_reason": backtest["selection_reason"],
                            "methods": backtest["methods"],
                            "selection_date": timeutil.business_today().isoformat(),
                        },
                    }
                    hybrid_method_counts[method] = hybrid_method_counts.get(method, 0) + 1
            with _prophet_lock:
                _hybrid_store[key] = (time.monotonic(), hybrid_result)
        with _prophet_lock:
            _prophet_store[key] = (time.monotonic(), result)
    global _prophet_generation
    with _prophet_lock:
        _prophet_generation += 1

    store_growth_flat = store_growth_linear = store_growth_fallback = 0
    for metric in forecasting.STORE_METRICS:
        growth_rows, growth_series = forecasting._store_daily_series(
            conn, GROWTH_SELECTION_LOOKBACK_DAYS, now=anchor)[metric]
        selection = forecasting.select_prophet_growth(
            growth_rows, growth_series, horizon=horizon_days)
        series_key = f"store:{metric}"
        cached_selection = {
            **selection,
            "series_key": series_key,
            "selection_date": timeutil.business_today().isoformat(),
            "training_cutoff": selection["tuning_end_date"],
            "flat_validation_wape": selection["mean_wape"].get("flat"),
            "linear_validation_wape": selection["mean_wape"].get("linear"),
        }
        forecasting.cache_growth_selection(conn, series_key, cached_selection)
        if selection["is_fallback"]:
            store_growth_fallback += 1
        elif selection["selected_growth"] == "linear":
            store_growth_linear += 1
        else:
            store_growth_flat += 1

    return {
        "itemsConsidered": len(items), "fitted": fitted,
        "insufficientData": insufficient, "fitFailed": failed,
        "horizonDays": horizon_days, "lookbackDays": lookback_days,
        "growthFlat": growth_flat, "growthLinear": growth_linear,
        "growthFallback": growth_fallback,
        "storeGrowthFlat": store_growth_flat, "storeGrowthLinear": store_growth_linear,
        "storeGrowthFallback": store_growth_fallback,
        "effectiveNow": timeutil.business_today(anchor).isoformat(),
        "hybridMethodCounts": hybrid_method_counts,
        "hybridUnavailable": hybrid_unavailable,
    }


# ===========================================================================
# 2026-10 fix: an automatic refresh, so "hybrid" is what actually runs
# ===========================================================================
# refresh_prophet_baselines() above was only ever invoked by a human calling
# POST /console/forecasts/refresh-prophet-baselines (or a test). Nothing in
# this codebase's own startup/scheduling ever called it, so the
# depletion_demand_model setting could read "hybrid" while every item's
# actual _prophet_daily_for_item()/_hybrid_daily_for_item() still returned
# None (never refreshed) and silently fell back to EWMA for that item --
# confirmed by this project's own test_default_setting_is_hybrid. The fix
# below is deliberately the smallest thing that closes that gap: a single
# background thread, started once at process startup, that calls the SAME
# refresh_prophet_baselines() on a fixed interval well inside
# PROPHET_STALE_AFTER_SECONDS -- never retrained per-request, never
# retrained per POS transaction, and never blocking app startup (the thread
# is spawned and the startup handler returns immediately).
REFRESH_INTERVAL_SECONDS = 12 * 3600  # 4x inside the 48h staleness window
_refresh_run_lock = threading.Lock()
_refresh_running = False


def get_refresh_status(conn: sqlite3.Connection) -> dict | None:
    """Last-known outcome of refresh_prophet_baselines(), however it was
    triggered (the background scheduler or the manual admin endpoint) --
    both go through run_prophet_refresh_job() below, which is the only
    writer of this row. Returns None before the very first run in this
    database's lifetime (e.g. immediately after a fresh migration, before
    the background thread's first pass completes)."""
    row = conn.execute(
        "SELECT value FROM app_setting WHERE key='prophet_refresh_status'"
    ).fetchone()
    if not row or not row["value"]:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return None


def _save_refresh_status(conn: sqlite3.Connection, status: dict) -> None:
    with write_tx(conn):
        conn.execute(
            "INSERT INTO app_setting (key, value, updated_by, updated_at) "
            "VALUES ('prophet_refresh_status', ?, 'system', datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (json.dumps(status),),
        )


def run_prophet_refresh_job(conn: sqlite3.Connection, *, triggered_by: str,
                            horizon_days: int = HORIZON_DAYS,
                            lookback_days: int = LOOKBACK_DAYS) -> dict:
    """The one path both the background scheduler and the manual admin
    endpoint call to actually run refresh_prophet_baselines() -- guards
    against two refreshes overlapping (Prophet fitting a realistic menu
    takes on the order of a minute; a second trigger arriving mid-run would
    otherwise race the same in-memory _prophet_store/_hybrid_store), records
    a durable last-run status so the console can show real freshness
    instead of silence, and never lets a Prophet failure propagate out to
    crash a background thread or an API request -- a failed run is recorded
    as a failure and the existing per-item EWMA fallback keeps serving
    forecasts exactly as it already does for any never-refreshed item."""
    global _refresh_running
    with _refresh_run_lock:
        if _refresh_running:
            return {"skipped": True, "reason": "a refresh is already running"}
        _refresh_running = True
    started_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        summary = refresh_prophet_baselines(conn, horizon_days, lookback_days)
        # Merged, not nested: every existing caller of this endpoint reads
        # fields like body["fitted"] straight off the top level (this used
        # to BE refresh_prophet_baselines()'s own return value) -- the new
        # status fields are additive, not a reshape.
        status = {**summary, "lastRunAt": started_at, "lastRunOk": True,
                 "triggeredBy": triggered_by}
        _save_refresh_status(conn, status)
        return status
    except Exception as exc:  # noqa: BLE001 -- must never crash the caller
        status = {
            "lastRunAt": started_at, "lastRunOk": False,
            "triggeredBy": triggered_by, "error": str(exc),
        }
        _save_refresh_status(conn, status)
        return status
    finally:
        with _refresh_run_lock:
            _refresh_running = False


def start_prophet_refresh_scheduler(get_conn) -> None:
    """Spawns the one background thread that keeps hybrid forecasting
    actually fed with fresh Prophet fits, on a fixed interval, without
    blocking whatever calls this (app startup). `get_conn` is a zero-arg
    callable returning this process's own connection (same pattern as
    api.py's own `conn()` dependency) -- called fresh each iteration rather
    than captured once, since a long-lived background thread should not
    hold a single sqlite3.Connection open across hours of POS traffic.

    Runs an initial refresh almost immediately (a fresh deployment should
    not have to wait REFRESH_INTERVAL_SECONDS for its first real Prophet
    fit) and then on the fixed interval forever, for the lifetime of the
    process. A daemon thread: it never needs its own graceful-shutdown path
    because it does nothing but call an idempotent, already-transactional
    job function."""
    import logging
    logger = logging.getLogger("wt.depletion.prophet_scheduler")

    def _loop() -> None:
        time.sleep(5)  # let the app finish starting before the first fit
        while True:
            try:
                run_prophet_refresh_job(get_conn(), triggered_by="scheduler")
            except Exception:  # noqa: BLE001 -- the loop must never die
                logger.exception("scheduled Prophet refresh failed")
            time.sleep(REFRESH_INTERVAL_SECONDS)

    t = threading.Thread(target=_loop, name="prophet-refresh-scheduler", daemon=True)
    t.start()


def _prophet_daily_for_item(conn: sqlite3.Connection, item_pk: int,
                            lookback_days: int, horizon_days: int) -> dict | None:
    """Whatever refresh_prophet_baselines() last cached for this item, or
    None if it has never been refreshed, has gone stale (no refresh in
    PROPHET_STALE_AFTER_SECONDS -- an old Prophet forecast being served as
    current would be worse than an honest, current EWMA fallback), or the
    item did not have enough history at the time of that refresh. Never
    fits inline -- see refresh_prophet_baselines().

    See _prophet_scope_key() for why this is keyed by database path rather
    than id(conn) whenever WT_ENV is configured -- otherwise a refresh
    handled by one worker thread's connection could be invisible to a
    forecast request handled by another.
    """
    key = (_prophet_scope_key(conn), item_pk, lookback_days, horizon_days)
    with _prophet_lock:
        hit = _prophet_store.get(key)
    if hit is None:
        return None
    computed_at, result = hit
    if time.monotonic() - computed_at > PROPHET_STALE_AFTER_SECONDS:
        return None
    return result


def _hybrid_daily_for_item(conn: sqlite3.Connection, item_pk: int,
                           lookback_days: int, horizon_days: int) -> dict | None:
    """Whatever refresh_prophet_baselines() last cached for this item's
    Level-2 selected method, or None on the identical three conditions
    _prophet_daily_for_item() documents (never refreshed / stale / too
    little history) -- same cache contract, same fallback-to-EWMA meaning
    for the caller, just reading _hybrid_store instead of _prophet_store."""
    key = (_prophet_scope_key(conn), item_pk, lookback_days, horizon_days)
    with _prophet_lock:
        hit = _hybrid_store.get(key)
    if hit is None:
        return None
    computed_at, result = hit
    if time.monotonic() - computed_at > PROPHET_STALE_AFTER_SECONDS:
        return None
    return result


def build_demand(conn: sqlite3.Connection, lookback_days: int = LOOKBACK_DAYS,
                 horizon_days: int = HORIZON_DAYS, now: _dt.datetime | None = None,
                 demand_model: str = "ewma") -> dict:
    """Every forecastable demand series in one pass: each POS-visible menu
    item, plus the unlimited head count.

    demand_model='ewma' (the default, and the only behavior before 2026-09):
    every series is the EWMA/day-of-week/nowcast model below, unchanged.

    demand_model='prophet': for future days (index 1..horizon_days) only,
    each POS-visible menu item uses whatever Prophet forecast
    refresh_prophet_baselines() most recently cached for it, if any is fresh
    -- see _prophet_daily_for_item(). An item with no fresh Prophet fit
    (never refreshed, refresh went stale, or too little history) keeps its
    EWMA series for that one item; this is a per-item fallback, not
    all-or-nothing. today_remaining/pace/momentum are computed identically
    either way and are never sourced from Prophet -- see DemandSeries'
    demand_source note. The unlimited head-count series is also always EWMA:
    Prophet is scoped to menu-item sales, which is what it was actually fit
    on; head counts are a different signal this pass does not extend to.

    demand_model='hybrid' (2026-09, production-integration pass): the same
    future-day substitution as 'prophet', but each item uses whichever
    method forecasting._backtest_series()'s Level-2 selection actually
    validated as most accurate for THAT item (naive / trailing-average /
    day-of-week / Prophet-with-its-own-selected-growth) -- see
    _hybrid_daily_for_item() and refresh_prophet_baselines(). This is the
    strategy evaluate_demand_model() reports 55.69% fleet-mean WAPE for;
    'prophet' alone is not the same thing (it forces Prophet on every item
    with a fresh fit, which evaluate_demand_model() measures at 57.81% --
    worse, because Prophet is not the best method for every series). Same
    per-item fallback-to-EWMA on a cache miss/stale entry/insufficient
    history as 'prophet'; never re-tunes or re-backtests inline.
    """
    today = timeutil.business_today(now)
    now_manila = timeutil.to_manila(now or timeutil.utc_now())
    manila_hour = now_manila.hour
    share = _intraday_share(conn, lookback_days, now)
    # 2026-10 fix: share[h] is cumulative through the END of hour h, but only
    # part of the current hour has elapsed. Reading share[manila_hour] whole
    # counted sales that cannot have happened yet as "expected by now", which
    # understated pace (and, through it, today's remainder and the momentum
    # carried into every future day) -- by ~23% at 20:00 Manila on the real
    # profile, where hour 20 alone holds ~20% of a day's units. Interpolate
    # linearly inside the hour instead, between the previous hour's cumulative
    # share and this one's.
    hour_start = share[manila_hour - 1] if manila_hour > 0 else 0.0
    elapsed = (now_manila.minute * 60 + now_manila.second) / 3600.0
    share_by_now = _clamp(hour_start + elapsed * (share[manila_hour] - hour_start), 0.0, 1.0)

    trading = set(_trading_days(conn, lookback_days, now))
    per_item = _item_daily_qty(conn, lookback_days, now)
    heads = _heads_daily(conn, lookback_days, now)

    items = conn.execute(
        "SELECT id, name, category, recipe_id, is_unli, price FROM menu_item WHERE pos_visible = 1"
    ).fetchall()

    series: dict[int, DemandSeries] = {}
    meta: dict[int, sqlite3.Row] = {}
    for it in items:
        day_map = per_item.get(it["id"], {})
        points = [(_dt.date.fromisoformat(d), day_map.get(d, 0.0)) for d in sorted(trading)]
        s = DemandSeries(it["id"], it["name"], points, today, share_by_now, horizon_days)
        if demand_model in ("prophet", "hybrid"):
            pf = (_prophet_daily_for_item(conn, it["id"], lookback_days, horizon_days)
                 if demand_model == "prophet" else
                 _hybrid_daily_for_item(conn, it["id"], lookback_days, horizon_days))
            if pf is not None:
                for k in range(1, horizon_days + 1):
                    day = pf["forecast"][k - 1]
                    s.daily[k] = max(0.0, day["qty"] * s.momentum)
                    # (high-low)/2 under an 80%-interval normal approximation
                    # (z=1.2816 each side) -- Prophet's own uncertainty where
                    # the selected method IS Prophet; a baseline method's
                    # forecast has low=high=qty (a point forecast, no native
                    # interval -- see forecasting.forecast_with_method()),
                    # which correctly collapses this to sigma=0 rather than
                    # fabricating an uncertainty band a simple average never
                    # produced.
                    s.sigma_daily[k] = max(0.0, day["high"] - day["low"]) / (2 * 1.2816)
                s.demand_source = "prophet" if demand_model == "prophet" else pf.get("method_used", "prophet")
        series[it["id"]] = s
        meta[it["id"]] = it

    head_points = [(_dt.date.fromisoformat(d), heads.get(d, 0.0)) for d in sorted(trading)]
    head_series = DemandSeries("heads", "Unlimited covers", head_points, today,
                               share_by_now, horizon_days)

    return {
        "today": today,
        "manila_hour": manila_hour,
        "share_by_now": share_by_now,
        "trading_days": sorted(trading),
        "items": series,
        "item_meta": meta,
        "heads": head_series,
        "horizon_days": horizon_days,
        "lookback_days": lookback_days,
    }


# ===========================================================================
# Historical stock reconstruction
# ===========================================================================
def _stock_history(conn: sqlite3.Connection, ingredient_id: int, on_hand: float,
                   days: int, today: _dt.date, now: _dt.datetime | None = None) -> list[dict]:
    """End-of-day on-hand for the last `days` Manila days, reconstructed by
    walking the ledger backwards from the current balance.

    Deliberately not read from stock_ledger.balance_after_base: that column is
    correct at insertion time, but the seeded history was inserted out of
    chronological order, so the highest id is not the latest event. Summing
    signed deltas after a cutoff is order-independent and cannot drift from
    ingredient.on_hand_base, which is the number the POS actually gates on.
    """
    bd = timeutil.sql_business_date("created_at")
    rows = conn.execute(
        f"SELECT {bd} AS d, SUM(delta_base) AS delta FROM stock_ledger "
        f"WHERE ingredient_id = ? AND created_at >= ? AND {_REAL_LEDGER_USER_CLAUSE} GROUP BY d",
        (ingredient_id, timeutil.utc_bound_n_manila_days_ago(days, now)),
    ).fetchall()
    by_day = {r["d"]: float(r["delta"] or 0.0) for r in rows}

    out: list[dict] = []
    balance = on_hand
    for i in range(days):
        d = today - _dt.timedelta(days=i)
        out.append({"date": d.isoformat(), "qty": round(balance, 3)})
        balance -= by_day.get(d.isoformat(), 0.0)   # step back over that day's net movement
    out.reverse()
    return out


# ===========================================================================
# Ingredient projection
# ===========================================================================
def _ingredient_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # available_base excludes whatever sits in an already-expired open batch
    # (migrations/0040_expired_stock_usability.sql's v_ingredient_expired_stock)
    # — the same usable-stock definition the POS availability view and the
    # reservation guard use (see reservation._try_hold). Without this, a
    # purchase recommendation could read a healthy days-of-supply off stock
    # that can no longer legally be sold, understating how soon a reorder is
    # actually needed.
    return conn.execute(
        """
        SELECT i.id, i.ingredient_id, i.name, i.category, i.base_unit, i.purchase_unit,
               i.pack_size, i.yield_factor, i.lead_time_days, i.reorder_point_base,
               i.minimum_order_qty,
               i.inventory_type, i.on_hand_base, i.reserved_base, i.supplier,
               (i.on_hand_base - i.reserved_base - COALESCE(x.expired_base, 0)) AS available_base,
               COALESCE(x.expired_base, 0) AS expired_base,
               i.unit_cost, i.unit_cost_status, i.pack_size_status,
               r.is_deductable
          FROM ingredient i
          JOIN v_ingredient_readiness r ON r.id = i.id
          LEFT JOIN v_ingredient_expired_stock x ON x.ingredient_id = i.id
         WHERE i.is_active = 1 AND r.is_deductable = 1
           AND NOT (i.kind = 'sub_recipe' AND i.sub_recipe_mode = 'exploded')
        """
    ).fetchall()


def _operational_rate(conn: sqlite3.Connection, lookback_days: int) -> dict[int, float]:
    """Per-day usage for supplies that never pass through the POS (gloves,
    cleaning alcohol, trash bags). These have no recipe and no demand curve --
    a flat rate off their own ledger reason is the only honest estimate, and
    it is spread over calendar days because staff use them on closed days too."""
    rows = conn.execute(
        "SELECT ingredient_id, -SUM(delta_base) AS used FROM stock_ledger "
        "WHERE reason = 'operational_use' AND created_at >= ? GROUP BY ingredient_id",
        (timeutil.utc_bound_n_manila_days_ago(lookback_days),),
    ).fetchall()
    return {r["ingredient_id"]: max(0.0, float(r["used"] or 0)) / lookback_days for r in rows}


def _incoming_ordered(conn: sqlite3.Connection) -> dict[int, list[tuple[float, str | None]]]:
    """Per-ingredient list of (outstanding base-unit qty, expected_delivery_date
    ISO string or None) for every confirmed ('ordered' or 'partially_received')
    purchase-order LINE -- one entry per PO, so multiple incoming orders for
    the same ingredient are each kept separate rather than collapsed into one
    number. A 'draft' PO is not yet a commitment to a supplier
    (wt/purchasing.py's own lifecycle: draft -> ordered -> ...), so it is
    deliberately excluded here: counting it as incoming would let an owner's
    own unsent draft mask a real shortfall on this same screen. A 'cancelled'
    or fully 'received' PO is excluded by the same status filter. Partial
    deliveries are already correctly reduced by `received_qty_base`.

    2026-10 fix: this used to return one aggregated total that the caller
    subtracted from the shortfall regardless of WHEN the order was due --
    an order arriving in 30 days could fully mask an urgent 3-day shortfall.
    Returning each line's own expected_delivery_date lets the caller (which
    already knows the ingredient's own lead_time_days) decide, per order,
    whether it actually arrives in time to count against that urgency --
    see _split_incoming_base() below. purchase_order_item.quantity is stored
    in the ingredient's purchase_unit (see purchasing.create()); converted to
    base the same way order_qty is converted back to purchase_unit above, via
    pack_size * yield_factor."""
    rows = conn.execute(
        "SELECT poi.ingredient_id, "
        "       (poi.quantity * COALESCE(i.pack_size, 1) * COALESCE(i.yield_factor, 1) "
        "           - poi.received_qty_base) AS line_base, "
        "       po.expected_delivery_date "
        "  FROM purchase_order_item poi "
        "  JOIN purchase_order po ON po.id = poi.purchase_order_id "
        "  JOIN ingredient i ON i.id = poi.ingredient_id "
        " WHERE po.status IN ('ordered','partially_received')",
    ).fetchall()
    out: dict[int, list[tuple[float, str | None]]] = {}
    for r in rows:
        qty = max(0.0, float(r["line_base"] or 0))
        if qty <= 0:
            continue
        out.setdefault(r["ingredient_id"], []).append((qty, r["expected_delivery_date"]))
    return out


def _split_incoming_base(lines: list[tuple[float, str | None]], today: _dt.date,
                         lead_days: float) -> tuple[float, float, float]:
    """Splits one ingredient's incoming PO lines into
    (in_time_base, late_base, overdue_base) using THIS ingredient's own
    lead_time_days as the cutoff date (today + lead_days): a line only
    offsets an urgent shortfall if it is due on or before that cutoff, so a
    delivery arriving after stock would already run out -- or one with no
    reliable date at all -- never quietly cancels out the warning. A line
    with no expected_delivery_date is treated as 'late' rather than assumed
    to arrive immediately, per the same reasoning. A line already overdue
    against its own promised date is reported separately (`overdue_base`,
    a subset of in_time_base) so a caller can flag the supplier risk without
    changing the netting itself -- it is still due "by the cutoff" (the
    cutoff is in the future; an overdue date is in the past), so it still
    counts toward in_time_base."""
    cutoff = today + _dt.timedelta(days=max(0.0, lead_days))
    in_time = 0.0
    late = 0.0
    overdue = 0.0
    for qty, due in lines:
        due_date = None
        if due:
            try:
                due_date = _dt.date.fromisoformat(str(due)[:10])
            except ValueError:
                due_date = None
        if due_date is not None and due_date <= cutoff:
            in_time += qty
            if due_date < today:
                overdue += qty
        else:
            late += qty
    return in_time, late, overdue


def _incoming_by_day(lines: list[tuple[float, str | None]], today: _dt.date,
                     n: int) -> list[float]:
    """2026-10-14 fix: the day-indexed counterpart to _split_incoming_base()
    above. That function answers 'does this ingredient's TOTAL incoming
    quantity arrive by a generic lead-time cutoff' -- useful for the
    purchase-recommendation explanation, but not precise enough to answer
    the real question a stockout warning needs answered: will stock run out
    on a SPECIFIC day before a SPECIFIC delivery lands on ITS day. A PO due
    in 4 days passes the old 'in time' test against a 5-day lead time, but
    that says nothing about whether consumption empties the shelf on day 2
    -- two days before that same PO arrives. This function instead places
    each line's quantity on the exact day index it is due, so the caller
    can add it to the day-by-day balance walk at the right point and let a
    real zero-crossing on an earlier day stand, regardless of what arrives
    later.

    Returned as a list of length `n` (index 0 = the rest of today, index i
    = i full days from today, matching ingredient_forecast()'s own `cons`/
    `raw`/`traj` arrays exactly) so a caller can do `bal += incoming[k]` in
    the same loop that does `bal -= cons[k]`. A line already overdue (due
    date in the past) is placed at index 0 -- it could arrive at any
    moment, and treating it as already-available-today is the same
    assumption _split_incoming_base() makes when it counts an overdue line
    toward in_time_base. A line with no reliable date, or due beyond this
    call's own horizon, contributes nothing to the walk (excluded, not
    assumed immediate) -- it still shows up in the aggregate late/
    unscheduled totals _split_incoming_base() reports for transparency, it
    just cannot be placed on a specific day that does not exist in this
    projection."""
    by_day = [0.0] * n
    for qty, due in lines:
        if not due:
            continue
        try:
            due_date = _dt.date.fromisoformat(str(due)[:10])
        except ValueError:
            continue
        offset = (due_date - today).days
        k = max(0, offset)
        if k < n:
            by_day[k] += qty
    return by_day


def _refill_lambda(conn: sqlite3.Connection, lookback_days: int,
                   now: _dt.datetime | None = None) -> dict[int, float]:
    """2026-10 fix (the remaining partial-demand-coverage audit): real
    per-head consumption rate for every ingredient ever deducted as an
    unlimited-wings REFILL flavour sauce (unli_refill_line, state=
    'committed' -- see wt/refills.py's own module docstring: "every
    unlimited round served... took sauce out of the walk-in that the
    inventory never knew about"). Reconstructed entirely from real
    operational records, never fabricated or blended with a flat ledger
    average:

        refill_lambda[ingredient] = SUM(real committed refill_line.qty_base)
                                   / SUM(real heads on unli orders)

    over the SAME bounded [now-lookback_days, now) window `heads` itself
    uses elsewhere in this module -- the identical "per real head, over a
    real window" quantity _unli_streams()'s registered lambda_value already
    represents for wings/rice/fries/drinks, just derived from refill-line
    records instead of a stored coefficient (no unli_item registration
    exists per flavour, so there is nothing to look up). Validated directly
    against real data before being wired in: for Honey and Garlic (Minced),
    refill_lambda * real average daily heads landed within 0.2-2.3% of the
    independently-measured real trailing ledger rate for those exact
    ingredients -- the gap this fixes was a genuinely missing, genuinely
    reconstructible demand component, not noise.

    Applied in ingredient_forecast() as an ADDITIVE term alongside recipe
    and base-stream demand (never a replacement, never a blend with a flat
    average) -- an ingredient that already has some real recipe-based
    demand (e.g. Honey also appears in an a-la-carte recipe) keeps that
    term fully intact and gains this one on top, so
    total = recipe/menu demand + unli-refill demand + (base-stream demand,
    where applicable) stays a legible sum of named, real sources, never an
    opaque override.

    now=None (every caller except replenishment_backtest()) reads up to the
    live wall clock, same as every other demand source in this module."""
    lower = timeutil.utc_bound_n_manila_days_ago(lookback_days, now)
    heads_upper_clause = " AND o.created_at < ?" if now is not None else ""
    refill_upper_clause = " AND l.created_at < ?" if now is not None else ""
    upper_params = (timeutil.utc_upper_bound_sql(now),) if now is not None else ()

    heads_row = conn.execute(
        f"""
        SELECT SUM(o.head_count * ol.qty) AS h
          FROM customer_order o
          JOIN order_line ol ON ol.order_id = o.id
          JOIN menu_item m ON m.id = ol.menu_item_id
         WHERE m.is_unli = 1 AND o.status != 'void' AND o.created_at >= ?
         {heads_upper_clause}
        """,
        (lower, *upper_params),
    ).fetchone()
    heads = float(heads_row["h"] or 0.0)
    if heads <= 0:
        return {}

    rows = conn.execute(
        f"""
        SELECT l.ingredient_id AS ing_id, SUM(l.qty_base) AS q
          FROM unli_refill_line l
         WHERE l.state = 'committed' AND l.created_at >= ?
         {refill_upper_clause}
         GROUP BY l.ingredient_id
        """,
        (lower, *upper_params),
    ).fetchall()
    return {r["ing_id"]: (r["q"] or 0.0) / heads for r in rows if (r["q"] or 0.0) > 0}


def _suggested_order_qty(shortfall_base: float, pack_size: float | None,
                         yield_factor: float | None,
                         minimum_order_qty: float | None) -> int | None:
    """Purchase-unit quantity to recommend ordering, or None if nothing is
    needed. `shortfall_base` is already net of available stock, safety
    stock, and any confirmed order arriving IN TIME (see
    _split_incoming_base()) -- this function only turns a positive base-unit
    gap into a whole number of purchase units. Returns None for an invalid
    supplier configuration (no pack_size) or when there is no shortfall, so
    a caller never recommends an order that doesn't make sense.

    minimum_order_qty is stored in the same purchase-unit count as the
    result (migration 0047) -- it is ceil'd the same way the pack-size
    quantity is, so the floor it applies is always a whole number of
    purchase units too, never a fractional pack. It can only ever RAISE an
    already-needed order (the shortfall>0 guard decides whether an order
    happens at all), never create one on its own."""
    if shortfall_base <= 0 or not pack_size:
        return None
    packs_needed = math.ceil(shortfall_base / (pack_size * (yield_factor or 1.0)))
    if minimum_order_qty:
        packs_needed = max(packs_needed, math.ceil(minimum_order_qty))
    return packs_needed


def _replenishment_message(name: str, status: str, days_of_supply: float | None,
                           deviation_ratio: float | None, deviation_flag: bool,
                           below_rop: bool, arrives_too_late: bool = False) -> str:
    """Plain-language depletion/days-of-supply warning for the floor, built
    only from figures ingredient_forecast() has already computed above --
    this never runs a second estimate of its own. Always calls the figure
    "estimated": real on-hand can differ from the ledger's number because of
    spills, over-portioning, spoilage and the like, and the wording should
    say so rather than imply a physical count.

    `deviation_flag`/`deviation_ratio` compare the forward forecast's steady
    rate against the same ingredient's own trailing-ledger average (see
    FORECAST_DEVIATION_RATIO above) -- when the forward rate is the higher
    of the two, that is today's usage running above the ingredient's usual
    rate, which is the specific thing worth naming in the warning rather
    than leaving "may run out" unexplained.
    """
    days_text = None
    if days_of_supply is not None:
        d = max(0, round(days_of_supply))
        days_text = f"approximately {d} more day{'' if d == 1 else 's'}"

    usage_up = bool(deviation_flag and deviation_ratio is not None and deviation_ratio > 1.0)

    if status == "out":
        return f"{name} is out of stock right now."

    if status in ("critical", "at_risk"):
        lead_in = (f"{name} may require earlier replenishment than scheduled."
                  if status == "critical" else
                  f"{name} may fall below a safe level before the next delivery.")
        usage_clause = " Current usage is higher than the usual rate." if usage_up else ""
        # below_rop can only ever raise a status to "at_risk" (see the caller),
        # never reach here on its own without the forecast tier agreeing --
        # named explicitly rather than left implicit in the generic wording.
        rop_clause = (f" Stock has also fallen below the level set as safe for this ingredient."
                     if below_rop else "")
        supply_clause = (f" Estimated stock may only be sufficient for {days_text}."
                         if days_text else
                         " Estimated stock may not be sufficient to reach the next delivery.")
        # A confirmed order already exists but, per its own expected delivery
        # date, will not arrive in time to prevent this -- named explicitly
        # so this doesn't read as "nothing has been done about it" when a PO
        # is in fact already placed, just not placed in time.
        late_clause = (" A purchase order is already on its way but is not expected to "
                       "arrive in time."
                       if arrives_too_late else "")
        return lead_in + usage_clause + rop_clause + supply_clause + late_clause

    if below_rop:
        return (f"{name}'s estimated stock has fallen below the level set as safe for it. "
               "Consider replenishing earlier than scheduled.")

    if status == "idle":
        return (f"No recent usage recorded for {name} — estimated depletion cannot be "
               "projected from current data.")

    if status == "excess":
        return f"{name}'s estimated stock is well above typical needs — no action needed."

    return (f"{name}'s estimated stock is sufficient to cover the period through the "
           "next scheduled delivery.")


def _cross_zero(trajectory: list[float], start: float) -> float | None:
    """Fractional day index at which the running balance first reaches zero,
    linearly interpolated inside the day it happens. Returns None if the line
    never crosses inside the horizon -- which is stated as such, never
    rounded up into an invented date."""
    prev = start
    for i, val in enumerate(trajectory):
        if val <= 0:
            # trajectory[i] is the balance at the END of offset-day i, so the
            # crossing sits between offset i-1 and i. Interpolate inside it.
            drop = prev - val
            frac = (prev / drop) if drop > 0 else 0.0
            return max(0.0, (i - 1) + _clamp(frac, 0.0, 1.0))
        prev = val
    return None


def _interp_at(trajectory: list[float], start: float, day: float) -> float:
    """Projected balance at a fractional day offset (day 0 = now)."""
    if day <= 0:
        return start
    lo = int(math.floor(day))
    hi = lo + 1
    def at(i: int) -> float:
        if i <= 0:
            return start
        if i - 1 < len(trajectory):
            return trajectory[i - 1]
        return trajectory[-1] if trajectory else start
    a, b = at(lo), at(hi)
    return a + (b - a) * (day - lo)


def ingredient_forecast(conn: sqlite3.Connection, horizon_days: int = HORIZON_DAYS,
                        lookback_days: int = LOOKBACK_DAYS,
                        now: _dt.datetime | None = None,
                        demand_model: str = "ewma",
                        historical_available: dict[int, float] | None = None) -> dict:
    """The whole chain, for every deductable ingredient.

    Returns one row per ingredient carrying: current stock, projected daily
    consumption, the full day-by-day trajectory, projected depletion date,
    days of supply, supplier lead time, projected stock at the lead-time
    boundary, status, and the demand assumptions behind all of it.

    demand_model: see build_demand()'s docstring. Callers normally pass the
    'depletion_demand_model' app_setting (api.py) rather than hardcoding
    this, so a deployment can switch between the EWMA and Prophet-driven
    baselines by changing one setting rather than a code deploy.

    historical_available: 2026-10 fix for replenishment_backtest() --
    {ingredient_id: on-hand AS OF `now`}, reconstructed by the caller (see
    _balances_at_cutoff(), the same backward-ledger-walk technique already
    used elsewhere in this module) and substituted in place of
    `available_base`'s live, CURRENT value for that ingredient.

    Every other caller leaves this None and gets the unchanged, correct-for-
    live-use behavior (available_base, read from the live ingredient row).
    This matters ONLY when `now` is itself in the past: _ingredient_rows()
    reads ingredient.on_hand_base/reserved_base directly, which are always
    today's real values -- there is no historical variant of those columns.
    Confirmed directly: calling this function with `now` set to 10, 30, and
    50 days ago for the same ingredient returned the IDENTICAL `available`
    and `onHand` every time, because nothing was reconstructing them to
    that date. A real backtest (replenishment_backtest()) anchors its
    demand curve correctly to `now` via build_demand(), but was then
    starting that correctly-historical demand curve from TODAY's stock
    level -- silently grading "if today's shelf had looked like this nine
    weeks ago" rather than how the shelf actually looked nine weeks ago.
    This parameter lets the demand curve and the starting stock level agree
    on which date they are both describing.

    Scope, stated plainly: this reconstructs on-hand only. It does not
    attempt to reconstruct a historical reserved_base (reservations are
    transient live state with no ledger trail once the order they held
    stock for is long since committed or released, and are realistically
    ~0 by the time an evaluation point is days or weeks old) or a
    historical expired-batch exclusion (fefo_batch.qty_base is itself a
    live, current-state column with no per-batch history of what it held
    on an earlier date -- there is no reliable source to reconstruct that
    from). Both are real, disclosed simplifications, not hidden ones.
    """
    demand = build_demand(conn, lookback_days, horizon_days, now, demand_model)
    today: _dt.date = demand["today"]
    leaves = _recipe_leaves(conn)
    streams = _unli_streams(conn)
    op_rate = _operational_rate(conn, lookback_days)
    ingredients = _ingredient_rows(conn)

    # Independent sanity check: the same ingredient's simple trailing-ledger
    # rate (a different calculation, over the same stock_ledger, that this
    # module's own docstring explicitly does NOT replace for this purpose --
    # see the module docstring's "What this does NOT do" note). Computed
    # once for every ingredient here rather than per-row, since
    # forecasting.ingredient_depletion() already scans every deductable
    # ingredient in one pass.
    trailing_rate = {r["ingredient_id"]: r["avg_daily_consumption"]
                     for r in forecasting.ingredient_depletion(conn, lookback_days=lookback_days, now=now)}
    incoming_ordered = _incoming_ordered(conn) if historical_available is None else {}

    n = horizon_days + 1        # index 0 = rest of today, then horizon_days full days

    # --- ingredient x day consumption, and who caused it ---------------------
    consumption: dict[int, list[float]] = {ing["id"]: [0.0] * n for ing in ingredients}
    variance: dict[int, list[float]] = {ing["id"]: [0.0] * n for ing in ingredients}
    contributors: dict[int, dict[int, float]] = {ing["id"]: {} for ing in ingredients}
    # Which ingredients had at least one contributing menu item forecast by
    # Prophet rather than the EWMA fallback -- surfaced per ingredient below
    # (demandSource) since one ingredient can be fed by several menu items
    # that don't all necessarily have a fresh Prophet fit at the same time.
    prophet_driven: dict[int, bool] = {ing["id"]: False for ing in ingredients}
    known = set(consumption)

    for pk, s in demand["items"].items():
        meta = demand["item_meta"][pk]
        if meta["recipe_id"] is None:
            continue
        for ing_id, qty_base in leaves.get(meta["recipe_id"], ()):
            if ing_id not in known:
                continue
            row = consumption[ing_id]
            vrow = variance[ing_id]
            for k in range(n):
                row[k] += s.daily[k] * qty_base
                # Variances add for independent series; sigma is per-unit-day
                # demand spread scaled by the recipe quantity.
                vrow[k] += (s.sigma_daily[k] * qty_base) ** 2
            contributors[ing_id][pk] = contributors[ing_id].get(pk, 0.0) + s.daily[1] * qty_base
            if s.demand_source == "prophet":
                prophet_driven[ing_id] = True

    heads = demand["heads"]
    for st in streams:
        ing_id = st["ingredient_id"]
        if ing_id not in known:
            continue
        lam = float(st["lambda_value"] or 0.0)
        row, vrow = consumption[ing_id], variance[ing_id]
        for k in range(n):
            row[k] += heads.daily[k] * lam
            vrow[k] += (heads.sigma_daily[k] * lam) ** 2

    # 2026-10 fix (see _refill_lambda()'s own docstring): a real, named,
    # ADDITIVE demand component for ingredients deducted as unli-refill
    # flavour sauces -- added on top of whatever recipe/stream demand an
    # ingredient already has, never replacing it. refill_daily_base[ing_id]
    # (k=1, i.e. tomorrow's full day) is kept so the response can show this
    # component's own size rather than only the combined total.
    refill_lambda = _refill_lambda(conn, lookback_days, now)
    refill_daily_base: dict[int, float] = {}
    for ing_id, lam in refill_lambda.items():
        if ing_id not in known:
            continue
        row, vrow = consumption[ing_id], variance[ing_id]
        for k in range(n):
            row[k] += heads.daily[k] * lam
            vrow[k] += (heads.sigma_daily[k] * lam) ** 2
        refill_daily_base[ing_id] = heads.daily[1] * lam

    for ing_id, rate in op_rate.items():
        if ing_id in known and not any(consumption[ing_id]):
            consumption[ing_id] = [rate * (1.0 - demand["share_by_now"])] + [rate] * horizon_days

    # 2026-10 fix: an ingredient can have real, measurable consumption in
    # the ledger that neither of the two structural sources above ever
    # sees -- confirmed directly against this restaurant's real data for
    # per-flavor unlimited-wings refill sauces (e.g. Corn Syrup, Garlic):
    # their consumption posts as genuine sale_commit rows (unli refills),
    # so _operational_rate() above correctly skips them (they are not
    # reason='operational_use'), but they are also not tied to any
    # POS-visible menu item's recipe (leaves, above) or any of the 4
    # registered unli_item streams (streams, above) -- a specific flavor's
    # sauce usage is neither. The result, before this fix: consumption stays
    # all-zero, the ingredient is classified "idle" (steady<=0), and it can
    # genuinely run out with zero warning (confirmed: Corn Syrup did,
    # twice, in the real replenishment backtest). trailing_rate (computed
    # above from forecasting.ingredient_depletion(), which reads
    # reason='sale_commit' for any non-operational-supply ingredient
    # regardless of which menu item or refill caused it) already measures
    # this correctly -- it was only ever used as a sanity-check comparison
    # before. Used here as a last-resort fallback, and ONLY when the
    # structural sources found literally nothing: an ingredient with any
    # real recipe/stream-driven signal keeps that signal unchanged.
    for ing_id in known:
        if any(consumption[ing_id]):
            continue
        rate = trailing_rate.get(ing_id) or 0.0
        if rate > 0:
            consumption[ing_id] = [rate * (1.0 - demand["share_by_now"])] + [rate] * horizon_days

    # --- trajectory + status --------------------------------------------------
    out = []
    for ing in ingredients:
        ing_id = ing["id"]
        available = float(ing["available_base"] or 0.0)
        if historical_available is not None and ing_id in historical_available:
            available = historical_available[ing_id]
        cons = consumption[ing_id]
        sig = [math.sqrt(v) for v in variance[ing_id]]
        lead = ing["lead_time_days"] or 0
        rop = ing["reorder_point_base"]

        # 2026-10-14 fix: _incoming_ordered() only ever reads the CURRENT
        # purchase_order table, so it cannot be backdated -- excluded
        # during replenishment_backtest() (historical_available is set),
        # same reasoning as ingredient_forecast()'s own docstring for why
        # that parameter reconstructs on-hand only, not every live table.
        incoming_lines = incoming_ordered.get(ing_id, []) if historical_available is None else []
        incoming_total_base = sum(q for q, _ in incoming_lines)
        # Aggregate in-time/late/overdue totals, kept for the purchase-
        # recommendation explanation and the "arrives too late" message --
        # see _split_incoming_base()'s own docstring for why this generic
        # lead-time cutoff is NOT precise enough to drive the trajectory or
        # the shortfall below on its own.
        incoming_in_time_base, incoming_late_base, incoming_overdue_base = (
            _split_incoming_base(incoming_lines, today, lead)
        )
        # The precise, day-indexed version: places each line on the exact
        # day it is actually due so the balance walk below can show a real
        # stockout on an earlier day standing even when a later delivery
        # would eventually have covered the aggregate shortfall -- see
        # _incoming_by_day()'s own docstring for the gap this closes (a PO
        # 'within lead time' by the generic cutoff can still arrive after
        # consumption would already have emptied the shelf).
        incoming_by_day = (
            _incoming_by_day(incoming_lines, today, n) if historical_available is None
            else [0.0] * n
        )

        # Two runs of the same arithmetic. `raw` is allowed to go negative --
        # that is where the zero crossing and the size of any shortfall come
        # from. `traj` is the physical stock line, floored at zero, because a
        # storeroom cannot hold minus two kilos of fries: once it is empty the
        # remaining demand goes unserved, it does not keep draining a
        # non-existent balance. Plotting the raw line instead would put 90% of
        # the chart below the axis and hide the part anyone cares about.
        #
        # A confirmed delivery is added on its OWN day (`bal += incoming_
        # by_day[k]`), in the same walk, chronologically alongside every
        # other day's consumption -- never as a single lump-sum adjustment
        # at the end. This is what makes an earlier stockout (a day whose
        # running balance crosses zero before a later delivery's own day)
        # show up as a real zero-crossing in `raw` regardless of how much
        # is confirmed to arrive afterward, and what makes multiple
        # deliveries compose correctly: each lands exactly once, on its own
        # day, in calendar order.
        raw, traj, band_lo, band_hi = [], [], [], []
        bal = available
        cum_sig2 = 0.0
        for k in range(n):
            bal -= cons[k]
            bal += incoming_by_day[k]
            raw.append(bal)
            traj.append(max(0.0, bal))
            cum_sig2 += sig[k] ** 2
            spread = math.sqrt(cum_sig2)
            band_lo.append(max(0.0, bal - spread))
            band_hi.append(max(0.0, bal + spread))

        steady = sum(cons[1:]) / horizon_days if horizon_days else 0.0
        has_band = any(s > 0 for s in sig)

        # Sanity check against the independent trailing-ledger rate — see
        # FORECAST_DEVIATION_RATIO. Only flagged when BOTH figures are
        # positive and meaningfully different; a brand-new item with no
        # trailing history (trailing_rate == 0) is not itself suspicious,
        # since the forward model can legitimately see demand the trailing
        # average has not accumulated enough days to reflect yet.
        trailing = trailing_rate.get(ing_id) or 0.0
        deviation_ratio = (steady / trailing) if trailing > 0 else None
        forecast_deviation_flag = (
            deviation_ratio is not None
            and (deviation_ratio >= FORECAST_DEVIATION_RATIO
                or deviation_ratio <= 1.0 / FORECAST_DEVIATION_RATIO)
        )

        depletion_idx = _cross_zero(raw, available) if steady > 0 or cons[0] > 0 else None
        raw_at_lead = _interp_at(raw, available, lead)

        if available <= 0:
            status = "out"
        elif depletion_idx is None and steady <= 0:
            status = "idle"
        elif depletion_idx is not None and depletion_idx <= lead:
            status = "critical"
        elif (depletion_idx is not None and depletion_idx <= lead + REVIEW_CYCLE_DAYS) or (
                rop is not None and raw_at_lead < rop):
            status = "at_risk"
        elif steady > 0 and available / steady > EXCESS_DAYS_OF_SUPPLY:
            status = "excess"
        else:
            status = "healthy"

        # A reorder point the owner actually configured is a floor set from
        # knowledge the ledger does not have. It can raise the tier, never
        # lower it, and is never inferred for an ingredient that has none.
        below_rop = rop is not None and available < rop
        if below_rop and STATUS_ORDER[status] > STATUS_ORDER["at_risk"]:
            status = "at_risk"

        depletion_date = (
            (today + _dt.timedelta(days=depletion_idx)).isoformat()
            if depletion_idx is not None else None
        )
        # Safety stock, made explicit rather than left as an unlabeled
        # constant folded into the shortfall arithmetic: REVIEW_CYCLE_DAYS
        # of forecast demand held as a buffer beyond the supplier lead time,
        # covering the gap between "stock could physically last exactly
        # until the delivery lands" and "there is slack if the delivery, the
        # demand estimate, or the lead time itself is off by a bit". Surfaced
        # as its own base-unit figure (safetyStockBase) so a caller can show
        # it by name in the purchase-recommendation explanation instead of
        # it being invisible inside the shortfall total.
        safety_stock_base = steady * REVIEW_CYCLE_DAYS
        # 2026-10-14 fix: the purchase recommendation now reads the SAME
        # delivery-aware, real-daily-consumption trajectory the status tier
        # above was just computed from, rather than a separate flat
        # `steady x lead` approximation that could disagree with it (e.g.
        # ignore a nowcast-adjusted day 0, or net out a PO that arrives
        # after an earlier day's stockout already stands). Evaluated at the
        # same lead+safety-cycle horizon REVIEW_CYCLE_DAYS already uses
        # elsewhere, so "how much do we need to order" and "is this
        # critical/at_risk" are answers to the same underlying question,
        # never two independently-drifting calculations.
        eval_day = lead + REVIEW_CYCLE_DAYS
        projected_at_eval = _interp_at(raw, available, eval_day)
        shortfall = -projected_at_eval if projected_at_eval < 0 else 0.0
        order_qty = _suggested_order_qty(
            shortfall, ing["pack_size"], ing["yield_factor"], ing["minimum_order_qty"],
        )

        top = sorted(contributors[ing_id].items(), key=lambda kv: -kv[1])[:5]

        # 2026-10-14 fix: "arrives too late" now means exactly what the
        # trajectory just proved -- status is still critical/at_risk despite
        # SOME confirmed quantity being on order. incoming_late_base alone
        # (the generic lead-time-cutoff split) used to under-fire this: a PO
        # due in 4 days against a 5-day lead time counted as "in time" by
        # that cutoff even when the real day-by-day trajectory shows stock
        # emptying on day 1, three days before that same delivery lands.
        arrives_too_late = (
            status in ("critical", "at_risk") and incoming_total_base > 0
        )
        replenishment_message = _replenishment_message(
            ing["name"], status, depletion_idx, deviation_ratio,
            forecast_deviation_flag, below_rop, arrives_too_late=arrives_too_late)

        out.append({
            "ingredientId": ing_id,
            "ingredientCode": ing["ingredient_id"],
            "name": ing["name"],
            "category": ing["category"],
            "inventoryType": ing["inventory_type"],
            "baseUnit": ing["base_unit"],
            "purchaseUnit": ing["purchase_unit"],
            "packSize": ing["pack_size"],
            "yieldFactor": ing["yield_factor"],
            "minimumOrderQty": ing["minimum_order_qty"],
            "supplier": ing["supplier"],
            # Already placed with a supplier and not yet (fully) received --
            # see _incoming_ordered()'s own docstring for what counts. This
            # total, and the in-time/late/overdue split below, are reported
            # for the purchase-recommendation explanation; the actual
            # shortfall/order-qty math above reads the day-indexed
            # incoming_by_day placed directly into the trajectory (`raw`),
            # not these aggregates -- see _incoming_by_day()'s docstring for
            # why a generic lead-time cutoff isn't precise enough on its own.
            "incomingOrderedBase": round(incoming_total_base, 3),
            # The date-aware split (2026-10 fix): how much of the total
            # above actually offsets this ingredient's urgency (due by
            # today + lead_time_days) versus arrives too late/with no
            # reliable date to count on, surfaced separately so a caller
            # never has to guess which POs the recommendation already
            # accounted for.
            "incomingInTimeBase": round(incoming_in_time_base, 3),
            "incomingLateBase": round(incoming_late_base, 3),
            "incomingOverdueBase": round(incoming_overdue_base, 3),
            "onHand": round(float(ing["on_hand_base"] or 0), 3),
            "reserved": round(float(ing["reserved_base"] or 0), 3),
            "available": round(available, 3),
            "restOfTodayConsumption": round(cons[0], 3),
            "forecastDailyConsumption": round(steady, 4),
            # The unli-refill component's own share of forecastDailyConsumption
            # above (already included in it, not on top of it) -- see
            # _refill_lambda()'s docstring. None for an ingredient with no
            # real refill-line history at all, as opposed to 0.0 for one
            # that has some but none projected for tomorrow specifically.
            "refillSauceDailyConsumption": (
                round(refill_daily_base[ing_id], 4) if ing_id in refill_daily_base else None),
            "trailingLedgerDailyRate": round(trailing, 4) if trailing > 0 else None,
            "forecastDeviationRatio": round(deviation_ratio, 2) if deviation_ratio is not None else None,
            "forecastDeviationFlag": forecast_deviation_flag,
            # 'prophet' iff at least one contributing menu item's future-day
            # figures came from a fresh cached Prophet fit; 'ewma' otherwise
            # (including the always-EWMA unlimited-cover contribution, and
            # demand_model='ewma' requests). See each item's own
            # demand.demandSource in ingredient_detail()'s drivers for the
            # per-item breakdown when an ingredient mixes both.
            "demandSource": "prophet" if prophet_driven[ing_id] else "ewma",
            "daysOfSupply": round(depletion_idx, 2) if depletion_idx is not None else None,
            "depletionDate": depletion_date,
            "depletesWithinHorizon": depletion_idx is not None,
            "leadTimeDays": lead,
            "safetyStockDays": REVIEW_CYCLE_DAYS,
            "safetyStockBase": round(safety_stock_base, 3),
            # Physical stock at the delivery boundary (never below zero), and
            # separately how much demand would go unserved by then. One is a
            # stock level, the other is a shortfall; conflating them into a
            # single negative "stock" figure is what made this unreadable.
            "projectedAtLeadTime": round(max(0.0, raw_at_lead), 3),
            "shortfallAtLeadTime": round(max(0.0, -raw_at_lead), 3),
            "unmetDemandByHorizon": round(max(0.0, -raw[-1]) if raw else 0.0, 3),
            "reorderPoint": rop,
            "belowReorderPoint": below_rop,
            # Advice only, offered when the owner has not set one -- the
            # same avg-daily-rate x lead-time starting point
            # wt/forecasting.py's ingredient_depletion() derives, just off
            # the forward forecast rate instead of the trailing ledger rate.
            # Nothing alerts on it; see forecasting.py's note above
            # ROP_BREACH_MIN_RISK for why a derived ROP is never itself a
            # second alert.
            "suggestedReorderPoint": round(steady * lead, 3) if steady > 0 else None,
            "suggestedOrderQty": order_qty,
            "suggestedOrderUnit": ing["purchase_unit"] if order_qty is not None else None,
            "status": status,
            # Plain-language depletion warning for the floor/owner UI -- see
            # _replenishment_message()'s own docstring. Derived entirely
            # from the fields above it in this same dict; never a second
            # calculation.
            "replenishmentMessage": replenishment_message,
            "trajectory": [round(v, 3) for v in traj],
            "band": ({"low": [round(v, 3) for v in band_lo],
                      "high": [round(v, 3) for v in band_hi]} if has_band else None),
            "topDrivers": [
                {"menuItemId": pk, "name": demand["item_meta"][pk]["name"], "qtyBase": round(q, 3)}
                for pk, q in top if q > 0
            ],
        })

    out.sort(key=lambda r: (STATUS_ORDER[r["status"]],
                            r["daysOfSupply"] if r["daysOfSupply"] is not None else 1e9))

    buckets: dict[str, int] = {}
    for r in out:
        buckets[r["status"]] = buckets.get(r["status"], 0) + 1

    return {
        "asOf": timeutil.utc_now().isoformat(timespec="seconds"),
        "businessDate": today.isoformat(),
        "manilaHour": demand["manila_hour"],
        "dayElapsedShare": round(demand["share_by_now"], 4),
        "horizonDays": horizon_days,
        "lookbackDays": lookback_days,
        "tradingDaysInWindow": len(demand["trading_days"]),
        # Totals for callers that just need the count, not the list --
        # dashboard_summary's "below reorder point" KPI and Replenishment's
        # headline both read these instead of re-deriving them by filtering
        # `ingredients` themselves, so a dashboard/replenishment mismatch
        # cannot come from two different filters counting the same rows
        # differently.
        "belowReorderPoint": sum(1 for r in out if r["belowReorderPoint"]),
        "reorderPointsConfigured": sum(1 for r in out if r["reorderPoint"] is not None),
        "demandModel": demand_model,
        "prophetDrivenIngredients": sum(1 for r in out if r["demandSource"] == "prophet"),
        "dates": [(today + _dt.timedelta(days=k)).isoformat() for k in range(n)],
        "ingredients": out,
        "buckets": buckets,
        "categories": sorted({r["category"] for r in out if r["category"]}),
        "seasonality": {
            "weeklyPattern": any(s.seasonality for s in demand["items"].values()),
            "annualPattern": False,
            "note": ("Weekly (day-of-week) patterns are supported by this history and are used. "
                     "Annual or holiday seasonality is NOT modelled — this dataset spans "
                     f"{len(demand['trading_days'])} trading days, which contains no repeat of "
                     "any calendar month."),
        },
    }


# ===========================================================================
# Per-ingredient detail: trajectory, evidence, and what is driving it
# ===========================================================================
def _consumption_by_item(conn: sqlite3.Connection, ingredient_id: int,
                         since_utc: str, until_utc: str | None = None) -> list[dict]:
    """Actual base-unit consumption of one ingredient, attributed to the menu
    items that caused it, over a window. Straight from order_line x
    v_recipe_leaf -- measured, not modelled."""
    clause = "AND o.created_at < ?" if until_utc else ""
    params = [ingredient_id, since_utc] + ([until_utc] if until_utc else [])
    rows = conn.execute(
        f"""
        SELECT m.id AS pk, m.name, SUM(ol.qty) AS units, SUM(ol.qty * l.qty_base) AS qty_base
          FROM order_line ol
          JOIN customer_order o ON o.id = ol.order_id
          JOIN menu_item     m ON m.id = ol.menu_item_id
          JOIN v_recipe_leaf l ON l.recipe_id = m.recipe_id
         WHERE l.ingredient_id = ? AND o.status != 'void' AND o.created_at >= ? {clause}
         GROUP BY m.id
         ORDER BY qty_base DESC
        """,
        params,
    ).fetchall()
    return [{"menuItemId": r["pk"], "name": r["name"], "units": float(r["units"]),
             "qtyBase": round(float(r["qty_base"]), 3)} for r in rows]


def ingredient_detail(conn: sqlite3.Connection, ingredient_id: int,
                      horizon_days: int = HORIZON_DAYS, lookback_days: int = LOOKBACK_DAYS,
                      history_days: int = 30, now: _dt.datetime | None = None,
                      demand_model: str = "ewma") -> dict:
    """One ingredient's full picture: reconstructed stock history, forward
    trajectory, the menu items driving it, and the specific observations that
    moved the forecast. Every value below traces to a row in this database."""
    full = ingredient_forecast(conn, horizon_days, lookback_days, now, demand_model)
    row = next((r for r in full["ingredients"] if r["ingredientId"] == ingredient_id), None)
    if row is None:
        from .errors import NotFoundError
        raise NotFoundError(f"ingredient {ingredient_id} is not forecastable "
                            f"(inactive, not deductable, or an exploded sub-recipe)")

    today = _dt.date.fromisoformat(full["businessDate"])
    history = _stock_history(conn, ingredient_id, row["onHand"], history_days, today, now)

    today_start = timeutil.utc_bound_n_manila_days_ago(1, now)
    week_start = timeutil.utc_bound_n_manila_days_ago(7, now)
    prev_week_start = timeutil.utc_bound_n_manila_days_ago(14, now)

    today_by_item = _consumption_by_item(conn, ingredient_id, today_start)
    week_by_item = _consumption_by_item(conn, ingredient_id, week_start)
    prev_week = _consumption_by_item(conn, ingredient_id, prev_week_start, week_start)

    week_total = sum(x["qtyBase"] for x in week_by_item)
    prev_total = sum(x["qtyBase"] for x in prev_week)

    # Per TRADING day, not per calendar day. Comparing raw 7-day totals across
    # a window where the shop traded once against one where it traded seven
    # times reports a 93% collapse in demand that never happened -- it reports
    # a gap in the data. Both counts are surfaced so the reader can see which
    # of the two they are looking at.
    trading = set(_trading_days(conn, lookback_days, now))
    today_d = _dt.date.fromisoformat(full["businessDate"])
    week_days = sum(1 for i in range(7)
                    if (today_d - _dt.timedelta(days=i)).isoformat() in trading)
    prev_days = sum(1 for i in range(7, 14)
                    if (today_d - _dt.timedelta(days=i)).isoformat() in trading)
    week_rate = (week_total / week_days) if week_days else None
    prev_rate = (prev_total / prev_days) if prev_days else None
    trend_pct = (((week_rate - prev_rate) / prev_rate) * 100.0
                 if week_rate is not None and prev_rate else None)

    # Weekday comparison for the strongest driver: is today actually busy?
    demand = build_demand(conn, lookback_days, horizon_days, now, demand_model)
    evidence = []
    if today_by_item:
        top = today_by_item[0]
        s = demand["items"].get(top["menuItemId"])
        evidence.append({
            "kind": "today_units",
            "text": f"{top['units']:g} × {top['name']} sold so far today "
                    f"— {top['qtyBase']:g} {row['baseUnit']} of {row['name']}",
        })
        if s is not None and s.baseline > 0:
            normal = s.baseline * s.factors.get(today.weekday(), 1.0)
            if normal > 0:
                delta = (s.today_actual - normal * full["dayElapsedShare"]) / (
                    normal * full["dayElapsedShare"]) * 100.0 if full["dayElapsedShare"] > 0 else None
                if delta is not None:
                    word = "above" if delta >= 0 else "below"
                    wd = today.strftime("%A")
                    evidence.append({
                        "kind": "pace",
                        "text": f"{abs(delta):.0f}% {word} a normal {wd} at this hour "
                                f"({full['manilaHour']:02d}:00, {full['dayElapsedShare'] * 100:.0f}% "
                                f"of a typical day's units usually done by now)",
                    })
    if week_total > 0:
        evidence.append({
            "kind": "week_total",
            "text": f"{week_total:g} {row['baseUnit']} consumed over the last 7 days across "
                    f"{len(week_by_item)} menu item(s), on {week_days} trading "
                    f"day{'' if week_days == 1 else 's'}",
        })
    if trend_pct is not None:
        direction = "increasing" if trend_pct > 0 else "decreasing"
        evidence.append({
            "kind": "trend",
            "text": f"Per-trading-day consumption is {direction}: {abs(trend_pct):.0f}% "
                    f"({prev_rate:.0f} → {week_rate:.0f} {row['baseUnit']}/trading day, "
                    f"{prev_days} vs {week_days} trading days)",
        })
    if not evidence:
        evidence.append({
            "kind": "no_movement",
            "text": f"No recorded consumption of {row['name']} in the last 7 days — "
                    f"the projection below rests on the {full['lookbackDays']}-day window only.",
        })

    drivers = []
    for pk, s in demand["items"].items():
        meta = demand["item_meta"][pk]
        if meta["recipe_id"] is None:
            continue
        for ing_id, qty_base in _recipe_leaves(conn).get(meta["recipe_id"], ()):
            if ing_id == ingredient_id:
                drivers.append({
                    "menuItemId": pk,
                    "name": meta["name"],
                    "category": meta["category"],
                    "qtyBasePerServing": round(qty_base, 4),
                    "forecastUnitsPerDay": round(s.daily[1], 3),
                    "forecastQtyBasePerDay": round(s.daily[1] * qty_base, 3),
                    "demand": s.as_dict(),
                })
    drivers.sort(key=lambda d: -d["forecastQtyBasePerDay"])

    unli = [st for st in _unli_streams(conn) if st["ingredient_id"] == ingredient_id]
    if unli:
        lam = float(unli[0]["lambda_value"] or 0)
        drivers.insert(0, {
            "menuItemId": None,
            "name": f"{unli[0]['label']} (per cover)",
            "category": "Unlimited",
            "qtyBasePerServing": round(lam, 4),
            "forecastUnitsPerDay": round(demand["heads"].daily[1], 3),
            "forecastQtyBasePerDay": round(demand["heads"].daily[1] * lam, 3),
            "demand": demand["heads"].as_dict(),
            "coefficientStatus": unli[0]["status"],
        })

    return {
        **row,
        "asOf": full["asOf"],
        "businessDate": full["businessDate"],
        "horizonDays": horizon_days,
        "dates": full["dates"],
        "history": history,
        "evidence": evidence,
        "drivers": drivers,
        "consumedToday": today_by_item,
        "consumedLast7Days": week_by_item,
        "consumptionTrendPct": round(trend_pct, 1) if trend_pct is not None else None,
        "tradingDaysLast7": week_days,
        "tradingDaysPrev7": prev_days,
        "seasonality": full["seasonality"],
    }


# ===========================================================================
# Replenishment backtest — did this logic's own past recommendations
# actually hold up against what the real ledger later shows happened?
# ===========================================================================
# A fixed check window, not each ingredient's own lead_time_days: the
# backtest asks one consistent question ("would a stockout have happened in
# the week after this evaluation point") across every ingredient, so results
# are comparable to each other. Each ingredient's own lead time is still
# what the live system uses for its actual status/reorder math — this is a
# separate, uniform yardstick for evaluating that system's track record.
BACKTEST_CHECK_DAYS_AHEAD = 7
BACKTEST_STEP_DAYS = 10
BACKTEST_MIN_LOOKBACK_DAYS = 30
# A check window only tests a stockout warning if the shop was actually
# trading during it. If fewer than this share of a normal week's orders were
# recorded inside the window, no stock outcome could have been driven by
# demand, so the point is reported as "unobservable" instead of being graded.
# Not tuned: on the real history every observable point sits near 1.0 and the
# one data-gap window (no orders recorded for six days) sits at ~0.02.
BACKTEST_MIN_WINDOW_COVERAGE = 0.25
BACKTEST_COVERAGE_BASELINE_DAYS = 56


def _balances_at_cutoff(conn: sqlite3.Connection, cutoff_iso: str) -> dict[int, float]:
    """ingredient_id -> real on-hand balance AS OF cutoff_iso, reconstructed
    by walking back from the CURRENT on_hand_base: balance(cutoff) =
    current - SUM(deltas with created_at >= cutoff). Same order-independent
    technique _stock_history() uses for one ingredient at a time, applied to
    every ingredient in one pass since the backtest needs all of them at
    several cutoffs."""
    current = {r["id"]: float(r["on_hand_base"] or 0.0)
              for r in conn.execute("SELECT id, on_hand_base FROM ingredient")}
    after = {r["ingredient_id"]: float(r["d"] or 0.0) for r in conn.execute(
        "SELECT ingredient_id, SUM(delta_base) AS d FROM stock_ledger "
        f"WHERE created_at >= ? AND {_REAL_LEDGER_USER_CLAUSE} GROUP BY ingredient_id", (cutoff_iso,),
    ).fetchall()}
    return {iid: current[iid] - after.get(iid, 0.0) for iid in current}


def _received_within_window(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> set[int]:
    """ingredient_ids that had at least one real purchase_receipt ledger row
    land in [start_iso, end_iso) -- used by replenishment_backtest() to tell
    apart two different things the raw confusion matrix's 'false_alarm'
    bucket otherwise conflates: a flag that fired on an ingredient that was
    never really going to run out, vs. a flag that correctly caught a real
    shortage and a delivery (the same lead-time window the flag itself is
    keyed to) arrived in time to prevent it. The backtest's own docstring
    already discloses that it reads real purchasing as given rather than
    simulating a response to its own suggestion -- this makes that
    confound visible in the numbers instead of leaving it implicit."""
    rows = conn.execute(
        "SELECT DISTINCT ingredient_id FROM stock_ledger "
        "WHERE reason = 'purchase_receipt' AND created_at >= ? AND created_at < ?",
        (start_iso, end_iso),
    ).fetchall()
    return {r["ingredient_id"] for r in rows}


def _orders_between(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM customer_order WHERE status != 'void' "
        "AND created_at >= ? AND created_at < ?", (start_iso, end_iso)).fetchone()[0])


def _min_balances_in_window(conn: sqlite3.Connection, start_iso: str, end_iso: str,
                            start_balance: dict[int, float]) -> dict[int, float]:
    """ingredient_id -> the lowest running ledger balance reached anywhere in
    [start_iso, end_iso), walking the real stock_ledger rows in order from
    `start_balance`. replenishment_backtest()'s docstring has always defined
    a stockout as stock hitting zero *within* the check window, but its
    primary label only looks at the balance at the window's END -- a stock
    that ran out on day 3 and was restocked on day 5 reads as 'never hit
    zero'. This is the within-window version of the same question."""
    low = dict(start_balance)
    run = dict(start_balance)
    for r in conn.execute(
        "SELECT ingredient_id, delta_base FROM stock_ledger "
        f"WHERE created_at >= ? AND created_at < ? AND {_REAL_LEDGER_USER_CLAUSE} "
        "ORDER BY created_at, id",
        (start_iso, end_iso),
    ):
        i = r["ingredient_id"]
        run[i] = run.get(i, 0.0) + float(r["delta_base"] or 0.0)
        if run[i] < low.get(i, 0.0):
            low[i] = run[i]
    return low


def _confusion_metrics(c: dict) -> dict:
    tp, fp, fn = c["caught"], c["false_alarm"], c["missed"]
    p = tp / (tp + fp) if (tp + fp) else None
    r = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * p * r / (p + r)) if p is not None and r is not None and (p + r) > 0 else None
    rnd = lambda v: round(v, 4) if v is not None else None
    return {"confusion": dict(c), "precision": rnd(p), "recall": rnd(r), "f1": rnd(f1)}


def replenishment_backtest(conn: sqlite3.Connection, lookback_days: int = 60,
                           demand_model: str = "ewma") -> dict:
    """Historical simulation: at each of several past evaluation points,
    recompute ingredient_forecast() using ONLY the data that existed as of
    that point (via its own `now` parameter — the same demand model, same
    code path a live request uses), then check what the real ledger shows
    actually happened in the BACKTEST_CHECK_DAYS_AHEAD days that followed.

    Reports, per evaluation point and in aggregate, a 2x2 classification of
    the 'critical'/'out' status against real outcomes:
      caught      = flagged critical/out, and stock actually did hit zero
                    within the check window
      false_alarm = flagged critical/out, but stock never hit zero
      missed      = NOT flagged critical/out, but stock hit zero anyway
      quiet       = NOT flagged, and stock never hit zero (the common case
                    on a well-supplied ingredient)
    plus a service level (share of evaluation points with no real stockout)
    and, for points classified 'healthy'/'excess', the realized days-of-
    supply the ingredient actually carried (for spotting overstock).

    This does NOT simulate a human actually placing the suggested purchase
    order — the real ledger already reflects whatever purchasing actually
    happened historically, which this reads as given. What it evaluates is
    narrower and honest about it: was the EARLY-WARNING SIGNAL (the
    critical/out status) actually predictive of a real stockout on this
    data, not "would following the suggested quantity have been optimal".
    """
    today = timeutil.business_today()
    if lookback_days < BACKTEST_MIN_LOOKBACK_DAYS + BACKTEST_CHECK_DAYS_AHEAD:
        return {"available": False,
               "reason": (f"need at least {BACKTEST_MIN_LOOKBACK_DAYS + BACKTEST_CHECK_DAYS_AHEAD} "
                          f"days of ledger history for a backtest (got lookback_days={lookback_days})")}

    eval_offsets = list(range(lookback_days, BACKTEST_CHECK_DAYS_AHEAD, -BACKTEST_STEP_DAYS))
    if not eval_offsets:
        return {"available": False, "reason": "lookback_days too small for even one evaluation point"}

    counts = {"caught": 0, "false_alarm": 0, "missed": 0, "quiet": 0}
    # Split of false_alarm only -- see _received_within_window()'s docstring.
    # false_alarm already includes both; this does not change `counts` or
    # the existing precision/recall/f1 fields below, it adds a second,
    # narrower precision alongside them.
    false_alarm_split = {"resolved_by_restock": 0, "genuine": 0}
    excess_days_of_supply: list[float] = []
    points = []
    # Supplementary views, all computed from the same forecasts and the same
    # real ledger; none of them replaces `counts` above.
    #   observable       : `counts` restricted to points whose check window
    #                      actually had trading (BACKTEST_MIN_WINDOW_COVERAGE)
    #   within_window    : stockout label = balance <= 0 ANYWHERE in the
    #                      window, not only at its end
    #   any_warning      : positive = critical/out/at_risk, same end-of-window label
    observable_counts = {"caught": 0, "false_alarm": 0, "missed": 0, "quiet": 0}
    observable_split = {"resolved_by_restock": 0, "genuine": 0}
    within_counts = {"caught": 0, "false_alarm": 0, "missed": 0, "quiet": 0}
    any_warning_counts = {"caught": 0, "false_alarm": 0, "missed": 0, "quiet": 0}
    unobservable_points: list[dict] = []
    missed_cases: list[dict] = []
    genuine_false_alarm_cases: list[dict] = []
    for days_ago in eval_offsets:
        eval_date = today - _dt.timedelta(days=days_ago)
        eval_manila = _dt.datetime.combine(eval_date, _dt.time(20, 0), tzinfo=timeutil.MANILA_TZ)
        eval_utc = eval_manila.astimezone(_dt.timezone.utc)
        eval_start_iso = timeutil.utc_now_sql(eval_utc)
        check_cutoff_iso = timeutil.utc_now_sql(
            eval_utc + _dt.timedelta(days=BACKTEST_CHECK_DAYS_AHEAD))

        # 2026-10 fix: the forecast's starting stock level must be
        # reconstructed AS OF eval_utc, not read live -- see
        # ingredient_forecast()'s historical_available docstring for why
        # this was silently wrong before (every eval point was starting
        # from TODAY's on-hand regardless of how far in the past eval_utc
        # was).
        historical_on_hand = _balances_at_cutoff(conn, eval_start_iso)
        forecast = ingredient_forecast(conn, horizon_days=BACKTEST_CHECK_DAYS_AHEAD,
                                       now=eval_utc, demand_model=demand_model,
                                       historical_available=historical_on_hand)
        balances_then = {r["ingredientId"]: r["available"] for r in forecast["ingredients"]}
        balances_after = _balances_at_cutoff(conn, check_cutoff_iso)
        restocked = _received_within_window(conn, eval_start_iso, check_cutoff_iso)
        lowest_in_window = _min_balances_in_window(
            conn, eval_start_iso, check_cutoff_iso, historical_on_hand)
        window_orders = _orders_between(conn, eval_start_iso, check_cutoff_iso)
        baseline_orders = _orders_between(
            conn, timeutil.utc_now_sql(eval_utc - _dt.timedelta(days=BACKTEST_COVERAGE_BASELINE_DAYS)),
            eval_start_iso)
        expected_window_orders = baseline_orders * BACKTEST_CHECK_DAYS_AHEAD / BACKTEST_COVERAGE_BASELINE_DAYS
        coverage = (window_orders / expected_window_orders) if expected_window_orders > 0 else None
        observable = coverage is None or coverage >= BACKTEST_MIN_WINDOW_COVERAGE
        if not observable:
            unobservable_points.append({
                "evaluationDate": eval_date.isoformat(), "ordersInCheckWindow": window_orders,
                "expectedOrdersInCheckWindow": round(expected_window_orders, 1),
                "coverage": round(coverage, 4)})

        point_counts = {"caught": 0, "false_alarm": 0, "missed": 0, "quiet": 0}
        for r in forecast["ingredients"]:
            ing_id = r["ingredientId"]
            flagged = r["status"] in ("critical", "out")
            actual_stockout = balances_after.get(ing_id, 0.0) <= 0
            if flagged and actual_stockout:
                key = "caught"
            elif flagged and not actual_stockout:
                key = "false_alarm"
                false_alarm_split["resolved_by_restock" if ing_id in restocked else "genuine"] += 1
            elif not flagged and actual_stockout:
                key = "missed"
            else:
                key = "quiet"
            counts[key] += 1
            point_counts[key] += 1
            if observable:
                observable_counts[key] += 1
                if key == "false_alarm":
                    observable_split["resolved_by_restock" if ing_id in restocked else "genuine"] += 1
            # within-window label (see _min_balances_in_window)
            hit_zero = lowest_in_window.get(ing_id, balances_then[ing_id]) <= 0
            within_counts["caught" if flagged and hit_zero else "false_alarm" if flagged
                          else "missed" if hit_zero else "quiet"] += 1
            warned = r["status"] in ("critical", "out", "at_risk")
            any_warning_counts["caught" if warned and actual_stockout else "false_alarm" if warned
                               else "missed" if actual_stockout else "quiet"] += 1
            if key == "missed":
                dep = r["daysOfSupply"]
                missed_cases.append({
                    "evaluationDate": eval_date.isoformat(), "ingredient": r["name"],
                    "status": r["status"], "availableAtEval": r["available"],
                    "forecastDailyConsumption": r["forecastDailyConsumption"],
                    "projectedDepletionDays": dep, "leadTimeDays": r["leadTimeDays"],
                    # The cause is read off the forecast itself, not guessed:
                    "cause": ("predicted_inside_window_but_after_lead_time"
                              if dep is not None and dep <= BACKTEST_CHECK_DAYS_AHEAD
                              else "demand_exceeded_forecast")})
            elif key == "false_alarm" and ing_id not in restocked:
                genuine_false_alarm_cases.append({
                    "evaluationDate": eval_date.isoformat(), "ingredient": r["name"],
                    "status": r["status"], "availableAtEval": r["available"],
                    "forecastDailyConsumption": r["forecastDailyConsumption"],
                    "projectedDepletionDays": r["daysOfSupply"],
                    "windowObservable": observable})
            if r["status"] in ("healthy", "excess") and r["forecastDailyConsumption"] > 0:
                excess_days_of_supply.append(balances_then[ing_id] / r["forecastDailyConsumption"])

        points.append({"evaluationDate": eval_date.isoformat(), "checkedThrough":
                       (eval_date + _dt.timedelta(days=BACKTEST_CHECK_DAYS_AHEAD)).isoformat(),
                       "ingredientsEvaluated": len(forecast["ingredients"]),
                       "ordersInCheckWindow": window_orders,
                       "windowCoverage": round(coverage, 4) if coverage is not None else None,
                       "windowObservable": observable, **point_counts})

    total = sum(counts.values())
    stockout_events = counts["caught"] + counts["missed"]
    service_level = (1.0 - stockout_events / total) if total else None
    precision = (counts["caught"] / (counts["caught"] + counts["false_alarm"])
                if (counts["caught"] + counts["false_alarm"]) > 0 else None)
    recall = (counts["caught"] / (counts["caught"] + counts["missed"])
             if (counts["caught"] + counts["missed"]) > 0 else None)
    # Adjusted precision: excludes false_alarm_split.resolved_by_restock from
    # the denominator -- those are flags where a real purchase_receipt
    # landed for that exact ingredient inside the check window, i.e. the
    # early warning may well have driven (or coincided usefully with) the
    # restock that prevented the stockout, not a wrong call. recall is
    # unaffected (it is defined only over caught/missed, neither of which
    # this split touches).
    genuine_false_alarms = false_alarm_split["genuine"]
    adjusted_precision = (counts["caught"] / (counts["caught"] + genuine_false_alarms)
                          if (counts["caught"] + genuine_false_alarms) > 0 else None)
    adjusted_f1 = (2 * adjusted_precision * recall / (adjusted_precision + recall)
                  if adjusted_precision is not None and recall is not None
                  and (adjusted_precision + recall) > 0 else None)
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and (precision + recall) > 0 else None)

    # 2026-09 audit: avg_days_of_supply_when_healthy_or_excess alone is a
    # mean over a heavily right-skewed real distribution (a handful of
    # near-zero-demand ingredients produce days-of-supply in the thousands
    # when divided by a tiny forecastDailyConsumption). The mean is kept
    # (it is real, not wrong) but a median/percentile breakdown is added
    # so it is not read as if it described a typical ingredient.
    dos_distribution = None
    if excess_days_of_supply:
        s = sorted(excess_days_of_supply)
        n = len(s)

        def _pct(p: float) -> float:
            return round(s[min(n - 1, int(p * n))], 2)

        dos_distribution = {
            "n": n,
            "mean": round(sum(s) / n, 2),
            "median": round(statistics.median(s), 2),
            "min": round(s[0], 2),
            "max": round(s[-1], 2),
            "p25": _pct(0.25),
            "p75": _pct(0.75),
            "p90": _pct(0.90),
        }

    return {
        "available": True,
        "methodology": (f"{len(eval_offsets)} evaluation point(s), every {BACKTEST_STEP_DAYS} days "
                        f"across the last {lookback_days} days, each recomputed with "
                        "ingredient_forecast()'s own `now` parameter using only data that "
                        f"existed at that point, checked against real ledger balances "
                        f"{BACKTEST_CHECK_DAYS_AHEAD} days later."),
        "evaluation_points": len(eval_offsets),
        "check_days_ahead": BACKTEST_CHECK_DAYS_AHEAD,
        "total_ingredient_evaluations": total,
        "confusion": counts,
        "false_alarm_breakdown": {
            **false_alarm_split,
            "note": ("'resolved_by_restock': a real purchase_receipt ledger row landed for "
                     "that exact ingredient within the check window -- the flag's own "
                     "lead-time logic is what that window is keyed to, so this is at least "
                     "as consistent with the warning having been acted on as with it being "
                     "wrong. 'genuine': no such receiving event occurred, yet the ingredient "
                     "never actually hit zero -- the model overestimated risk with no "
                     "restocking to explain why."),
        },
        "observable_windows": {
            **_confusion_metrics(observable_counts),
            "false_alarm_breakdown": observable_split,
            "precision_adjusted": (
                round(observable_counts["caught"] / (observable_counts["caught"] + observable_split["genuine"]), 4)
                if (observable_counts["caught"] + observable_split["genuine"]) > 0 else None),
            "excluded_points": unobservable_points,
            "note": (f"Same as `confusion`, but skips evaluation points whose 7-day check window "
                     f"recorded under {BACKTEST_MIN_WINDOW_COVERAGE:.0%} of a normal week's orders "
                     "(a data-collection gap or closure). With no trading, no stock outcome can "
                     "have been driven by demand, so a flag there is neither confirmed nor refuted. "
                     "`confusion` above still includes them."),
        },
        "within_window_label": {
            **_confusion_metrics(within_counts),
            "note": ("Stockout = balance <= 0 at any point inside the check window (what the "
                     "docstring has always claimed), instead of only at its end. Same flags, "
                     "same forecasts."),
        },
        "any_warning": {
            **_confusion_metrics(any_warning_counts),
            "note": ("Positive = critical, out OR at_risk, against the same end-of-window label. "
                     "at_risk means 'depletes after the lead time but within lead + review cycle' "
                     "-- ordering today still prevents it, so it is not part of the primary flag."),
        },
        "missed_stockouts": missed_cases,
        "genuine_false_alarms": genuine_false_alarm_cases,
        "service_level": round(service_level, 4) if service_level is not None else None,
        "critical_flag_precision": round(precision, 4) if precision is not None else None,
        "critical_flag_recall": round(recall, 4) if recall is not None else None,
        "critical_flag_f1": round(f1, 4) if f1 is not None else None,
        "critical_flag_precision_adjusted": round(adjusted_precision, 4) if adjusted_precision is not None else None,
        "critical_flag_f1_adjusted": round(adjusted_f1, 4) if adjusted_f1 is not None else None,
        "adjusted_precision_note": ("Excludes false_alarm_breakdown.resolved_by_restock from the "
                                    "false-alarm count -- see that field's note. critical_flag_precision "
                                    "above is kept unchanged alongside this for comparison."),
        "avg_days_of_supply_when_healthy_or_excess": (
            round(sum(excess_days_of_supply) / len(excess_days_of_supply), 2)
            if excess_days_of_supply else None),
        "days_of_supply_distribution_when_healthy_or_excess": dos_distribution,
        "points": points,
    }


# ===========================================================================
# Batch expiry annotation
# ===========================================================================
def annotate_batch_expiry(b: dict) -> dict | None:
    """Turn a batch's raw expiry_mode/expires_at/received_at into a usable
    date. Moved here from wt/api.py (2026-10 audit) so count_today() below
    and api.py's /console/expiry and ingredient detail all read the exact
    same calculation — there was exactly one place this happened before, and
    there still is, just relocated next to the other expiry-aware stock
    arithmetic (see v_ingredient_expired_stock in
    migrations/0040_expired_stock_usability.sql, which this function's
    `expired`/`days_to_expiry < 0` rule matches).

    Pops shelf_life_days/expiry_mode off `b` — they're inputs to this
    calculation, not part of what either caller wants back. Mutates and
    returns `b` with tracking/days_to_expiry/expired/storage_status added, or
    returns None when this batch has no computable expiry at all (not on a
    shelf-life estimate, and either missing or an unparseable expires_at) —
    the caller decides what that means for it: expiry_monitor drops it (it
    isn't an expiry signal to monitor), the drawer keeps it (it's still real
    stock the owner should see, just with nothing to say about when it
    turns).
    """
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


# ===========================================================================
# Count Today — a deterministic, explainable physical-count shortlist
# ===========================================================================
# Named thresholds, not a learned score. Each one is a plain business rule a
# manager could restate without this code; the UI shows exactly which ones
# fired (count_today()'s `reasons`), never a numeric score (see Change 4
# spec: "do not expose a meaningless numeric risk score").
#
# Stockout-risk statuses come straight from ingredient_forecast()'s own
# `status` field (out/critical/at_risk/idle/healthy/excess) -- not
# re-derived here, so Count Today can never disagree with what Replenishment
# itself is already showing for the same ingredient.
COUNT_TODAY_URGENT_STATUSES = ("out", "critical")   # may not last to the next delivery
COUNT_TODAY_WATCH_STATUSES = ("at_risk",)           # a softer version of the same risk
# Matches annotate_batch_expiry's own "Use Soon" <= 2 days / "Expired" < 0.
COUNT_TODAY_EXPIRY_URGENT_DAYS = 2
COUNT_TODAY_EXPIRY_WATCH_DAYS = 5
# "Hasn't been physically checked in about two weeks" -- long enough that
# recipe-deduction drift (portioning, scooping, small unlogged losses) has
# had time to accumulate on an ingredient already showing some other risk.
# Staleness alone never promotes an otherwise-healthy ingredient onto the
# list (see count_today()) -- it only adds to the explanation for one that
# is already there, per the spec's "avoid alert overload".
COUNT_TODAY_STALE_COUNT_DAYS = 14


def _nearest_expiry_by_ingredient(conn: sqlite3.Connection) -> dict[int, dict]:
    """Per ingredient: the single open batch with the soonest expiry signal
    (smallest days_to_expiry, already-expired batches included and sorting
    first). Same source query expiry_monitor uses in api.py, just grouped
    down to one row per ingredient instead of listed per batch."""
    rows = conn.execute(
        """
        SELECT b.qty_base, b.received_at, b.expires_at,
               i.id AS ingredient_id, i.expiry_mode, i.shelf_life_days
          FROM fefo_batch b JOIN ingredient i ON i.id = b.ingredient_id
         WHERE b.state = 'open' AND b.qty_base > 0
           AND (b.expires_at IS NOT NULL OR i.expiry_mode = 'shelf_life_estimate')
        """
    ).fetchall()
    nearest: dict[int, dict] = {}
    for r in rows:
        annotated = annotate_batch_expiry(dict(r))
        if annotated is None:
            continue
        current = nearest.get(r["ingredient_id"])
        if current is None or annotated["days_to_expiry"] < current["days_to_expiry"]:
            nearest[r["ingredient_id"]] = annotated
    return nearest


def count_today(conn: sqlite3.Connection, now: _dt.datetime | None = None) -> dict:
    """A manageable, explainable shortlist of which ingredients are worth a
    physical count right now — not all 131. Built entirely from numbers the
    rest of the system already computes (ingredient_forecast's status/days
    of supply, reservation's repeated-variance signal, the expiry monitor's
    nearest-batch calculation, and the ledger's own count history); this
    function only combines them against the named thresholds above and
    writes nothing.

    Two tiers:
      needsChecking — at least one urgent condition fired (likely to run out
        before the next delivery, a confirmed repeated variance pattern, or
        stock already expired/expiring within COUNT_TODAY_EXPIRY_URGENT_DAYS).
      worthChecking — only a softer condition fired (at_risk status, or
        expiring within the wider watch window) -- worth a look, not urgent.

    An ingredient that triggers neither tier is left off entirely; "count
    every ingredient" remains available separately (the existing full
    ingredient list / bulk-count screen), unaffected by this function.
    """
    forecast = ingredient_forecast(conn, now=now)
    status_by_id = {r["ingredientId"]: r for r in forecast["ingredients"]}

    reconciliation = reservation.ingredient_reconciliation_report(conn, days=30)
    signal_by_id = {
        item["ingredient_id"]: item["repeated_variance_signal"]
        for item in reconciliation["items"]
        if item.get("repeated_variance_signal")
    }

    last_counted = reservation.last_count_dates(conn)
    nearest_expiry = _nearest_expiry_by_ingredient(conn)
    today = timeutil.business_today(now)

    needs: list[dict] = []
    worth: list[dict] = []

    for ing_id, row in status_by_id.items():
        reasons: list[str] = []
        urgent = False

        if row["status"] in COUNT_TODAY_URGENT_STATUSES:
            urgent = True
            if row["daysOfSupply"] is not None:
                reasons.append(
                    f"May run out before the next delivery arrives — about "
                    f"{row['daysOfSupply']:g} day(s) of stock left against a "
                    f"{row['leadTimeDays']} day lead time."
                )
            else:
                reasons.append("Already out of usable stock.")
        elif row["status"] in COUNT_TODAY_WATCH_STATUSES:
            reasons.append("Running lower than usual — worth confirming before it becomes urgent.")

        signal = signal_by_id.get(ing_id)
        if signal and signal.get("active"):
            urgent = True
            direction = "short" if signal.get("direction") == "below" else "over"
            reasons.append(
                f"The last {signal['observation_count']} physical counts were consistently "
                f"{direction} of what the system expected — a repeated difference, not a one-off."
            )

        expiry = nearest_expiry.get(ing_id)
        if expiry is not None:
            if expiry["days_to_expiry"] <= COUNT_TODAY_EXPIRY_URGENT_DAYS:
                urgent = True
                reasons.append(
                    "Already expired and may still be counted as stock."
                    if expiry["expired"] else
                    "Some stock is expiring within a couple of days — may reduce what's usable."
                )
            elif expiry["days_to_expiry"] <= COUNT_TODAY_EXPIRY_WATCH_DAYS:
                reasons.append("Some stock is expiring soon.")

        if not reasons:
            continue

        last_at = last_counted.get(ing_id)
        stale_days = None
        if last_at:
            try:
                last_date = _dt.date.fromisoformat(timeutil.business_date_of(last_at))
                stale_days = (today - last_date).days
            except (ValueError, TypeError):
                stale_days = None
        if stale_days is None or stale_days >= COUNT_TODAY_STALE_COUNT_DAYS:
            reasons.append(
                "Hasn't been physically checked in over two weeks." if last_at is not None
                else "Has never been physically counted."
            )

        entry = {
            "ingredientId": ing_id,
            "ingredientCode": row["ingredientCode"],
            "name": row["name"],
            "baseUnit": row["baseUnit"],
            "expectedBase": row["onHand"],
            "status": row["status"],
            "daysOfSupply": row["daysOfSupply"],
            "leadTimeDays": row["leadTimeDays"],
            "lastCountedAt": last_at,
            "staleDays": stale_days,
            "reasons": reasons,
        }
        (needs if urgent else worth).append(entry)

    needs.sort(key=lambda r: (r["daysOfSupply"] if r["daysOfSupply"] is not None else -1))
    worth.sort(key=lambda r: (r["daysOfSupply"] if r["daysOfSupply"] is not None else 1e9))

    return {
        "asOf": timeutil.utc_now().isoformat(timespec="seconds"),
        "businessDate": today.isoformat(),
        "totalIngredients": len(status_by_id),
        "needsChecking": needs,
        "worthChecking": worth,
        "thresholds": {
            "urgentStatuses": list(COUNT_TODAY_URGENT_STATUSES),
            "watchStatuses": list(COUNT_TODAY_WATCH_STATUSES),
            "expiryUrgentDays": COUNT_TODAY_EXPIRY_URGENT_DAYS,
            "expiryWatchDays": COUNT_TODAY_EXPIRY_WATCH_DAYS,
            "staleCountDays": COUNT_TODAY_STALE_COUNT_DAYS,
        },
    }
