"""Phase 3 — demand and depletion forecasting.

Two different questions:

  - ingredient_depletion: at the actual recent consumption rate, how many
    days until this ingredient runs out, and is that inside its own lead
    time? A rate-and-runway calculation, not a time series problem — this
    stays stdlib arithmetic over stock_ledger regardless of what forecasts
    an individual item's demand. Answerable the moment a single sale has
    happened, honestly labeled "no sales data yet" until then.

  - menu_item_demand: how much of this item sells on a given day, projected
    forward a few days? Two implementations, used for two different jobs:

      - menu_item_demand() — a 14-day trailing average with a day-of-week
        factor, stdlib only. Cheap enough to run for every menu item on one
        page load (the Forecasts overview table).
      - menu_item_demand_prophet() — Meta's Prophet, real weekly seasonality
        and uncertainty intervals fit via cmdstan. Genuinely better, and
        explicitly requested by name, but each fit costs several seconds
        (it spawns a compiled sampler subprocess) — fine for the one item a
        manager clicks into, not for 20-30 items on every dashboard load.
        Falls back to the trailing-average method if Prophet/cmdstan isn't
        available in this environment (see _PROPHET_AVAILABLE) rather than
        hard-failing the page.

Both report "insufficient data" out loud rather than guessing when there
isn't enough real history yet — no synthetic data is ever mixed into either
path, Prophet included.
"""
from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import threading
import time

from . import timeutil
from .errors import NotFoundError

MIN_DEMAND_SAMPLE_DAYS = 7      # distinct days with at least one sale before a forecast is offered
MIN_SEASONALITY_WEEKS = 2       # weeks of history before a weekday factor is trusted
RESIDUAL_SIGMA = 2.5            # how far off its own fit a day must be to be called an outlier

# 2026-09 accuracy-optimization audit: Prophet's own default growth='linear'
# fits a trend component from whatever history a series has -- on this
# dataset's ~100-day training windows that trend estimate is noisy enough
# that it actively hurt out-of-sample accuracy (measured: a controlled
# 24-configuration nested rolling-origin search across all 27 eligible
# series found growth was the single dominant lever -- changepoint_prior_
# scale and seasonality_prior_scale showed no consistent effect, seasonality_
# mode only a small one -- 'flat' growth lowered mean held-out WAPE from
# ~57% to ~52% at a 7-day horizon, bringing Prophet to parity with the
# best baseline (day-of-week-seasonal, ~52%) instead of trailing it by
# ~5 points). This matches, rather than contradicts, this module's existing
# philosophy: menu_item_demand()'s own docstring already argues "with this
# little data a fitted trend line is more likely to be noise than signal"
# for its hand-rolled trailing-average method -- 'flat' growth is the same
# judgment applied to Prophet's own trend component. See
# docs/PROPHET_ACCURACY_OPTIMIZATION_AUDIT.md for the full experiment.
# fit_prophet()'s `growth` parameter still allows overriding back to
# 'linear' (or anything else) per call -- this is this module's own
# considered default, not a hardcoded ceiling.
PROPHET_DEFAULT_GROWTH = "flat"

try:
    import pandas as _pd
    from prophet import Prophet as _Prophet
    logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
    logging.getLogger("prophet").setLevel(logging.WARNING)
    _PROPHET_AVAILABLE = True
except Exception:   # pragma: no cover - exercised only when cmdstan isn't installed
    _PROPHET_AVAILABLE = False

RISK_ORDER = {"out": 0, "critical": 1, "warning": 2, "idle": 3, "ok": 4, "no_data": 5}
REVIEW_CYCLE_DAYS = 3   # buffer beyond lead time before the next check-in, not just enough to exactly not run out

# ---------------------------------------------------------------------------
# Reorder point vs. days of supply — how the two relate, since the schema has
# carried reorder_point_base since 0001 and nothing ever read it.
#
# They are the same judgement expressed in different units, and they are NOT
# two independent alerts:
#
#     days_of_supply = available / avg_daily_consumption
#     ROP            = avg_daily_consumption x lead_time_days   (+ safety stock)
#     S < ROP        <=>  days_of_supply < lead_time_days       (when ROP is
#                                                                derived, and
#                                                                no safety
#                                                                stock is set)
#
# So a derived ROP would fire at exactly the same moment the existing
# 'critical' tier already does, and shipping both would put two alerts on one
# screen for one condition. What a *configured* ROP adds that days-of-supply
# cannot is a floor the owner sets from knowledge the ledger does not have —
# a supplier's minimum order, a Sunday with no deliveries, a party booking
# next week. That is a genuinely different signal, so:
#
#   * below_reorder_point is reported ONLY for an ingredient whose
#     reorder_point_base the owner actually set. It is never inferred, and an
#     unset ROP is not a silent zero.
#   * the risk tier stays days-of-supply-driven. A configured ROP breach
#     raises the tier to at least 'warning' (it cannot lower one), so the two
#     signals reinforce rather than contradict.
#   * suggested_reorder_point_base is offered alongside as advice only — the
#     arithmetic above, made visible so the owner has a starting number to
#     accept or override. Nothing alerts on it.
# ---------------------------------------------------------------------------
ROP_BREACH_MIN_RISK = "warning"


def _elapsed_days(first_day_iso: str | None, now: _dt.datetime | None = None) -> int:
    if not first_day_iso:
        return 0
    first = _dt.date.fromisoformat(first_day_iso)
    return max(1, (timeutil.business_today(now) - first).days + 1)


def ingredient_depletion(conn: sqlite3.Connection, lookback_days: int = 30,
                         now: _dt.datetime | None = None) -> list[dict]:
    """One row per deductable ingredient: real avg daily consumption from the
    ledger, projected days remaining at that rate, and a risk tier relative
    to the ingredient's own lead time (set on the ingredient record, used
    nowhere else until now).

    "Consumption" means different things for different rows: a food
    ingredient or sellable product is consumed by a sale (reason
    'sale_commit'); an operational supply — tissue, cleaning alcohol,
    gloves — never passes through the POS, so its consumption is whatever
    staff recorded as used ('operational_use'). Reading the wrong reason
    for a supply would show it as permanently "no sales data", which is
    true but useless — it was never going to have sales data.

    This does NOT report a purchase quantity. It used to derive one from
    this same trailing rate, which made it a second, independent reorder
    engine running alongside wt/depletion.py's forward one (that one also
    accounts for weekday seasonality, today's actual pace, and yield_factor
    in the pack conversion, none of which a trailing average can) — the two
    could recommend different quantities for the same ingredient from the
    same on-hand figure. wt/depletion.ingredient_forecast() is the sole
    source of suggestedOrderQty now; every caller that needs a purchase
    recommendation reads that, not this. This function stays for what it is
    genuinely good at: a cheap, always-answerable runway/risk number, used
    where a screen wants an at-a-glance figure without the cost of the full
    forward model (see api.py's forecast_depletion and listIngredients'
    withRunway).

    now: 2026-10 fix -- every real caller before this one omitted it, which
    meant the "trailing N days" window was always bounded by the real
    wall-clock moment the call happened to run at, never by the historical
    instant a backtest is actually asking about. depletion.ingredient_
    forecast()'s own replenishment_backtest() caller passes its `now`
    through here now, so a rate computed for a date in the past only ever
    reads ledger rows that existed as of THAT date -- confirmed directly:
    before this fix, this function returned the IDENTICAL avg_daily_
    consumption for the same ingredient at every one of six different
    simulated dates spanning two months, which is only possible if the
    historical `now` was never actually being used to bound the window.
    Every other caller still omits `now` (real current-moment behavior,
    unchanged) and gets back exactly what it always has.
    """
    ingredients = conn.execute(
        """
        SELECT i.id, i.name, i.base_unit, i.lead_time_days, i.pack_size, i.purchase_unit,
               i.inventory_type, i.reorder_point_base,
               (i.on_hand_base - i.reserved_base) AS available_base
          FROM ingredient i
          JOIN v_ingredient_readiness r ON r.id = i.id
         WHERE i.is_active = 1 AND r.is_deductable = 1
        """
    ).fetchall()

    upper_bound_clause = " AND created_at < ?" if now is not None else ""
    upper_bound_params = (timeutil.utc_upper_bound_sql(now),) if now is not None else ()

    out = []
    for ing in ingredients:
        consumption_reason = "operational_use" if ing["inventory_type"] == "operational_supply" else "sale_commit"
        row = conn.execute(
            f"""
            SELECT -SUM(delta_base) AS consumed,
                   COUNT(DISTINCT {timeutil.sql_business_date("created_at")}) AS days_active,
                   MIN({timeutil.sql_business_date("created_at")}) AS first_day
              FROM stock_ledger
             WHERE ingredient_id = ? AND reason = ?
               AND created_at >= ?
               {upper_bound_clause}
            """,
            (ing["id"], consumption_reason, timeutil.utc_bound_n_manila_days_ago(lookback_days, now),
             *upper_bound_params),
        ).fetchone()

        consumed = row["consumed"] or 0.0
        days_active = row["days_active"] or 0
        # The rate is spread over the window actually elapsed since the first
        # sale, not just the days something sold — a slow mover that sold on
        # 2 of the last 30 days still has a 30-day rate, not a 2-day one.
        span_days = min(lookback_days, _elapsed_days(row["first_day"], now))
        avg_daily = (consumed / span_days) if span_days > 0 else 0.0

        available = ing["available_base"]
        lead = ing["lead_time_days"] or 0
        if avg_daily <= 0:
            days_remaining = None
            risk = "no_data" if days_active == 0 else "idle"
        else:
            days_remaining = available / avg_daily
            if days_remaining <= 0:
                risk = "out"
            elif days_remaining <= lead:
                risk = "critical"
            elif days_remaining <= lead + 3:
                risk = "warning"
            else:
                risk = "ok"

        # S_k < ROP_k — only for an ingredient the owner actually gave a
        # reorder point. See the note above ROP_BREACH_MIN_RISK for why this
        # is never derived and never becomes a second, competing alert.
        rop = ing["reorder_point_base"]
        below_rop = rop is not None and available < rop
        if below_rop and RISK_ORDER[risk] > RISK_ORDER[ROP_BREACH_MIN_RISK]:
            risk = ROP_BREACH_MIN_RISK
        suggested_rop = round(avg_daily * lead, 3) if avg_daily > 0 else None

        out.append({
            "ingredient_id": ing["id"], "name": ing["name"], "base_unit": ing["base_unit"],
            "inventory_type": ing["inventory_type"],
            "available_base": round(available, 3),
            "avg_daily_consumption": round(avg_daily, 4),
            "days_remaining": round(days_remaining, 1) if days_remaining is not None else None,
            "lead_time_days": lead,
            "risk": risk,
            "reorder_point_base": rop,
            "below_reorder_point": below_rop,
            "suggested_reorder_point_base": suggested_rop,
        })

    out.sort(key=lambda x: (RISK_ORDER[x["risk"]], x["days_remaining"] if x["days_remaining"] is not None else 1e9))
    return out


def _item_or_404(conn: sqlite3.Connection, menu_item_pk: int) -> sqlite3.Row:
    item = conn.execute("SELECT name FROM menu_item WHERE id=?", (menu_item_pk,)).fetchone()
    if not item:
        raise NotFoundError(f"menu item {menu_item_pk} not found")
    return item


# ===========================================================================
# Dead-tail-aware "now" -- 2026-09 second adaptive-growth pass
#
# PROPHET_ACCURACY_OPTIMIZATION_AUDIT.md manually identified (section B) that
# this deployment's order history can carry a trailing DATA-COLLECTION gap:
# dense real trading, then a period of near-zero orders that reflects a
# recording gap, not a real demand collapse -- and explicitly flagged
# (section N) that evaluate_demand_model()/evaluate_store_demand_model()
# still read the live wall clock with no defense against this, "until...
# given the same now override this audit added... not done in this pass".
#
# Confirmed still live and consequential just now: calling
# depletion.refresh_prophet_baselines() unanchored (today's real wall
# clock, which currently sits a few days past this dataset's own such gap)
# selects growth='linear' for only 3/32 items and 1/3 store metrics --
# against an anchored, leakage-free evaluation that (correctly, per Level 1's
# own tuning-fold WAPE) selects 'linear' for the store's own aggregate
# orders/items/sales AND its highest-volume item. The gap silently
# corrupts whichever recent fold lands inside it.
#
# effective_now() automates the same judgment the audit made by hand:
# compare each recent day's real order count against the MEDIAN of the
# last DEAD_TAIL_MEDIAN_WINDOW_DAYS days (robust to a gap that is short
# relative to that window), then walk backward from the true wall clock
# until a day clears DEAD_TAIL_MIN_FRACTION_OF_MEDIAN of that median. A
# deployment with no such gap gets back the real wall clock unchanged --
# this is a no-op on healthy, continuous data, self-healing the moment real
# trading resumes past whatever gap triggered it. Nothing is deleted or
# hidden: only which date backtest/growth-selection code treats as "today"
# for windowing changes; the sparse/gap rows stay in the database untouched.
#
# Deliberately scoped to backtest/growth-selection/reporting paths only
# (evaluate_demand_model, evaluate_store_demand_model, and
# refresh_prophet_baselines' growth selection + live fit) -- NOT the EWMA
# nowcast path in depletion.py, which legitimately needs the real wall
# clock to answer "how is today, already partway done, actually going".
# Anchoring that path to a stale date would be actively wrong, not safer.
# ===========================================================================
DEAD_TAIL_MEDIAN_WINDOW_DAYS = 90
DEAD_TAIL_MIN_FRACTION_OF_MEDIAN = 0.25


def effective_now(conn: sqlite3.Connection, *, now: _dt.datetime | None = None) -> _dt.datetime:
    """See the module section above this function for the full rationale.
    Returns `now` (or the real wall clock if `now` is omitted) unchanged
    whenever recent trading looks healthy; otherwise returns an instant
    whose Manila business date is the most recent day that cleared the
    activity bar, so every `_daily_series`/`_store_daily_series` caller
    downstream treats that day as "today" instead of a trailing gap.
    """
    # 2026-10 leakage audit: upper-bounded by `now`, but ONLY when a caller
    # explicitly supplied a historical one -- captured as `was_given` BEFORE
    # resolving the live-clock default below, because once resolved the two
    # cases are indistinguishable. An always-on upper bound (tried first)
    # broke a real existing test: test fixtures commonly stamp "today" at a
    # fixed example time (e.g. noon UTC) that can be LATER than the actual
    # wall-clock instant a test happens to run at, which `created_at <
    # real_utc_now()` would then wrongly exclude -- a test-fixture-timing
    # artifact, not a real production concern (a real order's created_at is
    # always <= the real moment it was inserted, by construction). Bounding
    # only the genuinely-historical-`now` case avoids that false positive
    # while still closing the same latent leak _daily_series() closes for
    # that case.
    was_given = now is not None
    now = now or timeutil.utc_now()
    upper = " AND created_at < ?" if was_given else ""
    params = [timeutil.utc_bound_n_manila_days_ago(DEAD_TAIL_MEDIAN_WINDOW_DAYS, now)]
    if was_given:
        params.append(timeutil.utc_upper_bound_sql(now))
    rows = conn.execute(
        f"""
        SELECT {timeutil.sql_business_date("created_at")} AS d, COUNT(*) AS n
          FROM customer_order
         WHERE status != 'void' AND created_at >= ?{upper}
         GROUP BY d
        """,
        params,
    ).fetchall()
    if not rows:
        return now
    counts = sorted(r["n"] for r in rows)
    mid = len(counts) // 2
    median = counts[mid] if len(counts) % 2 else (counts[mid - 1] + counts[mid]) / 2
    if median <= 0:
        return now
    threshold = median * DEAD_TAIL_MIN_FRACTION_OF_MEDIAN
    by_day = {r["d"]: r["n"] for r in rows}
    d = timeutil.business_today(now)
    for _ in range(DEAD_TAIL_MEDIAN_WINDOW_DAYS):
        if by_day.get(d.isoformat(), 0) >= threshold:
            if d == timeutil.business_today(now):
                return now  # today itself already clears the bar -- no gap, real now unchanged
            return _dt.datetime.combine(d, _dt.time(12, 0), tzinfo=timeutil.MANILA_TZ)
        d -= _dt.timedelta(days=1)
    return now  # no day in the whole window cleared the bar -- give up rather than walk back indefinitely


def _daily_series(conn: sqlite3.Connection, menu_item_pk: int, lookback_days: int,
                  now: _dt.datetime | None = None):
    """Real daily sold-qty rows plus the same series zero-filled across every
    calendar day in the window — shared by both forecasting methods below so
    they can't disagree about what "the last N days" contains.

    `now` is the same optional override depletion.build_demand() already
    accepts (passed straight through to timeutil.business_today()/
    utc_bound_n_manila_days_ago()) — omitted, every live caller gets today's
    exact unchanged behavior. It exists for evaluation code that needs to
    anchor a window to a specific reference date rather than the live
    system clock (e.g. the 2026-09 accuracy-optimization experiment,
    which anchors to this dataset's own last real trading day rather than
    a system clock that has since moved past it).

    2026-10 leakage audit: `rows` (unlike `series`, which was already safe
    by construction -- the zero-fill loop below never asks for a day past
    `today`) had no upper bound on created_at, so a caller handed a
    historical `now` could still read real order rows dated AFTER it.
    `series`/the Prophet fit itself (which trains on `series`, never
    `rows` -- see fit_prophet()) were never affected; the one real consumer
    of raw `rows` is fit_prophet()'s own weeks_covered seasonality count,
    which could have overcounted distinct ISO weeks for a historical `now`
    with real future data on the live clock. No current caller in this
    codebase passes a historical `now` into anything that reaches this
    function (confirmed by audit), so this was latent, not an active
    cause of any previously reported number -- closed anyway, on the same
    "every historical evaluation must reproduce exactly what the system
    could have known" principle the rest of this audit applies."""
    bd = timeutil.sql_business_date("o.created_at")
    upper = " AND o.created_at < ?" if now is not None else ""
    params = [menu_item_pk, timeutil.utc_bound_n_manila_days_ago(lookback_days, now)]
    if now is not None:
        params.append(timeutil.utc_upper_bound_sql(now))
    rows = conn.execute(
        f"""
        SELECT {bd} AS d, SUM(ol.qty) AS qty
          FROM order_line ol JOIN customer_order o ON o.id = ol.order_id
         WHERE ol.menu_item_id = ? AND o.status != 'void'
           AND o.created_at >= ?{upper}
         GROUP BY {bd}
        """,
        params,
    ).fetchall()
    by_day = {r["d"]: r["qty"] for r in rows}
    today = timeutil.business_today(now)
    series = []
    for i in range(lookback_days - 1, -1, -1):
        d = today - _dt.timedelta(days=i)
        series.append({"date": d.isoformat(), "qty": by_day.get(d.isoformat(), 0)})
    return rows, series


def menu_item_demand(
    conn: sqlite3.Connection, menu_item_pk: int, horizon_days: int = 7, lookback_days: int = 60,
) -> dict:
    """Daily sold-quantity series for one menu item plus a short forecast.

    Method: a trailing moving average over the last 14 days (or all available
    history if shorter), adjusted by a day-of-week factor once at least
    MIN_SEASONALITY_WEEKS of history exists. Deliberately no trend
    extrapolation — with this little data a fitted trend is more likely to be
    noise than signal, and a false "trending up" call is worse than a flat,
    honest average.

    Cheap by design: this runs once per menu item on the Forecasts overview
    table, so it has to stay fast for 20-30 items on one page load. See
    menu_item_demand_prophet() for the real model, used one item at a time.
    """
    item = _item_or_404(conn, menu_item_pk)
    rows, series = _daily_series(conn, menu_item_pk, lookback_days)
    active_days = len(rows)
    total_qty = sum(r["qty"] for r in rows)

    if active_days < MIN_DEMAND_SAMPLE_DAYS:
        return {
            "menu_item_id": menu_item_pk, "name": item["name"], "insufficient_data": True,
            "active_days": active_days, "required_days": MIN_DEMAND_SAMPLE_DAYS,
            "series": series, "forecast": [], "method": "trailing_average",
        }

    trailing = series[-14:]
    trailing_avg = sum(p["qty"] for p in trailing) / len(trailing)

    weeks_covered = len({_dt.date.fromisoformat(r["d"]).isocalendar()[:2] for r in rows})
    weekday_factor: dict[int, float] = {}
    seasonality_applied = weeks_covered >= MIN_SEASONALITY_WEEKS
    if seasonality_applied:
        by_weekday: dict[int, list[float]] = {}
        for p in series:
            wd = _dt.date.fromisoformat(p["date"]).weekday()
            by_weekday.setdefault(wd, []).append(p["qty"])
        overall_avg = (sum(p["qty"] for p in series) / len(series)) or 1.0
        for wd, vals in by_weekday.items():
            weekday_factor[wd] = (sum(vals) / len(vals)) / overall_avg

    today = timeutil.business_today()
    forecast = []
    for i in range(1, horizon_days + 1):
        d = today + _dt.timedelta(days=i)
        factor = weekday_factor.get(d.weekday(), 1.0)
        forecast.append({"date": d.isoformat(), "qty": round(trailing_avg * factor, 2)})

    return {
        "menu_item_id": menu_item_pk, "name": item["name"], "insufficient_data": False,
        "active_days": active_days, "total_qty_in_window": total_qty,
        "trailing_avg_daily": round(trailing_avg, 2),
        "seasonality_applied": seasonality_applied,
        "series": series, "forecast": forecast, "method": "trailing_average",
    }


def menu_item_demand_prophet(
    conn: sqlite3.Connection, menu_item_pk: int, horizon_days: int = 7, lookback_days: int = 90,
) -> dict:
    """The real model: Meta's Prophet, fit fresh on this item's actual daily
    sales. Weekly seasonality and uncertainty intervals come from the model
    itself rather than a hand-rolled weekday factor.

    Falls back to menu_item_demand() if Prophet/cmdstan raises for any
    reason (not installed, or a runtime failure) — a forecast page that
    hard-500s because a subprocess-backed sampler hiccuped is worse than one
    that quietly uses the simpler method and says so.

    Growth: uses whatever growth depletion.refresh_prophet_baselines() most
    recently cached for this item via cache_growth_selection() (see that
    cache's own docstring) -- the same validated per-item choice
    replenishment fits with, so this detail page and replenishment can no
    longer silently disagree. A cache miss (never refreshed yet) or a stale
    entry falls back to PROPHET_DEFAULT_GROWTH, never an inline re-tune —
    this stays a single fit, not a backtest, exactly as before.

    Window: anchored to effective_now() (see that function), not the live
    wall clock, so a trailing data-collection gap isn't fit as if it were
    real recent demand. The non-Prophet fallback below (prophet_unavailable
    or insufficient_data) deliberately keeps reading the real wall clock via
    menu_item_demand()'s own default -- that cheap path is shared by many
    other callers and out of scope for this fix.
    """
    if not _PROPHET_AVAILABLE:
        return {**menu_item_demand(conn, menu_item_pk, horizon_days, lookback_days),
               "method": "trailing_average", "prophet_unavailable": True}

    item = _item_or_404(conn, menu_item_pk)
    rows, series = _daily_series(conn, menu_item_pk, lookback_days, now=effective_now(conn))
    active_days = len(rows)
    total_qty = sum(r["qty"] for r in rows)

    if active_days < MIN_DEMAND_SAMPLE_DAYS:
        return {
            "menu_item_id": menu_item_pk, "name": item["name"], "insufficient_data": True,
            "active_days": active_days, "required_days": MIN_DEMAND_SAMPLE_DAYS,
            "series": series, "forecast": [], "method": "prophet",
        }

    cached_growth = cached_growth_for(conn, f"menu_item:{menu_item_pk}")
    growth = cached_growth["selected_growth"] if cached_growth else PROPHET_DEFAULT_GROWTH
    fitted_model = fit_prophet(series, rows, horizon_days, growth=growth)
    if fitted_model is None:
        fallback = menu_item_demand(conn, menu_item_pk, horizon_days, lookback_days)
        return {**fallback, "method": "trailing_average", "prophet_unavailable": True}

    trailing = series[-14:]
    trailing_avg = sum(p["qty"] for p in trailing) / len(trailing)

    return {
        "menu_item_id": menu_item_pk, "name": item["name"], "insufficient_data": False,
        "active_days": active_days, "total_qty_in_window": total_qty,
        "trailing_avg_daily": round(trailing_avg, 2),
        "seasonality_applied": fitted_model["seasonality_applied"],
        "series": series, "forecast": fitted_model["forecast"],
        "fitted": fitted_model["fitted"], "outliers": fitted_model["outliers"],
        "method": "prophet",
        "growth_used": growth,
        "growth_source": "cached_adaptive" if cached_growth else "default_not_yet_refreshed",
    }


def fit_prophet(series: list[dict], rows: list[dict], horizon_days: int, *,
                changepoint_prior_scale: float | None = None,
                seasonality_prior_scale: float | None = None,
                seasonality_mode: str | None = None,
                growth: str | None = None) -> dict | None:
    """The actual Prophet fit, factored out of menu_item_demand_prophet() so
    a second caller (wt/depletion.py's ingredient-level Prophet demand model)
    fits exactly the same way instead of carrying its own copy of this logic
    that could quietly drift from it.

    `series` is the zero-filled {date, qty} list _daily_series() returns;
    `rows` is that same function's real (non-zero-filled) rows, used only to
    count distinct ISO weeks for the weekly-seasonality decision. Returns
    None on any fit failure (Prophet/cmdstan not usable right now) — the
    caller decides its own fallback; this function never falls back itself,
    so both callers share one failure signal instead of two.

    The four keyword-only parameters are 2026-09 accuracy-optimization audit
    additions, letting a controlled hyperparameter experiment drive real
    fits through the one real production function instead of a second,
    parallel implementation that could drift from it. Every existing caller
    (menu_item_demand_prophet, depletion.refresh_prophet_baselines,
    evaluate_demand_model's own backtest) omits all four and gets
    changepoint_prior_scale=0.05, seasonality_prior_scale=10.0,
    seasonality_mode='additive' (Prophet's own built-in defaults, never
    found to matter in the audit's search) and growth=PROPHET_DEFAULT_GROWTH
    ('flat', not Prophet's stock 'linear' — see that constant's own
    docstring for the measured justification). A caller can still request
    Prophet's stock 'linear' growth explicitly via growth='linear'.
    """
    if not _PROPHET_AVAILABLE:
        return None
    weeks_covered = len({_dt.date.fromisoformat(r["d"]).isocalendar()[:2] for r in rows})
    seasonality_applied = weeks_covered >= MIN_SEASONALITY_WEEKS

    try:
        df = _pd.DataFrame(series).rename(columns={"date": "ds", "qty": "y"})
        df["ds"] = _pd.to_datetime(df["ds"])
        kwargs = {"growth": PROPHET_DEFAULT_GROWTH}
        if changepoint_prior_scale is not None:
            kwargs["changepoint_prior_scale"] = changepoint_prior_scale
        if seasonality_prior_scale is not None:
            kwargs["seasonality_prior_scale"] = seasonality_prior_scale
        if seasonality_mode is not None:
            kwargs["seasonality_mode"] = seasonality_mode
        if growth is not None:
            kwargs["growth"] = growth
        model = _Prophet(daily_seasonality=False, weekly_seasonality=seasonality_applied,
                         yearly_seasonality=False, interval_width=0.8, **kwargs)
        model.fit(df)
        future = model.make_future_dataframe(periods=horizon_days)
        full = model.predict(future)
        fc = full.tail(horizon_days)
        forecast = [
            {"date": row.ds.date().isoformat(), "qty": round(max(0.0, row.yhat), 2),
             "low": round(max(0.0, row.yhat_lower), 2), "high": round(max(0.0, row.yhat_upper), 2)}
            for row in fc.itertuples()
        ]

        # In-sample fit: trend + weekly seasonality evaluated over the days the
        # model was trained on. Plotting it against what actually happened is
        # what makes the forecast line legible — you can see whether the model
        # tracks this item at all before trusting where it points next.
        insample = full.head(len(df))
        fitted = [
            {"date": row.ds.date().isoformat(), "qty": round(max(0.0, row.yhat), 2)}
            for row in insample.itertuples()
        ]
        # Days the model got badly wrong. Residual outliers are a real signal —
        # a day that sold far off its own established pattern — and they are
        # scored here rather than reused from the ledger anomaly pass, which
        # watches stock losses, not sales.
        resid = [p["qty"] - f["qty"] for p, f in zip(series, fitted)]
        n = len(resid)
        mean_r = sum(resid) / n if n else 0.0
        sd = (sum((r - mean_r) ** 2 for r in resid) / n) ** 0.5 if n else 0.0
        outliers = []
        if sd > 0:
            for i, (p, f, r) in enumerate(zip(series, fitted, resid)):
                if abs(r - mean_r) >= RESIDUAL_SIGMA * sd:
                    outliers.append({"index": i, "date": p["date"], "qty": p["qty"],
                                     "expected": f["qty"], "sigma": round(abs(r - mean_r) / sd, 2)})
    except Exception:
        return None

    return {"forecast": forecast, "fitted": fitted, "outliers": outliers,
            "seasonality_applied": seasonality_applied}


EVAL_METHOD_VERSION = "2026-09-rolling-backtest-v1"

# --- rolling backtest configuration -----------------------------------------
# Prophet costs ~1.8s/fit (see depletion.py's refresh_prophet_baselines
# docstring for the measurement) and a proper backtest fits it once per fold
# per item, so both knobs below trade backtest thoroughness for the caller's
# patience. This function is meant to be called on demand for the technical
# panel (api.py caches the result — see _model_details_cache), not on every
# forecast page load, so a slower, thorough run here is the right trade.
ROLLING_MIN_TRAIN_DAYS = 21     # 3 weeks minimum before the first fold is scored
ROLLING_MAX_FOLDS = 4           # most recent N expanding-window folds per item, not all of them
# 2026-10 audit: raised 8 -> 16. All 32 real menu items clear
# ROLLING_MIN_TRAIN_DAYS + horizon_days of real sale-days (items_insufficient_
# history is 0 at every value tried, including 32) -- the earlier cap was a
# request-latency trade-off, not a reflection of real data being scarce.
# Measured on the live DB before raising this: 8 items ~2s/call, 16 items
# ~34s/call, 32 items ~69s/call, with the fleet-level conclusion identical
# at every size (Prophet within ~1-1.5 WAPE pts of the best baseline, never
# ahead) -- so the cap was never hiding a different answer. 16 doubles real
# coverage of the technical panel while staying well inside the 30-minute
# cache TTL (_model_details_cache) and typical request timeouts; 32 was left
# out specifically because a cold cache would make whichever request
# triggers the recompute wait over a minute.
ROLLING_MAX_ITEMS = 16          # items with the most real history, evaluated fully
BASELINE_METHODS = ("naive_previous_period", "trailing_moving_average", "day_of_week_seasonal")
ALL_METHODS = BASELINE_METHODS + ("prophet",)


# --- zero-safe error metrics -------------------------------------------------
# MAPE divides by the actual value, so it is meaningless (or infinite) on a
# day with zero real demand -- exactly the case an intermittent-demand menu
# item produces constantly. It is still computed, over actual>0 days only,
# and reported alongside the metrics below that stay well-defined at zero,
# rather than silently dropped -- see the module's evaluate_demand_model()
# docstring for how each one is meant to be read.
def _mae(actual: list[float], pred: list[float]) -> float:
    return sum(abs(a - p) for a, p in zip(actual, pred)) / len(actual)


def _rmse(actual: list[float], pred: list[float]) -> float:
    return (sum((a - p) ** 2 for a, p in zip(actual, pred)) / len(actual)) ** 0.5


def _wape(actual: list[float], pred: list[float]) -> float | None:
    """Weighted Absolute Percentage Error: total error over total actual
    volume. Well-defined whenever *any* real demand occurred in the window,
    unlike MAPE which fails on every individual zero-demand day."""
    denom = sum(abs(a) for a in actual)
    if denom <= 0:
        return None
    return sum(abs(a - p) for a, p in zip(actual, pred)) / denom * 100


def _smape(actual: list[float], pred: list[float]) -> float | None:
    """Symmetric MAPE: bounded 0-200%, and defined even when actual is zero
    (a zero-actual, zero-forecast day contributes 0 error, not a divide-by-zero)."""
    n = len(actual)
    if n == 0:
        return None
    terms = []
    for a, p in zip(actual, pred):
        denom = abs(a) + abs(p)
        terms.append((2 * abs(a - p) / denom) if denom > 0 else 0.0)
    return sum(terms) / n * 100


def _mape_safe(actual: list[float], pred: list[float]) -> float | None:
    pairs = [(a, p) for a, p in zip(actual, pred) if a > 0]
    if not pairs:
        return None
    return sum(abs(a - p) / a for a, p in pairs) / len(pairs) * 100


def _mase(actual: list[float], pred: list[float], train_qtys: list[float]) -> float | None:
    """Mean Absolute Scaled Error: MAE of this forecast against the MAE a
    naive lag-1 forecast would have made IN-SAMPLE (on the training data
    itself). <1 means "beats a same-day-repeats-yesterday guess", which is a
    meaningful bar even for a low-volume, intermittent series where WAPE
    alone can look dramatic on a single bad day."""
    if len(train_qtys) < 2:
        return None
    naive_errs = [abs(train_qtys[i] - train_qtys[i - 1]) for i in range(1, len(train_qtys))]
    scale = sum(naive_errs) / len(naive_errs)
    if scale <= 0:
        return None
    return _mae(actual, pred) / scale


def _score(actual: list[float], pred: list[float], train_qtys: list[float]) -> dict:
    wape = _wape(actual, pred)
    smape = _smape(actual, pred)
    mape = _mape_safe(actual, pred)
    mase = _mase(actual, pred, train_qtys)
    return {
        "mae": round(_mae(actual, pred), 4),
        "rmse": round(_rmse(actual, pred), 4),
        "wape": round(wape, 2) if wape is not None else None,
        "smape": round(smape, 2) if smape is not None else None,
        "mape": round(mape, 1) if mape is not None else None,
        "mape_note": ("computed over actual>0 days only; omitted on days with zero "
                      "real demand rather than treated as infinite or dropped silently"),
        "mase": round(mase, 3) if mase is not None else None,
    }


# --- baseline forecasters (stdlib only, no fitting) -------------------------
def _naive_forecast(train: list[dict], horizon_days: int) -> list[float]:
    """Repeats the single most recent observed day forward. The floor any
    real model has to clear."""
    last = float(train[-1]["qty"]) if train else 0.0
    return [last] * horizon_days


def _trailing_avg_forecast(train: list[dict], horizon_days: int, window: int = 14) -> list[float]:
    tail = train[-window:] if len(train) >= window else train
    avg = (sum(p["qty"] for p in tail) / len(tail)) if tail else 0.0
    return [avg] * horizon_days


def _seasonal_naive_forecast(train: list[dict], horizon_days: int, last_train_date: _dt.date) -> list[float]:
    """Each future day gets this weekday's historical average from the
    training window (falling back to the overall average for a weekday never
    seen in training)."""
    by_weekday: dict[int, list[float]] = {}
    for p in train:
        wd = _dt.date.fromisoformat(p["date"]).weekday()
        by_weekday.setdefault(wd, []).append(p["qty"])
    overall = (sum(p["qty"] for p in train) / len(train)) if train else 0.0
    out = []
    for i in range(1, horizon_days + 1):
        d = last_train_date + _dt.timedelta(days=i)
        vals = by_weekday.get(d.weekday())
        out.append((sum(vals) / len(vals)) if vals else overall)
    return out


def _rolling_fold_starts(n_days: int, min_train: int, horizon: int, max_folds: int) -> list[int]:
    """Expanding-window fold boundaries: train on [0:start), test on
    [start:start+horizon). Chronological only — never a random split, so a
    fold never trains on data from after the period it is scored on. Capped
    to the most RECENT max_folds (not an even sample of all of them): recent
    performance is what the thesis and the live system both care about, and
    it keeps the Prophet-fit cost in this function bounded regardless of how
    much history an item has."""
    starts = list(range(min_train, n_days - horizon + 1, horizon))
    return starts[-max_folds:] if starts else []


# ===========================================================================
# Level 1 -- per-series adaptive Prophet growth selection (2026-09 adaptive-
# growth pass)
#
# The 2026-09 accuracy-optimization audit picked growth='flat' as a single
# GLOBAL default (PROPHET_DEFAULT_GROWTH, above) from a 133-day dataset,
# where a fitted trend line was noise everywhere it was tried. A full-year
# re-test against the live database (2025-01-01 -> 2026-09-21) found that
# conclusion no longer holds uniformly: growth='linear' now clearly wins for
# this restaurant's highest-volume series (store-wide orders/items/sales,
# and the flagship Unlimited Wings item -- roughly half the WAPE of 'flat'
# on those specifically), while most lower-volume items still prefer or tie
# with 'flat' (25 of 35 series in that re-test). Forcing one global choice
# is therefore now measurably wrong for a real minority of series. This
# section replaces the single global default with a per-series choice, fed
# into _backtest_series()'s existing method-selection machinery as a new
# "Level 1" that runs BEFORE the existing baseline-vs-Prophet "Level 2"
# selection below -- not a second, parallel selection system.
# ===========================================================================
GROWTH_CANDIDATES = ("flat", "linear")
GROWTH_SELECTION_MIN_FOLDS = 2     # need at least 2 independent tuning folds before trusting any WAPE difference at all
GROWTH_SELECTION_MAX_FOLDS = 3     # deliberately fewer than a reporting backtest's own fold count (ROLLING_MAX_FOLDS/a caller's max_folds) -- see select_prophet_growth()'s docstring for why this keeps growth-selection folds and whatever folds a caller reports accuracy on chronologically disjoint
GROWTH_TIE_EPSILON = 1e-6          # a WAPE-point difference below this is treated as a genuine tie (never a real signal, never broken randomly)


def _decide_growth(mean_wape: dict[str, float | None], folds_attempted: int) -> tuple[str, str]:
    """The actual decision table, isolated from fold-scoring/Prophet-fitting
    so it can be tested with hand-picked numbers rather than depending on a
    real Prophet fit producing an exact tie or an exact fold count. Returns
    (selected_growth, reason). Deterministic, never random -- see the six
    named reasons below, checked in this fixed order:

      1. insufficient_tuning_folds   -- fewer than GROWTH_SELECTION_MIN_FOLDS
         usable folds exist (too little history). Conservative default.
      2. no_successful_prophet_fits  -- neither growth ever fit successfully
         across every fold tried (Prophet unavailable, or every fit failed).
      3. linear_fit_failed_every_fold / flat_fit_failed_every_fold -- only
         one growth ever produced a usable fit; that one wins by default,
         reported symmetrically (a genuine fit failure is evidence, not an
         automatic vote for 'flat').
      4. exact_tie_prefers_flat      -- both fit, and their mean tuning WAPE
         differs by less than GROWTH_TIE_EPSILON.
      5. lower_tuning_wape           -- otherwise, whichever mean tuning WAPE
         is lower wins.
    """
    if folds_attempted < GROWTH_SELECTION_MIN_FOLDS:
        return PROPHET_DEFAULT_GROWTH, "insufficient_tuning_folds"
    flat_w, linear_w = mean_wape.get("flat"), mean_wape.get("linear")
    if flat_w is None and linear_w is None:
        return PROPHET_DEFAULT_GROWTH, "no_successful_prophet_fits"
    if linear_w is None:
        return "flat", "linear_fit_failed_every_fold"
    if flat_w is None:
        return "linear", "flat_fit_failed_every_fold"
    if abs(flat_w - linear_w) < GROWTH_TIE_EPSILON:
        return "flat", "exact_tie_prefers_flat"
    return ("flat" if flat_w < linear_w else "linear"), "lower_tuning_wape"


def select_prophet_growth(rows: list[dict], series: list[dict], *,
                          horizon: int = 7, min_train: int = ROLLING_MIN_TRAIN_DAYS,
                          max_folds: int = GROWTH_SELECTION_MAX_FOLDS) -> dict:
    """Per-series Level-1 decision: does THIS series' own recent history
    favor Prophet growth='flat' or growth='linear'? Always returns a usable
    decision plus a `reason` -- never raises, never leaves a caller without
    a growth string to fit with.

    LEAKAGE: uses only chronological expanding-window folds carved from the
    `series`/`rows` it is given (_rolling_fold_starts, unmodified, same
    mechanism _backtest_series() itself uses for its own folds). It is the
    CALLER's responsibility to pass a slice that excludes whatever period
    will later be evaluated or forecast:
      - _backtest_series() passes series[:fold_starts[0]] -- everything
        strictly before the first fold IT is about to report accuracy on,
        so those reported folds can never have influenced their own
        model's growth choice.
      - refresh_prophet_baselines() (live replenishment) passes the item's
        full available history -- there is no held-out period to protect
        there, because the forecast this selection feeds targets genuinely
        future days that no fold here, or anywhere, has ever seen.

    See _decide_growth() for the exact, deterministic fallback/tie rules.
    """
    fold_starts = _rolling_fold_starts(len(series), min_train, horizon, max_folds)
    wape: dict[str, list[float]] = {g: [] for g in GROWTH_CANDIDATES}
    folds_attempted = 0
    for start in fold_starts:
        train = series[:start]
        test = series[start:start + horizon]
        if len(test) < horizon:
            continue
        test_start_date = test[0]["date"]
        train_rows = [r for r in rows if r["d"] < test_start_date]
        if len(train_rows) < MIN_DEMAND_SAMPLE_DAYS:
            continue
        folds_attempted += 1
        actual = [p["qty"] for p in test]
        for g in GROWTH_CANDIDATES:
            fitted = fit_prophet(train, train_rows, horizon, growth=g)
            if fitted is None:
                continue
            pred = [p["qty"] for p in fitted["forecast"]]
            w = _wape(actual, pred)
            if w is not None:
                wape[g].append(w)

    folds_scored = {g: len(v) for g, v in wape.items()}
    mean_wape = {g: (round(sum(v) / len(v), 2) if v else None) for g, v in wape.items()}
    selected_growth, reason = _decide_growth(mean_wape, folds_attempted)

    return {
        "selected_growth": selected_growth,
        "reason": reason,
        "is_fallback": reason in ("insufficient_tuning_folds", "no_successful_prophet_fits"),
        "folds_attempted": folds_attempted,
        "folds_scored": folds_scored,
        "mean_wape": mean_wape,
        "horizon": horizon,
        "min_train": min_train,
        "max_folds": max_folds,
        "tuning_end_date": series[-1]["date"] if series else None,
    }


# ===========================================================================
# Live growth-selection cache -- shared by every LIVE Prophet-fitting path
# (menu_item_demand_prophet, store_demand_prophet, and, via
# depletion.refresh_prophet_baselines(), ingredient-level replenishment) so
# a per-item/per-metric growth decision is made ONCE by the batch refresh
# and reused everywhere, rather than each live path either re-tuning inline
# (too slow -- see refresh_prophet_baselines()'s own ~1.8s/fit measurement,
# and select_prophet_growth() needs up to 2x that many fits again) or
# silently keeping the old global PROPHET_DEFAULT_GROWTH forever (the
# inconsistency this cache exists to close). A cache miss or a stale entry
# both fall back to PROPHET_DEFAULT_GROWTH -- the same conservative
# behavior _prophet_daily_for_item() already uses for the Prophet FIT cache
# in depletion.py; this is that same pattern, one level up, for the growth
# DECISION rather than the fit result.
#
# Deliberately NOT keyed by horizon_days: growth is a structural choice
# (does this series have a real, estimable trend, or not) decided once from
# whatever horizon the batch refresh actually used (production replenishment's
# own horizon), and reused for whatever horizon a live detail-page request
# happens to ask for. Making this horizon-specific would need the batch
# refresh to run once per distinct horizon requested live, which no caller
# of this cache currently needs.
# ===========================================================================
GROWTH_SELECTION_STALE_AFTER_SECONDS = 48 * 3600   # matches depletion.PROPHET_STALE_AFTER_SECONDS
_growth_selection_store: dict[tuple, tuple[float, dict]] = {}
_growth_selection_lock = threading.Lock()


def _scope_key(conn: sqlite3.Connection) -> str:
    """Mirrors depletion._prophet_scope_key() exactly -- same PRAGMA
    database_list technique, for the same thread/connection-reuse safety
    reasoning documented there. Duplicated rather than imported the other
    direction because forecasting.py is the lower-level module depletion.py
    already depends on, never the reverse."""
    return conn.execute("PRAGMA database_list").fetchone()[2] or "memory"


def cache_growth_selection(conn: sqlite3.Connection, series_key: str, selection: dict) -> None:
    """Called by whatever batch job just computed a fresh growth decision
    for this series (today, only depletion.refresh_prophet_baselines()) --
    never by a live request."""
    with _growth_selection_lock:
        _growth_selection_store[(_scope_key(conn), series_key)] = (time.monotonic(), selection)


def cached_growth_for(conn: sqlite3.Connection, series_key: str) -> dict | None:
    """Whatever the last refresh cached for this series, or None if it was
    never cached or has gone stale. Callers must treat None as "use
    PROPHET_DEFAULT_GROWTH" -- never fit an ad-hoc selection inline here."""
    with _growth_selection_lock:
        hit = _growth_selection_store.get((_scope_key(conn), series_key))
    if hit is None:
        return None
    computed_at, selection = hit
    if time.monotonic() - computed_at > GROWTH_SELECTION_STALE_AFTER_SECONDS:
        return None
    return selection


def _backtest_series(rows: list[dict], series: list[dict],
                     horizon_days: int, min_train: int, max_folds: int,
                     *, prophet_growth: str | None = None) -> dict | None:
    """The entity-agnostic core of a chronological rolling backtest: every
    method in ALL_METHODS, scored across the same expanding-window folds,
    with per-fold and fold-averaged scores plus a WAPE-based selection.
    Shared by _backtest_item (one menu item) and _backtest_store_metric (the
    store's own aggregate orders/items/sales series) so the backtest
    mechanics exist exactly once regardless of what series is being scored.

    `rows` is the real (non-zero-filled) day rows for this series across its
    whole available history — used only to decide, per fold, whether enough
    real observations exist for Prophet and for weekly-seasonality; `series`
    is the zero-filled daily series used for every method's actual
    arithmetic. Returns None when there isn't enough history for even one
    fold — the caller decides how to report that (it needs entity-specific
    fields the core result does not carry).

    `prophet_growth`: the Level-1 growth choice (see select_prophet_growth())
    to use for every fold's 'prophet' method here. None (the default) means
    THIS function selects it itself, using only the data strictly before the
    first fold this function is about to report accuracy on -- so those
    reported folds can never have influenced their own model's growth
    choice. A caller that already made this decision elsewhere (e.g.
    depletion.py's cached live selection) may pass it directly to skip
    re-deriving it."""
    # `series` is zero-filled across the whole calendar lookback (see
    # _daily_series), so its length alone cannot distinguish "365 days of
    # real trading" from "3 real observations padded out to 365 zeros" -- a
    # series needs real, distinct non-zero days too, matching the same
    # MIN_DEMAND_SAMPLE_DAYS bar menu_item_demand()/menu_item_demand_prophet()
    # already use for "insufficient data".
    fold_starts = _rolling_fold_starts(len(series), min_train, horizon_days, max_folds)
    if not fold_starts or len(rows) < MIN_DEMAND_SAMPLE_DAYS:
        return None

    growth_selection = None
    if prophet_growth is None:
        # Level 1, leakage-free: derive this series' growth choice using
        # ONLY series[:tuning_cutoff] / rows before that date -- strictly
        # before fold_starts[0], the earliest fold Level 2 below reports
        # accuracy on. See select_prophet_growth()'s own leakage note and
        # TestProphetGrowthSelectionLeakage for the regression test that
        # would catch this boundary moving.
        tuning_cutoff = fold_starts[0]
        tuning_series = series[:tuning_cutoff]
        tuning_cutoff_date = series[tuning_cutoff]["date"]
        tuning_rows = [r for r in rows if r["d"] < tuning_cutoff_date]
        growth_selection = select_prophet_growth(tuning_rows, tuning_series,
                                                  horizon=horizon_days, min_train=min_train)
        prophet_growth = growth_selection["selected_growth"]

    by_method: dict[str, list[dict]] = {m: [] for m in ALL_METHODS}
    folds_meta = []
    for start in fold_starts:
        train = series[:start]
        test = series[start:start + horizon_days]
        actual = [p["qty"] for p in test]
        train_qtys = [p["qty"] for p in train]
        last_train_date = _dt.date.fromisoformat(train[-1]["date"])
        test_start_date = test[0]["date"]

        preds = {
            "naive_previous_period": _naive_forecast(train, horizon_days),
            "trailing_moving_average": _trailing_avg_forecast(train, horizon_days),
            "day_of_week_seasonal": _seasonal_naive_forecast(train, horizon_days, last_train_date),
        }

        train_rows = [r for r in rows if r["d"] < test_start_date]
        if _PROPHET_AVAILABLE and len(train_rows) >= MIN_DEMAND_SAMPLE_DAYS:
            fitted = fit_prophet(train, train_rows, horizon_days, growth=prophet_growth)
            if fitted is not None:
                preds["prophet"] = [p["qty"] for p in fitted["forecast"]]

        folds_meta.append({"train_days": len(train), "test_start": test_start_date,
                           "test_end": test[-1]["date"]})
        for method, pred in preds.items():
            by_method[method].append(_score(actual, pred, train_qtys))

    def _avg(key: str, scores: list[dict]) -> float | None:
        vals = [s[key] for s in scores if s[key] is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    per_method = {}
    for method, scores in by_method.items():
        if not scores:
            per_method[method] = {"folds_scored": 0}
            continue
        per_method[method] = {
            "folds_scored": len(scores),
            "mae": _avg("mae", scores), "rmse": _avg("rmse", scores),
            "wape": _avg("wape", scores), "smape": _avg("smape", scores),
            "mape": _avg("mape", scores), "mase": _avg("mase", scores),
        }

    # Model selection: lowest mean WAPE wins (well-defined whenever any real
    # demand occurred; falls back to MAE for the pathological all-zero case).
    # Never forces Prophet -- a baseline that scores better IS the selection.
    def _rank_key(item):
        _, m = item
        if m.get("folds_scored", 0) == 0:
            return (2, 0.0)
        primary = m.get("wape")
        return (0, primary) if primary is not None else (1, m.get("mae", 1e9))

    selected_method, selected_scores = min(per_method.items(), key=_rank_key)

    return {
        "days_available": len(series), "folds": folds_meta,
        "methods": per_method,
        "selected_method": selected_method,
        "selection_reason": (f"lowest mean WAPE across {selected_scores.get('folds_scored', 0)} "
                             f"rolling backtest fold(s)" if selected_scores.get("wape") is not None
                             else "no method produced a defined WAPE (no real demand in any test "
                                  "fold); selected by lowest MAE instead"),
        "prophet_growth": prophet_growth,
        "growth_selection": growth_selection,
    }


# ===========================================================================
# Level 2, live: turning a validated selected_method into an actual forecast
# -- 2026-09 second adaptive-growth pass, production-integration follow-up.
#
# _backtest_series() above answers "which method should this series use" by
# scoring all four candidates on historical rolling-backtest folds. It never
# produces a live, forward-looking forecast itself (every fold's "forecast"
# is scored against already-known history, not future days). This function
# is the other half: given a method _backtest_series() already selected,
# apply that SAME method to the FULL available series to get the real
# forecast for genuinely future days -- the step depletion.
# refresh_prophet_baselines() needs to turn Level 2's selection into
# something build_demand() can actually consume.
# ===========================================================================
def forecast_with_method(series: list[dict], rows: list[dict], horizon_days: int,
                         method: str, *, growth: str | None = None) -> list[dict] | None:
    """Forward forecast for `horizon_days` days past series[-1], using
    exactly the named method (one of ALL_METHODS) -- no fitting/tuning here,
    this reuses the SAME baseline forecaster functions/fit_prophet()
    _backtest_series() already scored `method` with, just applied to the
    whole series instead of one fold's training slice.

    Returns the same shape fit_prophet()['forecast'] already uses --
    [{date, qty, low, high}, ...] -- so every caller downstream (build_demand)
    handles a Prophet-sourced or baseline-sourced forecast identically. The
    three baseline methods have no native uncertainty model, so low=high=qty
    for those (a documented point forecast, not a missing value) -- Prophet
    is the only method here with a real interval.

    Returns None only when method='prophet' and the fit itself fails (empty/
    insufficient series, or a transient cmdstan failure) -- callers already
    treat a None forecast as "fall back to EWMA for this item", the same
    fallback 'prophet'-only mode has always used.
    """
    if not series:
        return None
    if method == "prophet":
        fitted = fit_prophet(series, rows, horizon_days, growth=growth or PROPHET_DEFAULT_GROWTH)
        return fitted["forecast"] if fitted is not None else None
    if method not in BASELINE_METHODS:
        raise ValueError(f"unknown forecasting method {method!r}; expected one of {ALL_METHODS}")

    last_date = _dt.date.fromisoformat(series[-1]["date"])
    if method == "naive_previous_period":
        values = _naive_forecast(series, horizon_days)
    elif method == "trailing_moving_average":
        values = _trailing_avg_forecast(series, horizon_days)
    else:  # day_of_week_seasonal
        values = _seasonal_naive_forecast(series, horizon_days, last_date)

    out = []
    for i, v in enumerate(values, start=1):
        d = last_date + _dt.timedelta(days=i)
        q = round(max(0.0, v), 2)
        out.append({"date": d.isoformat(), "qty": q, "low": q, "high": q})
    return out


def _backtest_item(item_id: int, item_name: str, rows: list[dict], series: list[dict],
                   horizon_days: int, min_train: int, max_folds: int) -> dict:
    """One menu item's rolling backtest — see _backtest_series for the actual
    mechanics. Wraps the entity-agnostic core with the item_id/item_name/
    insufficient_history/real_sale_days shape evaluate_demand_model's callers
    (api.py, the System panel, and this module's own tests) already expect."""
    core = _backtest_series(rows, series, horizon_days, min_train, max_folds)
    if core is None:
        return {"item_id": item_id, "item_name": item_name, "insufficient_history": True,
                "days_available": len(series), "real_sale_days": len(rows),
                "required_min_train_days": min_train + horizon_days,
                "required_real_sale_days": MIN_DEMAND_SAMPLE_DAYS}
    return {"item_id": item_id, "item_name": item_name, "insufficient_history": False, **core}


def evaluate_demand_model(conn: sqlite3.Connection, horizon_days: int = 7,
                          now: _dt.datetime | None = None) -> dict:
    """A real chronological rolling backtest across multiple menu items,
    scoring Prophet against three simple baselines on data none of them saw
    while fitting -- not a single 83/7 split on one representative item.

    Method (see the module-level constants above for the exact knobs):
      - up to ROLLING_MAX_ITEMS menu items with the most real sales history
        are evaluated (an item below ROLLING_MIN_TRAIN_DAYS + horizon_days
        of real sale-days is reported as insufficient_history, never
        skipped silently).
      - per item, an EXPANDING window walks forward: train on everything up
        to fold start, forecast horizon_days, score against what actually
        happened, then move the fold start forward by horizon_days and
        repeat -- the most recent ROLLING_MAX_FOLDS such folds are kept.
      - every fold is scored for naive-previous-period, trailing-moving-
        average, day-of-week-seasonal, AND Prophet (when available and the
        fold's training data supports a fit), using MAE/RMSE/WAPE/sMAPE/
        MAPE(zero-safe)/MASE.
      - the method with the lowest mean WAPE is recorded as "selected" for
        that item -- this can be, and often is, a baseline. Prophet is
        never assumed superior; see fleet_summary.prophet_vs_baseline for
        whether it measurably is on THIS data.

    `now`: passed straight to effective_now() -- omitted (the default) by
    every real caller, which anchors to the dead-tail-aware "now" (see that
    function's own section) rather than blindly trusting the live wall
    clock. A test may pin an exact instant the same way _daily_series()
    already allows.

    Backs the technical panel under System Check (api.py caches this --
    see _model_details_cache -- since a full rolling backtest with Prophet
    fits is too slow to redo on every page load).
    """
    items = conn.execute("SELECT id, name FROM menu_item WHERE pos_visible=1").fetchall()
    if not items:
        return {"available": False, "reason": "no menu items", "method_version": EVAL_METHOD_VERSION}

    anchor = effective_now(conn, now=now)

    # Full available history per item (not capped at 90 days -- more folds
    # is a more convincing backtest), then keep the ROLLING_MAX_ITEMS with
    # the most real sale-days: those are the only ones with enough history
    # for a multi-fold backtest to say anything, and evaluating every menu
    # item with a full Prophet-per-fold pass would not finish in a request
    # a person is waiting on.
    LOOKBACK_FOR_BACKTEST = 365
    candidates = []
    for it in items:
        rows, series = _daily_series(conn, it["id"], LOOKBACK_FOR_BACKTEST, now=anchor)
        candidates.append((it, rows, series))
    candidates.sort(key=lambda c: len(c[1]), reverse=True)

    evaluated_dates_min = evaluated_dates_max = None
    per_item_results = []
    excluded = []
    for it, rows, series in candidates[:ROLLING_MAX_ITEMS]:
        result = _backtest_item(it["id"], it["name"], rows, series,
                                horizon_days, ROLLING_MIN_TRAIN_DAYS, ROLLING_MAX_FOLDS)
        per_item_results.append(result)
        if not result["insufficient_history"]:
            item_dates = [p["date"] for p in series]
            evaluated_dates_min = min(evaluated_dates_min or item_dates[0], item_dates[0])
            evaluated_dates_max = max(evaluated_dates_max or item_dates[-1], item_dates[-1])
    for it, rows, series in candidates[ROLLING_MAX_ITEMS:]:
        excluded.append({"item_id": it["id"], "item_name": it["name"],
                         "reason": f"not among the {ROLLING_MAX_ITEMS} items with the most "
                                   f"real sales history (has {len(rows)} sale-days)"})

    scored_items = [r for r in per_item_results if not r["insufficient_history"]]
    if not scored_items:
        return {
            "available": False,
            "reason": "not enough real sales history yet for a rolling backtest on any item",
            "method_version": EVAL_METHOD_VERSION,
            "items": per_item_results,
        }

    # Fleet-wide summary: mean of each method's mean WAPE/MAE/RMSE across
    # every item that could be scored for it, plus how many items actually
    # selected each method -- this is what answers "is Prophet actually
    # better here, in general" rather than for one cherry-picked item.
    fleet_by_method: dict[str, dict] = {}
    for method in ALL_METHODS:
        wapes = [r["methods"][method]["wape"] for r in scored_items
                if r["methods"].get(method, {}).get("wape") is not None]
        maes = [r["methods"][method]["mae"] for r in scored_items
               if r["methods"].get(method, {}).get("mae") is not None]
        fleet_by_method[method] = {
            "items_scored": len(maes),
            "mean_wape": round(sum(wapes) / len(wapes), 2) if wapes else None,
            "mean_mae": round(sum(maes) / len(maes), 4) if maes else None,
        }

    selection_counts: dict[str, int] = {}
    for r in scored_items:
        selection_counts[r["selected_method"]] = selection_counts.get(r["selected_method"], 0) + 1

    # Level-1 growth-selection tally, alongside Level 2's selection_counts
    # above -- "how many series picked flat/linear/fell back" for the
    # per-item growth_selection field each result already carries via
    # _backtest_series().
    growth_selection_counts: dict[str, int] = {"flat": 0, "linear": 0, "fallback": 0}
    for r in scored_items:
        gs = r.get("growth_selection")
        if gs is None:
            continue
        if gs.get("is_fallback"):
            growth_selection_counts["fallback"] += 1
        else:
            growth_selection_counts[gs["selected_growth"]] = growth_selection_counts.get(gs["selected_growth"], 0) + 1

    baseline_best_wape = min(
        (fleet_by_method[m]["mean_wape"] for m in BASELINE_METHODS if fleet_by_method[m]["mean_wape"] is not None),
        default=None,
    )
    prophet_wape = fleet_by_method["prophet"]["mean_wape"]
    if baseline_best_wape is not None and prophet_wape is not None:
        abs_improvement = baseline_best_wape - prophet_wape
        pct_improvement = (abs_improvement / baseline_best_wape * 100) if baseline_best_wape > 0 else None
        prophet_vs_baseline = {
            "best_baseline_mean_wape": baseline_best_wape,
            "prophet_mean_wape": prophet_wape,
            "absolute_improvement_wape_pts": round(abs_improvement, 2),
            "percentage_improvement": round(pct_improvement, 1) if pct_improvement is not None else None,
            "prophet_actually_better": abs_improvement > 0,
            "note": ("Positive improvement means Prophet's fleet-mean WAPE across the rolling "
                     "backtest folds is lower than the best baseline's. A negative value is "
                     "reported as-is -- it means the baseline measurably won on this data."),
        }
    else:
        prophet_vs_baseline = {"available": False,
                               "reason": "not enough scored folds for both Prophet and a baseline "
                                         "to compare on the same items"}

    return {
        "available": True,
        "method_version": EVAL_METHOD_VERSION,
        "methodology": ("Chronological expanding-window rolling backtest, never a random split. "
                        f"Up to {ROLLING_MAX_ITEMS} items with the most real sales history, up to "
                        f"{ROLLING_MAX_FOLDS} most-recent folds each, {horizon_days}-day forecast "
                        f"horizon per fold, minimum {ROLLING_MIN_TRAIN_DAYS}-day initial training "
                        "window. Every fold scored for naive-previous-period, trailing-moving-"
                        "average, day-of-week-seasonal, and Prophet."),
        "effective_now": timeutil.business_today(anchor).isoformat(),
        "horizon_days": horizon_days,
        "min_train_days": ROLLING_MIN_TRAIN_DAYS,
        "max_folds_per_item": ROLLING_MAX_FOLDS,
        "items_considered": len(items),
        "items_evaluated": len(scored_items),
        "items_insufficient_history": sum(1 for r in per_item_results if r["insufficient_history"]),
        "items_excluded_for_capacity": excluded,
        "evaluated_date_range": ({"from": evaluated_dates_min, "to": evaluated_dates_max}
                                 if evaluated_dates_min else None),
        "items": per_item_results,
        "fleet_summary": fleet_by_method,
        "selection_counts": selection_counts,
        "growth_selection_counts": growth_selection_counts,
        "prophet_vs_baseline": prophet_vs_baseline,
    }


# ===========================================================================
# Store-level demand (2026-09 Prophet data-integration audit)
#
# Everything above this point is scoped per menu item (or, in depletion.py,
# per ingredient). Nothing in this module previously answered the aggregate
# question a store-wide demand plan actually needs first: how many orders,
# how many items, how much peso volume is the WHOLE store likely to do
# tomorrow -- not "how many Spicy Korean Wing Bowls". This section adds
# exactly that, reusing every piece of machinery above (fit_prophet, the
# baseline forecasters, _backtest_series, the zero-safe metrics) completely
# unmodified -- a store-wide series is just another {date, qty} series in
# the same shape _daily_series() already produces per item.
# ===========================================================================
STORE_METRICS = ("orders", "items", "sales")
STORE_METRIC_LABELS = {
    "orders": "Daily order count",
    "items": "Daily items sold",
    "sales": "Daily gross sales (PHP)",
}
STORE_LOOKBACK_FOR_BACKTEST = 365  # matches evaluate_demand_model's per-item lookback


def _store_daily_series(conn: sqlite3.Connection, lookback_days: int,
                        now: _dt.datetime | None = None
                        ) -> dict[str, tuple[list[dict], list[dict]]]:
    """Store-wide daily {orders, items sold, gross sales}, aggregated across
    every menu item rather than one at a time -- the same real
    customer_order/order_line tables every other series in this module reads.
    Zero-filled across the full calendar window exactly like _daily_series():
    a day the shop opened but took few orders is a real low number, and a
    calendar day with no matching customer_order row at all is a real zero,
    not a gap -- "the store did zero recorded business this day" is itself
    part of what a store-level demand forecast needs to see.

    Returns {metric: (rows, series)} for metric in STORE_METRICS, in exactly
    the shape fit_prophet() / the baseline forecasters / _backtest_series()
    already expect, so every one of those functions works on the store
    aggregate completely unmodified -- no second implementation of Prophet
    fitting, backtesting, or scoring for the store-level series.

    `now`: see _daily_series()'s docstring -- the same optional reference-
    date override, omitted by every live caller.
    """
    # 2026-10 leakage audit: same upper-bound fix as _daily_series(), same
    # reasoning -- latent only (no current caller passes a historical
    # `now` this far), closed on the same principle.
    bd = timeutil.sql_business_date("o.created_at")
    upper = " AND o.created_at < ?" if now is not None else ""
    params = [timeutil.utc_bound_n_manila_days_ago(lookback_days, now)]
    if now is not None:
        params.append(timeutil.utc_upper_bound_sql(now))
    raw = conn.execute(
        f"""
        SELECT {bd} AS d, COUNT(DISTINCT o.id) AS orders,
               COALESCE(SUM(ol.qty), 0) AS items,
               COALESCE(SUM(ol.qty * ol.unit_price), 0) AS sales
          FROM customer_order o
          JOIN order_line ol ON ol.order_id = o.id
         WHERE o.status != 'void' AND o.created_at >= ?{upper}
         GROUP BY d
        """,
        params,
    ).fetchall()
    by_day = {r["d"]: r for r in raw}
    today = timeutil.business_today(now)

    out: dict[str, tuple[list[dict], list[dict]]] = {}
    for metric in STORE_METRICS:
        rows_m = [{"d": r["d"], "qty": float(r[metric])} for r in raw if float(r[metric]) > 0]
        series_m = []
        for i in range(lookback_days - 1, -1, -1):
            d = today - _dt.timedelta(days=i)
            r = by_day.get(d.isoformat())
            series_m.append({"date": d.isoformat(), "qty": float(r[metric]) if r else 0.0})
        out[metric] = (rows_m, series_m)
    return out


def store_demand_prophet(conn: sqlite3.Connection, metric: str = "items",
                         horizon_days: int = 7, lookback_days: int = 90) -> dict:
    """Store-wide demand for one metric (orders/items/sales): the real daily
    series plus a Prophet forecast -- the store-level analogue of
    menu_item_demand_prophet(), sharing its fit/fallback contract exactly
    (fit_prophet() is the identical function, called on the store's own
    series instead of one item's). Falls back to the trailing-average method
    on any Prophet/cmdstan failure, same as every other caller of
    fit_prophet() in this module.

    Growth: reads whatever growth was last cached for this metric via
    cache_growth_selection() under series_key f"store:{metric}" -- see
    menu_item_demand_prophet()'s identical note and cached_growth_for()'s
    own docstring. Falls back to PROPHET_DEFAULT_GROWTH on a cache miss,
    never re-tunes inline.

    Window: anchored to effective_now(), same reasoning and same scope
    boundary as menu_item_demand_prophet()'s identical note."""
    if metric not in STORE_METRICS:
        raise ValueError(f"unknown store metric {metric!r}; expected one of {STORE_METRICS}")
    anchor = effective_now(conn)
    rows, series = _store_daily_series(conn, lookback_days, now=anchor)[metric]
    active_days = len(rows)
    total_qty = sum(r["qty"] for r in rows)
    label = STORE_METRIC_LABELS[metric]

    if active_days < MIN_DEMAND_SAMPLE_DAYS:
        return {
            "metric": metric, "label": label, "insufficient_data": True,
            "active_days": active_days, "required_days": MIN_DEMAND_SAMPLE_DAYS,
            "series": series, "forecast": [], "method": "prophet",
        }

    trailing = series[-14:]
    trailing_avg = sum(p["qty"] for p in trailing) / len(trailing)
    cached_growth = cached_growth_for(conn, f"store:{metric}")
    growth = cached_growth["selected_growth"] if cached_growth else PROPHET_DEFAULT_GROWTH
    fitted_model = fit_prophet(series, rows, horizon_days, growth=growth)

    if fitted_model is None:
        today = timeutil.business_today(anchor)
        flat = _trailing_avg_forecast(series, horizon_days)
        return {
            "metric": metric, "label": label, "insufficient_data": False,
            "active_days": active_days, "total_qty_in_window": round(total_qty, 2),
            "trailing_avg_daily": round(trailing_avg, 2), "series": series,
            "forecast": [{"date": (today + _dt.timedelta(days=i)).isoformat(),
                         "qty": round(flat[i - 1], 2)} for i in range(1, horizon_days + 1)],
            "method": "trailing_average", "prophet_unavailable": True,
        }

    return {
        "metric": metric, "label": label, "insufficient_data": False,
        "active_days": active_days, "total_qty_in_window": round(total_qty, 2),
        "trailing_avg_daily": round(trailing_avg, 2),
        "seasonality_applied": fitted_model["seasonality_applied"],
        "series": series, "forecast": fitted_model["forecast"],
        "fitted": fitted_model["fitted"], "outliers": fitted_model["outliers"],
        "method": "prophet",
        "growth_used": growth,
        "growth_source": "cached_adaptive" if cached_growth else "default_not_yet_refreshed",
    }


def _backtest_store_metric(metric: str, rows: list[dict], series: list[dict],
                           horizon_days: int, min_train: int, max_folds: int) -> dict:
    """One store-wide metric's rolling backtest -- see _backtest_series() for
    the shared mechanics. Mirrors _backtest_item()'s wrapper shape with
    metric/real_days in place of item_id/item_name/real_sale_days."""
    core = _backtest_series(rows, series, horizon_days, min_train, max_folds)
    if core is None:
        return {"metric": metric, "label": STORE_METRIC_LABELS[metric], "insufficient_history": True,
                "days_available": len(series), "real_days": len(rows),
                "required_min_train_days": min_train + horizon_days,
                "required_real_days": MIN_DEMAND_SAMPLE_DAYS}
    return {"metric": metric, "label": STORE_METRIC_LABELS[metric],
           "insufficient_history": False, **core}


def evaluate_store_demand_model(conn: sqlite3.Connection, horizon_days: int = 7,
                                now: _dt.datetime | None = None) -> dict:
    """The same chronological rolling-backtest methodology as
    evaluate_demand_model(), applied to the store's own aggregate daily
    series (orders taken, items sold, gross sales) instead of one menu item
    at a time -- see that function's docstring for the full methodology,
    shared unchanged via _backtest_series().

    `now`: see evaluate_demand_model()'s identical parameter -- anchors to
    effective_now() by default.

    Backs the technical panel's store-level demand card and the ML
    integration audit -- this is the evidence for whether Prophet has
    actually earned a role at the store level, never assumed. Prophet is
    never forced to win here any more than it is per item: a baseline
    scoring lower WAPE on the store's own aggregate series IS the selection.
    """
    anchor = effective_now(conn, now=now)
    per_metric = _store_daily_series(conn, STORE_LOOKBACK_FOR_BACKTEST, now=anchor)
    results = {
        metric: _backtest_store_metric(metric, rows, series, horizon_days,
                                       ROLLING_MIN_TRAIN_DAYS, ROLLING_MAX_FOLDS)
        for metric, (rows, series) in per_metric.items()
    }
    scored = {m: r for m, r in results.items() if not r["insufficient_history"]}
    if not scored:
        return {
            "available": False,
            "reason": "not enough real store-level history yet for a rolling backtest",
            "method_version": EVAL_METHOD_VERSION,
            "metrics": results,
        }

    selection_counts: dict[str, int] = {}
    for r in scored.values():
        selection_counts[r["selected_method"]] = selection_counts.get(r["selected_method"], 0) + 1

    growth_selection_counts: dict[str, int] = {"flat": 0, "linear": 0, "fallback": 0}
    for r in scored.values():
        gs = r.get("growth_selection")
        if gs is None:
            continue
        if gs.get("is_fallback"):
            growth_selection_counts["fallback"] += 1
        else:
            growth_selection_counts[gs["selected_growth"]] = growth_selection_counts.get(gs["selected_growth"], 0) + 1

    return {
        "available": True,
        "method_version": EVAL_METHOD_VERSION,
        "methodology": ("Chronological expanding-window rolling backtest -- identical methodology "
                        "to the menu-item backtest above, applied to the store's own aggregate daily "
                        f"series instead. Up to {ROLLING_MAX_FOLDS} most-recent folds, {horizon_days}-"
                        f"day forecast horizon per fold, minimum {ROLLING_MIN_TRAIN_DAYS}-day initial "
                        "training window. Every fold scored for naive-previous-period, trailing-"
                        "moving-average, day-of-week-seasonal, and Prophet."),
        "effective_now": timeutil.business_today(anchor).isoformat(),
        "horizon_days": horizon_days,
        "min_train_days": ROLLING_MIN_TRAIN_DAYS,
        "max_folds": ROLLING_MAX_FOLDS,
        "metrics_evaluated": len(scored),
        "metrics_insufficient_history": sum(1 for r in results.values() if r["insufficient_history"]),
        "metrics": results,
        "selection_counts": selection_counts,
        "growth_selection_counts": growth_selection_counts,
    }


# ===========================================================================
# Forecastability -- descriptive evidence for whether a series is worth
# forecasting at all, computed once here for the technical panel and the ML
# audit rather than eyeballed per series. Never used to gate the live
# forecast paths above (those keep their own unchanged MIN_DEMAND_SAMPLE_DAYS
# checks) -- this is an additive reporting layer, not a behavior change.
# ===========================================================================
INTERMITTENCY_ZERO_PCT = 30.0   # zero-demand share at/above which a series is called intermittent
HIGH_CV_THRESHOLD = 1.0         # coefficient of variation at/above which demand is called volatile


def series_forecastability(rows: list[dict], series: list[dict],
                           min_sample_days: int = MIN_DEMAND_SAMPLE_DAYS) -> dict:
    """Descriptive statistics plus an explicit forecastability verdict for
    any {date, qty} series in the shape _daily_series()/_store_daily_series()
    produce: observation counts, zero-demand share, mean/std/coefficient of
    variation, weekly-seasonality and trend evidence, intermittency, and a
    forecastability label ('insufficient_data' / 'intermittent' / 'volatile'
    / 'forecastable') plus prophet_eligible -- the same MIN_DEMAND_SAMPLE_DAYS
    bar the live Prophet paths already use, reported explicitly here rather
    than left implicit in each call site."""
    n = len(series)
    non_zero = len(rows)
    qtys = [p["qty"] for p in series]
    mean = (sum(qtys) / n) if n else 0.0
    variance = (sum((q - mean) ** 2 for q in qtys) / n) if n else 0.0
    std = variance ** 0.5
    cv = (std / mean) if mean > 0 else None
    zero_pct = round((1 - non_zero / n) * 100, 1) if n else 100.0

    weeks_covered = len({_dt.date.fromisoformat(r["d"]).isocalendar()[:2] for r in rows}) if rows else 0
    weekly_seasonality_evidence = weeks_covered >= MIN_SEASONALITY_WEEKS

    half = n // 2
    trend_evidence_pct = None
    if half >= 7 and (n - half) >= 7:
        first_half_mean = sum(p["qty"] for p in series[:half]) / half
        second_half_mean = sum(p["qty"] for p in series[half:]) / (n - half)
        if first_half_mean > 0:
            trend_evidence_pct = round((second_half_mean - first_half_mean) / first_half_mean * 100, 1)

    intermittent = non_zero >= min_sample_days and zero_pct >= INTERMITTENCY_ZERO_PCT
    if non_zero < min_sample_days:
        forecastability = "insufficient_data"
    elif intermittent:
        forecastability = "intermittent"
    elif cv is not None and cv >= HIGH_CV_THRESHOLD:
        forecastability = "volatile"
    else:
        forecastability = "forecastable"

    return {
        "observations": n,
        "non_zero_observations": non_zero,
        "zero_pct": zero_pct,
        "history_days": n,
        "date_span": {"from": series[0]["date"], "to": series[-1]["date"]} if series else None,
        "mean": round(mean, 4),
        "std": round(std, 4),
        "coefficient_of_variation": round(cv, 3) if cv is not None else None,
        "weeks_covered": weeks_covered,
        "weekly_seasonality_evidence": weekly_seasonality_evidence,
        "trend_evidence_pct": trend_evidence_pct,
        "intermittent": intermittent,
        "forecastability": forecastability,
        "prophet_eligible": non_zero >= min_sample_days,
    }
