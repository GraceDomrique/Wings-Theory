"""Single authoritative timezone / business-date utility.

Persisted timestamps stay UTC everywhere in this system: every schema
DEFAULT is `datetime('now')`, which SQLite always evaluates in UTC
regardless of the host OS's own timezone setting. That part was already
correct and nothing here changes it.

"Business day" -- which calendar day an order, refill, or expiry belongs
to for a manager reading a screen -- is Asia/Manila, a fixed UTC+8 offset
with no DST, for this single-branch Philippine deployment. Converting
between the two is exactly one calculation, and it belongs in exactly one
place: here.

Before this module existed, that conversion was done ad hoc and
inconsistently -- some code read `date.today()` / `datetime.now()` (the
*host machine's* local clock, correct only by coincidence when the host
happens to be set to UTC+8) and some SQL truncated a UTC timestamp to a
calendar day with bare `date(col)` (the UTC calendar day, wrong for the
~8 hours of every Manila day that fall before UTC has rolled over). The
two disagreed for roughly a third of every day, in different code paths
by different amounts. Every caller in this codebase that needs to know
"what business day is this" now goes through one of the functions below.
"""
from __future__ import annotations

import datetime as _dt

# A fixed +8:00 offset, not zoneinfo.ZoneInfo("Asia/Manila") -- Manila has
# held that exact offset with no DST since 1962, so a literal offset is
# already precisely correct here, and it sidesteps a real portability trap:
# stdlib zoneinfo has no bundled tzdata on Windows (only on platforms that
# ship an IANA database or have the `tzdata` PyPI package installed), so
# ZoneInfo("Asia/Manila") raises ZoneInfoNotFoundError on a bare Windows
# Python install -- exactly this project's dev environment. If this
# deployment ever adds a second branch in a different timezone, this whole
# module (not just this constant) needs to become per-branch.
MANILA_UTC_OFFSET_HOURS = 8
MANILA_TZ = _dt.timezone(_dt.timedelta(hours=MANILA_UTC_OFFSET_HOURS), name="Asia/Manila")
_SQL_SHIFT = f"+{MANILA_UTC_OFFSET_HOURS} hours"


def utc_now() -> _dt.datetime:
    """The real current instant, timezone-aware UTC. The only wall-clock
    read in this module -- every other function here is pure arithmetic
    on an instant, so a test can pass one in via `now=` instead of having
    to wait on (or mock) the real clock."""
    return _dt.datetime.now(_dt.timezone.utc)


def to_manila(instant: _dt.datetime) -> _dt.datetime:
    """A UTC (or any timezone-aware) instant, converted to Manila
    wall-clock time. A naive instant is treated as already UTC, matching
    how every timestamp in this database is stored."""
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=_dt.timezone.utc)
    return instant.astimezone(MANILA_TZ)


def business_today(now: _dt.datetime | None = None) -> _dt.date:
    """Today's date on the shop's own calendar -- Manila local, derived
    from the real UTC instant rather than the host OS's local clock. That
    makes this correct no matter what timezone the server itself is
    configured for, unlike the `date.today()` calls this replaces, which
    were only ever correct because this dev machine happens to also sit
    at UTC+8.

    `now` exists only so a test can supply an exact simulated instant;
    real callers always omit it.
    """
    return to_manila(now or utc_now()).date()


def business_today_iso(now: _dt.datetime | None = None) -> str:
    return business_today(now).isoformat()


def business_date_of(utc_timestamp: str) -> str:
    """The Manila business date a stored SQLite UTC timestamp falls on,
    as an ISO date string. Accepts the usual `YYYY-MM-DD HH:MM:SS` form
    SQLite writes, or an ISO 'T'-separated variant.

    A bare `YYYY-MM-DD` with no time component (some received_at values
    are imported this way, with no time-of-day ever recorded) is returned
    unchanged -- there is no instant to shift, so the date already *is*
    the business date, not a UTC one masquerading as one.

    This is the Python-side equivalent of sql_business_date() below -- use
    that one instead when the conversion can happen inside the query;
    this one is for the few call sites (e.g. FEFO expiry annotation) that
    already have the timestamp as a Python string and would otherwise
    have truncated it with a bare `[:10]` slice, which -- same bug in a
    different spot -- reads the UTC calendar date, not the Manila one.
    """
    raw = utc_timestamp[:19].replace("T", " ")
    if len(raw) <= 10:
        return raw
    parsed = _dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_dt.timezone.utc)
    return to_manila(parsed).date().isoformat()


def sql_business_date(column_expr: str) -> str:
    """SQL fragment: the Manila calendar date of a UTC timestamp column.

    Use this instead of bare `date(col)` anywhere a query buckets rows by
    "what day did this happen on" -- a customer order, a refill, a
    reconciliation window. `date(col)` alone truncates to the *UTC*
    calendar day, which is one day behind the correct Manila business day
    for any row timestamped between UTC 16:00 and 23:59:59 (Manila
    00:00-07:59:59) -- roughly a third of every day.
    """
    return f"date({column_expr}, '{_SQL_SHIFT}')"


def business_date_n_days_ago(n: int, now: _dt.datetime | None = None) -> str:
    """Manila calendar date N days before today, as an ISO date string --
    for bounding a DATE-valued column such as unli_reconciliation.service_date
    (e.g. `service_date >= business_date_n_days_ago(30)`)."""
    return (business_today(now) - _dt.timedelta(days=n)).isoformat()


def utc_bound_n_manila_days_ago(n_days: int, now: _dt.datetime | None = None) -> str:
    """The exact UTC timestamp string for Manila midnight, N Manila
    calendar days before (and including) today -- for bounding a
    TIMESTAMP-valued column such as stock_ledger.created_at or
    customer_order.created_at (e.g. `created_at >= utc_bound_n_manila_days_ago(30)`)
    so a "last N days" window's edge lands exactly on a Manila business-day
    boundary instead of an arbitrary point partway through one.

    n_days=30 means "today plus the 29 Manila days before it" -- the same
    span a caller gets from zero-filling `range(29, -1, -1)` days back from
    business_today(), which is exactly how the daily-series builders in
    forecasting.py construct their day list. The two must stay in sync.
    """
    anchor = business_today(now) - _dt.timedelta(days=n_days - 1)
    manila_midnight = _dt.datetime.combine(anchor, _dt.time.min, tzinfo=MANILA_TZ)
    return manila_midnight.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
