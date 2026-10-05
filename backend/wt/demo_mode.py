"""Demo Mode — a deterministic, fully isolated demonstration environment.

Why a separate database, not a flag inside the real one
---------------------------------------------------------
Every ML-provenance audit so far (2026-10) converged on one principle: a
real-vs-synthetic DISTINCTION inside one database is only as safe as every
query that is supposed to respect it — forget one WHERE clause and
contaminated data silently reaches a real model (exactly what happened
before _REAL_USER_CLAUSE/_REAL_RECORDER_CLAUSE existed). A physically
separate SQLite file removes that entire class of risk: a query against
wings_theory_demo.db cannot accidentally return a row from wings_theory.db
no matter what its WHERE clause says, because there is no code path that
opens both at once. wt/db.py already had the infrastructure for exactly
this shape (WT_ENV/WT_DB, with a hard-enforced env<->path pairing) for
PRODUCTION_DB; this module adds the identical pairing for DEMO_DB rather
than inventing a second mechanism.

The two-factor guard
---------------------
Per the explicit instruction not to depend solely on a filename: every
demo-mutating function here checks BOTH
  1. db.environment() == "demo" AND db.db_path() resolves to db.DEMO_DB
     (wt/db.py's own cross-validated pairing — see db_path()'s docstring),
     and
  2. once a connection is open, the database's own CONTENT declares itself
     demo (app_setting['db_environment_marker'] == DEMO_MARKER_VALUE) —
     checked by is_demo_database() before any reset/reseed touches an
     ALREADY-INITIALIZED database. A brand-new, empty DEMO_DB has no marker
     yet (nothing to check), so init_demo() writes it as its own first
     authoritative act, immediately after db.reset() — every function after
     that point can require it.

Provenance inside demo mode
-----------------------------
Section B's scenario-builder functions record events through the EXACT SAME
production code paths a real admin/kitchen call would use
(reservation.record_count/record_wastage, unli.record_reconciliation) and
attribute them to the demo database's own SEEDED staff accounts
(seed.seed_users() already creates 'kitchen1'/'cashier1'/the admin account
in every fresh database, demo or real) — not a fabricated non-account
string. This is deliberate, not an oversight: within an isolated demo
database, "kitchen1 recorded this" is not a false claim about Wings
Theory's real history, and routing scenario data through app_user-
membership eligibility (unchanged, same _REAL_USER_CLAUSE/_REAL_RECORDER_
CLAUSE every real row is judged by) is what lets the demo database exercise
the REAL sampling/review/metrics pipeline end-to-end, not a parallel
demo-only copy of it. The one thing this module does differently from a
real write: every demo-scenario note/reason is prefixed "DEMO SCENARIO:" —
soft, human-readable provenance, not a machine-enforced one, because the
machine-enforced guarantee here is the separate database file itself, not a
marker on each row. See DEMO_NOTE_PREFIX.

What this module deliberately does NOT do
--------------------------------------------
It does not add a demo-only "eligibility" concept to wt/anomaly.py, and it
does not touch MIN_POOLED_SAMPLES/MIN_RECONCILED_DAYS/MAX_FLAG_FRACTION/
contamination or any other model constant. The existing pipeline, unchanged,
is what runs against the demo database; only the DATA differs. If a
deterministic scenario does not produce the desired mix of flags, the fix is
in this module's own fixture numbers (see _build_consumption_scenario's
comments for exactly this kind of empirical tuning), never in wt/anomaly.py.

Labeling demo results as demo, not as real performance (section 7/8 of the
ML-safety audit) is a PRESENTATION decision, made once, at the API/console
layer (GET /health's existing `environment` field, already exposed before
this module existed) — not a second metrics function. See Losses.js's
validation-results panel for where that relabeling actually happens.
"""
from __future__ import annotations

import datetime as _dt
import random

from . import anomaly, db, demo_seed, seed, unli, xlsx_history_import
from .errors import InvalidStateError
from .reservation import record_count, record_wastage

DEMO_MARKER_KEY = "db_environment_marker"
DEMO_MARKER_VALUE = "WINGS_THEORY_DEMO_DB_V1"
# Soft, human-readable provenance (see module docstring) — never relied on by
# any eligibility check, only by a human later reading a note/reason field.
DEMO_NOTE_PREFIX = "DEMO SCENARIO:"
# A fixed seed, not os.urandom — see module docstring's "deterministic"
# requirement: running init_demo() twice must build byte-identical scenario
# numbers both times.
DEMO_RNG_SEED = 20261001


def _require_demo_environment() -> None:
    """The first of the two factors described in the module docstring. Fires
    before anything is touched, including before a connection is opened for
    operations that take an explicit path."""
    env = db.environment()
    if env != "demo":
        raise db.ConfigurationError(
            f"demo_mode functions can only run under WT_ENV=demo (current: {env!r}). "
            "Refusing — this guard exists specifically so a demo script accidentally "
            "pointed at a real environment fails loudly instead of touching it."
        )
    path = db.db_path()  # raises on its own if WT_DB doesn't also match DEMO_DB
    if path.resolve() != db.DEMO_DB.resolve():
        raise db.ConfigurationError(
            f"WT_DB={path} does not match the demo database ({db.DEMO_DB}). Refusing."
        )


def is_demo_database(conn) -> bool:
    """The second factor: does THIS CONNECTION's database content declare
    itself demo. False for a brand-new, not-yet-initialized DEMO_DB (nothing
    has written the marker yet) as well as for any real database — callers
    that need "is this database safe to destructively reset/reseed AS demo"
    should combine this with _require_demo_environment(), not substitute for
    it; a fresh real database obviously has no marker either, and the
    env/path check is what actually rules that case out."""
    try:
        row = conn.execute(
            "SELECT value FROM app_setting WHERE key=?", (DEMO_MARKER_KEY,)
        ).fetchone()
    except Exception:
        return False
    return bool(row) and row["value"] == DEMO_MARKER_VALUE


def init_demo(verbose: bool = True) -> dict:
    """Deterministically (re)build the entire demo database from scratch —
    master data, a copy of the genuine historical sales workbook (for a
    realistic Prophet/FP-Growth demonstration), and a small, deliberate
    stock-loss/consumption scenario. Idempotent by construction: every call
    fully rebuilds from zero, so calling it twice produces the identical
    database both times rather than accumulating anything.

    Hard-refuses outside WT_ENV=demo + WT_DB=DEMO_DB (see
    _require_demo_environment) before touching anything — including before
    the eventual db.reset() call, which re-checks the identical pairing
    itself (see db.reset()'s own docstring) as a second, independent layer.
    """
    _require_demo_environment()

    stats: dict = {}
    seed.run(fresh=True)  # db.reset() (re-verifies env+path itself) + master data + users
    conn = db.get_conn()

    # Mark the database as demo FIRST, immediately after the data it will
    # never be mistaken for being: anything that crashes after this point
    # still leaves a database that correctly identifies itself on the next
    # is_demo_database() check, rather than a half-built, unmarked one that
    # might otherwise look like an ordinary fresh development database.
    conn.execute(
        "INSERT INTO app_setting (key, value, updated_by) VALUES (?, ?, 'demo_mode.init_demo') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, "
        "updated_at=datetime('now')",
        (DEMO_MARKER_KEY, DEMO_MARKER_VALUE),
    )

    if verbose:
        print("  seeding genuine historical sales workbook (no synthetic tail)...")
    stats["history_import"] = xlsx_history_import.run(
        conn, dry_run=False, verbose=verbose, seed_synthetic_history=False,
    )

    rng = random.Random(DEMO_RNG_SEED)
    staff = _demo_staff_accounts(conn)

    if verbose:
        print("  building the stock-loss demonstration scenario...")
    stats["stock_loss_scenario"] = _build_stock_loss_scenario(conn, rng, staff)

    if verbose:
        print("  building the Unlimited-Wings consumption demonstration scenario...")
    stats["consumption_scenario"] = _build_consumption_scenario(conn, rng, staff)

    if verbose:
        print("  sampling and labeling a few demo reviews, to demonstrate the workflow...")
    stats["demo_reviews"] = _build_demo_reviews(conn, staff)

    return stats


def reset_demo(verbose: bool = True) -> dict:
    """Alias for init_demo() — a full deterministic rebuild IS the reset
    (see that function's own docstring). Named separately because
    "reset_demo()" is what section 15's safety audit asked to exist and be
    independently tested, even though its implementation is identical."""
    return init_demo(verbose=verbose)


def _demo_staff_accounts(conn) -> dict[str, str]:
    """The real app_user accounts seed.run() just created in THIS database
    (demo or otherwise) — see the module docstring's "provenance inside
    demo mode" section for why scenario events are attributed to these
    rather than to a fabricated non-account string."""
    rows = conn.execute("SELECT username, role FROM app_user WHERE is_active=1").fetchall()
    by_role: dict[str, str] = {}
    for r in rows:
        by_role.setdefault(r["role"], r["username"])
    return {
        "admin": by_role.get("admin", "Administrator"),
        "kitchen": by_role.get("kitchen", by_role.get("admin", "Administrator")),
    }


# ===========================================================================
# Stock-loss scenario — section 5's exact list: normal count, small expected
# variance, unusual negative variance, wastage, spoilage, a Correct Stock
# adjustment, a Count Today observation.
# ===========================================================================
def _build_stock_loss_scenario(conn, rng: random.Random, staff: dict[str, str]) -> dict:
    ings = conn.execute(
        "SELECT id, name, on_hand_base FROM ingredient "
        "WHERE kind='raw' AND is_active=1 AND pack_size IS NOT NULL "
        "ORDER BY name LIMIT 2"
    ).fetchall()
    if len(ings) < 2:
        return {"skipped": "fewer than 2 usable raw ingredients in this database"}
    primary, secondary = ings[0], ings[1]
    today = _dt.date.today()
    counted = 0

    # A pooled run of ordinary, small-variance physical counts on `secondary`
    # — the "normal physical count" / "small expected variance" cases, and
    # also what gives the stock-loss forest enough SAME-ingredient history
    # to judge the later outlier against (MIN_POOLED_SAMPLES=15 is pooled
    # across ingredients, but a same-ingredient baseline makes the later
    # outlier's relative_mag feature meaningfully large).
    base = secondary["on_hand_base"] or 1000.0
    for i in range(13):
        pct = rng.uniform(-0.02, 0.02)  # +/-2% — "small expected variance"
        record_count(conn, secondary["id"], round(max(0.0, base * (1 + pct)), 2),
                    note=f"{DEMO_NOTE_PREFIX} normal closing count", user_id=staff["admin"])
        counted += 1

    # "Unusual negative variance" — a much larger, deliberate shortfall,
    # entered through Count Today (source='count_today') the way a risk-
    # flagged routine check would actually be recorded.
    record_count(conn, secondary["id"], round(base * 0.55, 2),
                 note=f"{DEMO_NOTE_PREFIX} Count Today — unusually low", source="count_today",
                 user_id=staff["kitchen"])
    counted += 1

    # "Correct Stock adjustment" — an emergency mid-service correction on
    # `primary`, through the exact Correct Stock source.
    primary_on_hand = primary["on_hand_base"] or 100.0
    record_count(conn, primary["id"], round(primary_on_hand * 0.10, 2),
                 note=f"{DEMO_NOTE_PREFIX} Correct Stock — kitchen found far less than expected",
                 source="correct_stock", user_id=staff["kitchen"])
    counted += 1

    # "Real-looking wastage" and "spoilage" — known-loss events on `primary`.
    on_hand_now = conn.execute(
        "SELECT on_hand_base FROM ingredient WHERE id=?", (primary["id"],)
    ).fetchone()["on_hand_base"]
    wastage_qty = round(min(on_hand_now * 0.5, max(1.0, on_hand_now * 0.08)), 2)
    if wastage_qty > 0:
        record_wastage(conn, primary["id"], wastage_qty, reason="wastage",
                       note=f"{DEMO_NOTE_PREFIX} dropped during prep", user_id=staff["kitchen"])
    on_hand_now = conn.execute(
        "SELECT on_hand_base FROM ingredient WHERE id=?", (primary["id"],)
    ).fetchone()["on_hand_base"]
    spoilage_qty = round(min(on_hand_now * 0.9, max(1.0, on_hand_now * 0.4)), 2)
    if spoilage_qty > 0:
        record_wastage(conn, primary["id"], spoilage_qty, reason="spoilage",
                       note=f"{DEMO_NOTE_PREFIX} cooler outage overnight", user_id=staff["admin"])

    return {
        "ingredients_used": [primary["name"], secondary["name"]],
        "physical_counts": counted,
        "wastage_events": 1 if wastage_qty > 0 else 0,
        "spoilage_events": 1 if spoilage_qty > 0 else 0,
        "as_of": today.isoformat(),
    }


# ===========================================================================
# Consumption scenario — section 5's list: a baseline of normal days / slightly
# above / slightly below / a day severe enough to trip BOTH detectors
# (genuinely high, and genuinely low) / a day only the deterministic rule
# catches / a day only the forest catches.
#
# The exact ratios below were chosen against this project's own tolerance
# (RULE_VARIANCE_MULTIPLE x unli_variance_tolerance, default 2x10%=20%, see
# wt/anomaly.py — unchanged by this module): 1.22/0.42/1.85 clear that bar,
# 1.08/0.93 stay comfortably inside it, and 1.15 sits in between, below the
# rule's fixed cutoff but far enough from a ~±5% baseline spread to usually
# register as an outlier to the forest once it has trained. They are
# demo-fixture numbers, not model thresholds, and the thing this module is
# expected to retune if that pairing ever drifts — see the module's own
# docstring. Because the forest trains on whatever ~26-point sample this
# run happens to produce, EXACTLY which of rule_only/iforest_only land as
# "only one detector" vs. "both agreed" can vary slightly run to run — that
# variability is real model behavior being shown honestly, not a fixture
# bug, and is exactly why section 12's rule_flagged/iforest_prediction
# separation exists as data instead of being inferred from which demo label
# a row happens to carry.
#
# The forest needs MIN_RECONCILED_DAYS genuine reconciled days before it
# trains at all (wt/anomaly.py) and meaningfully more than that bare minimum
# before an outlier actually reads as unusual rather than within a tiny
# sample's noise -- hence a 20-day jittered baseline here, not just the 3-4
# cases actually being demonstrated.
#
# Scenario days intentionally OVERLAP the genuine historical workbook's own
# order activity -- that's fine, not a contamination risk: each day's
# target ratio is computed from provisional_for_date() READ AFTER this
# day's scenario order is inserted, so it reflects whatever theoretical_base
# that date actually ends up with (real orders included), and the variance
# RATIO record_reconciliation is asked to hit is unaffected by how much of
# that theoretical_base is real vs. scenario. hour=12 (not evening service)
# is still deliberate: this system's business day boundary is local-
# midnight-minus-8-hours (timeutil.sql_business_date), so a plausible
# dinner-hour order can roll onto the NEXT calendar day's business date;
# noon safely cannot.
# ===========================================================================
def _build_consumption_scenario(conn, rng: random.Random, staff: dict[str, str]) -> dict:
    stream = conn.execute(
        "SELECT id, item_key, label, ingredient_id FROM unli_item WHERE is_active=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if not stream:
        return {"skipped": "no active unlimited-wings stream in this database"}

    from .refills import list_flavors
    flavors = list_flavors(conn, active_only=True)
    flavors_included = [f for f in flavors if f["flavor_type"] == "included"]
    flavors_premium = [f for f in flavors if f["flavor_type"] == "special"]
    if not flavors_included:
        return {"skipped": "no active included flavour to seed an order against"}

    menu_row = conn.execute(
        "SELECT id, price FROM menu_item WHERE is_unli=1 AND pos_visible=1 LIMIT 1"
    ).fetchone()
    if not menu_row:
        return {"skipped": "no active unlimited-wings menu item in this database"}

    today = _dt.date.today()

    # seed.run() stamps every freshly-seeded unli_coefficient's effective_from
    # as "now" (the moment the demo database was built) -- correct for a real
    # restaurant, where no order could have existed before its coefficient
    # did, but wrong for this scenario, which backdates orders up to ~30 days
    # into the past. provisional_for_date() only counts an order if a
    # 'rolling' coefficient's effective_from <= the order's own timestamp, so
    # without this the backdated orders below would silently compute
    # theoretical=0 and every scenario day would be skipped. Backdating the
    # coefficient itself (not weakening that check) is the correct fix: it is
    # simply declaring the coefficient was already in force before this
    # demo's earliest scenario day, which, for a fixture being built from
    # scratch, it may as well always have been.
    conn.execute(
        "UPDATE unli_coefficient SET effective_from=? "
        "WHERE unli_item_id=? AND role='rolling'",
        ((today - _dt.timedelta(days=100)).isoformat() + " 00:00:00", stream["id"]),
    )

    baseline_ratios = [round(rng.uniform(0.95, 1.05), 4) for _ in range(20)]
    all_labels: list[tuple[str, float]] = (
        [(f"normal_{i + 1}", r) for i, r in enumerate(baseline_ratios)]
        + [
            ("slightly_above", 1.08),
            ("slightly_below", 0.93),
            ("rule_only", 1.22),          # exceeds the fixed-multiple rule, ordinary otherwise
            ("iforest_only", 1.15),       # under the rule's threshold, but an outlier in context
            ("genuinely_high_both", 1.85),
            ("genuinely_low_both", 0.42),
        ]
    )
    # One distinct calendar day per label, starting a few days back (never
    # "today" itself -- see end_of_day below) and reaching back far enough
    # that every one of them lands before "today", comfortably inside the
    # window unli_coefficient.effective_from was just backdated to cover.
    labels = [(label, ratio, i + 3) for i, (label, ratio) in enumerate(all_labels)]

    prev_demo_user_id = demo_seed.USER_ID
    demo_seed.USER_ID = staff["kitchen"]
    # _seed_unlimited_order backdates stock_ledger/order_charge rows when it
    # reserves/commits (and again if a reactive restock fires) -- the same
    # intentional, narrowly-scoped exception to the append-only guarantee
    # that demo_seed.run() and xlsx_history_import.run() each take for the
    # exact same reason (see demo_seed._LEDGER_NO_UPDATE_TRIGGER's own
    # docstring). Restored in finally regardless of outcome.
    conn.execute("DROP TRIGGER IF EXISTS trg_ledger_no_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_order_charge_no_update")
    created: list[dict] = []
    try:
        for label, ratio, days_back in labels:
            day = today - _dt.timedelta(days=days_back)
            heads = 20
            demo_seed._seed_unlimited_order(
                conn, day, 12, rng, flavors_included, flavors_premium,
                menu_row["id"], menu_row["price"], head_count=heads, walkout_rate=0.0,
                # same reasoning as xlsx_history_import.py's identical call --
                # a reactive restock's shelf life must be measured from
                # today, never from the backdated scenario day it happens to
                # be triggered by (see _seed_unlimited_order's own
                # docstring).
                restock_reference_date=today,
            )
            prov = unli.provisional_for_date(conn, stream["id"], day.isoformat())
            theoretical = prov["theoretical_base"]
            if theoretical <= 0:
                continue
            actual = theoretical * ratio
            leftover_ratio = 0.05
            opening = actual / (1 - leftover_ratio)
            closing = max(0.0, opening - actual)
            r = unli.record_reconciliation(
                conn, day.isoformat(), stream["item_key"],
                opening_base=round(opening, 2), closing_base=round(closing, 2),
                note=f"{DEMO_NOTE_PREFIX} {label}", user_id=staff["kitchen"],
            )
            end_of_day = f"{day.isoformat()} 22:{rng.randint(0, 59):02d}:00"
            conn.execute("UPDATE unli_reconciliation SET created_at=? WHERE id=?",
                        (end_of_day, r["id"]))
            created.append({"label": label, "reconciliation_id": r["id"], "ratio": ratio})
    finally:
        conn.execute(demo_seed._LEDGER_NO_UPDATE_TRIGGER)
        conn.execute(demo_seed._ORDER_CHARGE_NO_UPDATE_TRIGGER)
        demo_seed.USER_ID = prev_demo_user_id

    flagged = anomaly.detect_consumption_anomalies(conn)
    by_id = {f["reconciliation_id"]: f["method"] for f in flagged}
    outcome = {c["label"]: by_id.get(c["reconciliation_id"], "unflagged") for c in created}

    return {"stream": stream["item_key"], "days_created": len(created), "outcome_by_label": outcome}


def _build_demo_reviews(conn, staff: dict[str, str]) -> dict:
    """Sample and label a handful of demo observations through the REAL
    sampling/labeling functions (unchanged), purely so the Validation Queue
    has something to show immediately after init_demo() rather than
    requiring an admin to click "Pull new records" first during a live
    demo. Labels all four confusion-matrix cells on purpose — see section 6
    — so TP/FP/FN/TN are all visible at once."""
    out = {"stock_loss": None, "consumption": None}

    stock_sample = anomaly.sample_for_review(conn, n_near_threshold=2, n_random=2)
    out["stock_loss"] = stock_sample
    rows = conn.execute(
        "SELECT id, sample_source, model_prediction FROM anomaly_validation_sample "
        "WHERE human_label='pending' ORDER BY id"
    ).fetchall()
    # Alternate confirmed/expected so at least one of each lands, regardless
    # of exactly how many of each pool the sampler found this run.
    for i, r in enumerate(rows):
        label = "confirmed_anomaly" if i % 2 == 0 else "expected_or_normal"
        try:
            anomaly.submit_validation_label(conn, r["id"], label,
                                            note=f"{DEMO_NOTE_PREFIX} illustrative label",
                                            user_id=staff["admin"])
        except InvalidStateError:
            continue

    consumption_sample = anomaly.sample_consumption_for_review(conn, n_near_threshold=2, n_random=2)
    out["consumption"] = consumption_sample
    rows = conn.execute(
        "SELECT id FROM unli_consumption_validation_sample WHERE human_label='pending' ORDER BY id"
    ).fetchall()
    for i, r in enumerate(rows):
        label = "confirmed_anomaly" if i % 2 == 0 else "expected_or_explained"
        try:
            anomaly.submit_consumption_validation_label(
                conn, r["id"], label, note=f"{DEMO_NOTE_PREFIX} illustrative label",
                user_id=staff["admin"])
        except InvalidStateError:
            continue

    return out


if __name__ == "__main__":
    import argparse
    import os

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reset", action="store_true",
                   help="alias for the default action — a full rebuild IS the reset")
    p.parse_args()

    print(f"WT_ENV: {os.environ.get('WT_ENV', '(unset)')}")
    print(f"WT_DB: {os.environ.get('WT_DB', '(unset)')}")
    stats = init_demo()
    print("demo database rebuilt:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
