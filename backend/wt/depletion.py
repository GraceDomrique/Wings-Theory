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
import math
import sqlite3
import statistics
import threading
import time

from . import forecasting, timeutil

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


def prophet_refresh_generation() -> int:
    with _prophet_lock:
        return _prophet_generation


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
def _trading_days(conn: sqlite3.Connection, lookback_days: int) -> list[str]:
    """Manila calendar days inside the window on which the shop actually took
    an order. Excludes days with no `customer_order` row at all -- see the
    module docstring: that is a closed/no-data day, not a zero-sales day, and
    averaging over it silently understates every rate downstream."""
    bd = timeutil.sql_business_date("created_at")
    rows = conn.execute(
        f"SELECT DISTINCT {bd} AS d FROM customer_order "
        f"WHERE status != 'void' AND created_at >= ? ORDER BY d",
        (timeutil.utc_bound_n_manila_days_ago(lookback_days),),
    ).fetchall()
    return [r["d"] for r in rows]


def _item_daily_qty(conn: sqlite3.Connection, lookback_days: int) -> dict[int, dict[str, float]]:
    bd = timeutil.sql_business_date("o.created_at")
    rows = conn.execute(
        f"""
        SELECT ol.menu_item_id AS pk, {bd} AS d, SUM(ol.qty) AS qty
          FROM order_line ol JOIN customer_order o ON o.id = ol.order_id
         WHERE o.status != 'void' AND o.created_at >= ?
         GROUP BY pk, d
        """,
        (timeutil.utc_bound_n_manila_days_ago(lookback_days),),
    ).fetchall()
    out: dict[int, dict[str, float]] = {}
    for r in rows:
        out.setdefault(r["pk"], {})[r["d"]] = float(r["qty"])
    return out


def _heads_daily(conn: sqlite3.Connection, lookback_days: int) -> dict[str, float]:
    """Covers per day on unlimited tickets. Unlimited consumption is sized by
    heads x lambda, never by line quantity (see resolver.order_requirements),
    so heads -- not menu units -- is the series that drives those streams."""
    bd = timeutil.sql_business_date("o.created_at")
    rows = conn.execute(
        f"""
        SELECT {bd} AS d, SUM(o.head_count * ol.qty) AS heads
          FROM customer_order o
          JOIN order_line ol ON ol.order_id = o.id
          JOIN menu_item  m  ON m.id = ol.menu_item_id
         WHERE m.is_unli = 1 AND o.status != 'void' AND o.created_at >= ?
         GROUP BY d
        """,
        (timeutil.utc_bound_n_manila_days_ago(lookback_days),),
    ).fetchall()
    return {r["d"]: float(r["heads"] or 0) for r in rows}


def _intraday_share(conn: sqlite3.Connection, lookback_days: int) -> list[float]:
    """share[h] = fraction of a normal day's units sold at or before Manila
    hour h, pooled across every item (one item alone is too sparse to profile
    an hour with). This is what makes "40 units by 7pm" comparable to a whole
    day's baseline."""
    shift = f"+{timeutil.MANILA_UTC_OFFSET_HOURS} hours"
    rows = conn.execute(
        f"""
        SELECT CAST(strftime('%H', o.created_at, '{shift}') AS INTEGER) AS hr,
               SUM(ol.qty) AS qty
          FROM order_line ol JOIN customer_order o ON o.id = ol.order_id
         WHERE o.status != 'void' AND o.created_at >= ?
         GROUP BY hr
        """,
        (timeutil.utc_bound_n_manila_days_ago(lookback_days),),
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
    """
    items = conn.execute("SELECT id, name FROM menu_item WHERE pos_visible = 1").fetchall()
    scope = _prophet_scope_key(conn)
    fitted = insufficient = failed = 0
    for it in items:
        rows, series = forecasting._daily_series(conn, it["id"], lookback_days)
        key = (scope, it["id"], lookback_days, horizon_days)
        if len(rows) < forecasting.MIN_DEMAND_SAMPLE_DAYS:
            result = None
            insufficient += 1
        else:
            result = forecasting.fit_prophet(series, rows, horizon_days)
            if result is None:
                failed += 1
            else:
                fitted += 1
        with _prophet_lock:
            _prophet_store[key] = (time.monotonic(), result)
    global _prophet_generation
    with _prophet_lock:
        _prophet_generation += 1
    return {
        "itemsConsidered": len(items), "fitted": fitted,
        "insufficientData": insufficient, "fitFailed": failed,
        "horizonDays": horizon_days, "lookbackDays": lookback_days,
    }


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
    """
    today = timeutil.business_today(now)
    manila_hour = timeutil.to_manila(now or timeutil.utc_now()).hour
    share = _intraday_share(conn, lookback_days)
    share_by_now = _clamp(share[manila_hour], 0.0, 1.0)

    trading = set(_trading_days(conn, lookback_days))
    per_item = _item_daily_qty(conn, lookback_days)
    heads = _heads_daily(conn, lookback_days)

    items = conn.execute(
        "SELECT id, name, category, recipe_id, is_unli, price FROM menu_item WHERE pos_visible = 1"
    ).fetchall()

    series: dict[int, DemandSeries] = {}
    meta: dict[int, sqlite3.Row] = {}
    for it in items:
        day_map = per_item.get(it["id"], {})
        points = [(_dt.date.fromisoformat(d), day_map.get(d, 0.0)) for d in sorted(trading)]
        s = DemandSeries(it["id"], it["name"], points, today, share_by_now, horizon_days)
        if demand_model == "prophet":
            pf = _prophet_daily_for_item(conn, it["id"], lookback_days, horizon_days)
            if pf is not None:
                for k in range(1, horizon_days + 1):
                    day = pf["forecast"][k - 1]
                    s.daily[k] = max(0.0, day["qty"] * s.momentum)
                    # (high-low)/2 under an 80%-interval normal approximation
                    # (z=1.2816 each side) -- Prophet's own uncertainty,
                    # reused rather than re-estimated from EWMA residuals.
                    s.sigma_daily[k] = max(0.0, day["high"] - day["low"]) / (2 * 1.2816)
                s.demand_source = "prophet"
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
        f"WHERE ingredient_id = ? AND created_at >= ? GROUP BY d",
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
    return conn.execute(
        """
        SELECT i.id, i.ingredient_id, i.name, i.category, i.base_unit, i.purchase_unit,
               i.pack_size, i.yield_factor, i.lead_time_days, i.reorder_point_base,
               i.inventory_type, i.on_hand_base, i.reserved_base,
               (i.on_hand_base - i.reserved_base) AS available_base,
               i.unit_cost, i.unit_cost_status, i.pack_size_status,
               r.is_deductable
          FROM ingredient i
          JOIN v_ingredient_readiness r ON r.id = i.id
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
                        demand_model: str = "ewma") -> dict:
    """The whole chain, for every deductable ingredient.

    Returns one row per ingredient carrying: current stock, projected daily
    consumption, the full day-by-day trajectory, projected depletion date,
    days of supply, supplier lead time, projected stock at the lead-time
    boundary, status, and the demand assumptions behind all of it.

    demand_model: see build_demand()'s docstring. Callers normally pass the
    'depletion_demand_model' app_setting (api.py) rather than hardcoding
    this, so a deployment can switch between the EWMA and Prophet-driven
    baselines by changing one setting rather than a code deploy.
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
                     for r in forecasting.ingredient_depletion(conn, lookback_days=lookback_days)}

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

    for ing_id, rate in op_rate.items():
        if ing_id in known and not any(consumption[ing_id]):
            consumption[ing_id] = [rate * (1.0 - demand["share_by_now"])] + [rate] * horizon_days

    # --- trajectory + status --------------------------------------------------
    out = []
    for ing in ingredients:
        ing_id = ing["id"]
        available = float(ing["available_base"] or 0.0)
        cons = consumption[ing_id]
        sig = [math.sqrt(v) for v in variance[ing_id]]

        # Two runs of the same arithmetic. `raw` is allowed to go negative --
        # that is where the zero crossing and the size of any shortfall come
        # from. `traj` is the physical stock line, floored at zero, because a
        # storeroom cannot hold minus two kilos of fries: once it is empty the
        # remaining demand goes unserved, it does not keep draining a
        # non-existent balance. Plotting the raw line instead would put 90% of
        # the chart below the axis and hide the part anyone cares about.
        raw, traj, band_lo, band_hi = [], [], [], []
        bal = available
        cum_sig2 = 0.0
        for k in range(n):
            bal -= cons[k]
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
        lead = ing["lead_time_days"] or 0
        rop = ing["reorder_point_base"]
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
        order_qty = None
        if steady > 0 and ing["pack_size"]:
            shortfall = steady * lead + safety_stock_base - available
            if shortfall > 0:
                order_qty = math.ceil(shortfall / (ing["pack_size"] * (ing["yield_factor"] or 1.0)))

        top = sorted(contributors[ing_id].items(), key=lambda kv: -kv[1])[:5]

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
            "onHand": round(float(ing["on_hand_base"] or 0), 3),
            "reserved": round(float(ing["reserved_base"] or 0), 3),
            "available": round(available, 3),
            "restOfTodayConsumption": round(cons[0], 3),
            "forecastDailyConsumption": round(steady, 4),
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
    trading = set(_trading_days(conn, lookback_days))
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
        "WHERE created_at >= ? GROUP BY ingredient_id", (cutoff_iso,),
    ).fetchall()}
    return {iid: current[iid] - after.get(iid, 0.0) for iid in current}


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
    excess_days_of_supply: list[float] = []
    points = []
    for days_ago in eval_offsets:
        eval_date = today - _dt.timedelta(days=days_ago)
        eval_manila = _dt.datetime.combine(eval_date, _dt.time(20, 0), tzinfo=timeutil.MANILA_TZ)
        eval_utc = eval_manila.astimezone(_dt.timezone.utc)
        check_cutoff_iso = timeutil.utc_now_sql(
            eval_utc + _dt.timedelta(days=BACKTEST_CHECK_DAYS_AHEAD))

        forecast = ingredient_forecast(conn, horizon_days=BACKTEST_CHECK_DAYS_AHEAD,
                                       now=eval_utc, demand_model=demand_model)
        balances_then = {r["ingredientId"]: r["available"] for r in forecast["ingredients"]}
        balances_after = _balances_at_cutoff(conn, check_cutoff_iso)

        point_counts = {"caught": 0, "false_alarm": 0, "missed": 0, "quiet": 0}
        for r in forecast["ingredients"]:
            ing_id = r["ingredientId"]
            flagged = r["status"] in ("critical", "out")
            actual_stockout = balances_after.get(ing_id, 0.0) <= 0
            if flagged and actual_stockout:
                key = "caught"
            elif flagged and not actual_stockout:
                key = "false_alarm"
            elif not flagged and actual_stockout:
                key = "missed"
            else:
                key = "quiet"
            counts[key] += 1
            point_counts[key] += 1
            if r["status"] in ("healthy", "excess") and r["forecastDailyConsumption"] > 0:
                excess_days_of_supply.append(balances_then[ing_id] / r["forecastDailyConsumption"])

        points.append({"evaluationDate": eval_date.isoformat(), "checkedThrough":
                       (eval_date + _dt.timedelta(days=BACKTEST_CHECK_DAYS_AHEAD)).isoformat(),
                       "ingredientsEvaluated": len(forecast["ingredients"]), **point_counts})

    total = sum(counts.values())
    stockout_events = counts["caught"] + counts["missed"]
    service_level = (1.0 - stockout_events / total) if total else None
    precision = (counts["caught"] / (counts["caught"] + counts["false_alarm"])
                if (counts["caught"] + counts["false_alarm"]) > 0 else None)
    recall = (counts["caught"] / (counts["caught"] + counts["missed"])
             if (counts["caught"] + counts["missed"]) > 0 else None)
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
        "service_level": round(service_level, 4) if service_level is not None else None,
        "critical_flag_precision": round(precision, 4) if precision is not None else None,
        "critical_flag_recall": round(recall, 4) if recall is not None else None,
        "critical_flag_f1": round(f1, 4) if f1 is not None else None,
        "avg_days_of_supply_when_healthy_or_excess": (
            round(sum(excess_days_of_supply) / len(excess_days_of_supply), 2)
            if excess_days_of_supply else None),
        "days_of_supply_distribution_when_healthy_or_excess": dos_distribution,
        "points": points,
    }
