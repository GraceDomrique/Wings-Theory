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
import math
import sqlite3

from .errors import NotFoundError

MIN_DEMAND_SAMPLE_DAYS = 7      # distinct days with at least one sale before a forecast is offered
MIN_SEASONALITY_WEEKS = 2       # weeks of history before a weekday factor is trusted
RESIDUAL_SIGMA = 2.5            # how far off its own fit a day must be to be called an outlier

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
    return max(1, (_dt.date.today() - first).days + 1)


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
            """
            SELECT -SUM(delta_base) AS consumed,
                   COUNT(DISTINCT date(created_at)) AS days_active,
                   MIN(date(created_at)) AS first_day
              FROM stock_ledger
             WHERE ingredient_id = ? AND reason = ?
               AND created_at >= datetime('now', ?)
            """,
            (ing["id"], consumption_reason, f"-{lookback_days} days"),
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

        # How much to buy right now so stock covers demand through the next
        # delivery *and* a few days of slack before anyone has to check
        # again — not sized to arrive with exactly zero left, which is one
        # missed delivery away from a stockout.
        order_qty = None
        if avg_daily > 0 and ing["pack_size"]:
            target_base = avg_daily * (lead + REVIEW_CYCLE_DAYS)
            shortfall_base = target_base - available
            if shortfall_base > 0:
                order_qty = math.ceil(shortfall_base / ing["pack_size"])

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
            "suggested_order_qty": order_qty,
            "suggested_order_unit": ing["purchase_unit"] if order_qty is not None else None,
        })

    out.sort(key=lambda x: (RISK_ORDER[x["risk"]], x["days_remaining"] if x["days_remaining"] is not None else 1e9))
    return out


def _item_or_404(conn: sqlite3.Connection, menu_item_pk: int) -> sqlite3.Row:
    item = conn.execute("SELECT name FROM menu_item WHERE id=?", (menu_item_pk,)).fetchone()
    if not item:
        raise NotFoundError(f"menu item {menu_item_pk} not found")
    return item


def _daily_series(conn: sqlite3.Connection, menu_item_pk: int, lookback_days: int):
    """Real daily sold-qty rows plus the same series zero-filled across every
    calendar day in the window — shared by both forecasting methods below so
    they can't disagree about what "the last N days" contains."""
    rows = conn.execute(
        """
        SELECT date(o.created_at) AS d, SUM(ol.qty) AS qty
          FROM order_line ol JOIN customer_order o ON o.id = ol.order_id
         WHERE ol.menu_item_id = ? AND o.status != 'void'
           AND o.created_at >= datetime('now', ?)
         GROUP BY date(o.created_at)
        """,
        (menu_item_pk, f"-{lookback_days} days"),
    ).fetchall()
    by_day = {r["d"]: r["qty"] for r in rows}
    today = _dt.date.today()
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

    today = _dt.date.today()
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

    weeks_covered = len({_dt.date.fromisoformat(r["d"]).isocalendar()[:2] for r in rows})
    seasonality_applied = weeks_covered >= MIN_SEASONALITY_WEEKS

    try:
        df = _pd.DataFrame(series).rename(columns={"date": "ds", "qty": "y"})
        df["ds"] = _pd.to_datetime(df["ds"])
        model = _Prophet(daily_seasonality=False, weekly_seasonality=seasonality_applied,
                         yearly_seasonality=False, interval_width=0.8)
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
        fallback = menu_item_demand(conn, menu_item_pk, horizon_days, lookback_days)
        return {**fallback, "method": "trailing_average", "prophet_unavailable": True}

    trailing = series[-14:]
    trailing_avg = sum(p["qty"] for p in trailing) / len(trailing)

    return {
        "menu_item_id": menu_item_pk, "name": item["name"], "insufficient_data": False,
        "active_days": active_days, "total_qty_in_window": total_qty,
        "trailing_avg_daily": round(trailing_avg, 2),
        "seasonality_applied": seasonality_applied,
        "series": series, "forecast": forecast, "fitted": fitted, "outliers": outliers,
        "method": "prophet",
    }


def evaluate_demand_model(conn: sqlite3.Connection, horizon_days: int = 7) -> dict:
    """A real holdout backtest, not an in-sample number dressed up as
    accuracy: picks the menu item with the most real sales history, fits
    Prophet on everything except the most recent `horizon_days`, predicts
    that held-out window, and scores the prediction against what actually
    happened. MAE/RMSE/MAPE below are errors on data the model never saw
    while fitting.

    This backs the technical panel under System Check, not the main
    Forecasts page — real accuracy numbers, kept separate from the
    plain-language operational view per the owner's own instruction.
    """
    if not _PROPHET_AVAILABLE:
        return {"available": False, "reason": "Prophet is not installed on this server"}

    items = conn.execute("SELECT id, name FROM menu_item WHERE pos_visible=1").fetchall()
    if not items:
        return {"available": False, "reason": "no menu items"}

    best_item, best_rows, best_series = None, None, None
    for it in items:
        r, s = _daily_series(conn, it["id"], 90)
        if best_rows is None or len(r) > len(best_rows):
            best_item, best_rows, best_series = it, r, s

    if best_item is None or len(best_rows) < MIN_DEMAND_SAMPLE_DAYS + horizon_days:
        return {"available": False, "reason": "not enough real sales history yet for a holdout test"}

    train = best_series[:-horizon_days]
    test = best_series[-horizon_days:]

    try:
        df = _pd.DataFrame(train).rename(columns={"date": "ds", "qty": "y"})
        df["ds"] = _pd.to_datetime(df["ds"])
        weeks_covered = len({_dt.date.fromisoformat(p["date"]).isocalendar()[:2] for p in train})
        model = _Prophet(daily_seasonality=False, weekly_seasonality=weeks_covered >= MIN_SEASONALITY_WEEKS,
                         yearly_seasonality=False, interval_width=0.8)
        model.fit(df)
        future = model.make_future_dataframe(periods=horizon_days)
        fc = model.predict(future)
        test_fc = fc.tail(horizon_days)

        actual = [p["qty"] for p in test]
        predicted = test_fc["yhat"].tolist()
        n = len(actual)
        errors = [a - p for a, p in zip(actual, predicted)]
        mae = sum(abs(e) for e in errors) / n
        rmse = (sum(e * e for e in errors) / n) ** 0.5
        with_actual_sales = [(a, e) for a, e in zip(actual, errors) if a > 0]
        mape = (sum(abs(e) / a for a, e in with_actual_sales) / len(with_actual_sales) * 100
                if with_actual_sales else None)

        trend_vals = fc["trend"].tolist()
        trend_per_week = (trend_vals[-1] - trend_vals[0]) / (len(trend_vals) / 7) if len(trend_vals) > 7 else 0.0
    except Exception as e:   # pragma: no cover - genuine runtime failure, not a code path to rely on
        return {"available": False, "reason": f"backtest failed: {e}"}

    return {
        "available": True, "item_name": best_item["name"],
        "trained_on_days": len(train), "holdout_days": horizon_days,
        "mae": round(mae, 3), "rmse": round(rmse, 3),
        "mape": round(mape, 1) if mape is not None else None,
        "trend_per_week": round(trend_per_week, 4),
    }
