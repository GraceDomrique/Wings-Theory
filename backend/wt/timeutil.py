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


def utc_now_sql(now: _dt.datetime | None = None) -> str:
    """The current instant as a string directly comparable to any
    `datetime('now')`-stamped column (e.g. app_user.locked_until) --
    SQLite's own 'YYYY-MM-DD HH:MM:SS' format, not ISO's 'T' separator."""
    return (now or utc_now()).strftime("%Y-%m-%d %H:%M:%S")


def utc_upper_bound_sql(now: _dt.datetime | None = None) -> str:
    """utc_now_sql() for use as an EXCLUSIVE upper bound (`created_at < ?`) on
    a column stamped at whole-second resolution.

    utc_now_sql() floors to the second, so a live instant like 18:57:23.488
    becomes '18:57:23' and `created_at < '18:57:23'` silently drops every row
    stamped during that same wall-clock second -- including one written a few
    milliseconds earlier, which is exactly "already happened" as far as the
    caller is concerned. Rounding a sub-second instant UP to the next whole
    second keeps the bound exclusive of anything genuinely at or after `now`
    (such a row's stamp is at least the next second) while no longer losing
    the second `now` is in. An instant already on a whole second -- every
    historical anchor a backtest supplies (e.g. 20:00:00) -- is returned
    unchanged, so historical-leakage guarantees are exactly as before."""
    instant = now or utc_now()
    if instant.microsecond:
        instant = instant.replace(microsecond=0) + _dt.timedelta(seconds=1)
    return instant.strftime("%Y-%m-%d %H:%M:%S")


def utc_sql_after_minutes(n: int, now: _dt.datetime | None = None) -> str:
    """utc_now_sql(), n minutes in the future -- for stamping a column
    meant to be compared against utc_now_sql() later (a lockout expiry)."""
    return ((now or utc_now()) + _dt.timedelta(minutes=n)).strftime("%Y-%m-%d %H:%M:%S")


def business_date_n_days_ago(n: int, now: _dt.datetime | None = None) -> str:
    """Manila calendar date N days before today, as an ISO date string --
    for bounding a DATE-valued column such as unli_reconciliation.service_date
    (e.g. `service_date >= business_date_n_days_ago(30)`)."""
    return (business_today(now) - _dt.timedelta(days=n)).isoformat()


def utc_bound_for_manila_date(date_iso: str) -> str:
    """The UTC timestamp string for Manila midnight on `date_iso`
    (YYYY-MM-DD) -- the lower bound for "that Manila business day onward"
    in a TIMESTAMP-valued column comparison. Pass the day AFTER the last day
    you want included (`date.fromisoformat(x) + timedelta(days=1)`) to get
    an exclusive upper bound for "through that day" instead -- same
    Manila-midnight arithmetic utc_bound_n_manila_days_ago already uses for
    its lower bound, just anchored on an explicit date rather than "N days
    before today"."""
    d = _dt.date.fromisoformat(date_iso)
    manila_midnight = _dt.datetime.combine(d, _dt.time.min, tzinfo=MANILA_TZ)
    return manila_midnight.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def utc_bound_n_manila_months_ago(n_months: int, now: _dt.datetime | None = None) -> str:
    """The exact UTC timestamp string for Manila midnight, N calendar
    months before today's Manila business date, same day-of-month (clamped
    to the target month's last day, e.g. Aug 31 - 3 months -> May 31, not
    an overflowed June 1). Calendar-month arithmetic, not `n * 30 days` --
    for a "last 3 months" rolling window (the POS terminal's visible order
    history) that should track wall-clock months the way an operator reads
    them, not drift against months of different lengths.
    """
    today = business_today(now)
    total_months = today.year * 12 + (today.month - 1) - n_months
    year, month0 = divmod(total_months, 12)
    month = month0 + 1
    import calendar
    day = min(today.day, calendar.monthrange(year, month)[1])
    anchor = _dt.date(year, month, day)
    manila_midnight = _dt.datetime.combine(anchor, _dt.time.min, tzinfo=MANILA_TZ)
    return manila_midnight.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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
