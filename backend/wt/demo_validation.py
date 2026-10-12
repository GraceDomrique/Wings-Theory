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
import statistics
import tempfile
from pathlib import Path

import openpyxl
from sklearn.metrics import average_precision_score, roc_auc_score

from . import anomaly, db, simulated_workbook_import as swi, timeutil

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


# ===========================================================================
# Pass A / Pass C, scored against the SECOND workbook's own injected ground
# truth (data/Wings_Theory_1_Year_Historical_Collection_simulatedd.xlsx,
# "Injected Synthetic Truth" column on Stock Counts/Unli Reconciliation) --
# NOT this module's own RNG-generated fixtures above. Same isolation
# contract as the rest of this file: a brand-new throwaway database, never
# the production one; the ONLY thing read from `real_conn` is the real
# ingredient/unli_flavor reference catalog (names, units, FK shape), never
# any row this harness itself writes. Ground truth lives only in this
# function's own Python dict, built straight from the workbook, exactly
# like the RNG harnesses above never persist their ground truth to a schema
# column. Every synthetic row here is written with user_id/recorded_by=NULL
# (not wt.simulated_workbook_import.USER_ID) specifically so the REAL
# detect_anomalies()/detect_flavor_consumption_anomalies() -- run completely
# unmodified -- see it as eligible inside THIS throwaway database; this is
# the opposite tagging choice from the production importer, which stamps
# USER_ID precisely so the SAME rows are structurally excluded once they
# land in the real database. Per the workbook's own "ML Evaluation
# Requirements" sheet, these IsolationForest results answer "does the
# synthetic pipeline correctly flag an injected anomaly," never "is this
# production-accurate" (production_validated is always False here).
# ===========================================================================

def _copy_ingredients_for_workbook(real_conn: sqlite3.Connection, iso_conn: sqlite3.Connection,
                                   norm_ids: set[str]) -> dict[str, dict]:
    rows = real_conn.execute(
        "SELECT id, ingredient_id, name, category, base_unit, purchase_unit, "
        "pack_size, pack_size_status, unit_cost, unit_cost_status, yield_factor FROM ingredient"
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        norm = swi._norm_ingredient_ref(r["ingredient_id"])
        if norm not in norm_ids:
            continue
        cur = iso_conn.execute(
            "INSERT INTO ingredient (ingredient_id, name, category, base_unit, "
            "purchase_unit, pack_size, pack_size_status, on_hand_base, unit_cost, "
            "unit_cost_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["ingredient_id"], r["name"], r["category"], r["base_unit"], r["purchase_unit"],
             r["pack_size"] or 1.0, r["pack_size_status"], 0.0,
             r["unit_cost"] or 1.0, r["unit_cost_status"]),
        )
        out[norm] = {"id": cur.lastrowid, "ingredient_id": r["ingredient_id"], "base_unit": r["base_unit"],
                    "pack_size": r["pack_size"], "yield_factor": r["yield_factor"]}
    iso_conn.commit()
    return out


def _copy_flavors_for_workbook(real_conn: sqlite3.Connection, iso_conn: sqlite3.Connection,
                               flavor_names: set[str]) -> dict[str, dict]:
    rows = real_conn.execute(
        "SELECT f.display_name, f.flavor_type, i.name, i.category, i.base_unit, "
        "i.purchase_unit, i.pack_size, i.pack_size_status, i.unit_cost, i.unit_cost_status "
        "FROM unli_flavor f JOIN ingredient i ON i.id = f.ingredient_id"
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        if r["display_name"] not in flavor_names:
            continue
        cur = iso_conn.execute(
            "INSERT INTO ingredient (ingredient_id, name, category, base_unit, "
            "purchase_unit, pack_size, pack_size_status, on_hand_base, unit_cost, "
            "unit_cost_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"WB-FLAVOR-{r['display_name']}", r["name"], r["category"], r["base_unit"],
             r["purchase_unit"], r["pack_size"] or 1.0, r["pack_size_status"], 0.0,
             r["unit_cost"] or 1.0, r["unit_cost_status"]),
        )
        ing_pk = cur.lastrowid
        cur2 = iso_conn.execute(
            "INSERT INTO unli_flavor (ingredient_id, display_name, flavor_type) VALUES (?,?,?)",
            (ing_pk, r["display_name"], r["flavor_type"] or "included"),
        )
        out[r["display_name"]] = {"id": cur2.lastrowid, "display_name": r["display_name"],
                                  "base_unit": r["base_unit"]}
    iso_conn.commit()
    return out


def _confusion_matrix(id_to_ref: dict[int, str], ground_truth: dict[str, str],
                      flagged_ids: set[int], anomaly_label: str) -> dict:
    tp = fp = fn = tn = 0
    for rid, ref in id_to_ref.items():
        is_anomaly = ground_truth.get(ref) == anomaly_label
        is_flagged = rid in flagged_ids
        if is_anomaly and is_flagged:
            tp += 1
        elif not is_anomaly and is_flagged:
            fp += 1
        elif is_anomaly and not is_flagged:
            fn += 1
        else:
            tn += 1
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else None
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall and (precision + recall) > 0 else None
    return {
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": round(precision, 3) if precision is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
    }


def _chronological_calibrate_and_score(
    items: list[tuple[str, str, float, str]], train_fraction: float = 0.6,
) -> dict:
    """Shared threshold-calibration step for the two workbook-ground-truth
    IsolationForest harnesses below. `items` is (ref, date_str, score,
    truth_label) tuples for every record the model scored (score = the
    model's own continuous IsolationForest decision score, public
    convention higher-is-more-anomalous, exactly what anomaly.py's
    `_evaluate_*` functions already compute). Sorted chronologically and
    split into an EARLIER train fold (threshold search only -- never sees
    which split element it is scored against) and a LATER held-out test
    fold, so the reported test-fold metrics never came from a threshold
    chosen with knowledge of those same labels. The threshold itself is
    whichever score cutoff maximizes F1 on the train fold alone; ties
    broken toward the higher (stricter) cutoff.
    """
    items = sorted(items, key=lambda t: t[1])
    n = len(items)
    cut = max(1, int(n * train_fraction))
    train, test = items[:cut], items[cut:]

    def _f1_at(fold, threshold):
        tp = sum(1 for _, _, s, y in fold if s >= threshold and y == "ANOMALY")
        fp = sum(1 for _, _, s, y in fold if s >= threshold and y == "NORMAL")
        fn = sum(1 for _, _, s, y in fold if s < threshold and y == "ANOMALY")
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        return (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    candidate_thresholds = sorted({round(s, 4) for _, _, s, _ in train}, reverse=True)
    best_threshold, best_f1 = (candidate_thresholds[0] if candidate_thresholds else 0.0), -1.0
    for thr in candidate_thresholds:
        f1 = _f1_at(train, thr)
        if f1 > best_f1:
            best_f1, best_threshold = f1, thr

    tp = fp = fn = tn = 0
    for _, _, s, y in test:
        pred_pos = s >= best_threshold
        is_pos = y == "ANOMALY"
        if pred_pos and is_pos:
            tp += 1
        elif pred_pos and not is_pos:
            fp += 1
        elif not pred_pos and is_pos:
            fn += 1
        else:
            tn += 1
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else None
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall and (precision + recall) > 0 else None
    fpr = (fp / (fp + tn)) if (fp + tn) > 0 else None

    test_scores = [s for _, _, s, _ in test]
    test_labels = [1 if y == "ANOMALY" else 0 for _, _, _, y in test]
    roc_auc = pr_auc = None
    if test_labels and 0 < sum(test_labels) < len(test_labels):
        roc_auc = round(float(roc_auc_score(test_labels, test_scores)), 4)
        pr_auc = round(float(average_precision_score(test_labels, test_scores)), 4)

    return {
        "calibration_method": (
            "Chronological 60/40 split; decision threshold = the IsolationForest score "
            "cutoff maximizing F1 on the train fold only, applied unchanged to the test fold. "
            "ROC-AUC/PR-AUC computed on the test fold's continuous scores (threshold-free)."
        ),
        "train_fold": {"n": len(train), "n_anomalous": sum(1 for *_, y in train if y == "ANOMALY"),
                       "date_range": [train[0][1], train[-1][1]] if train else None},
        "test_fold": {"n": len(test), "n_anomalous": sum(1 for *_, y in test if y == "ANOMALY"),
                     "date_range": [test[0][1], test[-1][1]] if test else None},
        "chosen_threshold": round(best_threshold, 4),
        "train_f1_at_threshold": round(best_f1, 4),
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": round(precision, 3) if precision is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
        "false_positive_rate": round(fpr, 4) if fpr is not None else None,
        "roc_auc": roc_auc,
        "pr_auc": pr_auc,
        "alert_rate": round((tp + fp) / len(test), 4) if test else None,
    }


def simulate_stock_loss_from_workbook(
    real_conn: sqlite3.Connection, source_path: Path | None = None, lookback_days: int = 420,
) -> dict:
    """Pass A (stock-loss IsolationForest) scored against simulatedd.xlsx's
    own injected ground truth (Stock Counts' "Injected Synthetic Truth" ==
    INJECTED_STOCK_LOSS / SYNTHETIC_NORMAL). See this section's own
    docstring above for the isolation contract."""
    source_path = source_path or swi.XLSX_PATH
    wb = openpyxl.load_workbook(source_path, read_only=True, data_only=True)
    wastage_raw = swi._load_sheet(wb, swi._WASTAGE_SHEET)
    counts_raw = swi._load_sheet(wb, swi._COUNTS_SHEET)
    receiving_raw = swi._load_sheet(wb, swi._RECEIVING_SHEET)
    wb.close()

    norm_ids = {swi._norm_ingredient_ref(r.get("Ingredient ID")) for r in wastage_raw + counts_raw}
    norm_ids.discard(None)

    conn, path = _fresh_isolated_db()
    try:
        ingredients = _copy_ingredients_for_workbook(real_conn, conn, norm_ids)
        if not ingredients:
            return {"mode": MODE, "available": False,
                   "reason": "no real ingredient rows matched the workbook's Ingredient IDs"}

        wastage_ok, _ = swi._validate_rows(
            wastage_raw, id_col="Waste ID", ingredient_col="Ingredient ID",
            qty_cols=["Quantity (base units)"], ingredients=ingredients,
        )
        counts_ok, _ = swi._validate_rows(
            counts_raw, id_col="Count ID", ingredient_col="Ingredient ID",
            qty_cols=["System Expected Qty (base units)", "Physical Count Qty (base units)"],
            ingredients=ingredients,
        )
        receiving_ok, _ = swi._validate_rows(
            receiving_raw, id_col="Receipt ID", ingredient_col="Ingredient ID",
            qty_cols=["Ordered Qty", "Received Qty"], ingredients=ingredients,
        )
        ground_truth = {r["Waste ID"]: r.get("Injected Synthetic Truth") for r in wastage_ok}
        ground_truth.update({r["Count ID"]: r.get("Injected Synthetic Truth") for r in counts_ok})

        # receiving_rows folded into the balance bookkeeping only (no ledger
        # row) -- see _replay_ledger's 2026-10-08 docstring update: without
        # this, a count's delta/fraction wrongly includes interim restocking
        # as if it were loss, which was the dominant source of false
        # positives in the rule-based layer below.
        ledger_rows = swi._replay_ledger(conn, wastage_ok, counts_ok, ingredients, receiving_rows=receiving_ok)
        id_to_ref: dict[int, str] = {}
        for lr in ledger_rows:
            lid = swi.insert_ledger_row(conn, lr, None)
            id_to_ref[lid] = lr["ref_id"]
        conn.commit()

        # "default" numbers: the REAL, unmodified detect_anomalies() (rule
        # layer + IsolationForest cap, production's own thresholds) --
        # answers "how does the model behave out of the box on the
        # receiving-balance-corrected data," before any calibration below.
        flagged = anomaly.detect_anomalies(conn, lookback_days=lookback_days)
        flagged_ids = {f["ledger_id"] for f in flagged}
        default_scored = _confusion_matrix(id_to_ref, ground_truth, flagged_ids, "INJECTED_STOCK_LOSS")
        n_anomalous = sum(1 for v in ground_truth.values() if v == "INJECTED_STOCK_LOSS")

        # "calibrated" numbers: the model's own continuous IsolationForest
        # score (_evaluate_stock_loss's `score` field -- same fit, same
        # features, nothing recomputed), with a decision threshold chosen
        # on an earlier chronological fold and tested on a later one.
        records, _ = anomaly._evaluate_stock_loss(conn, lookback_days=lookback_days)
        items = []
        for rec in records:
            ref = id_to_ref.get(rec["row"]["id"])
            if ref is None or rec["score"] is None:
                continue
            truth = "ANOMALY" if ground_truth.get(ref) == "INJECTED_STOCK_LOSS" else "NORMAL"
            items.append((ref, rec["row"]["created_at"], rec["score"], truth))
        calibrated = (_chronological_calibrate_and_score(items) if items else
                     {"available": False, "reason": "IsolationForest did not train (pool below MIN_POOLED_SAMPLES)"})

        return {
            "mode": MODE,
            "available": True,
            "production_validated": False,
            "methodology": (
                "Wastage Log + Stock Counts + Receiving Log rows from simulatedd.xlsx replayed "
                "into an isolated, throwaway database (never production) against REAL "
                "ingredient reference rows copied read-only from the live database. Ground "
                "truth is the workbook's own 'Injected Synthetic Truth' column, read directly."
            ),
            "source_workbook": str(source_path),
            "sample_count": len(id_to_ref),
            "ground_truth_counts": {"INJECTED_STOCK_LOSS": n_anomalous,
                                    "SYNTHETIC_NORMAL": len(ground_truth) - n_anomalous},
            "default_thresholds": {
                "description": "detect_anomalies() as shipped: rule layer (fraction>=0.25) OR "
                               "IsolationForest capped to the worst 10% of the pool.",
                "detect_anomalies_flagged_count": len(flagged),
                "alert_rate": round(len(flagged_ids) / len(id_to_ref), 4) if id_to_ref else None,
                **default_scored,
            },
            "calibrated": calibrated,
        }
    finally:
        conn.close()
        _scrub(path)


def simulate_flavor_consumption_from_workbook(
    real_conn: sqlite3.Connection, source_path: Path | None = None, lookback_days: int = 420,
) -> dict:
    """Pass C (flavour-level consumption IsolationForest) scored against
    simulatedd.xlsx's own injected ground truth (Unli Reconciliation's
    "Injected Synthetic Truth" == INJECTED_CONSUMPTION_ANOMALY /
    SYNTHETIC_NORMAL). See this section's own docstring above for the
    isolation contract. The workbook has no equivalent sheet for the
    generic (non-flavour) unli consumption stream Pass B scores -- callers
    needing that number must report it unavailable rather than substitute
    this one, per the workbook's own 'ML Evaluation Requirements' sheet."""
    source_path = source_path or swi.XLSX_PATH
    wb = openpyxl.load_workbook(source_path, read_only=True, data_only=True)
    recon_raw = swi._load_sheet(wb, swi._RECONCILIATION_SHEET)
    wb.close()

    def _strip(raw):
        return raw[len(swi._FLAVOR_PREFIX):] if raw and raw.startswith(swi._FLAVOR_PREFIX) else raw

    flavor_names = {_strip(r.get("Flavor")) for r in recon_raw}
    flavor_names.discard(None)

    conn, path = _fresh_isolated_db()
    try:
        flavors = _copy_flavors_for_workbook(real_conn, conn, flavor_names)
        if not flavors:
            return {"mode": MODE, "available": False,
                   "reason": "no real unli_flavor rows matched the workbook's Flavor names"}

        recon_ok = []
        for row in recon_raw:
            name = _strip(row.get("Flavor"))
            if row.get("Reconciliation ID") and name in flavors:
                recon_ok.append({**row, "_flavor_name": name})
        ground_truth = {r["Reconciliation ID"]: r.get("Injected Synthetic Truth") for r in recon_ok}

        batch_id = f"workbook-eval-{_dt.datetime.now().strftime('%Y%m%d%H%M%S')}"
        id_to_ref: dict[int, str] = {}
        for row in recon_ok:
            ref = row["Reconciliation ID"]
            flavor = flavors[row["_flavor_name"]]
            lid = swi.insert_reconciliation_row(conn, row, flavor, ref, batch_id, None)
            id_to_ref[lid] = ref
        conn.commit()

        flagged = anomaly.detect_flavor_consumption_anomalies(conn, lookback_days=lookback_days)
        flagged_ids = {f["reconciliation_id"] for f in flagged}
        default_scored = _confusion_matrix(id_to_ref, ground_truth, flagged_ids, "INJECTED_CONSUMPTION_ANOMALY")
        n_anomalous = sum(1 for v in ground_truth.values() if v == "INJECTED_CONSUMPTION_ANOMALY")

        records, _ = anomaly._evaluate_flavor_consumption(conn, lookback_days=lookback_days)
        items = []
        for rec in records:
            ref = id_to_ref.get(rec["row"]["reconciliation_id"])
            if ref is None or rec["score"] is None:
                continue
            truth = "ANOMALY" if ground_truth.get(ref) == "INJECTED_CONSUMPTION_ANOMALY" else "NORMAL"
            items.append((ref, rec["row"]["service_date"], rec["score"], truth))
        calibrated = (_chronological_calibrate_and_score(items) if items else
                     {"available": False, "reason": "IsolationForest did not train (pool below FLAVOR_MIN_RECONCILED_DAYS)"})

        return {
            "mode": MODE,
            "available": True,
            "production_validated": False,
            "methodology": (
                "Unli Reconciliation rows from simulatedd.xlsx replayed into an isolated, "
                "throwaway database (never production) against REAL unli_flavor/ingredient "
                "reference rows copied read-only from the live database. Ground truth is the "
                "workbook's own 'Injected Synthetic Truth' column, read directly."
            ),
            "source_workbook": str(source_path),
            "sample_count": len(id_to_ref),
            "ground_truth_counts": {"INJECTED_CONSUMPTION_ANOMALY": n_anomalous,
                                    "SYNTHETIC_NORMAL": len(ground_truth) - n_anomalous},
            "default_thresholds": {
                "description": "detect_flavor_consumption_anomalies() as shipped: rule layer "
                               "(variance/leftover/share) OR IsolationForest capped to the "
                               "worst 25% of the pool.",
                "detect_flavor_consumption_anomalies_flagged_count": len(flagged),
                "alert_rate": round(len(flagged_ids) / len(id_to_ref), 4) if id_to_ref else None,
                **default_scored,
            },
            "calibrated": calibrated,
        }
    finally:
        conn.close()
        _scrub(path)


def simulate_unli_consumption_from_workbook(
    real_conn: sqlite3.Connection, source_path: Path | None = None, lookback_days: int = 420,
) -> dict:
    """Pass B (generic, non-flavour-level unli consumption IsolationForest)
    executed against simulatedd.xlsx. The workbook has no day-level 'whole
    unli stream' sheet of its own (only per-flavour Unli Reconciliation) --
    this legitimately rolls that same per-flavour data UP to one row per
    service_date (heads = the day's max per-flavour head count, a defensible
    proxy for 'diners exposed to the unli offer that day' since every diner
    sees every flavour; servings/wings/leftover/consumption = summed across
    flavours, valid because all 14 flavour ingredients share the same base
    unit, 'g', confirmed against the real catalog before summing) rather
    than inventing day-level figures from nothing. A day's ground truth is
    ANOMALY if ANY flavour reconciled that day carries
    INJECTED_CONSUMPTION_ANOMALY -- a real anomaly in one flavour's sauce
    usage is a real anomaly in the day's aggregate consumption too."""
    source_path = source_path or swi.XLSX_PATH
    wb = openpyxl.load_workbook(source_path, read_only=True, data_only=True)
    recon_raw = swi._load_sheet(wb, swi._RECONCILIATION_SHEET)
    wb.close()

    def _strip(raw):
        return raw[len(swi._FLAVOR_PREFIX):] if raw and raw.startswith(swi._FLAVOR_PREFIX) else raw

    by_date: dict[str, list[dict]] = {}
    for row in recon_raw:
        name = _strip(row.get("Flavor"))
        sd = row.get("Service Date")
        if not name or not sd:
            continue
        by_date.setdefault(swi._iso(sd), []).append(row)

    conn, path = _fresh_isolated_db()
    try:
        ing_cur = conn.execute(
            "INSERT INTO ingredient (ingredient_id, name, category, base_unit, "
            "purchase_unit, pack_size, pack_size_status, on_hand_base, unit_cost, "
            "unit_cost_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("WB-UNLI-AGG-001", "Workbook-aggregate wings sauce (all flavours)", "wet_food", "g",
             "g", 1.0, "verified", 0.0, 1.0, "verified"),
        )
        ingredient_pk = ing_cur.lastrowid
        item_cur = conn.execute(
            "INSERT INTO unli_item (item_key, label, ingredient_id) VALUES (?,?,?)",
            ("wings", "Unlimited Wings (workbook aggregate)", ingredient_pk),
        )
        unli_item_pk = item_cur.lastrowid

        day_rows = []
        for sd, rows in sorted(by_date.items()):
            heads = max(int(r.get("Heads Exposed / Relevant Heads") or 0) for r in rows)
            servings = sum(int(r.get("Servings / Refills") or 0) for r in rows)
            wings_served = sum(float(r.get("Wings Served (pcs)") or 0) for r in rows)
            leftover = sum(float(r.get("Leftovers (pcs)") or 0) for r in rows)
            actual = sum(float(r.get("Actual Measured Ingredient Consumption") or 0) for r in rows)
            provisional = sum(float(r["Provisional Consumption"]) for r in rows
                             if r.get("Provisional Consumption") is not None)
            is_anomaly = any(r.get("Injected Synthetic Truth") == "INJECTED_CONSUMPTION_ANOMALY" for r in rows)
            day_rows.append({
                "service_date": sd, "heads": heads, "servings": servings,
                "wings_served": wings_served, "leftover": leftover,
                "actual": actual, "provisional": provisional, "is_anomaly": is_anomaly,
            })

        normal_rates = [d["actual"] / d["heads"] for d in day_rows if d["heads"] > 0 and not d["is_anomaly"]]
        baseline_lambda = statistics.median(normal_rates) if normal_rates else 0.0
        coef_cur = conn.execute(
            "INSERT INTO unli_coefficient (unli_item_id, lambda_value, role, status) VALUES (?,?,?,?)",
            (unli_item_pk, baseline_lambda, "baseline", "verified"),
        )
        coefficient_pk = coef_cur.lastrowid
        conn.commit()

        ground_truth: dict[str, str] = {}
        for d in day_rows:
            if d["heads"] <= 0:
                continue
            variance = d["actual"] - d["provisional"]
            variance_ratio = (variance / d["provisional"]) if d["provisional"] else None
            opening = max(d["provisional"], d["actual"]) * 1.2
            closing = max(0.0, opening - d["actual"])
            conn.execute(
                "INSERT INTO unli_reconciliation (service_date, unli_item_id, heads, "
                "theoretical_base, actual_base, variance_base, variance_ratio, coefficient_id, "
                "opening_base, added_base, closing_base, status, validation_state, "
                "correction_base, recorded_by, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d["service_date"], unli_item_pk, d["heads"], d["provisional"], d["actual"],
                 variance, variance_ratio, coefficient_pk, opening, 0.0, closing,
                 "within_tolerance", "validated", 0.0, None,
                 "[workbook-aggregate] " + d["service_date"]),
            )
            ground_truth[d["service_date"]] = "ANOMALY" if d["is_anomaly"] else "NORMAL"
        conn.commit()

        id_to_date = {
            r["id"]: r["service_date"] for r in
            conn.execute("SELECT id, service_date FROM unli_reconciliation").fetchall()
        }

        flagged = anomaly.detect_consumption_anomalies(conn, lookback_days=lookback_days)
        flagged_ids = {f["reconciliation_id"] for f in flagged}
        default_scored = _confusion_matrix(id_to_date, ground_truth, flagged_ids, "ANOMALY")
        n_anomalous = sum(1 for v in ground_truth.values() if v == "ANOMALY")

        records, _ = anomaly._evaluate_consumption(conn, lookback_days=lookback_days)
        items = []
        for rec in records:
            sd = id_to_date.get(rec["row"]["reconciliation_id"])
            if sd is None or rec["score"] is None:
                continue
            items.append((sd, sd, rec["score"], ground_truth.get(sd, "NORMAL")))
        calibrated = (_chronological_calibrate_and_score(items) if items else
                     {"available": False, "reason": "IsolationForest did not train (pool below MIN_POOLED_SAMPLES)"})

        return {
            "mode": MODE,
            "available": True,
            "production_validated": False,
            "methodology": (
                "Unli Reconciliation rows from simulatedd.xlsx aggregated per service_date "
                "(summed across all 14 flavours, a single shared base unit) into one "
                "day-level stream and replayed into an isolated, throwaway database. Ground "
                "truth: a day is anomalous if any flavour that day carried "
                "INJECTED_CONSUMPTION_ANOMALY."
            ),
            "source_workbook": str(source_path),
            "sample_count": len(ground_truth),
            "ground_truth_counts": {"ANOMALY": n_anomalous, "NORMAL": len(ground_truth) - n_anomalous},
            "default_thresholds": {
                "description": "detect_consumption_anomalies() as shipped.",
                "detect_consumption_anomalies_flagged_count": len(flagged),
                "alert_rate": round(len(flagged_ids) / len(ground_truth), 4) if ground_truth else None,
                **default_scored,
            },
            "calibrated": calibrated,
        }
    finally:
        conn.close()
        _scrub(path)
