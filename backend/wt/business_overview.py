"""Owner Overview / Today business-companion aggregation (2026-10).

A read-only presentation layer over data every other endpoint in this
codebase already computes authoritatively -- nothing here derives a new
number the rest of the system doesn't already stand behind:

  - period revenue/orders/covers: the exact SQL shape /console/reports/sales
    (api.py's sales_summary) already uses, reapplied to different windows.
  - the forecast line: wt.forecasting.store_demand_prophet(), called
    directly, completely unmodified -- this module never fits its own model
    or re-tunes anything. If that function returns a trailing-average
    fallback (Prophet unavailable/declined) or insufficient_data, this
    module reports "not enough pattern yet" rather than inventing a number.

Three period comparisons, each against a DELIBERATELY matched baseline
rather than a raw total:
  - "today" vs the SAME weekday one week ago, both capped at the SAME
    elapsed time since Manila midnight -- comparing a partial day against a
    full day would always show "today" short.
  - "this week" vs last week, both capped at the same COUNT of days elapsed
    since each week's Monday (this week's last day can be partial-to-now;
    last week's matching day is a full historical day -- a known, common
    dashboard convention, not a bug).
  - "this month" vs last month, same shape, days-elapsed-since-the-1st
    instead of since Monday, clamped so a day that doesn't exist in a
    shorter previous month (e.g. "day 31" against February) doesn't
    overflow into the month after.
"""
from __future__ import annotations

import datetime as _dt

from . import timeutil

_SQL_FMT = "%Y-%m-%d %H:%M:%S"


def _manila_midnight_utc(d: _dt.date) -> _dt.datetime:
    return _dt.datetime.combine(d, _dt.time.min, tzinfo=timeutil.MANILA_TZ).astimezone(_dt.timezone.utc)


def _sql(instant: _dt.datetime) -> str:
    return instant.strftime(_SQL_FMT)


def _period_totals(conn, start: _dt.datetime, end: _dt.datetime) -> dict:
    """Revenue/orders/covers/items for [start, end) -- same status/void
    filter and the same two-query shape (orders+covers, then revenue via
    order_line) api.py's sales_summary already uses for every other date
    window in this console, just parameterised on an exact instant pair
    instead of a named preset."""
    params = {"start": _sql(start), "end": _sql(end)}
    totals = conn.execute(
        """
        SELECT COUNT(*) AS orders, COALESCE(SUM(o.head_count), 0) AS covers
          FROM customer_order o
         WHERE o.status != 'void' AND o.is_demo = 0 AND o.created_at >= :start AND o.created_at < :end
        """,
        params,
    ).fetchone()
    line_totals = conn.execute(
        """
        SELECT COALESCE(SUM(ol.qty * ol.unit_price), 0) AS revenue,
               COALESCE(SUM(ol.qty), 0) AS items
          FROM order_line ol JOIN customer_order o ON o.id = ol.order_id
         WHERE o.status != 'void' AND o.is_demo = 0 AND o.created_at >= :start AND o.created_at < :end
        """,
        params,
    ).fetchone()
    revenue = line_totals["revenue"]
    orders = totals["orders"]
    return {
        "revenue": round(revenue, 2),
        "orders": orders,
        "covers": totals["covers"],
        "items": line_totals["items"],
        "avg_order": round(revenue / orders, 2) if orders else None,
    }


def _delta_pct(current: float | int | None, prior: float | int | None) -> float | None:
    """Percent change, current vs. prior. None (not 0 or inf) when there is
    nothing to compare against -- "0 orders last week too" is a real,
    reportable 0%, but "0 orders last week" against any nonzero today has no
    meaningful percentage and must not print one."""
    if current is None or prior is None:
        return None
    if prior == 0:
        # 0 -> 0 is a real, reportable "no change"; 0 -> nonzero has no
        # finite percentage (division by zero) and must not print one.
        return 0.0 if current == 0 else None
    return round((current - prior) / prior * 100, 1)


def _compare_block(conn, label: str, current_start: _dt.datetime, current_end: _dt.datetime,
                    prior_start: _dt.datetime, prior_end: _dt.datetime) -> dict:
    current = _period_totals(conn, current_start, current_end)
    prior = _period_totals(conn, prior_start, prior_end)
    return {
        "label": label,
        "current": current,
        "prior": prior,
        "deltas": {
            "revenue_pct": _delta_pct(current["revenue"], prior["revenue"]),
            "orders_pct": _delta_pct(current["orders"], prior["orders"]),
            "avg_order_pct": _delta_pct(current["avg_order"], prior["avg_order"]),
            "covers_pct": _delta_pct(current["covers"], prior["covers"]),
            "items_pct": _delta_pct(current["items"], prior["items"]),
        },
    }


def today_vs_yesterday(conn, now: _dt.datetime | None = None) -> dict:
    """Admin Today's "Today's Sales" tile: today-so-far vs literally
    YESTERDAY, same elapsed time since Manila midnight -- distinct from
    business_overview()'s own `today` block above, which deliberately
    compares against the SAME WEEKDAY last week (a steadier baseline for a
    business-performance read). The Admin console asks a narrower, same-day
    operational question ("are we having a normal day so far, compared with
    yesterday"), so it gets the literal day-before comparison instead."""
    now = now or timeutil.utc_now()
    today = timeutil.business_today(now)
    today_start = _manila_midnight_utc(today)
    elapsed = now - today_start
    yesterday = today - _dt.timedelta(days=1)
    yesterday_start = _manila_midnight_utc(yesterday)
    return _compare_block(
        conn, "vs same time yesterday",
        today_start, now, yesterday_start, yesterday_start + elapsed,
    )


def _trend_weeks(conn, now: _dt.datetime, weeks: int = 12) -> dict:
    """The last N *completed* Manila weeks (Monday-Sunday), oldest first --
    the current, still-in-progress week is deliberately excluded so a
    half-finished week never drags the average down and gets flagged as
    "below average" for the trivial reason that it isn't over yet."""
    today = timeutil.business_today(now)
    this_week_monday = today - _dt.timedelta(days=today.weekday())
    series = []
    for i in range(weeks, 0, -1):
        week_start = this_week_monday - _dt.timedelta(days=7 * i)
        week_end = week_start + _dt.timedelta(days=7)
        totals = _period_totals(conn, _manila_midnight_utc(week_start), _manila_midnight_utc(week_end))
        series.append({"week_start": week_start.isoformat(), "revenue": totals["revenue"]})
    revenues = [w["revenue"] for w in series]
    avg = round(sum(revenues) / len(revenues), 2) if revenues else 0
    # Trailing run of consecutive weeks (most recent first) that closed
    # below the window's own average -- a pattern worth a plain-language
    # callout, not a single bad week.
    streak = 0
    for r in reversed(revenues):
        if r < avg:
            streak += 1
        else:
            break
    return {"weeks": series, "average_revenue": avg, "below_average_streak": streak}


def _outlook(result: dict | None) -> dict:
    """One forecast line, straight from the existing, unmodified Prophet
    store-demand fit (wt.forecasting.store_demand_prophet, called by
    api.py's own cached /console/forecasts/store-demand route and handed in
    here as `result` -- this module never fits its own model, and never
    bypasses that endpoint's TTLCache, which is what keeps a several-
    second-per-fit Prophet run from repeating on every single Today page
    load). Reported ONLY when that call actually produced a real Prophet
    fit (method == "prophet"); a trailing-average fallback or insufficient-
    data result is surfaced as "not available" rather than dressed up as a
    forecast, per the standing rule that this layer must never present an
    assumption as a prediction."""
    if result is None:
        return {"available": False, "reason": "forecast unavailable"}
    if result.get("insufficient_data") or result.get("method") != "prophet":
        return {"available": False, "reason": "not enough order history yet for a 7-day forecast"}
    forecast_total = round(sum(p["qty"] for p in result["forecast"]), 2)
    trailing_total = round(result["trailing_avg_daily"] * 7, 2)
    return {
        "available": True,
        "horizon_days": 7,
        "forecast_total": forecast_total,
        "trailing_avg_total": trailing_total,
        "delta_pct": _delta_pct(forecast_total, trailing_total),
    }


def business_overview(conn, now: _dt.datetime | None = None, forecast: dict | None = None) -> dict:
    now = now or timeutil.utc_now()
    today = timeutil.business_today(now)
    today_start = _manila_midnight_utc(today)
    elapsed = now - today_start

    # ---- Business Today: today-so-far vs the same weekday last week, same
    # elapsed time since Manila midnight ----
    prior_day = today - _dt.timedelta(days=7)
    prior_start = _manila_midnight_utc(prior_day)
    today_block = _compare_block(
        conn, prior_day.strftime("same time last %A"),
        today_start, now, prior_start, prior_start + elapsed,
    )

    # ---- this week vs last week, same count of days elapsed since Monday ----
    week_start = today - _dt.timedelta(days=today.weekday())
    days_elapsed_week = today.weekday() + 1
    last_week_start = week_start - _dt.timedelta(days=7)
    week_block = _compare_block(
        conn, "vs last week",
        _manila_midnight_utc(week_start), now,
        _manila_midnight_utc(last_week_start),
        _manila_midnight_utc(last_week_start + _dt.timedelta(days=days_elapsed_week)),
    )

    # ---- this month vs last month, same count of days elapsed since the 1st,
    # clamped to the previous month's own length ----
    month_start = today.replace(day=1)
    days_elapsed_month = today.day
    prev_month_last_day = month_start - _dt.timedelta(days=1)
    prev_month_start = prev_month_last_day.replace(day=1)
    prev_month_length = (month_start - prev_month_start).days
    days_elapsed_prev_month = min(days_elapsed_month, prev_month_length)
    month_block = _compare_block(
        conn, "vs last month",
        _manila_midnight_utc(month_start), now,
        _manila_midnight_utc(prev_month_start),
        _manila_midnight_utc(prev_month_start + _dt.timedelta(days=days_elapsed_prev_month)),
    )

    # Same gap dashboard_summary's own "freshness" block warns about: order
    # data can trail the real current instant by days on a dataset that
    # isn't being actively entered against right now. A "today"/"this week"
    # comparison computed against a stale window isn't wrong arithmetic, but
    # presenting it as "sales are down" without saying the data itself is
    # old would be exactly the kind of unearned claim this module exists to
    # avoid -- so the frontend gets the gap explicitly and decides whether
    # to caveat or suppress those two blocks, rather than silently printing
    # a scary percentage that is actually just a reporting lag.
    latest_order_at = conn.execute(
        "SELECT MAX(created_at) AS t FROM customer_order"
    ).fetchone()["t"]
    stale_hours = None
    if latest_order_at:
        latest_dt = _dt.datetime.strptime(latest_order_at, _SQL_FMT).replace(tzinfo=_dt.timezone.utc)
        stale_hours = round((now - latest_dt).total_seconds() / 3600, 1)

    return {
        "as_of": _sql(now),
        "latest_order_at": latest_order_at,
        "data_stale_hours": stale_hours,
        "today": today_block,
        "this_week": week_block,
        "this_month": month_block,
        "trend": _trend_weeks(conn, now),
        "outlook": _outlook(forecast),
    }
