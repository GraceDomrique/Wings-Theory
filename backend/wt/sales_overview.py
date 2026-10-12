"""Owner Sales / Admin Sales aggregation (2026-10, Phase 2).

Same standing rule as business_overview.py and dashboard_overview.py: a
read-only presentation layer over customer_order/order_line (same status/
void filter every other sales endpoint in this codebase uses), plus the
existing forward-depletion forecast, FP-Growth combinations, and recipe
expansion -- nothing here fits a model or invents a figure. Owner Sales and
Admin Sales share this module's period/comparison math and several of its
per-section builders (category mix, item metrics, business rhythm, recent
orders already live in dashboard_overview.py and are reused directly); the
two public entry points at the bottom differ only in which sections they
assemble, matching the real difference in what each role needs from a sales
screen.
"""
from __future__ import annotations

import datetime as _dt

from . import business_overview as bo
from . import dashboard_overview as do
from . import timeutil

_SQL_FMT = "%Y-%m-%d %H:%M:%S"
MIN_PRIOR_SAMPLE_ORDERS = 5  # below this, a comparison period is too thin to call a trend


# ===========================================================================
# Period / comparison resolution — shared by both Owner and Admin Sales
# ===========================================================================
def _week_start(d: _dt.date) -> _dt.date:
    return d - _dt.timedelta(days=d.weekday())


def _items_per_order(items: int | float, orders: int) -> float | None:
    """Eligible Items Sold / Eligible Orders — 'eligible' meaning the exact
    same population _period_totals() already filters to (status != 'void'),
    the one eligible-order/eligible-revenue population every total on this
    page shares. None (not 0 or a divide-by-zero) when there were no
    eligible orders to divide by."""
    if not orders:
        return None
    return round(items / orders, 2)


def resolve_period(preset: str, now: _dt.datetime, custom_start: str | None = None,
                    custom_end: str | None = None) -> dict:
    """A Manila-calendar-aligned [start_date, end_date] (inclusive) plus the
    real-time [start_dt, end_dt) window to query against -- end_dt is `now`
    itself for a still-in-progress period (today is inside the range), never
    midnight-after-today, so every window here is apples-to-apples with
    business_overview.py's own "partial period" convention."""
    today = timeutil.business_today(now)
    if preset == "custom" and custom_start and custom_end:
        start_d = _dt.date.fromisoformat(custom_start)
        end_d = _dt.date.fromisoformat(custom_end)
        label = f"{start_d.isoformat()} to {end_d.isoformat()}"
    elif preset == "today":
        start_d = end_d = today
        label = "Today"
    elif preset == "this_month":
        start_d, end_d = today.replace(day=1), today
        label = "This Month"
    elif preset == "this_year":
        start_d, end_d = today.replace(month=1, day=1), today
        label = "This Year"
    else:
        preset = "this_week"
        start_d, end_d = _week_start(today), today
        label = "This Week"
    if end_d < start_d:
        start_d, end_d = end_d, start_d
    start_dt = bo._manila_midnight_utc(start_d)
    end_dt = now if end_d >= today else bo._manila_midnight_utc(end_d + _dt.timedelta(days=1))
    return {
        "preset": preset, "label": label, "startDate": start_d, "endDate": end_d,
        "startDt": start_dt, "endDt": end_dt, "isPartial": end_d >= today,
    }


def resolve_comparison(period: dict, mode: str, custom_start: str | None = None,
                        custom_end: str | None = None) -> dict:
    """Previous equivalent period (same elapsed real-time duration, ending
    the instant before the current period starts), same period last year
    (calendar-aligned a year back, same elapsed duration), or an explicit
    custom range. See resolve_period() above for why elapsed-matching a
    still-partial current period against a FULL prior period would always
    read as a manufactured decline."""
    start_d, end_d = period["startDate"], period["endDate"]
    elapsed = period["endDt"] - period["startDt"]
    if mode == "last_year":
        try:
            prior_start_d = start_d.replace(year=start_d.year - 1)
        except ValueError:
            prior_start_d = start_d.replace(year=start_d.year - 1, day=28)
        prior_start_dt = bo._manila_midnight_utc(prior_start_d)
        prior_end_dt = prior_start_dt + elapsed
        label = "vs same period last year"
    elif mode == "custom" and custom_start and custom_end:
        prior_start_d = _dt.date.fromisoformat(custom_start)
        prior_end_d = _dt.date.fromisoformat(custom_end)
        prior_start_dt = bo._manila_midnight_utc(prior_start_d)
        prior_end_dt = bo._manila_midnight_utc(prior_end_d + _dt.timedelta(days=1))
        label = f"vs {prior_start_d.isoformat()} to {prior_end_d.isoformat()}"
        mode = "custom"
    else:
        mode = "previous_period"
        prior_start_d = start_d - (end_d - start_d) - _dt.timedelta(days=1)
        prior_start_dt = bo._manila_midnight_utc(prior_start_d)
        prior_end_dt = prior_start_dt + elapsed
        label = "vs previous period"
    return {"mode": mode, "label": label, "startDt": prior_start_dt, "endDt": prior_end_dt}


# ===========================================================================
# Series / breakdowns
# ===========================================================================
def _series_daily(conn, start: _dt.datetime, end: _dt.datetime) -> list[dict]:
    day_bd = timeutil.sql_business_date("o.created_at")
    rows = conn.execute(
        f"""
        SELECT {day_bd} AS day, COUNT(DISTINCT o.id) AS orders,
               COALESCE(SUM(ol.qty*ol.unit_price),0) AS revenue
          FROM customer_order o LEFT JOIN order_line ol ON ol.order_id = o.id
         WHERE o.status != 'void' AND o.created_at >= ? AND o.created_at < ?
         GROUP BY {day_bd} ORDER BY day
        """,
        (bo._sql(start), bo._sql(end)),
    ).fetchall()
    return [{"period": r["day"], "orders": r["orders"], "revenue": round(r["revenue"], 2)} for r in rows]


def _series_monthly(conn, start: _dt.datetime, end: _dt.datetime) -> list[dict]:
    month_expr = f"substr({timeutil.sql_business_date('o.created_at')}, 1, 7)"
    rows = conn.execute(
        f"""
        SELECT {month_expr} AS month, COUNT(DISTINCT o.id) AS orders,
               COALESCE(SUM(ol.qty*ol.unit_price),0) AS revenue
          FROM customer_order o LEFT JOIN order_line ol ON ol.order_id = o.id
         WHERE o.status != 'void' AND o.created_at >= ? AND o.created_at < ?
         GROUP BY month ORDER BY month
        """,
        (bo._sql(start), bo._sql(end)),
    ).fetchall()
    return [{"period": r["month"], "orders": r["orders"], "revenue": round(r["revenue"], 2)} for r in rows]


def _main_chart(conn, period: dict, comparison: dict) -> dict:
    """Daily aggregation inside a week/month; monthly inside a year. The
    comparison period's own series is included at the same granularity so
    the frontend can draw two aligned lines."""
    monthly = period["preset"] == "this_year" or (period["endDate"] - period["startDate"]).days > 95
    series_fn = _series_monthly if monthly else _series_daily
    return {
        "granularity": "month" if monthly else "day",
        "current": series_fn(conn, period["startDt"], period["endDt"]),
        "comparison": series_fn(conn, comparison["startDt"], comparison["endDt"]),
    }


def _busy_times(conn, start: _dt.datetime, end: _dt.datetime) -> dict:
    rows = conn.execute(
        """
        SELECT o.created_at FROM customer_order o
         WHERE o.status != 'void' AND o.created_at >= ? AND o.created_at < ?
        """,
        (bo._sql(start), bo._sql(end)),
    ).fetchall()
    hours = {h: 0 for h in range(24)}
    for r in rows:
        local = timeutil.to_manila(_dt.datetime.strptime(r["created_at"], _SQL_FMT).replace(tzinfo=_dt.timezone.utc))
        hours[local.hour] += 1
    busiest = max(hours, key=hours.get) if any(hours.values()) else None
    return {
        "hours": [{"hour": h, "orders": c} for h, c in sorted(hours.items())],
        "busiestHour": busiest, "totalOrders": len(rows),
    }


def _day_time_heatmap(conn, now: _dt.datetime, window_days: int = 84) -> dict:
    """Weekday x hour order/revenue density over a fixed trailing window
    (12 completed weeks), independent of whatever period the reader has
    picked for the rest of the page -- a heatmap needs enough cells
    populated to mean anything, which a single selected week/month usually
    doesn't have."""
    start = bo._manila_midnight_utc(timeutil.business_today(now) - _dt.timedelta(days=window_days))
    rows = conn.execute(
        """
        SELECT o.created_at, COALESCE(
                 (SELECT SUM(ol.qty*ol.unit_price) FROM order_line ol WHERE ol.order_id=o.id), 0
               ) AS revenue
          FROM customer_order o WHERE o.status != 'void' AND o.created_at >= ? AND o.created_at < ?
        """,
        (bo._sql(start), bo._sql(now)),
    ).fetchall()
    cells = {(wd, h): {"orders": 0, "revenue": 0.0} for wd in range(7) for h in range(24)}
    for r in rows:
        local = timeutil.to_manila(_dt.datetime.strptime(r["created_at"], _SQL_FMT).replace(tzinfo=_dt.timezone.utc))
        key = (local.weekday(), local.hour)
        cells[key]["orders"] += 1
        cells[key]["revenue"] += r["revenue"]
    available = len(rows) >= 20
    return {
        "available": available, "windowDays": window_days,
        "cells": [
            {"weekday": wd, "hour": h, "orders": v["orders"], "revenue": round(v["revenue"], 2)}
            for (wd, h), v in sorted(cells.items())
        ] if available else [],
    }


def _sales_highlights(conn, start: _dt.datetime, end: _dt.datetime) -> dict:
    daily = _series_daily(conn, start, end)
    best = max(daily, key=lambda d: d["revenue"], default=None)
    quiet = min((d for d in daily if d["orders"] > 0), key=lambda d: d["revenue"], default=None)
    busiest = max(daily, key=lambda d: d["orders"], default=None)
    largest_order = conn.execute(
        """
        SELECT o.id, o.order_no, o.created_at,
               COALESCE(SUM(ol.qty*ol.unit_price),0) AS total
          FROM customer_order o LEFT JOIN order_line ol ON ol.order_id = o.id
         WHERE o.status != 'void' AND o.created_at >= ? AND o.created_at < ?
         GROUP BY o.id ORDER BY total DESC LIMIT 1
        """,
        (bo._sql(start), bo._sql(end)),
    ).fetchone()
    return {
        "bestDay": best, "quietestDay": quiet, "busiestDay": busiest,
        "largestOrder": ({"orderNo": largest_order["order_no"], "total": round(largest_order["total"], 2),
                          "createdAt": largest_order["created_at"]} if largest_order and largest_order["total"] else None),
    }


def _item_changes(conn, cur_start, cur_end, prior_start, prior_end) -> tuple[dict, dict, list[tuple]]:
    """Per-item qty/revenue for both periods, plus the joined (name, cur,
    prior, pct_change) list for every item seen in either period -- the
    shared input for Customer Favorites, What's Driving Sales, and Watch
    This Item below, computed once per request."""
    current = do._item_qty_by_period(conn, cur_start, cur_end)
    prior = do._item_qty_by_period(conn, prior_start, prior_end)
    names = set(current) | set(prior)
    changes = []
    for name in names:
        cur = current.get(name, {"qty": 0, "revenue": 0.0, "orders": 0})
        pre = prior.get(name, {"qty": 0, "revenue": 0.0, "orders": 0})
        pct = bo._delta_pct(cur["qty"], pre["qty"]) if pre["orders"] >= MIN_PRIOR_SAMPLE_ORDERS else None
        changes.append((name, cur, pre, pct))
    return current, prior, changes


def _category_performance(conn, cur_start, cur_end, prior_start, prior_end) -> list[dict]:
    current = {c["category"]: c for c in do._category_mix(conn, cur_start, cur_end)}
    prior = {c["category"]: c for c in do._category_mix(conn, prior_start, prior_end)}
    out = []
    for cat, cur in current.items():
        pre = prior.get(cat)
        out.append({
            "category": cat, "revenue": cur["revenue"], "sharePct": cur["sharePct"],
            "deltaPct": bo._delta_pct(cur["revenue"], pre["revenue"]) if pre else None,
        })
    out.sort(key=lambda c: c["revenue"], reverse=True)
    return out


def _customer_favorites(current: dict, prior: dict, changes: list[tuple]) -> dict:
    cur_by_rev = sorted(current.items(), key=lambda kv: kv[1]["revenue"], reverse=True)
    cur_by_qty = sorted(current.items(), key=lambda kv: kv[1]["qty"], reverse=True)
    prior_by_rev = {n for n, _ in sorted(prior.items(), key=lambda kv: kv[1]["revenue"], reverse=True)[:5]}
    cur_top5 = {n for n, _ in cur_by_rev[:5]}

    scored = [c for c in changes if c[3] is not None]
    rising = sorted([c for c in scored if c[3] > 0], key=lambda c: c[3], reverse=True)
    slowing = sorted([c for c in scored if c[3] < 0], key=lambda c: c[3])
    consistent = [n for n in cur_top5 if n in prior_by_rev]
    new_momentum = [
        name for name, cur, pre, _ in changes
        if pre["orders"] == 0 and cur["orders"] >= MIN_PRIOR_SAMPLE_ORDERS and sum(p["orders"] for p in prior.values()) >= 20
    ]

    def item(n, src):
        v = src.get(n, {})
        return {"name": n, "qty": v.get("qty"), "revenue": v.get("revenue"), "orders": v.get("orders")}

    return {
        "mostOrdered": [item(n, current) for n, _ in cur_by_qty[:3]],
        "highestSales": [item(n, current) for n, _ in cur_by_rev[:3]],
        "risingFastest": [{**item(n, current), "pct": p} for n, c, pr, p in rising[:3]],
        "losingMomentum": [{**item(n, current), "pct": p} for n, c, pr, p in slowing[:3]],
        "consistentFavorite": [item(n, current) for n in consistent[:3]],
        "newMomentum": [item(n, current) for n in new_momentum[:3]],
    }


def _sales_to_stock(conn, current: dict, top_risks: list[dict], limit: int = 5,
                     top_seller_count: int = 10) -> list[dict]:
    """Links top-selling items to the specific at-risk ingredients their
    recipe actually consumes, via the same recipe-expansion resolver.
    expand_recipe() the POS deduction path and recipe costing both use --
    never a second recipe-flattening implementation.

    2026-10 audit finding: scoping this to only the top `top_seller_count`
    sellers' recipes meant an ingredient that is genuinely 'out' or
    'critical' (per the same forward-depletion engine Admin Today's
    Inventory Pulse reads) but used only by a slower-selling item was
    silently absent from this panel -- a real shortage, invisible on the one
    screen whose whole point is surfacing risk. `top_risks` is already
    capped and soonest-runway-first (see api.py's _top_risk_ingredients), so
    every out/critical ingredient still at risk after the demand-linked pass
    is appended here, `linkedToTopSeller: False`, rather than dropped -- this
    panel's guarantee is "every severe risk appears", not "only risks tied
    to today's best sellers"."""
    if not top_risks:
        return []
    out: list[dict] = []
    seen_ingredients: set[int] = set()

    if current:
        from .resolver import expand_recipe
        risk_by_ingredient = {r["ingredientId"]: r for r in top_risks}
        top_items = sorted(current.items(), key=lambda kv: kv[1]["revenue"], reverse=True)[:top_seller_count]
        item_names = [n for n, _ in top_items]
        if item_names:
            placeholders = ",".join("?" * len(item_names))
            menu_rows = conn.execute(
                f"SELECT id, name, recipe_id FROM menu_item WHERE name IN ({placeholders})", item_names
            ).fetchall()
            for m in menu_rows:
                if m["recipe_id"] is None:
                    continue
                try:
                    leaves = expand_recipe(conn, m["recipe_id"])
                except Exception:
                    continue
                for leaf in leaves:
                    risk = risk_by_ingredient.get(leaf.ingredient_id)
                    if risk and leaf.ingredient_id not in seen_ingredients:
                        seen_ingredients.add(leaf.ingredient_id)
                        out.append({
                            "itemName": m["name"], "ingredientName": risk["name"],
                            "status": risk["status"], "daysOfSupply": risk["daysOfSupply"],
                            "linkedToTopSeller": True,
                        })

    # Backfill: any 'out'/'critical' ingredient not already surfaced above,
    # soonest-runway-first (top_risks' own sort order) -- never silently
    # excluded just because it isn't behind this period's best sellers.
    for r in top_risks:
        if len(out) >= limit:
            break
        if r["status"] not in ("out", "critical") or r["ingredientId"] in seen_ingredients:
            continue
        seen_ingredients.add(r["ingredientId"])
        out.append({
            "itemName": None, "ingredientName": r["name"],
            "status": r["status"], "daysOfSupply": r["daysOfSupply"],
            "linkedToTopSeller": False,
        })

    return out[:limit]


def _watch_this_item(changes: list[tuple]) -> dict | None:
    scored = [c for c in changes if c[3] is not None and c[2]["orders"] >= MIN_PRIOR_SAMPLE_ORDERS]
    if not scored:
        return None
    name, cur, pre, pct = max(scored, key=lambda c: abs(c[3]))
    if abs(pct) < 15:
        return None
    direction = "up" if pct > 0 else "down"
    return {
        "name": name, "pct": pct, "direction": direction,
        "currentQty": cur["qty"], "priorQty": pre["qty"],
        "note": f"{name} sold {abs(pct):.0f}% {'more' if direction == 'up' else 'fewer'} units than the "
                f"comparison period ({cur['qty']:g} vs {pre['qty']:g}). Shown as an observed change, not a cause.",
    }


def _sales_pulse(period_totals: dict, deltas: dict, favorites: dict) -> list[str]:
    badges = []
    if deltas["revenue_pct"] is not None:
        if deltas["revenue_pct"] > 5:
            badges.append("Doing Well")
        elif deltas["revenue_pct"] < -5:
            badges.append("Worth Watching")
    if favorites["highestSales"]:
        top = favorites["highestSales"][0]
        if top["revenue"] and period_totals["revenue"] and top["revenue"] / period_totals["revenue"] >= 0.25:
            badges.append("Customer Favorite")
    if favorites["losingMomentum"] and len(favorites["losingMomentum"]) > len(favorites["risingFastest"]):
        badges.append("Losing Momentum")
    return badges or ["Steady"]


def _interpretation(deltas: dict, favorites: dict) -> str:
    rp = deltas["revenue_pct"]
    if rp is None:
        return "Not enough comparison data yet."
    driver = None
    if favorites["risingFastest"]:
        driver = f"{favorites['risingFastest'][0]['name']} drove much of the change."
    if rp > 2:
        return f"Revenue is up {rp:.1f}% against the comparison period." + (f" {driver}" if driver and rp > 0 else "")
    if rp < -2:
        return f"Revenue is down {abs(rp):.1f}% against the comparison period."
    return "Revenue is roughly flat against the comparison period."


# ===========================================================================
# Public entry points
# ===========================================================================
def owner_sales(conn, *, now: _dt.datetime, period_preset: str, comparison_mode: str,
                 custom_period: tuple[str | None, str | None] = (None, None),
                 custom_comparison: tuple[str | None, str | None] = (None, None),
                 combinations: dict | None, top_risks: list[dict]) -> dict:
    period = resolve_period(period_preset, now, *custom_period)
    comparison = resolve_comparison(period, comparison_mode, *custom_comparison)

    cur_totals = bo._period_totals(conn, period["startDt"], period["endDt"])
    prior_totals = bo._period_totals(conn, comparison["startDt"], comparison["endDt"])
    deltas = {
        "revenue_pct": bo._delta_pct(cur_totals["revenue"], prior_totals["revenue"]),
        "orders_pct": bo._delta_pct(cur_totals["orders"], prior_totals["orders"]),
        "items_pct": bo._delta_pct(cur_totals["items"], prior_totals["items"]),
        "avg_order_pct": bo._delta_pct(cur_totals["avg_order"], prior_totals["avg_order"]),
    }

    current_items, prior_items, changes = _item_changes(
        conn, period["startDt"], period["endDt"], comparison["startDt"], comparison["endDt"])
    favorites = _customer_favorites(current_items, prior_items, changes)
    categories = _category_performance(conn, period["startDt"], period["endDt"], comparison["startDt"], comparison["endDt"])

    earliest = conn.execute("SELECT MIN(created_at) AS t FROM customer_order WHERE status != 'void'").fetchone()["t"]
    yoy = None
    if earliest and (now - _dt.datetime.strptime(earliest, _SQL_FMT).replace(tzinfo=_dt.timezone.utc)).days >= 365 \
            and comparison_mode != "last_year":
        yoy_cmp = resolve_comparison(period, "last_year")
        yoy_totals = bo._period_totals(conn, yoy_cmp["startDt"], yoy_cmp["endDt"])
        yoy = {
            "label": yoy_cmp["label"], "revenue": yoy_totals["revenue"],
            "deltaPct": bo._delta_pct(cur_totals["revenue"], yoy_totals["revenue"]),
        }

    top_pair = None
    if combinations and combinations.get("combos"):
        best = combinations["combos"][0]
        top_pair = {"itemA": best.get("item_a"), "itemB": best.get("item_b"),
                    "confidence": best.get("confidence"), "lift": best.get("lift")}

    return {
        "asOf": bo._sql(now),
        "period": {"preset": period["preset"], "label": period["label"],
                   "startDate": period["startDate"].isoformat(), "endDate": period["endDate"].isoformat(),
                   "isPartial": period["isPartial"]},
        "comparison": {"mode": comparison["mode"], "label": comparison["label"]},
        "howAreSalesDoing": {
            "revenue": cur_totals["revenue"], "orders": cur_totals["orders"],
            "items": cur_totals["items"], "avgOrder": cur_totals["avg_order"],
            "deltas": deltas, "interpretation": _interpretation(deltas, favorites),
        },
        "mainSalesChart": _main_chart(conn, period, comparison),
        "whatChanged": {
            "revenue": {"current": cur_totals["revenue"], "prior": prior_totals["revenue"], "pct": deltas["revenue_pct"]},
            "orders": {"current": cur_totals["orders"], "prior": prior_totals["orders"], "pct": deltas["orders_pct"]},
            "itemsPerOrder": {
                "current": _items_per_order(cur_totals["items"], cur_totals["orders"]),
                "prior": _items_per_order(prior_totals["items"], prior_totals["orders"]),
                "pct": bo._delta_pct(
                    _items_per_order(cur_totals["items"], cur_totals["orders"]),
                    _items_per_order(prior_totals["items"], prior_totals["orders"]),
                ),
            },
            "avgOrder": {"current": cur_totals["avg_order"], "prior": prior_totals["avg_order"], "pct": deltas["avg_order_pct"]},
            "interpretation": _interpretation(deltas, favorites),
        },
        "salesPulse": _sales_pulse(cur_totals, deltas, favorites),
        "whatsDrivingSales": {
            "growing": [{"name": n, "revenueDelta": round(c["revenue"] - p["revenue"], 2), "pct": pct}
                        for n, c, p, pct in sorted([x for x in changes if x[3] is not None and x[3] > 0],
                                                    key=lambda x: x[1]["revenue"] - x[2]["revenue"], reverse=True)[:5]],
            "declining": [{"name": n, "revenueDelta": round(c["revenue"] - p["revenue"], 2), "pct": pct}
                          for n, c, p, pct in sorted([x for x in changes if x[3] is not None and x[3] < 0],
                                                      key=lambda x: x[1]["revenue"] - x[2]["revenue"])[:5]],
        },
        "dayTimeHeatmap": _day_time_heatmap(conn, now),
        "customerFavorites": favorites,
        "categoryPerformance": categories,
        "thenVsNow": {
            "current": cur_totals, "prior": prior_totals, "deltas": deltas,
            "comparisonLabel": comparison["label"], "yearOverYear": yoy,
        },
        "salesHighlights": _sales_highlights(conn, period["startDt"], period["endDt"]),
        "businessRhythm": do._business_rhythm(conn, now),
        "oftenOrderedTogether": {
            "available": bool(combinations and not combinations.get("insufficient_data")),
            "pairs": (combinations.get("combos", [])[:5] if combinations else []),
            "dataQualityNote": combinations.get("data_quality_note") if combinations else None,
        },
        "salesToStock": _sales_to_stock(conn, current_items, top_risks),
        "watchThisItem": _watch_this_item(changes),
        "recentOrders": do._recent_orders(conn, period["startDt"], period["endDt"], limit=15),
    }


def admin_sales(conn, *, now: _dt.datetime, period_preset: str, top_risks: list[dict]) -> dict:
    period = resolve_period(period_preset if period_preset in ("today", "this_week", "this_month") else "today", now)
    comparison_mode = "previous_period"
    comparison = resolve_comparison(period, comparison_mode)

    cur_totals = bo._period_totals(conn, period["startDt"], period["endDt"])
    prior_totals = bo._period_totals(conn, comparison["startDt"], comparison["endDt"])
    deltas = {
        "revenue_pct": bo._delta_pct(cur_totals["revenue"], prior_totals["revenue"]),
        "orders_pct": bo._delta_pct(cur_totals["orders"], prior_totals["orders"]),
        "items_pct": bo._delta_pct(cur_totals["items"], prior_totals["items"]),
        "avg_order_pct": bo._delta_pct(cur_totals["avg_order"], prior_totals["avg_order"]),
    }
    current_items = do._item_qty_by_period(conn, period["startDt"], period["endDt"])
    whats_selling = sorted(current_items.items(), key=lambda kv: kv[1]["qty"], reverse=True)[:8]
    categories = do._category_mix(conn, period["startDt"], period["endDt"])
    activity = do._order_activity(conn, period["startDt"], period["endDt"])
    busy = _busy_times(conn, period["startDt"], period["endDt"])

    rp = deltas["revenue_pct"]
    if rp is None:
        interpretation = "Not enough data from the comparison period yet."
    elif rp > 2:
        interpretation = f"Sales are running {rp:.1f}% ahead of the equivalent comparison window."
    elif rp < -2:
        interpretation = f"Sales are running {abs(rp):.1f}% behind the equivalent comparison window."
    else:
        interpretation = "Sales are roughly even with the equivalent comparison window."

    exceptions = []
    if activity["voided"] > 0:
        exceptions.append({"kind": "voided", "count": activity["voided"], "note": f"{activity['voided']} voided order(s) in this window."})
    if cur_totals["orders"] and activity["voided"] / cur_totals["orders"] > 0.1:
        exceptions.append({"kind": "high_void_rate", "note": "Voided orders are an unusually high share of today's total."})

    return {
        "asOf": bo._sql(now),
        "period": {"preset": period["preset"], "label": period["label"], "isPartial": period["isPartial"]},
        "salesSummary": {
            "revenue": cur_totals["revenue"], "orders": cur_totals["orders"], "items": cur_totals["items"],
            "avgOrder": cur_totals["avg_order"], "deltas": deltas, "comparisonLabel": comparison["label"],
            "interpretation": interpretation,
        },
        "compactSalesTrend": _main_chart(conn, period, comparison),
        "whatsSelling": [
            {"name": n, "qty": v["qty"], "revenue": v["revenue"], "orders": v["orders"]} for n, v in whats_selling
        ],
        "salesMix": categories,
        "orderActivity": activity,
        "busyTimes": busy,
        "salesToStock": _sales_to_stock(conn, current_items, top_risks),
        "exceptions": exceptions,
        "recentOrders": do._recent_orders(conn, period["startDt"], period["endDt"], limit=30),
    }
