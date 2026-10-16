"""Admin Today / Owner Overview landing-page aggregation (2026-10).

Both payloads are read-only compositions over data every other endpoint in
this codebase already computes authoritatively -- see business_overview.py's
own module docstring for the same standing rule. Nothing here fits a model,
re-derives an anomaly score, or invents a number; it queries customer_order/
order_line directly (same tables, same status/void filter sales_summary and
business_overview use) for the pieces those two modules don't already carry,
and leans on the existing forecast/anomaly/combination functions, passed in
by the caller, for everything else.

Both functions take already-computed building blocks as arguments (the
forward-depletion forecast, the store-demand forecast, the anomaly lists,
the FP-Growth combinations) rather than calling api.py's cached wrappers
themselves -- keeps this module free of the FastAPI/cache plumbing and lets
the two dashboard endpoints in api.py share one cached fetch per request
with every other panel that already needs the same data.
"""
from __future__ import annotations

import datetime as _dt

from . import business_overview as bo
from . import timeutil


def _week_bounds(today: _dt.date) -> tuple[_dt.date, _dt.date]:
    start = today - _dt.timedelta(days=today.weekday())
    return start, start + _dt.timedelta(days=7)


def _forecast_strip(forecast: dict | None) -> dict:
    """A 7-day bar-chart-ready strip from store_demand_prophet()'s output --
    shared by Admin "Next 7 Days" and Owner "Coming Up". Busy-level labels
    are relative to the forecast's own trailing average, not an invented
    absolute scale: >=130% of trailing average is "Busiest", >=110% "High",
    >=90% "Moderate", below that "Normal". Empty/insufficient-data forecasts
    return an explicit unavailable strip, never invented bars."""
    if not forecast or forecast.get("insufficient_data") or not forecast.get("forecast"):
        return {"available": False, "days": [], "note": "Not enough order history yet for a 7-day forecast."}
    trailing = forecast.get("trailing_avg_daily") or 0
    days = []
    for p in forecast["forecast"]:
        d = _dt.date.fromisoformat(p["date"])
        qty = p["qty"]
        if trailing > 0:
            ratio = qty / trailing
            level = "Busiest" if ratio >= 1.3 else "High" if ratio >= 1.1 else "Moderate" if ratio >= 0.9 else "Normal"
        else:
            level = "Normal"
        days.append({
            "date": p["date"], "weekday": d.strftime("%a"), "qty": qty, "level": level,
        })
    busiest = max(days, key=lambda x: x["qty"]) if days else None
    return {
        "available": True, "days": days, "metric": forecast.get("metric"),
        "method": forecast.get("method"),
        "busiestDay": busiest["weekday"] if busiest else None,
        "note": f"{busiest['weekday']} is expected to have the highest demand." if busiest else None,
    }


def _order_activity(conn, start: _dt.datetime, end: _dt.datetime) -> dict:
    """Real customer_order.status values only (punched/in_progress/ready/
    served/void -- migration 0001) -- this schema has no separate
    'cancelled' state distinct from void, so that bucket is not invented."""
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN status='served' THEN 1 ELSE 0 END) AS completed,
          SUM(CASE WHEN status IN ('punched','in_progress','ready') THEN 1 ELSE 0 END) AS preparing,
          SUM(CASE WHEN status='void' THEN 1 ELSE 0 END) AS voided,
          COUNT(*) AS total
        FROM customer_order WHERE created_at >= ? AND created_at < ?
        """,
        (bo._sql(start), bo._sql(end)),
    ).fetchone()
    return {
        "completed": row["completed"] or 0, "preparing": row["preparing"] or 0,
        "voided": row["voided"] or 0, "total": row["total"] or 0,
    }


def _recent_orders(conn, start: _dt.datetime, end: _dt.datetime, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        """
        SELECT o.id, o.order_no, o.status, o.table_label, o.created_at,
               (SELECT COALESCE(SUM(ol2.qty*ol2.unit_price),0) FROM order_line ol2
                 WHERE ol2.order_id=o.id) AS total,
               (SELECT COALESCE(SUM(ol3.qty),0) FROM order_line ol3
                 WHERE ol3.order_id=o.id) AS items
          FROM customer_order o
         WHERE o.created_at >= ? AND o.created_at < ?
         ORDER BY o.created_at DESC LIMIT ?
        """,
        (bo._sql(start), bo._sql(end), limit),
    ).fetchall()
    return [
        {"id": r["id"], "orderNo": r["order_no"], "status": r["status"], "tableLabel": r["table_label"],
         "createdAt": r["created_at"], "total": round(r["total"], 2), "items": r["items"]}
        for r in rows
    ]


def _deliveries_today(conn, today_iso: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT po.id, po.status, po.supplier, po.expected_delivery_date,
               (SELECT GROUP_CONCAT(i.name, ', ') FROM purchase_order_item poi
                  JOIN ingredient i ON i.id = poi.ingredient_id
                 WHERE poi.purchase_order_id = po.id) AS items_summary,
               (SELECT COUNT(*) FROM purchase_order_item poi WHERE poi.purchase_order_id = po.id) AS item_count
          FROM purchase_order po
         WHERE po.status IN ('ordered', 'partially_received')
           AND po.expected_delivery_date IS NOT NULL
           AND po.expected_delivery_date <= ?
         ORDER BY po.expected_delivery_date ASC
        """,
        (today_iso,),
    ).fetchall()
    return [
        {"id": r["id"], "poCode": f"PO-{r['id']:04d}", "status": r["status"], "supplier": r["supplier"],
         "expectedDate": r["expected_delivery_date"], "itemCount": r["item_count"],
         "itemsSummary": r["items_summary"], "overdue": r["expected_delivery_date"] < today_iso}
        for r in rows
    ]


def admin_today(conn, *, now: _dt.datetime, summary: dict, forecast: dict | None,
                 stock_loss_anomalies: list[dict], consumption_anomalies: list[dict],
                 counts_due: dict, top_risks: list[dict]) -> dict:
    """Admin Today — see module docstring. `summary` is the already-computed
    GET /dashboard/summary payload (kpis/alerts/buckets/coverage); this
    function does not recompute any of it, only adds what that endpoint
    does not carry: a same-day sales readout, deliveries, order-queue mix,
    recent orders, the two anomaly previews kept separate, and the
    accountability-log tail."""
    today = timeutil.business_today(now)
    day_start = bo._manila_midnight_utc(today)
    sales = bo.today_vs_yesterday(conn, now)

    buckets = summary["buckets"]
    purchase_needed = buckets.get("out", 0) + buckets.get("critical", 0)
    running_low = buckets.get("at_risk", 0)
    stock_healthy = max(0, summary["coverage"]["ingredientsForecast"] - purchase_needed - running_low)

    deliveries = _deliveries_today(conn, today.isoformat())
    activity = _order_activity(conn, day_start, now)
    recent = _recent_orders(conn, day_start, now)

    crit_open = sum(1 for a in summary["alerts"] if a["severity"] == "crit")
    need_attention = len(summary["alerts"])

    activity_log = [dict(r) for r in conn.execute(
        "SELECT entity_type, entity_id, action, field, previous_value, new_value, "
        "user_id, note, created_at FROM admin_audit_log WHERE entity_type != 'page' "
        "ORDER BY id DESC LIMIT 10"
    )]

    return {
        "asOf": bo._sql(now),
        "businessDate": today.isoformat(),
        "freshness": summary["freshness"],
        "storeStatus": {
            "operational": crit_open == 0,
            "needAttention": need_attention,
            "deliveriesToday": len(deliveries),
            "runningLow": running_low,
            "stockHealthy": stock_healthy,
        },
        "todaySales": {
            "revenue": sales["current"]["revenue"], "orders": sales["current"]["orders"],
            "items": sales["current"]["items"], "avgOrder": sales["current"]["avg_order"],
            "comparisonLabel": sales["label"], "deltas": sales["deltas"],
        },
        "doNow": summary["alerts"][:6],
        "next7Days": _forecast_strip(forecast),
        "inventoryPulse": {
            "purchaseNeeded": purchase_needed,
            "runningLow": running_low,
            "expiringSoon": next((k["value"] for k in summary["kpis"] if k["key"] == "expiringSoon"), 0),
            "countsDue": len(counts_due.get("needsChecking", [])) + len(counts_due.get("worthChecking", [])),
            "topRisks": [
                {"ingredientId": r["ingredientId"], "name": r["name"], "status": r["status"],
                 "daysOfSupply": r["daysOfSupply"]} for r in top_risks[:4]
            ],
        },
        "todaysDeliveries": deliveries,
        # 2026-10-14 fix: "Create Purchase Order" was offered to Admin here,
        # but purchase_order.create is owner-only (wt/auth.py) — clicking it
        # sent Admin to What to Buy with a label promising an action they
        # were never allowed to take. Relabeled to what Admin actually does
        # there (review the recommendations, purchase_order.read is granted
        # to both roles); the route is unchanged. "Record Waste" pointed at
        # Losses & Variance, a read-only anomaly review page with no waste
        # action on it at all — repointed to Inventory (onhand), where
        # "Record Waste" is a real Quick Action that opens the waste form.
        "quickActions": [
            {"key": "receive", "label": "Receive Delivery", "route": "receiving"},
            {"key": "waste", "label": "Record Waste", "route": "onhand"},
            {"key": "stockcount", "label": "Start Stock Count", "route": "stockcounts"},
            {"key": "createpo", "label": "Review Purchase Needs", "route": "whattobuy"},
        ],
        "orderActivity": activity,
        "recentOrders": recent,
        "unusualUsage": consumption_anomalies[:4],
        "unusualStockDifference": stock_loss_anomalies[:4],
        "activityLog": activity_log,
        "instructionsPreview": summary["instructionsPreview"],
    }


def _category_mix(conn, start: _dt.datetime, end: _dt.datetime) -> list[dict]:
    rows = conn.execute(
        """
        SELECT m.category AS category, SUM(ol.qty * ol.unit_price) AS revenue
          FROM order_line ol JOIN customer_order o ON o.id = ol.order_id
          JOIN menu_item m ON m.id = ol.menu_item_id
         WHERE o.status != 'void' AND o.is_demo = 0 AND o.created_at >= ? AND o.created_at < ?
         GROUP BY m.category ORDER BY revenue DESC
        """,
        (bo._sql(start), bo._sql(end)),
    ).fetchall()
    total = sum(r["revenue"] for r in rows) or 0
    return [
        {"category": r["category"], "revenue": round(r["revenue"], 2),
         "sharePct": round(r["revenue"] / total * 100, 1) if total else 0}
        for r in rows
    ]


def _item_qty_by_period(conn, start: _dt.datetime, end: _dt.datetime) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT m.name AS name, SUM(ol.qty) AS qty, SUM(ol.qty*ol.unit_price) AS revenue,
               COUNT(DISTINCT o.id) AS orders
          FROM order_line ol JOIN customer_order o ON o.id = ol.order_id
          JOIN menu_item m ON m.id = ol.menu_item_id
         WHERE o.status != 'void' AND o.is_demo = 0 AND o.created_at >= ? AND o.created_at < ?
         GROUP BY m.id
        """,
        (bo._sql(start), bo._sql(end)),
    ).fetchall()
    return {r["name"]: {"qty": r["qty"], "revenue": round(r["revenue"], 2), "orders": r["orders"]} for r in rows}


def _customer_pulse(conn, now: _dt.datetime) -> dict:
    today = timeutil.business_today(now)
    week_start, _ = _week_bounds(today)
    last_week_start = week_start - _dt.timedelta(days=7)

    this_week = _item_qty_by_period(conn, bo._manila_midnight_utc(week_start), now)
    last_week = _item_qty_by_period(
        conn, bo._manila_midnight_utc(last_week_start), bo._manila_midnight_utc(week_start))

    most_ordered = sorted(this_week.items(), key=lambda kv: kv[1]["qty"], reverse=True)[:1]

    MIN_PRIOR_ORDERS = 3  # a 1-order-to-2-order jump is not a "rising" product
    changes = []
    for name, cur in this_week.items():
        prior = last_week.get(name)
        if prior and prior["orders"] >= MIN_PRIOR_ORDERS:
            pct = bo._delta_pct(cur["qty"], prior["qty"])
            if pct is not None:
                changes.append((name, pct, cur, prior))
    rising = sorted([c for c in changes if c[1] > 0], key=lambda c: c[1], reverse=True)[:1]
    slowing = sorted([c for c in changes if c[1] < 0], key=lambda c: c[1])[:1]

    return {
        "mostOrdered": [{"name": n, "orders": v["orders"], "qty": v["qty"]} for n, v in most_ordered],
        "rising": [{"name": n, "pct": p} for n, p, _, _ in rising],
        "slowing": [{"name": n, "pct": p} for n, p, _, _ in slowing],
    }


def _business_rhythm(conn, now: _dt.datetime, window_days: int = 56) -> dict:
    """Weekend effect, peak-hour concentration, and a payday-window check --
    all computed over the trailing `window_days` of real orders (default 8
    completed weeks), each with its own minimum-sample guard so a thin
    dataset reports "not enough data" rather than a pattern that doesn't
    really exist yet."""
    WINDOW_DAYS = window_days
    today = timeutil.business_today(now)
    start = bo._manila_midnight_utc(today - _dt.timedelta(days=WINDOW_DAYS))
    rows = conn.execute(
        """
        SELECT o.created_at, COALESCE(
                 (SELECT SUM(ol.qty*ol.unit_price) FROM order_line ol WHERE ol.order_id = o.id), 0
               ) AS revenue
          FROM customer_order o WHERE o.status != 'void' AND o.is_demo = 0 AND o.created_at >= ? AND o.created_at < ?
        """,
        (bo._sql(start), bo._sql(now)),
    ).fetchall()
    if len(rows) < 20:
        return {"available": False, "reason": "not enough order history yet"}

    weekend_rev, weekday_rev, hour_counts, dom_rev, other_rev = 0.0, 0.0, {}, 0.0, 0.0
    total_rev = 0.0
    for r in rows:
        local = timeutil.to_manila(_dt.datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
                                    .replace(tzinfo=_dt.timezone.utc))
        total_rev += r["revenue"]
        if local.weekday() >= 4:  # Fri=4, Sat=5, Sun=6
            weekend_rev += r["revenue"]
        else:
            weekday_rev += r["revenue"]
        hour_counts[local.hour] = hour_counts.get(local.hour, 0) + 1
        if local.day in (14, 15, 16, 29, 30, 31, 1):
            dom_rev += r["revenue"]
        else:
            other_rev += r["revenue"]

    weekend_share = round(weekend_rev / total_rev * 100, 1) if total_rev else None
    peak_hour = max(hour_counts.items(), key=lambda kv: kv[1])[0] if hour_counts else None
    dinner_orders = sum(c for h, c in hour_counts.items() if h >= 17)
    dinner_share = round(dinner_orders / sum(hour_counts.values()) * 100, 1) if hour_counts else None
    payday_days = sum(1 for r in rows if timeutil.to_manila(
        _dt.datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_dt.timezone.utc)
    ).day in (14, 15, 16, 29, 30, 31, 1))
    payday_pattern = None
    if payday_days >= 10:
        payday_avg = dom_rev / payday_days
        other_days_count = max(1, len(rows) - payday_days)
        other_avg = other_rev / other_days_count
        if other_avg > 0:
            payday_pattern = round((payday_avg - other_avg) / other_avg * 100, 1)

    return {
        "available": True, "windowDays": WINDOW_DAYS,
        "weekendRevenueSharePct": weekend_share,
        "peakHour": peak_hour,
        "dinnerShareOfOrdersPct": dinner_share,
        "paydayDeltaPct": payday_pattern,
    }


def _needs_attention(summary: dict, declining: list[dict]) -> list[dict]:
    items = []
    for a in summary["alerts"][:3]:
        items.append({"kind": a["kind"], "severity": a["severity"], "headline": a["headline"],
                       "detail": a["detail"], "route": a["route"]})
    for d in declining[:1]:
        items.append({
            "kind": "declining_sales", "severity": "warn",
            "headline": f"{d['name']} — sales slowing",
            "detail": f"{abs(d['pct']):.0f}% fewer this week than last", "route": "salesoverview",
        })
    return items[:4]


def _period_tile(block: dict) -> dict:
    """Shrink a business_overview() compare-block down to the single-figure
    shape the Overview page's stat tiles need (current revenue + its own
    vs-prior delta), rather than exposing the full current/prior/deltas
    object every stat tile would otherwise have to reach into."""
    return {
        "revenue": block["current"]["revenue"],
        "orders": block["current"]["orders"],
        "deltaPct": block["deltas"]["revenue_pct"],
        "ordersDeltaPct": block["deltas"]["orders_pct"],
        "label": block["label"],
    }


def _kpi_value(summary: dict, key: str) -> int | None:
    """Pull one named KPI's value out of dashboard_summary()'s own kpis
    list (needRestock, expiringSoon, expired, ...) rather than re-deriving
    it -- Overview's "Stock at Risk" tile reuses the exact same forward-
    depletion count Stock & Replenishment's own page is built on."""
    for kpi in summary.get("kpis", []):
        if kpi["key"] == key:
            return kpi["value"]
    return None


def owner_overview(conn, *, now: _dt.datetime, summary: dict, period_overview: dict,
                    forecast: dict | None, combinations: dict | None,
                    top_risks: list[dict], today_vs_yesterday: dict | None = None) -> dict:
    """Owner Overview — see module docstring. `period_overview` is the
    already-computed business_overview() payload (today/thisWeek/thisMonth
    period comparisons + 12-week trend + outlook); this function adds the
    sections that payload does not carry: attention list, category mix,
    customer pulse, business rhythm, and recent orders, all computed from
    the same customer_order/order_line tables. `today_vs_yesterday`, if
    given, is business_overview.today_vs_yesterday()'s own block (same
    literal "vs yesterday" comparison Admin Today's tile already uses) —
    the Overview page's "Today's Revenue" tile reads this reuse rather than
    period_overview["today"], which compares against the same weekday last
    week instead, a different (also honest, just not what this tile says)
    baseline."""
    today = timeutil.business_today(now)
    week_start, _ = _week_bounds(today)
    week_start_dt = bo._manila_midnight_utc(week_start)

    pulse = _customer_pulse(conn, now)
    categories = _category_mix(conn, week_start_dt, now)
    rhythm = _business_rhythm(conn, now)
    recent = _recent_orders(conn, week_start_dt, now, limit=10)

    top_pair = None
    if combinations and combinations.get("combos"):
        best = combinations["combos"][0]
        top_pair = {
            "itemA": best.get("item_a"), "itemB": best.get("item_b"),
            "confidence": best.get("confidence"), "lift": best.get("lift"),
        }

    week = period_overview["this_week"]
    revenue_pct = week["deltas"]["revenue_pct"]
    orders_pct = week["deltas"]["orders_pct"]
    avg_order_pct = week["deltas"]["avg_order_pct"]
    if revenue_pct is None:
        status, interpretation = "flat", "Not enough data from last week to compare yet."
    elif revenue_pct > 2:
        status = "Doing Well"
        driver = "Higher order volume" if (orders_pct or 0) >= (avg_order_pct or 0) else "Higher average order value"
        interpretation = f"Sales are ahead of last week. {driver} contributed most to the increase."
    elif revenue_pct < -2:
        status = "Worth Watching"
        driver = "Fewer orders" if (orders_pct or 0) <= (avg_order_pct or 0) else "Lower average order value"
        interpretation = f"Sales are behind last week. {driver} contributed most to the decrease."
    else:
        status = "Steady"
        interpretation = "Sales are roughly even with last week."

    top_risk = top_risks[0] if top_risks else None

    return {
        "asOf": bo._sql(now),
        "latestOrderAt": period_overview["latest_order_at"],
        "dataStaleHours": period_overview["data_stale_hours"],
        "businessPulse": {
            "status": status, "interpretation": interpretation,
            "revenue": week["current"]["revenue"], "orders": week["current"]["orders"],
            "items": week["current"]["items"], "avgOrder": week["current"]["avg_order"],
            "deltas": week["deltas"], "label": week["label"],
        },
        "todayRevenue": _period_tile(today_vs_yesterday) if today_vs_yesterday else None,
        "monthRevenue": _period_tile(period_overview["this_month"]),
        "stockAtRisk": {
            "needRestock": _kpi_value(summary, "needRestock"),
            "expiringSoon": _kpi_value(summary, "expiringSoon"),
        },
        "needsAttention": _needs_attention(summary, pulse["slowing"]),
        "comingUp": _forecast_strip(forecast),
        "salesToStock": {
            "available": top_risk is not None,
            "ingredientName": top_risk["name"] if top_risk else None,
            "status": top_risk["status"] if top_risk else None,
            "daysOfSupply": top_risk["daysOfSupply"] if top_risk else None,
            "note": "Based on current demand, this ingredient may fall below a safe level soon."
                    if top_risk else "No ingredient is currently flagged as at risk.",
        },
        "customerPulse": {**pulse, "frequentPair": top_pair},
        "topCategories": categories,
        "businessRhythm": rhythm,
        "recentOrders": recent,
        "trend": period_overview["trend"],
        "outlook": period_overview["outlook"],
        "instructionsPreview": summary["instructionsPreview"],
    }
