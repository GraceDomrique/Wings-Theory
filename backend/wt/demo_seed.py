"""Synthetic historical operating-history generator — development/demo only.

Explicitly authorized, for system testing, demonstration and thesis
evaluation against an isolated development database
(backend/dev/wings_theory_dev.db), never against production
(backend/wings_theory.db). The orchestration script that calls this
(scripts/generate_demo_history.py) is what enforces that separation by
setting WT_DB before this module ever touches a connection — this module
itself just writes through whatever connection it is given or that
db.get_conn() resolves to, so getting WT_DB right before calling it is the
caller's responsibility, not this file's.

Every write here goes through the real service-layer functions a real POS
punch, a real receiving screen, a real physical count, and a real kitchen
scale would use — reserve_order/commit_order/release_order, receive_stock,
record_count, record_wastage, open_session/record_refill/serve_refill/
void_refill/correct_refill, record_reconciliation. Nothing computes a
forecast, an anomaly, or a replenishment figure directly; those come out of
running the real pipelines against this history afterward. The one thing
this module does that no ordinary API caller can: backdate the timestamps
those functions stamp with datetime('now'), so the history lands in the
past instead of piling up on today. That requires briefly dropping the two
append-only triggers (stock_ledger, order_charge) that would otherwise
reject the UPDATE — see run() for exactly how narrowly that's scoped, and
why it's always restored even if generation fails partway through.

Every row this writes carries user_id='demo_seed' (orders, ledger entries,
receiving, counts, refills, reconciliation) — the one marker convention,
applied consistently, that makes every synthetic row identifiable and
queryable as such after the fact.

Not imported by wt/api.py and never runs automatically — invoke
deliberately via scripts/generate_demo_history.py, not this module's CLI
directly, unless you have already independently verified WT_DB.
"""
from __future__ import annotations

import datetime as _dt
import random
import sqlite3

from . import db
from .errors import InsufficientStockError, InvalidStateError
from .reservation import (
    commit_order, receive_stock, record_count, record_wastage, release_order,
    reserve_order,
)
from .refills import (
    correct_refill, list_flavors, open_session, record_refill, serve_refill,
    void_refill,
)
from .unli import provisional_for_date, record_reconciliation

USER_ID = "demo_seed"

# Weighted popularity — a restaurant called Wings Theory sells a lot of
# wings. Anything not listed defaults to a modest weight of 3.
POPULARITY: dict[str, int] = {
    "Wings Platter 6 pcs w/ Fries": 10,
    "Wing Rack 12 pcs (2 Flavors)": 8,
    "Sulit Deal – 3 pcs Wings + Rice": 9,
    "Spicy Korean Wing Bowl": 6,
    "Chicken Salpicao": 5,
    "Creamy Chicken & Mushroom Alfredo": 5,
    "Taiwanese Porkchop": 4,
    "Creamy Sausage Pasta": 4,
    "Cheesy Bacon Fries": 7,
    "Onion Rings": 5,
    "Fish & Chips with Tartar": 4,
    "Bacon & Egg Silog": 5,
    "Beef Tapa Silog": 4,
    "1 Cup Rice": 9,
    "Ranch Dip": 6,
    "1 Pitcher Iced Tea": 8,
    "Coke in Can": 7,
    "Sprite in Can": 5,
}

# Items that tend to get ordered together — real co-occurrence, so a
# frequent-itemset pass over this history finds honest combos, not noise.
COMBOS: dict[str, list[tuple[str, float]]] = {
    "Wings Platter 6 pcs w/ Fries": [("1 Pitcher Iced Tea", 0.45)],
    "Sulit Deal – 3 pcs Wings + Rice": [("Ranch Dip", 0.4)],
    "Cheesy Bacon Fries": [("Coke in Can", 0.35)],
    "Wing Rack 12 pcs (2 Flavors)": [("1 Cup Rice", 0.4)],
}

# Category -> (healthy_min, healthy_max, approaching_min, approaching_max),
# in days from the receiving date, for ingredients on a real fixed expiry
# date. Rough, generic shelf-life bands by kind of good — a real owner would
# know these per-supplier; a synthetic dataset just needs them plausible.
# shelf_life_estimate ingredients never use this (see _pick_expires_at).
SHELF_LIFE_WINDOWS: dict[str, tuple[int, int, int, int]] = {
    "wet_food": (10, 25, 3, 9),
    "dry_food": (120, 365, 14, 30),
    "packaging": (365, 730, 60, 120),
    "consumable": (180, 400, 30, 60),
    "resale": (90, 365, 20, 45),
}

# How often (in days, roughly) a category's stock actually gets checked for
# restocking, before the on-hand-vs-target gate below even asks whether a
# delivery is warranted. Perishables get looked at often; packaging rarely.
RESTOCK_CHECK_CADENCE: dict[str, int] = {
    "wet_food": 4, "dry_food": 10, "packaging": 18, "consumable": 12, "resale": 8,
}
# How many packs "well stocked" means, by category — the reference level
# _seed_periodic_receiving tops an ingredient back up toward once it has
# actually run low from real consumption, not a level received on a clock.
RESTOCK_TARGET_PACKS: dict[str, float] = {
    "wet_food": 4.0, "dry_food": 2.0, "packaging": 1.5, "consumable": 2.0, "resale": 2.0,
}


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _sellable_items(conn: sqlite3.Connection) -> list[dict]:
    items = _rows(conn.execute(
        """
        SELECT m.id, m.name, m.price
          FROM menu_item m JOIN v_menu_item_availability a ON a.menu_item_pk = m.id
         WHERE m.pos_visible = 1 AND m.is_unli = 0 AND a.sellable = 1
        """
    ))
    for it in items:
        it["weight"] = POPULARITY.get(it["name"], 3)
    return items


def _weighted_choice(rng: random.Random, items: list[dict]) -> dict:
    total = sum(i["weight"] for i in items)
    r = rng.uniform(0, total)
    upto = 0.0
    for it in items:
        upto += it["weight"]
        if upto >= r:
            return it
    return items[-1]


def _pick_lines(rng: random.Random, items: list[dict], by_name: dict[str, dict]) -> dict[int, int]:
    n = rng.choices([1, 2, 3], weights=[35, 45, 20])[0]
    chosen = []
    for _ in range(n):
        item = _weighted_choice(rng, items)
        chosen.append(item)
        for paired_name, prob in COMBOS.get(item["name"], []):
            paired = by_name.get(paired_name)
            if paired and rng.random() < prob:
                chosen.append(paired)
    lines: dict[int, int] = {}
    for it in chosen:
        add = 2 if rng.random() < 0.15 else 1
        lines[it["id"]] = lines.get(it["id"], 0) + add
    return lines


def _new_order(conn: sqlite3.Connection, table_label: str, head_count: int) -> tuple[int, str]:
    # MAX(id), not COUNT(*): customer_order.id is AUTOINCREMENT (SQLite never
    # reuses it), so this stays gap-safe if any row has ever been deleted --
    # COUNT(*) would eventually regenerate an order_no that an earlier,
    # still-surviving row already holds once enough new rows fill the gap.
    # Identical to the COUNT(*) result whenever the table has never had a
    # row removed (id and row count march together from an empty table).
    n = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM customer_order").fetchone()["n"]
    order_no = f"WT-{n + 1001}"
    cur = conn.execute(
        "INSERT INTO customer_order (order_no, table_label, head_count) VALUES (?,?,?)",
        (order_no, table_label, head_count),
    )
    return cur.lastrowid, order_no


def _add_lines(conn: sqlite3.Connection, order_pk: int, lines: dict[int, int],
               price_by_id: dict[int, float]) -> None:
    for menu_id, qty in lines.items():
        conn.execute(
            "INSERT INTO order_line (order_id, menu_item_id, qty, unit_price) VALUES (?,?,?,?)",
            (order_pk, menu_id, qty, price_by_id[menu_id]),
        )


def _last_ledger_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM stock_ledger").fetchone()["n"]


def _backdate_ledger_range(conn: sqlite3.Connection, lo_id: int, hi_id: int, ts: str) -> None:
    if hi_id > lo_id:
        conn.execute("UPDATE stock_ledger SET created_at=? WHERE id > ? AND id <= ?", (ts, lo_id, hi_id))


# ---------------------------------------------------------------------------
# receiving, with realistic expiry
# ---------------------------------------------------------------------------
def _pick_expires_at(rng: random.Random, category: str, expiry_mode: str,
                     received_date: _dt.date, guarantee_fresh: bool = False) -> str | None:
    """A believable expiry date for one receiving event — mixed healthy/
    approaching/at-risk so the resulting batches read like a real shop's
    stock, not a uniform wall of one color. None for anything not on a real
    fixed date: a shelf_life_estimate ingredient (fresh produce, herbs) is
    tracked from received_at + its own configured shelf_life_days by FEFO
    itself, and supplying a date here would just be a second, competing
    number nobody asked for; 'none'-mode ingredients have nothing to invent
    a date for at all.

    guarantee_fresh (_restock_shortfalls only): skips the small roll that
    would otherwise land this batch already-expired or expiring within
    days. A reactive top-up exists for exactly one reason — to make a
    shortfall usable right now — so a result that can come back unusable
    on arrival would defeat its own purpose; the "a few batches are already
    near-death" realism is a property of the general stocked-shelf picture
    _seed_periodic_receiving builds, not of this one delivery.
    """
    if expiry_mode != "fixed_date":
        return None
    healthy_lo, healthy_hi, near_lo, near_hi = SHELF_LIFE_WINDOWS.get(category, (90, 200, 14, 30))
    roll = rng.random()
    if guarantee_fresh:
        days_out = rng.randint(healthy_lo, healthy_hi)
        expires = received_date + _dt.timedelta(days=days_out)
        # reserve_order's own FEFO freshness check runs at the REAL wall-
        # clock moment this generator happens to execute, not at the
        # synthetic `received_date` being backdated to -- so a "fresh for
        # healthy_hi days from received_date" batch can still read as
        # already-expired the instant it's created, whenever received_date
        # is further in the past than a short category's own healthy
        # window (wet_food: 10-25 days) reaches. That turned a reactive
        # shortfall top-up into one that could never actually resolve the
        # shortfall it exists to fix -- not a data problem, a clock
        # mismatch. Clamped to real-today-plus-the-same-window instead,
        # which keeps every OTHER guarantee_fresh call (received_date at or
        # near real "now", the tool's normal day-by-day use) unchanged.
        real_today = _dt.date.today()
        if expires <= real_today:
            expires = real_today + _dt.timedelta(days=days_out)
        return expires.isoformat()
    if roll < 0.75:
        days_out = rng.randint(healthy_lo, healthy_hi)
    elif roll < 0.93:
        days_out = rng.randint(near_lo, near_hi)
    else:
        days_out = rng.randint(-5, 3)   # a small number land already-expired or expiring imminently
    return (received_date + _dt.timedelta(days=days_out)).isoformat()


def _receive_realistic(conn: sqlite3.Connection, ingredient_id: int, category: str,
                       expiry_mode: str, purchase_qty: float, ts: str, day: _dt.date,
                       rng: random.Random, lot_prefix: str = "DEMO",
                       guarantee_fresh: bool = False) -> dict:
    expires_at = _pick_expires_at(rng, category, expiry_mode, day, guarantee_fresh=guarantee_fresh)
    before = _last_ledger_id(conn)
    result = receive_stock(conn, ingredient_id, purchase_qty, lot_no=f"{lot_prefix}-{day.isoformat()}",
                           expires_at=expires_at, user_id=USER_ID)
    after = _last_ledger_id(conn)
    _backdate_ledger_range(conn, before, after, ts)
    conn.execute(
        "UPDATE fefo_batch SET received_at=? "
        "WHERE id = (SELECT MAX(id) FROM fefo_batch WHERE ingredient_id=?)",
        (ts, ingredient_id),
    )
    return result


def _restock_shortfalls(conn: sqlite3.Connection, shortfalls: list[dict], ts: str,
                        day: _dt.date, rng: random.Random) -> int:
    """Reactive restocking — exactly what a real shop does when something
    runs low mid-service: sized generously (25x the immediate need) so the
    same ingredient isn't triggering a restock every other order.

    InsufficientStockError carries two different shortfall shapes (see
    errors.py): a quantity shortage (`needed`/`available`, from reserve_order
    or _refill_requirements running short on hand) — receiving more stock
    fixes exactly this — versus a structural block (`reason`, e.g. "pack
    size not set", from an ingredient reservation/refills.py refuses to
    touch at all). Receiving more of an ingredient that cannot be deducted
    changes nothing; retrying afterward would just raise the identical
    error again. Those are skipped here rather than crashing on a missing
    `needed` key, and are reported back so the caller knows this ingredient
    needs a data fix, not another delivery."""
    restocked = 0
    for s in shortfalls:
        if "needed" not in s:
            continue
        row = conn.execute("SELECT pack_size, category, expiry_mode FROM ingredient WHERE id=?",
                           (s["ingredient_id"],)).fetchone()
        pack_size = row["pack_size"] or 1.0
        shortfall_base = max(0.0, s["needed"] - s["available"])
        top_up_base = shortfall_base + s["needed"] * 25
        purchase_qty = max(1.0, top_up_base / pack_size)
        _receive_realistic(conn, s["ingredient_id"], row["category"], row["expiry_mode"],
                           purchase_qty, ts, day, rng, lot_prefix="DEMO", guarantee_fresh=True)
        restocked += 1
    return restocked


def _seed_periodic_receiving(conn: sqlite3.Connection, start: _dt.date, end: _dt.date,
                             rng: random.Random) -> int:
    """Proactive receiving, driven by actual on-hand level, not a calendar.

    Every RESTOCK_CHECK_CADENCE[category] days, per ingredient, check
    whether it has actually run low relative to a plausible "well stocked"
    reference level for its category — and only then receive. A fast-
    depleting ingredient (real recipe usage most days) gets topped up on
    most checks; a slow mover can go the entire window without ever
    triggering one. That is what makes receiving frequency track actual
    consumption instead of an arbitrary schedule — the "correlation between
    operations, not independent random events" requirement, applied to
    receiving specifically.
    """
    ings = _rows(conn.execute(
        """
        SELECT i.id, i.category, i.expiry_mode, i.pack_size, i.purchase_unit
          FROM ingredient i JOIN v_ingredient_readiness r ON r.id = i.id
         WHERE i.is_active = 1 AND r.is_deductable = 1
           AND i.kind = 'raw' AND i.pack_size IS NOT NULL
        """
    ))
    total_days = (end - start).days + 1
    events = 0
    for ing in ings:
        cadence = RESTOCK_CHECK_CADENCE.get(ing["category"], 10)
        target_packs = RESTOCK_TARGET_PACKS.get(ing["category"], 2.0)
        pack_size = ing["pack_size"] or 1.0
        target_base = target_packs * pack_size
        day_idx = rng.randint(0, cadence - 1)   # stagger so every ingredient isn't checked the same day
        while day_idx < total_days:
            day = start + _dt.timedelta(days=day_idx)
            on_hand = conn.execute(
                "SELECT on_hand_base FROM ingredient WHERE id=?", (ing["id"],)
            ).fetchone()["on_hand_base"]
            if on_hand < 0.4 * target_base:
                hour = rng.randint(8, 11)   # deliveries arrive in the morning
                ts = f"{day.isoformat()} {hour:02d}:{rng.randint(0, 59):02d}:00"
                top_up_base = (target_base - on_hand) * rng.uniform(0.9, 1.2)
                purchase_qty = max(1.0, round(top_up_base / pack_size))
                _receive_realistic(conn, ing["id"], ing["category"], ing["expiry_mode"],
                                   purchase_qty, ts, day, rng, lot_prefix="RECV")
                events += 1
            day_idx += cadence + rng.randint(-1, 2)   # jitter — deliveries aren't clockwork
    return events


# ---------------------------------------------------------------------------
# stock counts
# ---------------------------------------------------------------------------
def _seed_stock_counts(conn: sqlite3.Connection, start: _dt.date, end: _dt.date,
                       rng: random.Random) -> int:
    """Periodic physical counts through the real record_count() path.
    Mostly small variance, occasionally moderate, rarely significant —
    matching a real closing-shift count, not a stress test."""
    ings = _rows(conn.execute(
        """
        SELECT i.id FROM ingredient i JOIN v_ingredient_readiness r ON r.id = i.id
         WHERE i.is_active = 1 AND r.is_deductable = 1 AND i.kind = 'raw'
        """
    ))
    if not ings:
        return 0
    total_days = (end - start).days + 1
    events = 0
    day_idx = rng.randint(2, 6)
    while day_idx < total_days:
        day = start + _dt.timedelta(days=day_idx)
        sample = rng.sample(ings, k=min(rng.randint(6, 10), len(ings)))
        hour = rng.randint(21, 23)   # counted at close
        for ing in sample:
            ts = f"{day.isoformat()} {hour:02d}:{rng.randint(0, 59):02d}:00"
            on_hand = conn.execute(
                "SELECT on_hand_base FROM ingredient WHERE id=?", (ing["id"],)
            ).fetchone()["on_hand_base"]
            if on_hand <= 0:
                continue
            roll = rng.random()
            if roll < 0.70:
                pct = rng.uniform(-0.02, 0.02)
            elif roll < 0.92:
                pct = rng.choice([-1, 1]) * rng.uniform(0.03, 0.06)
            else:
                pct = rng.choice([-1, 1]) * rng.uniform(0.10, 0.20)
            actual = max(0.0, round(on_hand * (1 + pct), 2))
            before = _last_ledger_id(conn)
            record_count(conn, ing["id"], actual, note="periodic physical count", user_id=USER_ID)
            after = _last_ledger_id(conn)
            _backdate_ledger_range(conn, before, after, ts)
            events += 1
        day_idx += 7 + rng.randint(-1, 2)
    return events


# ---------------------------------------------------------------------------
# wastage
# ---------------------------------------------------------------------------
_ROUTINE_NOTES = ["prep trim", "over-portioned", "dropped during prep", "spillage during service"]
_OUTLIER_NOTES = ["cooler outage — batch discarded", "delivery arrived damaged and unusable",
                  "expired before use — discarded"]


def _seed_wastage_history(conn: sqlite3.Connection, start: _dt.date, end: _dt.date,
                          rng: random.Random) -> dict:
    """A believable baseline of small routine losses per ingredient (so the
    anomaly check has enough same-ingredient history to be meaningful),
    plus a handful of deliberate outliers for it to actually catch. Through
    the real record_wastage() path — the routine/outlier design is
    unchanged from the original version of this generator, only the write
    path is: record_wastage() already existed and is what the console's own
    waste button calls, so there was no real reason for this to hand-roll
    the ledger math a second time."""
    raw_ings = _rows(conn.execute(
        """
        SELECT i.id, i.name FROM ingredient i
          JOIN v_ingredient_readiness r ON r.id = i.id
         WHERE i.is_active = 1 AND r.is_deductable = 1 AND i.kind = 'raw'
         ORDER BY i.name
        """
    ))
    if not raw_ings:
        return {"routine": 0, "outliers": 0}

    routine_targets = rng.sample(raw_ings, k=min(6, len(raw_ings)))
    routine = 0
    total_days = (end - start).days + 1
    for ing in routine_targets:
        n_events = rng.randint(9, 14)
        for _ in range(n_events):
            day = start + _dt.timedelta(days=rng.randint(0, total_days - 1))
            ts = f"{day.isoformat()} {rng.randint(9, 21):02d}:{rng.randint(0, 59):02d}:00"
            on_hand = conn.execute("SELECT on_hand_base FROM ingredient WHERE id=?", (ing["id"],)).fetchone()["on_hand_base"]
            if on_hand <= 0:
                continue
            magnitude = min(on_hand * 0.999, max(0.01, on_hand * rng.uniform(0.005, 0.02)))
            before = _last_ledger_id(conn)
            record_wastage(conn, ing["id"], round(magnitude, 3), reason="wastage",
                           note=rng.choice(_ROUTINE_NOTES), user_id=USER_ID)
            after = _last_ledger_id(conn)
            _backdate_ledger_range(conn, before, after, ts)
            routine += 1

    outlier_targets = rng.sample(raw_ings, k=min(3, len(raw_ings)))
    outliers = 0
    for ing in outlier_targets:
        day = start + _dt.timedelta(days=rng.randint(int(total_days * 0.4), total_days - 1))
        ts = f"{day.isoformat()} {rng.randint(9, 21):02d}:{rng.randint(0, 59):02d}:00"
        on_hand = conn.execute("SELECT on_hand_base FROM ingredient WHERE id=?", (ing["id"],)).fetchone()["on_hand_base"]
        if on_hand <= 0:
            continue
        magnitude = min(on_hand * 0.999, on_hand * rng.uniform(0.3, 0.5))
        if magnitude <= 0:
            continue
        before = _last_ledger_id(conn)
        record_wastage(conn, ing["id"], round(magnitude, 3), reason="spoilage",
                       note=rng.choice(_OUTLIER_NOTES), user_id=USER_ID)
        after = _last_ledger_id(conn)
        _backdate_ledger_range(conn, before, after, ts)
        outliers += 1

    return {"routine": routine, "outliers": outliers}


# ---------------------------------------------------------------------------
# unlimited orders, refills, reconciliation
# ---------------------------------------------------------------------------
def _seed_unlimited_order(conn: sqlite3.Connection, day: _dt.date, hour: int, rng: random.Random,
                          flavors_included: list[dict], flavors_premium: list[dict],
                          unli_menu_id: int, price: float, *,
                          head_count: int | None = None,
                          walkout_rate: float = 0.03,
                          random_table_label: bool = True,
                          extra_lines: dict[int, int] | None = None,
                          extra_prices: dict[int, float] | None = None,
                          restock_reference_date: _dt.date | None = None) -> dict | None:
    """One unlimited table, start to close: order -> session -> 1-5 refill
    rounds, each individually held-then-served through the real two-phase
    refill path -> occasionally a round voided or corrected along the way.
    Returns None for a table that voided before the kitchen ever started
    (no ledger impact, matching release_order's own contract) or stats
    otherwise.

    head_count and walkout_rate default to this function's original
    self-contained synthetic model (a random 1-6 head draw, 3% pre-kitchen
    walkout) so demo_seed.py's own CLI and scripts/generate_demo_history.py
    -- callers with no real head-count or cancellation data of their own --
    are unaffected. wt/xlsx_history_import.py passes both explicitly: a
    source-derived head_count (the sum of the unli flavor-pick line
    quantities the workbook recorded for that Order ID) and walkout_rate=0.0,
    since the source records no cancellation concept at all and introducing
    one here would misrepresent completed historical sales as walkouts.

    random_table_label=False (also xlsx_history_import.py only) skips the
    T-01..T-14 random draw and leaves table_label NULL instead -- the
    workbook has no table/seating field at all, and a randomly generated one
    is not a fact about that historical sale.

    extra_lines/extra_prices (same shape as _add_lines' own two arguments)
    let a caller attach ordinary a-la-carte order_line rows to this SAME
    order -- reserved, committed and receipted as one order, one ticket. This
    exists for xlsx_history_import.py's mixed workbook orders (one source
    Order ID listing both unlimited-wings and a-la-carte lines): the two are
    no longer replayed as two separate customer_order rows just because this
    function only used to know how to create the unlimited half.

    restock_reference_date (xlsx_history_import.py only): the date a
    reactive mid-replay restock's shelf life is measured from, independent
    of `day` (the historical sale being replayed). Replaying a sale from
    long ago can still run an ingredient's FABRICATED current on-hand level
    short and need a reactive top-up (see _restock_shortfalls) -- but that
    top-up is a bookkeeping fiction invented purely to let the historical
    SALE go through, never a real delivery that happened back on `day`. A
    perishable category's shelf life (SHELF_LIFE_WINDOWS -- as little as 10
    days for wet_food) measured from a `day` that can be 600+ days in the
    past would make the fictional batch already expired the instant it is
    created, permanently and for no operational reason, under the exact
    expired-stock-is-unusable rule this system otherwise correctly relies
    on (migrations/0040, reservation._try_hold). Left None (every existing
    caller -- demo_seed.py's own bulk generator always calls with a recent
    `day`, where this distinction cannot arise), behavior is unchanged:
    the restock is measured from `day` exactly as before."""
    minute = rng.randint(0, 59)
    ts = f"{day.isoformat()} {hour:02d}:{minute:02d}:00"
    if head_count is None:
        head_count = rng.choices([1, 2, 3, 4, 5, 6], weights=[10, 30, 25, 18, 10, 7])[0]

    table_label = f"T-{rng.randint(1, 14):02d}" if random_table_label else None
    order_pk, order_no = _new_order(conn, table_label, head_count)
    conn.execute(
        "INSERT INTO order_line (order_id, menu_item_id, qty, unit_price) VALUES (?,?,?,?)",
        (order_pk, unli_menu_id, 1, price),
    )
    if extra_lines:
        _add_lines(conn, order_pk, extra_lines, extra_prices or {})

    # One retry after a restock isn't always enough when this runs on top
    # of a real, already-depleted stock position (as opposed to a freshly
    # seeded catalog with every ingredient at a full starting level, this
    # function's original scenario) -- a second ingredient can come up
    # short on the retry itself. Bounded at 3 attempts total rather than an
    # unbounded loop, so a genuinely unfixable structural block (see
    # _restock_shortfalls' own "reason", not "needed" distinction) still
    # surfaces as an error instead of spinning.
    for attempt in range(3):
        try:
            reserve_order(conn, order_pk, user_id=USER_ID)
            break
        except InsufficientStockError as e:
            if attempt == 2:
                raise
            _restock_shortfalls(conn, e.shortfalls, ts, restock_reference_date or day, rng)

    if rng.random() < walkout_rate:
        release_order(conn, order_pk, "demo: table left before kitchen started", user_id=USER_ID)
        conn.execute("UPDATE customer_order SET created_at=?, voided_at=? WHERE id=?", (ts, ts, order_pk))
        return None

    ledger_before = _last_ledger_id(conn)
    commit_order(conn, order_pk, user_id=USER_ID)
    ledger_after = _last_ledger_id(conn)
    _backdate_ledger_range(conn, ledger_before, ledger_after, ts)
    conn.execute(
        "UPDATE customer_order SET created_at=?, committed_at=? WHERE id=?",
        (ts, ts, order_pk),
    )

    session = open_session(conn, order_pk, user_id=USER_ID)
    session_pk = session["session_id"]
    conn.execute("UPDATE unli_session SET started_at=? WHERE id=?", (ts, session_pk))

    n_rounds = rng.choices([1, 2, 3, 4, 5], weights=[15, 35, 30, 15, 5])[0]
    served, voided, corrected = 0, 0, 0
    minute_cursor = minute
    round_hour = hour
    for _ in range(n_rounds):
        minute_cursor += rng.randint(8, 22)
        if minute_cursor >= 60:
            round_hour = min(round_hour + minute_cursor // 60, 23)
            minute_cursor %= 60
        round_ts = f"{day.isoformat()} {round_hour:02d}:{minute_cursor:02d}:00"

        use_premium = flavors_premium and rng.random() < 0.12
        flavor = rng.choice(flavors_premium) if use_premium else rng.choice(flavors_included)
        refill = None
        for attempt in range(3):
            try:
                refill = record_refill(conn, session_pk, flavor["id"], user_id=USER_ID)
                break
            except InsufficientStockError as e:
                if attempt == 2:
                    raise
                _restock_shortfalls(conn, e.shortfalls, round_ts, restock_reference_date or day, rng)
        refill_pk = refill["id"]
        conn.execute("UPDATE unli_refill SET created_at=? WHERE id=?", (round_ts, refill_pk))
        conn.execute("UPDATE unli_refill_line SET created_at=? WHERE refill_id=?", (round_ts, refill_pk))

        roll = rng.random()
        if roll < 0.03:
            # caught before it was cooked — no ledger impact, matching a
            # pending-round void exactly.
            void_refill(conn, refill_pk, reason="demo: wrong flavour caught before cooking", user_id=USER_ID)
            conn.execute("UPDATE unli_refill SET voided_at=? WHERE id=?", (round_ts, refill_pk))
            voided += 1
            continue

        ledger_before = _last_ledger_id(conn)
        serve_refill(conn, refill_pk, user_id=USER_ID)
        ledger_after = _last_ledger_id(conn)
        _backdate_ledger_range(conn, ledger_before, ledger_after, round_ts)
        conn.execute("UPDATE unli_refill SET served_at=? WHERE id=?", (round_ts, refill_pk))
        conn.execute("UPDATE unli_refill_line SET resolved_at=? WHERE refill_id=?", (round_ts, refill_pk))
        conn.execute("UPDATE order_charge SET created_at=? WHERE refill_id=?", (round_ts, refill_pk))
        served += 1

        if roll < 0.07 and served:
            # wrong flavour, caught after serving — void the served round and
            # send out a real replacement, exactly what a floor correction is.
            replacement_flavor = rng.choice(flavors_included)
            try:
                correction = correct_refill(conn, refill_pk, replacement_flavor["id"],
                                            reason="demo: wrong flavour served", user_id=USER_ID)
            except (InsufficientStockError, InvalidStateError):
                correction = None
            if correction:
                repl_pk = correction["id"]
                conn.execute("UPDATE unli_refill SET created_at=? WHERE id=?", (round_ts, repl_pk))
                conn.execute("UPDATE unli_refill_line SET created_at=? WHERE refill_id=?", (round_ts, repl_pk))
                ledger_before = _last_ledger_id(conn)
                serve_refill(conn, repl_pk, user_id=USER_ID)
                ledger_after = _last_ledger_id(conn)
                _backdate_ledger_range(conn, ledger_before, ledger_after, round_ts)
                conn.execute("UPDATE unli_refill SET served_at=? WHERE id=?", (round_ts, repl_pk))
                conn.execute("UPDATE unli_refill_line SET resolved_at=? WHERE refill_id=?", (round_ts, repl_pk))
                conn.execute("UPDATE order_charge SET created_at=? WHERE refill_id=?", (round_ts, repl_pk))
                corrected += 1
                served += 1

    end_ts = f"{day.isoformat()} {min(round_hour + 1, 23):02d}:{minute_cursor:02d}:00"
    conn.execute(
        "UPDATE unli_session SET status='completed', ended_at=?, completed_by=? WHERE id=?",
        (end_ts, USER_ID, session_pk),
    )
    conn.execute(
        "UPDATE customer_order SET status='served', served_at=? WHERE id=?",
        (end_ts, order_pk),
    )
    return {"order_pk": order_pk, "session_pk": session_pk, "rounds_served": served,
            "rounds_voided": voided, "rounds_corrected": corrected, "head_count": head_count}


def _seed_unlimited_reconciliation(conn: sqlite3.Connection, start: _dt.date, end: _dt.date,
                                   rng: random.Random, coverage: float = 0.85) -> dict:
    """Reconcile a realistic majority (not all) of the service-date/stream
    pairs that actually had committed unlimited orders — a real kitchen
    does not weigh every tray every single day. Distribution: mostly close
    to the provisional estimate (validated), a real minority meaningfully
    off (over-tolerance, held for review), and a rare few materially off —
    enough for IsolationForest to have genuine signal without every day
    being manufactured as an outlier.

    opening/closing/added readings are constructed backward from a target
    actual/theoretical ratio rather than invented independently, so
    actual = opening + added - closing always holds exactly, the same
    arithmetic record_reconciliation() itself uses.
    """
    streams = _rows(conn.execute("SELECT id, item_key, label FROM unli_item WHERE is_active=1"))
    total_days = (end - start).days + 1
    candidates = []
    for day_idx in range(total_days):
        day = start + _dt.timedelta(days=day_idx)
        service_date = day.isoformat()
        for s in streams:
            prov = provisional_for_date(conn, s["id"], service_date)
            if prov["heads"] > 0:
                candidates.append((service_date, s, prov))

    rng.shuffle(candidates)
    n_target = round(len(candidates) * coverage)
    chosen = candidates[:n_target]

    results = {"reconciled": 0, "within_tolerance": 0, "over_tolerance": 0,
               "under_tolerance": 0, "skipped_already_done": 0, "errors": 0}
    for service_date, s, prov in chosen:
        theoretical = prov["theoretical_base"]
        if theoretical <= 0:
            continue
        roll = rng.random()
        if roll < 0.75:
            ratio = rng.uniform(0.95, 1.10)              # normal — within or just at tolerance
        elif roll < 0.95:
            magnitude = rng.uniform(0.10, 0.30)           # over-tolerance — real operational variance
            ratio = (1 + magnitude) if rng.random() < 0.65 else (1 - magnitude)
        else:
            ratio = rng.choice([rng.uniform(0.35, 0.65), rng.uniform(1.5, 1.9)])   # rare genuine outlier
        actual = theoretical * ratio
        leftover_ratio = rng.uniform(0.03, 0.08)
        opening = actual / max(0.01, (1 - leftover_ratio))
        closing = max(0.0, opening - actual)
        added = 0.0 if rng.random() < 0.8 else opening * rng.uniform(0.05, 0.15)
        # re-derive opening so actual = opening + added - closing holds exactly
        opening = actual + closing - added

        try:
            r = record_reconciliation(
                conn, service_date, s["item_key"],
                opening_base=round(max(0.0, opening), 2),
                closing_base=round(max(0.0, closing), 2),
                added_base=round(max(0.0, added), 2),
                note="demo: synthetic reconciliation", user_id=USER_ID,
            )
        except InvalidStateError:
            results["skipped_already_done"] += 1
            continue
        except Exception:
            results["errors"] += 1
            continue

        end_of_day_ts = f"{service_date} 22:{rng.randint(0, 59):02d}:00"
        conn.execute("UPDATE unli_reconciliation SET created_at=? WHERE id=?", (end_of_day_ts, r["id"]))
        if r["ledger_id"]:
            conn.execute("UPDATE stock_ledger SET created_at=? WHERE id=?", (end_of_day_ts, r["ledger_id"]))
        results["reconciled"] += 1
        results[r["status"]] = results.get(r["status"], 0) + 1

    results["candidates"] = len(candidates)
    return results


_LEDGER_NO_UPDATE_TRIGGER = (
    "CREATE TRIGGER trg_ledger_no_update BEFORE UPDATE ON stock_ledger\n"
    "BEGIN SELECT RAISE(ABORT, 'stock_ledger is append-only'); END"
)
_ORDER_CHARGE_NO_UPDATE_TRIGGER = (
    "CREATE TRIGGER trg_order_charge_no_update BEFORE UPDATE ON order_charge\n"
    "BEGIN SELECT RAISE(ABORT, 'order_charge is append-only'); END"
)


def run(conn: sqlite3.Connection | None = None, days: int = 90, seed: int = 42,
       void_rate: float = 0.04, verbose: bool = True) -> dict:
    """Backdating a row's created_at is an UPDATE, and stock_ledger and
    order_charge are both deliberately append-only everywhere else in this
    system — that guarantee matters for a real audit trail. This is the
    one intentional, explicitly-authorized exception: both no-update
    triggers are dropped for the duration of this call and always
    recreated afterward, success or failure, so the append-only guarantee
    is back in force for every real write that follows, including if this
    function raises partway through."""
    conn = conn or db.get_conn()
    conn.execute("DROP TRIGGER IF EXISTS trg_ledger_no_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_order_charge_no_update")
    try:
        return _run(conn, days, seed, void_rate, verbose)
    finally:
        conn.execute(_LEDGER_NO_UPDATE_TRIGGER)
        conn.execute(_ORDER_CHARGE_NO_UPDATE_TRIGGER)


def _run(conn: sqlite3.Connection, days: int, seed: int, void_rate: float, verbose: bool) -> dict:
    rng = random.Random(seed)
    end = _dt.date.today() - _dt.timedelta(days=1)   # never today — keeps live testing clean
    start = end - _dt.timedelta(days=days - 1)

    items = _sellable_items(conn)
    by_name = {i["name"]: i for i in items}
    price_by_id = {i["id"]: i["price"] for i in items}
    if not items:
        raise RuntimeError("no sellable menu items — seed the real inventory first")

    unli_menu = conn.execute("SELECT id, price FROM menu_item WHERE is_unli=1 AND pos_visible=1 LIMIT 1").fetchone()
    flavors = list_flavors(conn, active_only=True)
    flavors_included = [f for f in flavors if f["flavor_type"] == "included"]
    flavors_premium = [f for f in flavors if f["flavor_type"] == "special"]
    can_seed_unli = bool(unli_menu and flavors_included)

    stats = {"orders": 0, "voided": 0, "restocked_ingredients": 0, "days": days,
             "unli_orders": 0, "unli_voided": 0, "unli_refills_served": 0,
             "unli_refills_voided": 0, "unli_refills_corrected": 0}

    for day_idx in range(days):
        day = start + _dt.timedelta(days=day_idx)
        weekend_boost = 1.6 if day.weekday() in (4, 5, 6) else 1.0
        trend = 1.0 + 0.4 * (day_idx / max(1, days - 1))
        # occasional unusually busy or unusually slow day — a real shop has
        # both, not just a smooth seasonal curve.
        day_mood = 1.0
        mood_roll = rng.random()
        if mood_roll < 0.06:
            day_mood = rng.uniform(1.4, 1.8)     # unusually busy
        elif mood_roll < 0.14:
            day_mood = rng.uniform(0.5, 0.7)     # unusually slow
        base_mean = 16 * weekend_boost * trend * day_mood
        n_orders = max(3, round(rng.gauss(base_mean, base_mean * 0.2)))

        for _ in range(n_orders):
            hour = rng.choices([11, 12, 13, 17, 18, 19, 20], weights=[8, 14, 8, 10, 16, 14, 6])[0]
            ts = f"{day.isoformat()} {hour:02d}:{rng.randint(0, 59):02d}:00"
            head_count = rng.choices([1, 2, 3, 4, 5], weights=[15, 35, 25, 15, 10])[0]

            lines = _pick_lines(rng, items, by_name)
            if not lines:
                continue

            order_pk, order_no = _new_order(conn, f"T-{rng.randint(1, 14):02d}", head_count)
            _add_lines(conn, order_pk, lines, price_by_id)

            # Same bounded-retry reasoning as _seed_unlimited_order's own
            # reserve_order call above: one restock isn't always enough
            # when running on top of a real, already-depleted position.
            for attempt in range(3):
                try:
                    reserve_order(conn, order_pk, user_id=USER_ID)
                    break
                except InsufficientStockError as e:
                    if attempt == 2:
                        raise
                    stats["restocked_ingredients"] += _restock_shortfalls(conn, e.shortfalls, ts, day, rng)

            if rng.random() < void_rate:
                release_order(conn, order_pk, "demo: customer changed mind", user_id=USER_ID)
                conn.execute("UPDATE customer_order SET created_at=?, voided_at=? WHERE id=?",
                            (ts, ts, order_pk))
                stats["voided"] += 1
                continue

            ledger_before = _last_ledger_id(conn)
            commit_order(conn, order_pk, user_id=USER_ID)
            ledger_after = _last_ledger_id(conn)
            _backdate_ledger_range(conn, ledger_before, ledger_after, ts)

            conn.execute(
                "UPDATE customer_order SET status='served', created_at=?, committed_at=?, "
                "served_at=? WHERE id=?",
                (ts, ts, ts, order_pk),
            )
            stats["orders"] += 1

        # unlimited tables — a realistic minority of the day's covers, same
        # weekday/weekend/mood scaling as everything else so consumption
        # actually correlates with how busy the day was.
        if can_seed_unli:
            n_unli = max(0, round(rng.gauss(n_orders * 0.15, 1.2)))
            for _ in range(n_unli):
                hour = rng.choices([11, 12, 13, 17, 18, 19, 20], weights=[6, 12, 8, 10, 18, 16, 6])[0]
                result = _seed_unlimited_order(conn, day, hour, rng, flavors_included, flavors_premium,
                                               unli_menu["id"], unli_menu["price"])
                if result is None:
                    stats["unli_voided"] += 1
                else:
                    stats["unli_orders"] += 1
                    stats["unli_refills_served"] += result["rounds_served"]
                    stats["unli_refills_voided"] += result["rounds_voided"]
                    stats["unli_refills_corrected"] += result["rounds_corrected"]

        if verbose and (day_idx + 1) % 10 == 0:
            print(f"  ...{day_idx + 1}/{days} days, {stats['orders']} orders, "
                  f"{stats['unli_orders']} unlimited so far")

    stats["wastage"] = _seed_wastage_history(conn, start, end, rng)
    stats["stock_counts"] = _seed_stock_counts(conn, start, end, rng)
    stats["receiving_events"] = _seed_periodic_receiving(conn, start, end, rng)
    if can_seed_unli:
        stats["reconciliation"] = _seed_unlimited_reconciliation(conn, start, end, rng)
    else:
        stats["reconciliation"] = {"reconciled": 0, "candidates": 0}
    stats["date_range"] = f"{start.isoformat()} to {end.isoformat()}"
    return stats


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    print(f"database: {db.db_path()}")
    db.require_destructive_confirmation(
        f"write {args.days} days of synthetic order history directly into"
    )
    c = db.get_conn()
    result = run(c, days=args.days, seed=args.seed)
    print("done:", result)
