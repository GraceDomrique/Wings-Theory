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


def _elapsed_days(first_day_iso: str | None) -> int:
    if not first_day_iso:
        return 0
    first = _dt.date.fromisoformat(first_day_iso)
    return max(1, (timeutil.business_today() - first).days + 1)


def ingredient_depletion(conn: sqlite3.Connection, lookback_days: int = 30) -> list[dict]:
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
            """,
            (ing["id"], consumption_reason, timeutil.utc_bound_n_manila_days_ago(lookback_days)),
        ).fetchone()

        consumed = row["consumed"] or 0.0
        days_active = row["days_active"] or 0
        # The rate is spread over the window actually elapsed since the first
        # sale, not just the days something sold — a slow mover that sold on
        # 2 of the last 30 days still has a 30-day rate, not a 2-day one.
        span_days = min(lookback_days, _elapsed_days(row["first_day"]))
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
    a system clock that has since moved past it)."""
    bd = timeutil.sql_business_date("o.created_at")
    rows = conn.execute(
        f"""
        SELECT {bd} AS d, SUM(ol.qty) AS qty
          FROM order_line ol JOIN customer_order o ON o.id = ol.order_id
         WHERE ol.menu_item_id = ? AND o.status != 'void'
           AND o.created_at >= ?
         GROUP BY {bd}
        """,
        (menu_item_pk, timeutil.utc_bound_n_manila_days_ago(lookback_days, now)),
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
    """
    if not _PROPHET_AVAILABLE:
        return {**menu_item_demand(conn, menu_item_pk, horizon_days, lookback_days),
               "method": "trailing_average", "prophet_unavailable": True}

    item = _item_or_404(conn, menu_item_pk)
    rows, series = _daily_series(conn, menu_item_pk, lookback_days)
    active_days = len(rows)
    total_qty = sum(r["qty"] for r in rows)

    if active_days < MIN_DEMAND_SAMPLE_DAYS:
        return {
            "menu_item_id": menu_item_pk, "name": item["name"], "insufficient_data": True,
            "active_days": active_days, "required_days": MIN_DEMAND_SAMPLE_DAYS,
            "series": series, "forecast": [], "method": "prophet",
        }

    fitted_model = fit_prophet(series, rows, horizon_days)
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
ROLLING_MAX_ITEMS = 8           # items with the most real history, evaluated fully
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


def _backtest_series(rows: list[dict], series: list[dict],
                     horizon_days: int, min_train: int, max_folds: int) -> dict | None:
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
    fields the core result does not carry)."""
    # `series` is zero-filled across the whole calendar lookback (see
    # _daily_series), so its length alone cannot distinguish "365 days of
    # real trading" from "3 real observations padded out to 365 zeros" -- a
    # series needs real, distinct non-zero days too, matching the same
    # MIN_DEMAND_SAMPLE_DAYS bar menu_item_demand()/menu_item_demand_prophet()
    # already use for "insufficient data".
    fold_starts = _rolling_fold_starts(len(series), min_train, horizon_days, max_folds)
    if not fold_starts or len(rows) < MIN_DEMAND_SAMPLE_DAYS:
        return None

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
            fitted = fit_prophet(train, train_rows, horizon_days)
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
    }


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


def evaluate_demand_model(conn: sqlite3.Connection, horizon_days: int = 7) -> dict:
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

    Backs the technical panel under System Check (api.py caches this --
    see _model_details_cache -- since a full rolling backtest with Prophet
    fits is too slow to redo on every page load).
    """
    items = conn.execute("SELECT id, name FROM menu_item WHERE pos_visible=1").fetchall()
    if not items:
        return {"available": False, "reason": "no menu items", "method_version": EVAL_METHOD_VERSION}

    # Full available history per item (not capped at 90 days -- more folds
    # is a more convincing backtest), then keep the ROLLING_MAX_ITEMS with
    # the most real sale-days: those are the only ones with enough history
    # for a multi-fold backtest to say anything, and evaluating every menu
    # item with a full Prophet-per-fold pass would not finish in a request
    # a person is waiting on.
    LOOKBACK_FOR_BACKTEST = 365
    candidates = []
    for it in items:
        rows, series = _daily_series(conn, it["id"], LOOKBACK_FOR_BACKTEST)
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
    bd = timeutil.sql_business_date("o.created_at")
    raw = conn.execute(
        f"""
        SELECT {bd} AS d, COUNT(DISTINCT o.id) AS orders,
               COALESCE(SUM(ol.qty), 0) AS items,
               COALESCE(SUM(ol.qty * ol.unit_price), 0) AS sales
          FROM customer_order o
          JOIN order_line ol ON ol.order_id = o.id
         WHERE o.status != 'void' AND o.created_at >= ?
         GROUP BY d
        """,
        (timeutil.utc_bound_n_manila_days_ago(lookback_days, now),),
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
    fit_prophet() in this module."""
    if metric not in STORE_METRICS:
        raise ValueError(f"unknown store metric {metric!r}; expected one of {STORE_METRICS}")
    rows, series = _store_daily_series(conn, lookback_days)[metric]
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
    fitted_model = fit_prophet(series, rows, horizon_days)

    if fitted_model is None:
        today = timeutil.business_today()
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


def evaluate_store_demand_model(conn: sqlite3.Connection, horizon_days: int = 7) -> dict:
    """The same chronological rolling-backtest methodology as
    evaluate_demand_model(), applied to the store's own aggregate daily
    series (orders taken, items sold, gross sales) instead of one menu item
    at a time -- see that function's docstring for the full methodology,
    shared unchanged via _backtest_series().

    Backs the technical panel's store-level demand card and the ML
    integration audit -- this is the evidence for whether Prophet has
    actually earned a role at the store level, never assumed. Prophet is
    never forced to win here any more than it is per item: a baseline
    scoring lower WAPE on the store's own aggregate series IS the selection.
    """
    per_metric = _store_daily_series(conn, STORE_LOOKBACK_FOR_BACKTEST)
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

    return {
        "available": True,
        "method_version": EVAL_METHOD_VERSION,
        "methodology": ("Chronological expanding-window rolling backtest -- identical methodology "
                        "to the menu-item backtest above, applied to the store's own aggregate daily "
                        f"series instead. Up to {ROLLING_MAX_FOLDS} most-recent folds, {horizon_days}-"
                        f"day forecast horizon per fold, minimum {ROLLING_MIN_TRAIN_DAYS}-day initial "
                        "training window. Every fold scored for naive-previous-period, trailing-"
                        "moving-average, day-of-week-seasonal, and Prophet."),
        "horizon_days": horizon_days,
        "min_train_days": ROLLING_MIN_TRAIN_DAYS,
        "max_folds": ROLLING_MAX_FOLDS,
        "metrics_evaluated": len(scored),
        "metrics_insufficient_history": sum(1 for r in results.values() if r["insufficient_history"]),
        "metrics": results,
        "selection_counts": selection_counts,
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
