"""Synthetic historical order generator.

Explicitly authorized, one-time, owner-requested: the console's Forecasts,
Anomaly Detection, and Reports sections need real order history to show
anything, and this database started at zero. The owner was warned this
writes directly into the live database (not a separate demo copy) and will
reduce real on-hand stock the same way real sales would, and chose to
proceed anyway so the live system shows populated data immediately.

Every order this writes goes through the real reservation pipeline
(reserve_order / commit_order) — the same code path a real POS punch uses —
so the resulting ledger, on-hand stock, and reservations are internally
consistent with a real system, not hand-waved numbers. Timestamps are
backdated after the fact so the history lands in the past, ending the day
before this is run (never on "today", so live testing afterward stays
clean).

Not imported by wt/api.py and never runs automatically — invoke deliberately:
    python -m wt.demo_seed [--days N] [--seed N]
"""
from __future__ import annotations

import datetime as _dt
import random
import sqlite3

from . import db
from .errors import InsufficientStockError
from .reservation import commit_order, receive_stock, release_order, reserve_order

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
    n = conn.execute("SELECT COUNT(*) AS n FROM customer_order").fetchone()["n"]
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


def _restock_shortfalls(conn: sqlite3.Connection, shortfalls: list[dict], ts: str) -> int:
    """Reactive restocking — exactly what a real shop does: something ran
    low, so it gets replenished. Sized generously (25x the immediate need)
    so the same ingredient isn't triggering a restock every other order."""
    for s in shortfalls:
        row = conn.execute("SELECT pack_size FROM ingredient WHERE id=?",
                           (s["ingredient_id"],)).fetchone()
        pack_size = row["pack_size"] or 1.0
        shortfall_base = max(0.0, s["needed"] - s["available"])
        top_up_base = shortfall_base + s["needed"] * 25
        purchase_qty = max(1.0, top_up_base / pack_size)
        before = _last_ledger_id(conn)
        receive_stock(conn, s["ingredient_id"], purchase_qty, lot_no=f"DEMO-{ts[:10]}", user_id=USER_ID)
        after = _last_ledger_id(conn)
        _backdate_ledger_range(conn, before, after, ts)
        conn.execute(
            "UPDATE fefo_batch SET received_at=? "
            "WHERE id = (SELECT MAX(id) FROM fefo_batch WHERE ingredient_id=?)",
            (ts, s["ingredient_id"]),
        )
    return len(shortfalls)


def _record_direct_ledger(conn: sqlite3.Connection, ingredient_id: int, delta: float,
                          reason: str, wastage_reason: str | None, ts: str, note: str | None = None) -> None:
    """Wastage/spoilage has no dedicated write path in the API yet (a real
    gap — see the console feature audit) so this writes the ledger row the
    same way reservation._ledger does, directly. ref_type is left NULL, not
    tagged with this script's name — a real wastage-recording feature
    wouldn't stamp an internal label onto a row the owner reads in plain
    English in Stock Movements, so this shouldn't either."""
    bal = conn.execute("SELECT on_hand_base FROM ingredient WHERE id=?", (ingredient_id,)).fetchone()["on_hand_base"]
    new_bal = max(0.0, bal + delta)
    conn.execute("UPDATE ingredient SET on_hand_base=?, updated_at=? WHERE id=?", (new_bal, ts, ingredient_id))
    conn.execute(
        "INSERT INTO stock_ledger (ingredient_id, delta_base, balance_after_base, reason, "
        "wastage_reason, ref_type, ref_id, user_id, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ingredient_id, delta, new_bal, reason, wastage_reason, None, None, USER_ID, note, ts),
    )


def _seed_wastage_history(conn: sqlite3.Connection, start: _dt.date, end: _dt.date, rng: random.Random) -> dict:
    """A believable baseline of small routine losses per ingredient (so the
    z-score check in wt/anomaly.py has enough same-ingredient history to be
    meaningful), plus a handful of deliberate outliers for it to actually
    catch."""
    raw_ings = _rows(conn.execute(
        """
        SELECT i.id, i.name, i.on_hand_base FROM ingredient i
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
            row = conn.execute("SELECT on_hand_base FROM ingredient WHERE id=?", (ing["id"],)).fetchone()
            magnitude = max(0.01, row["on_hand_base"] * rng.uniform(0.005, 0.02))
            _record_direct_ledger(conn, ing["id"], -magnitude, "wastage", "routine trim/prep loss", ts)
            routine += 1

    outlier_targets = rng.sample(raw_ings, k=min(3, len(raw_ings)))
    outliers = 0
    for ing in outlier_targets:
        day = start + _dt.timedelta(days=rng.randint(int(total_days * 0.4), total_days - 1))
        ts = f"{day.isoformat()} {rng.randint(9, 21):02d}:{rng.randint(0, 59):02d}:00"
        row = conn.execute("SELECT on_hand_base FROM ingredient WHERE id=?", (ing["id"],)).fetchone()
        magnitude = row["on_hand_base"] * rng.uniform(0.3, 0.5)
        if magnitude <= 0:
            continue
        _record_direct_ledger(conn, ing["id"], -magnitude, "spoilage",
                              "cooler outage — batch discarded", ts)
        outliers += 1

    return {"routine": routine, "outliers": outliers}


_LEDGER_NO_UPDATE_TRIGGER = (
    "CREATE TRIGGER trg_ledger_no_update BEFORE UPDATE ON stock_ledger\n"
    "BEGIN SELECT RAISE(ABORT, 'stock_ledger is append-only'); END"
)


def run(conn: sqlite3.Connection | None = None, days: int = 90, seed: int = 42,
       void_rate: float = 0.04, verbose: bool = True) -> dict:
    """Backdating a ledger row's created_at is an UPDATE, and stock_ledger is
    deliberately append-only (trg_ledger_no_update) everywhere else in this
    system — that guarantee matters for a real audit trail. This is the one
    intentional, explicitly-authorized exception: the trigger is dropped for
    the duration of this call and always recreated afterward, success or
    failure, so the append-only guarantee is back in force for every real
    write that follows."""
    conn = conn or db.get_conn()
    conn.execute("DROP TRIGGER IF EXISTS trg_ledger_no_update")
    try:
        return _run(conn, days, seed, void_rate, verbose)
    finally:
        conn.execute(_LEDGER_NO_UPDATE_TRIGGER)


def _run(conn: sqlite3.Connection, days: int, seed: int, void_rate: float, verbose: bool) -> dict:
    rng = random.Random(seed)
    end = _dt.date.today() - _dt.timedelta(days=1)   # never today — keeps live testing clean
    start = end - _dt.timedelta(days=days - 1)

    items = _sellable_items(conn)
    by_name = {i["name"]: i for i in items}
    price_by_id = {i["id"]: i["price"] for i in items}
    if not items:
        raise RuntimeError("no sellable menu items — seed the real inventory first")

    stats = {"orders": 0, "voided": 0, "restocked_ingredients": 0, "days": days}

    for day_idx in range(days):
        day = start + _dt.timedelta(days=day_idx)
        weekend_boost = 1.6 if day.weekday() in (4, 5, 6) else 1.0
        trend = 1.0 + 0.4 * (day_idx / max(1, days - 1))
        base_mean = 16 * weekend_boost * trend
        n_orders = max(4, round(rng.gauss(base_mean, base_mean * 0.2)))

        for _ in range(n_orders):
            hour = rng.choices([11, 12, 13, 17, 18, 19, 20], weights=[8, 14, 8, 10, 16, 14, 6])[0]
            ts = f"{day.isoformat()} {hour:02d}:{rng.randint(0, 59):02d}:00"
            head_count = rng.choices([1, 2, 3, 4, 5], weights=[15, 35, 25, 15, 10])[0]

            lines = _pick_lines(rng, items, by_name)
            if not lines:
                continue

            order_pk, order_no = _new_order(conn, f"T-{rng.randint(1, 14):02d}", head_count)
            _add_lines(conn, order_pk, lines, price_by_id)

            try:
                reserve_order(conn, order_pk, user_id=USER_ID)
            except InsufficientStockError as e:
                stats["restocked_ingredients"] += _restock_shortfalls(conn, e.shortfalls, ts)
                reserve_order(conn, order_pk, user_id=USER_ID)

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

        if verbose and (day_idx + 1) % 10 == 0:
            print(f"  ...{day_idx + 1}/{days} days, {stats['orders']} orders so far")

    stats["wastage"] = _seed_wastage_history(conn, start, end, rng)
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
