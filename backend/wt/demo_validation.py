"""
DEMO/SIMULATION-ONLY validation harness for the two IsolationForest passes
(stock-loss, wt.anomaly._evaluate_stock_loss / detect_anomalies; unlimited-
wings consumption, wt.anomaly._evaluate_consumption / detect_consumption_
anomalies) -- built to answer one question honestly: "does the existing
detection -> sampling -> human-labeling -> scientific-metrics pipeline
actually work," while the real system still has zero genuine human-
reviewed labels (see anomaly.py's own validation_metrics()/
scientific_validation_metrics() -- both report "unavailable" today because
nothing manufactures a label out of real data that does not exist yet).

HOW THIS STAYS ISOLATED FROM PRODUCTION (read this before changing it):
  - Every call here opens its OWN throwaway SQLite database, created fresh
    with db.connect(path) + db.migrate(conn) -- the exact same helper every
    unit test in tests/run_tests.py uses (see fresh_db() there). It is
    deleted at the end of the call (see _fresh_isolated_db()).
  - A real production connection, if passed in at all, is used ONLY for
    read-only SELECTs that learn realistic scale (real on-hand levels, real
    daily consumption rates, real head-count volumes, the real baseline
    lambda) -- see _real_stock_loss_scale()/_real_unli_scale(). Nothing
    generated here is ever INSERTed into that connection. _fresh_isolated_db()
    refuses to open anything but a brand-new temp-file/in-memory path, as a
    second, structural guard against ever writing demo rows into a real
    database by a future caller's mistake.
  - Every observation this module creates lives ONLY inside that throwaway
    database and in this module's own in-memory ground-truth maps
    (ledger_id/reconciliation_id -> "normal" | "injected_anomaly" + which
    rule). There is no is_synthetic column on the real schema for this to
    write to, by design (see anomaly.py's SYNTHETIC_SEED_USER_IDS
    docstring on why a schema-level provenance flag was rejected) -- the
    ground truth for THIS harness's own rows is simply never persisted
    anywhere a real query could find it.
  - Every dict this module returns carries "mode": "DEMO_SIMULATION" at the
    top level, and every metric inside is computed by calling the REAL,
    unmodified wt.anomaly functions (detect_anomalies/sample_for_review/
    submit_validation_label/scientific_validation_metrics and their
    consumption-side equivalents) against the throwaway database -- this
    module supplies data and ground-truth labels, never a precision/recall/
    F1 number it computed or chose itself.

HOW THIS TRANSITIONS TO REAL VALIDATION AUTOMATICALLY (no code change
needed later): validation_metrics()/scientific_validation_metrics() (and
their consumption-side equivalents) already read from the LIVE connection
passed to them, scoped to non-synthetic rows via _REAL_USER_CLAUSE/
_REAL_RECORDER_CLAUSE. The day real waste/count entries (or real unli
weigh-ins) start accumulating and get reviewed through the console's own
Validation Queue tab (Losses & Variance page), those SAME functions --
called the SAME way, on the real `wt.db.get_conn()` connection -- start
returning real, available metrics with no flag to flip and no code path to
switch: this demo harness and the real pipeline are the same code, pointed
at different databases. Nothing here needs to be torn out; it simply stops
being the only thing with enough labels to report a number.
"""

from __future__ import annotations

import datetime as _dt
import os
import random
import sqlite3
import tempfile
from pathlib import Path

from . import anomaly, db, timeutil

MODE = "DEMO_SIMULATION"


# ===========================================================================
# Isolation: a fresh, throwaway database per call
# ===========================================================================

def _fresh_isolated_db() -> tuple[sqlite3.Connection, str]:
    """A brand-new temp-file SQLite database, fully migrated, structurally
    incapable of being a real deployment's database: the path is one this
    function just created under the OS temp directory and is never a path
    db.db_path()/db.DEMO_DB could resolve to. Mirrors tests/run_tests.py's
    fresh_db() exactly (same technique, same reason: a real file path is
    needed because several of the real functions this harness calls use
    write_tx()/BEGIN IMMEDIATE, which :memory: handles less predictably
    across connections than a real temp file does)."""
    fd, path = tempfile.mkstemp(prefix="wt_demo_validation_", suffix=".db")
    os.close(fd)
    os.unlink(path)
    conn = db.connect(path)
    db.migrate(conn)
    return conn, path


def _scrub(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(path + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


# ===========================================================================
# Real-data grounding: read-only queries against a REAL connection, used
# only to parameterize realistic synthetic generation. Nothing here writes.
# ===========================================================================

def _real_stock_loss_scale(real_conn: sqlite3.Connection, lookback_days: int = 180,
                           n_ingredients: int = 10) -> list[dict]:
    """Up to n_ingredients real ingredients with real on-hand levels AND a
    computable real average daily consumption rate (from genuine sale_commit
    ledger activity, i.e. real orders x real recipes -- see resolver.
    order_requirements) -- both facts drawn directly from the real
    database, read-only. Returns [{name, base_unit, category, on_hand_base,
    real_avg_daily_consumption}], ingredients with zero real consumption or
    zero on-hand excluded (nothing to scale a realistic event against)."""
    since = timeutil.utc_bound_n_manila_days_ago(lookback_days)
    rows = real_conn.execute(
        """
        SELECT i.id, i.name, i.base_unit, i.category, i.on_hand_base,
               COALESCE(SUM(CASE WHEN l.reason = 'sale_commit' THEN -l.delta_base ELSE 0 END), 0)
                 AS total_sale_commit
          FROM ingredient i
          LEFT JOIN stock_ledger l ON l.ingredient_id = i.id AND l.created_at >= ?
         WHERE i.is_active = 1 AND i.on_hand_base > 0
         GROUP BY i.id
        HAVING total_sale_commit > 0
         ORDER BY total_sale_commit DESC
         LIMIT ?
        """,
        (since, n_ingredients * 3),
    ).fetchall()
    out = []
    for r in rows:
        avg_daily = r["total_sale_commit"] / lookback_days
        out.append({
            "name": r["name"], "base_unit": r["base_unit"], "category": r["category"],
            "on_hand_base": float(r["on_hand_base"]), "real_avg_daily_consumption": avg_daily,
        })
    random.Random(1).shuffle(out)  # fixed shuffle so "up to n" isn't always the same N top names
    return out[:n_ingredients]


def _real_unli_scale(real_conn: sqlite3.Connection, item_key: str = "wings",
                     lookback_days: int = 90) -> dict:
    """The real unli_item/baseline-coefficient row for `item_key`, and the
    real distribution of daily head counts actually served (bootstrap
    population, not a fitted distribution) -- pulled from real
    customer_order/order_line rows for whichever menu item that unli stream
    is keyed to. Raises if the real database has none of this yet (rather
    than silently inventing a lambda/ingredient that was never real)."""
    stream = real_conn.execute(
        """
        SELECT ui.id, ui.item_key, ui.label, ui.ingredient_id,
               i.name AS ingredient_name, i.base_unit
          FROM unli_item ui JOIN ingredient i ON i.id = ui.ingredient_id
         WHERE ui.item_key = ?
        """,
        (item_key,),
    ).fetchone()
    if not stream:
        raise LookupError(f"no real unli_item with item_key={item_key!r} to ground this on")
    baseline = real_conn.execute(
        "SELECT lambda_value FROM unli_coefficient WHERE unli_item_id=? AND role='baseline' "
        "ORDER BY id DESC LIMIT 1",
        (stream["id"],),
    ).fetchone()
    if not baseline:
        raise LookupError(f"no real baseline coefficient for unli_item_id={stream['id']}")

    since = timeutil.business_date_n_days_ago(lookback_days)
    head_rows = real_conn.execute(
        """
        SELECT date(co.created_at) AS d, SUM(ol.qty) AS heads
          FROM customer_order co
          JOIN order_line ol ON ol.order_id = co.id
          JOIN menu_item mi ON mi.id = ol.menu_item_id
         WHERE mi.name LIKE ? AND date(co.created_at) >= ?
         GROUP BY date(co.created_at)
        """,
        (f"%{stream['label']}%" if stream["label"] else "%Unlimited%", since),
    ).fetchall()
    real_head_counts = [int(r["heads"]) for r in head_rows if r["heads"] and r["heads"] >= 5]
    if not real_head_counts:
        raise LookupError("no real daily head-count observations to bootstrap from")

    return {
        "ingredient_name": stream["ingredient_name"], "base_unit": stream["base_unit"],
        "label": stream["label"], "baseline_lambda": float(baseline["lambda_value"]),
        "real_daily_head_counts": real_head_counts,
    }


# ===========================================================================
# Pass A -- stock-loss IsolationForest
# ===========================================================================

STOCK_LOSS_NORMAL_DAILY_SCALE_RANGE = (0.05, 0.3)   # x real daily consumption rate
STOCK_LOSS_NORMAL_ON_HAND_CAP_RANGE = (0.01, 0.06)  # x on_hand_base -- the OTHER ceiling, see below
STOCK_LOSS_SPIKE_FACTOR_RANGE = (8.0, 18.0)    # x that ingredient's OWN simulated peer-mean magnitude
STOCK_LOSS_FRACTION_RANGE = (0.35, 0.70)       # x on_hand_base (targets the large_fraction rule too)


def simulate_stock_loss_validation(
    real_conn: sqlite3.Connection, *, n_normal: int = 240, n_anomalous: int = 24,
    seed: int = 20261002, lookback_days: int = 90, n_ingredients: int = 10,
) -> dict:
    """Generates `n_normal` ordinary-looking stock-loss ledger entries and
    `n_anomalous` deliberately injected anomalies (each tagged with exactly
    which rule produced it), scaled against REAL ingredient on-hand levels
    and REAL average daily consumption rates read from `real_conn` (read-
    only -- see _real_stock_loss_scale()), writes them into a brand-new
    isolated database, then runs the ACTUAL, unmodified
    wt.anomaly.detect_anomalies() -> sample_for_review() ->
    submit_validation_label() -> scientific_validation_metrics() pipeline
    against it, submitting this harness's own KNOWN ground truth as the
    "human" label for every sampled record (every record is sampled here,
    not a partial draw, so the resulting confusion matrix covers the full
    generated population). The returned metrics are scientific_validation_
    metrics()'s own output, unedited.
    """
    rng = random.Random(seed)
    real_scale = _real_stock_loss_scale(real_conn, lookback_days=max(lookback_days, 180),
                                        n_ingredients=n_ingredients)
    if not real_scale:
        return {"mode": MODE, "available": False,
               "reason": "no real ingredient had both on-hand stock and real sale_commit "
                         "consumption to ground a realistic simulation on"}

    conn, path = _fresh_isolated_db()
    try:
        ingredient_ids = []
        for ing in real_scale:
            cur = conn.execute(
                "INSERT INTO ingredient (ingredient_id, name, category, base_unit, "
                "purchase_unit, pack_size, pack_size_status, on_hand_base, unit_cost, "
                "unit_cost_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"DEMO-{len(ingredient_ids)+1:03d}", ing["name"], ing["category"],
                 ing["base_unit"], ing["base_unit"], 1.0, "verified", ing["on_hand_base"],
                 1.0, "verified"),
            )
            ingredient_ids.append((cur.lastrowid, ing))
        conn.commit()

        now = timeutil.business_today()
        ground_truth: dict[int, dict] = {}   # ledger_id -> {"label": ..., "rule": ...}

        def _insert_event(ing_pk: int, ing_meta: dict, mag: float, on_hand: float,
                          label: str, rule: str | None, reason: str = "wastage") -> None:
            balance_before = on_hand
            balance_after = max(0.0, balance_before - mag)
            days_ago = rng.randint(0, max(1, lookback_days - 1))
            ts = (now - _dt.timedelta(days=days_ago)).isoformat() + " 13:00:00"
            cur = conn.execute(
                "INSERT INTO stock_ledger (ingredient_id, delta_base, balance_after_base, "
                "reason, wastage_reason, user_id, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (ing_pk, -mag, balance_after, reason,
                 "wastage" if reason == "wastage" else None, None,
                 f"[DEMO_SIMULATION] {label}" + (f" ({rule})" if rule else ""), ts),
            )
            conn.execute("UPDATE ingredient SET on_hand_base=? WHERE id=?", (balance_after, ing_pk))
            ground_truth[cur.lastrowid] = {"label": label, "rule": rule}

        # --- normal population: realistic, modest losses --------------------
        # Two independent real-data ceilings, the smaller wins: a slice of a
        # real day's usage, AND a small slice of real on-hand. Some sampled
        # real ingredients legitimately turn over faster than their current
        # on-hand snapshot (confirmed separately: several ratios of real
        # daily consumption to real on-hand exceed 1.0x) -- without the
        # on-hand cap, a "normal" event on one of those would itself exceed
        # RULE_FRACTION_THRESHOLD and get rule-flagged, which would be this
        # harness manufacturing false positives through a sizing mistake,
        # not a real detector weakness.
        for _ in range(n_normal):
            ing_pk, ing = ingredient_ids[rng.randrange(len(ingredient_ids))]
            daily_lo, daily_hi = STOCK_LOSS_NORMAL_DAILY_SCALE_RANGE
            cap_lo, cap_hi = STOCK_LOSS_NORMAL_ON_HAND_CAP_RANGE
            from_daily = ing["real_avg_daily_consumption"] * rng.uniform(daily_lo, daily_hi)
            from_on_hand_cap = ing["on_hand_base"] * rng.uniform(cap_lo, cap_hi)
            mag = max(min(from_daily, from_on_hand_cap), 0.01)
            _insert_event(ing_pk, ing, mag, ing["on_hand_base"], "normal", None)

        # --- injected anomalies: two documented, independent rules ----------
        normal_mag_midpoint = sum(STOCK_LOSS_NORMAL_ON_HAND_CAP_RANGE) / 2
        for i in range(n_anomalous):
            ing_pk, ing = ingredient_ids[rng.randrange(len(ingredient_ids))]
            use_fraction_rule = (i % 2 == 0)
            if use_fraction_rule:
                lo, hi = STOCK_LOSS_FRACTION_RANGE
                mag = ing["on_hand_base"] * rng.uniform(lo, hi)
                rule = "large_fraction_of_on_hand"
            else:
                lo, hi = STOCK_LOSS_SPIKE_FACTOR_RANGE
                peer_mag = ing["on_hand_base"] * normal_mag_midpoint
                mag = max(peer_mag, 0.01) * rng.uniform(lo, hi)
                rule = "relative_magnitude_spike"
            _insert_event(ing_pk, ing, mag, ing["on_hand_base"], "injected_anomaly", rule)
        conn.commit()

        # --- run the ACTUAL, unmodified pipeline -----------------------------
        flagged = anomaly.detect_anomalies(conn, lookback_days=lookback_days)
        sample_result = anomaly.sample_for_review(
            conn, n_flagged=n_normal + n_anomalous,
            n_near_threshold=n_normal + n_anomalous, n_random=n_normal + n_anomalous,
            lookback_days=lookback_days, rng=rng,
        )
        samples = conn.execute(
            "SELECT id, ledger_id FROM anomaly_validation_sample"
        ).fetchall()
        labeled = 0
        for s in samples:
            truth = ground_truth.get(s["ledger_id"])
            if truth is None:
                continue  # should not happen: every ledger row here is ours
            human_label = "confirmed_anomaly" if truth["label"] == "injected_anomaly" else "expected_or_normal"
            anomaly.submit_validation_label(conn, s["id"], human_label,
                                            note="[DEMO_SIMULATION] ground-truth label",
                                            user_id="demo_simulation_harness")
            labeled += 1

        metrics = anomaly.scientific_validation_metrics(conn)

        rule_breakdown: dict[str, int] = {}
        for truth in ground_truth.values():
            if truth["rule"]:
                rule_breakdown[truth["rule"]] = rule_breakdown.get(truth["rule"], 0) + 1

        return {
            "mode": MODE,
            "available": True,
            "methodology": (
                "Synthetic stock-loss ledger rows generated in an isolated, throwaway "
                "database (never the production database), scaled against REAL ingredient "
                "on-hand levels and REAL average daily sale_commit consumption rates read "
                "from the live database read-only. The REAL, unmodified detect_anomalies() "
                "-> sample_for_review() -> submit_validation_label() -> "
                "scientific_validation_metrics() pipeline is then run against that database, "
                "with this harness submitting its own known ground truth as the 'human' "
                "label for every sampled record."
            ),
            "seed": seed,
            "real_ingredients_used": [ing["name"] for _, ing in ingredient_ids],
            "generated": {
                "n_normal": n_normal, "n_anomalous": n_anomalous,
                "anomaly_injection_rules": {
                    "large_fraction_of_on_hand": (
                        f"magnitude = on_hand_base * Uniform{STOCK_LOSS_FRACTION_RANGE} "
                        f"-- also exercises the real RULE_FRACTION_THRESHOLD=0.25 rule layer"
                    ),
                    "relative_magnitude_spike": (
                        f"magnitude = (on_hand_base * {normal_mag_midpoint:.3f}, the normal "
                        f"population's own on-hand-cap midpoint) * Uniform{STOCK_LOSS_SPIKE_FACTOR_RANGE} "
                        f"-- targets the relative_mag feature IsolationForest trains on"
                    ),
                },
                "rule_breakdown": rule_breakdown,
            },
            "detect_anomalies_flagged_count": len(flagged),
            "sample_for_review_result": sample_result,
            "labels_submitted": labeled,
            "scientific_validation_metrics": metrics,
        }
    finally:
        conn.close()
        _scrub(path)


# ===========================================================================
# Pass B -- unlimited-wings consumption IsolationForest
# ===========================================================================

UNLI_NORMAL_NOISE_SIGMA = 0.06            # +/- fraction around theoretical, normal measurement noise
UNLI_ANOMALY_DEVIATION_RANGE = (0.35, 0.90)   # +/- fraction deviation from theoretical, injected


def simulate_unli_consumption_validation(
    real_conn: sqlite3.Connection, *, n_normal: int = 60, n_anomalous: int = 18,
    seed: int = 20261002, lookback_days: int = 120, item_key: str = "wings",
) -> dict:
    """Generates `n_normal` realistic reconciled service days and
    `n_anomalous` deliberately injected over/under-consumption days for the
    real 'wings' unli stream, scaled against the REAL baseline lambda and
    REAL bootstrap-sampled daily head counts read from `real_conn` (read-
    only -- see _real_unli_scale()). Writes them into a brand-new isolated
    database, then runs the ACTUAL, unmodified wt.anomaly.
    detect_consumption_anomalies() -> sample_consumption_for_review() ->
    submit_consumption_validation_label() ->
    scientific_consumption_validation_metrics() pipeline against it,
    submitting this harness's own known ground truth as the human label for
    every sampled record.
    """
    rng = random.Random(seed)
    scale = _real_unli_scale(real_conn, item_key=item_key, lookback_days=lookback_days)

    conn, path = _fresh_isolated_db()
    try:
        ing_cur = conn.execute(
            "INSERT INTO ingredient (ingredient_id, name, category, base_unit, "
            "purchase_unit, pack_size, pack_size_status, on_hand_base, unit_cost, "
            "unit_cost_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("DEMO-UNLI-001", scale["ingredient_name"], "wet_food", scale["base_unit"],
             scale["base_unit"], 1.0, "verified", 10_000_000.0, 1.0, "verified"),
        )
        ingredient_pk = ing_cur.lastrowid
        item_cur = conn.execute(
            "INSERT INTO unli_item (item_key, label, ingredient_id) VALUES (?,?,?)",
            (item_key, scale["label"] or "Unlimited Wings (demo)", ingredient_pk),
        )
        unli_item_pk = item_cur.lastrowid
        coef_cur = conn.execute(
            "INSERT INTO unli_coefficient (unli_item_id, lambda_value, role, status) "
            "VALUES (?,?,?,?)",
            (unli_item_pk, scale["baseline_lambda"], "baseline", "verified"),
        )
        coefficient_pk = coef_cur.lastrowid
        conn.commit()

        now = timeutil.business_today()
        lam = scale["baseline_lambda"]
        head_pool = scale["real_daily_head_counts"]
        ground_truth: dict[int, dict] = {}   # reconciliation_id -> {"label", "rule"}

        def _insert_day(heads: int, actual_per_head: float, label: str, rule: str | None,
                        day_offset: int) -> None:
            theoretical = heads * lam
            actual = heads * actual_per_head
            variance = actual - theoretical
            variance_ratio = (variance / theoretical) if theoretical > 0 else None
            opening = max(theoretical, actual) * 1.2
            added = 0.0
            closing = max(0.0, opening + added - actual)
            service_date = (now - _dt.timedelta(days=day_offset)).isoformat()
            tag = f"[DEMO_SIMULATION] {label}" + (f" ({rule})" if rule else "")
            cur = conn.execute(
                "INSERT INTO unli_reconciliation (service_date, unli_item_id, heads, "
                "theoretical_base, actual_base, variance_base, variance_ratio, coefficient_id, "
                "opening_base, added_base, closing_base, status, validation_state, "
                "correction_base, recorded_by, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (service_date, unli_item_pk, heads, theoretical, actual, variance,
                 variance_ratio, coefficient_pk, opening, added, closing,
                 "within_tolerance", "validated", 0.0, None, tag),
            )
            ground_truth[cur.lastrowid] = {"label": label, "rule": rule}

        # distinct day offsets so every synthetic row has its own service_date
        # (service_date, unli_item_id) is unique in the real schema/workflow
        offsets = list(range(1, n_normal + n_anomalous + 1))
        rng.shuffle(offsets)
        offset_iter = iter(offsets)

        for _ in range(n_normal):
            heads = head_pool[rng.randrange(len(head_pool))]
            noise = rng.gauss(0.0, UNLI_NORMAL_NOISE_SIGMA)
            actual_per_head = lam * (1.0 + noise)
            _insert_day(heads, actual_per_head, "normal", None, next(offset_iter))

        for i in range(n_anomalous):
            heads = head_pool[rng.randrange(len(head_pool))]
            lo, hi = UNLI_ANOMALY_DEVIATION_RANGE
            direction = 1 if (i % 2 == 0) else -1
            deviation = direction * rng.uniform(lo, hi)
            actual_per_head = lam * (1.0 + deviation)
            rule = "overconsumption_spike" if direction > 0 else "underconsumption_spike"
            _insert_day(heads, actual_per_head, "injected_anomaly", rule, next(offset_iter))
        conn.commit()

        # --- run the ACTUAL, unmodified pipeline -----------------------------
        flagged = anomaly.detect_consumption_anomalies(conn, lookback_days=lookback_days)
        sample_result = anomaly.sample_consumption_for_review(
            conn, n_flagged=n_normal + n_anomalous,
            n_near_threshold=n_normal + n_anomalous, n_random=n_normal + n_anomalous,
            lookback_days=lookback_days, rng=rng,
        )
        samples = conn.execute(
            "SELECT id, reconciliation_id FROM unli_consumption_validation_sample"
        ).fetchall()
        labeled = 0
        for s in samples:
            truth = ground_truth.get(s["reconciliation_id"])
            if truth is None:
                continue
            human_label = ("confirmed_anomaly" if truth["label"] == "injected_anomaly"
                           else "expected_or_explained")
            anomaly.submit_consumption_validation_label(
                conn, s["id"], human_label,
                note="[DEMO_SIMULATION] ground-truth label", user_id="demo_simulation_harness",
            )
            labeled += 1

        metrics = anomaly.scientific_consumption_validation_metrics(conn)

        rule_breakdown: dict[str, int] = {}
        for truth in ground_truth.values():
            if truth["rule"]:
                rule_breakdown[truth["rule"]] = rule_breakdown.get(truth["rule"], 0) + 1

        return {
            "mode": MODE,
            "available": True,
            "methodology": (
                "Synthetic unli_reconciliation rows generated in an isolated, throwaway "
                "database (never the production database) for the real 'wings' stream, "
                "scaled against the REAL baseline lambda coefficient and REAL bootstrap-"
                "sampled daily head counts read from the live database read-only. The REAL, "
                "unmodified detect_consumption_anomalies() -> sample_consumption_for_review() "
                "-> submit_consumption_validation_label() -> "
                "scientific_consumption_validation_metrics() pipeline is then run against "
                "that database, with this harness submitting its own known ground truth as "
                "the 'human' label for every sampled record."
            ),
            "seed": seed,
            "real_grounding": {
                "ingredient_name": scale["ingredient_name"], "baseline_lambda": lam,
                "real_daily_head_count_samples_available": len(head_pool),
                "real_daily_head_count_range": [min(head_pool), max(head_pool)],
            },
            "generated": {
                "n_normal": n_normal, "n_anomalous": n_anomalous,
                "anomaly_injection_rules": {
                    "overconsumption_spike": (
                        f"actual_per_head = baseline_lambda * (1 + Uniform{UNLI_ANOMALY_DEVIATION_RANGE})"
                    ),
                    "underconsumption_spike": (
                        f"actual_per_head = baseline_lambda * (1 - Uniform{UNLI_ANOMALY_DEVIATION_RANGE})"
                    ),
                },
                "normal_noise_model": f"actual_per_head = baseline_lambda * (1 + Gauss(0, {UNLI_NORMAL_NOISE_SIGMA}))",
                "rule_breakdown": rule_breakdown,
            },
            "detect_consumption_anomalies_flagged_count": len(flagged),
            "sample_consumption_for_review_result": sample_result,
            "labels_submitted": labeled,
            "scientific_consumption_validation_metrics": metrics,
        }
    finally:
        conn.close()
        _scrub(path)
