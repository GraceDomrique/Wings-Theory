# Recent order history demo seeding — 2026-10-13

## Summary

Implemented, ran, and verified a repeatable demo-order seeding system covering **2026-09-21 through 2026-10-13** so dashboards, sales analytics, and FP-Growth combo mining have recent data to show — without touching inventory, without inflating real revenue reports, and without disturbing any existing real/historical data.

| Metric | Result |
|---|---|
| Demo transactions generated | **1,944** orders, 23 days |
| Demo-attributed revenue | ₱702,464.00 (fully isolated from real reporting — see below) |
| Dashboards verified populated | Business Overview, Sales Overview (Owner/Admin), Today, Reports/Sales |
| Combos discovered by FP-Growth | 9 candidates, **8 drafted** for review |
| Inventory impact | **Zero** — stock_ledger row count and ingredient.on_hand_base checksum identical before/after, byte-for-byte |
| Real revenue impact | **Zero** — real-only revenue figure identical before/after |
| Regression suite | 1,118 passed, 1 skipped, 0 failed |
| Idempotency | Verified stable across 3 repeated runs (see "A bug I found and fixed" below) |

## 1. Schema change

New migration `backend/migrations/0054_customer_order_is_demo.sql` adds `customer_order.is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0,1))`.

This is **not** the same as `customer_order.source` — that column's CHECK constraint (migration 0030) deliberately permits only `NULL`/`'historical'`, and its own comment explicitly refuses `'demo'`/`'test'`/`'seed'`/`'synthetic'` on the grounds that every value there must mean "this is a real completed sale." I did not touch that invariant. `is_demo` is a new, orthogonal flag whose entire purpose is the distinction `source` was deliberately built to never make.

## 2. How orders were generated

`backend/scripts/seed_recent_demo_orders.py` — deterministic (fixed RNG seed), idempotent, fully repeatable.

- Reads real menu items/prices/categories directly from `menu_item` (no invented items or prices).
- One order at a time, day by day, Sep 21 → Oct 13: weekday volumes 60–85/day, weekend 85–143/day (Fri/Sat/Sun busier, matching real restaurant rhythm), hour-of-day weighted toward lunch (11:30–13:30) and dinner (18:00–21:00) peaks via a realistic weight table, all converted from Manila local time to UTC exactly the way every other part of this codebase does.
- 8 deliberately boosted item pairs get bundled together in ~32% of orders (plus 1–2 random extra items on ~55% of orders) — this is what gives FP-Growth real, repeatable signal; everything else is normal random selection from the real menu.
- **Never calls `reservation.reserve_order()` or `commit_order()`** — raw `INSERT`s into `customer_order`/`order_line` only, mirroring `xlsx_history_import.py`'s own pattern minus the stock-mutating steps. This is why zero `stock_ledger`/`reservation` rows were ever created.
- Every row: `order_no` prefixed `WTRORDEM-<date>-<seq>`, `is_demo = 1`, `created_by = 'demo_seed_recent_orders'`, `status = 'served'`.
- **Idempotent**: deletes every existing `is_demo = 1` row first (`order_line` cascades via its FK), then regenerates from the fixed seed — re-running produces the identical dataset, never duplicates. Verified across 3 consecutive runs: identical order count (1,944), identical revenue total, identical combo set each time.

## 3. Keeping demo data out of "actual business" numbers

`wt/associations.py`'s FP-Growth basket query (`_transactions()`) had **no** `source`/provenance filter at all — by design, I left it that way, so demo orders feed combo mining exactly as you asked.

Every revenue/demand query that's supposed to reflect **real** business activity now filters `is_demo = 0`. Patched sites (verified with a direct before/after comparison — the real-only revenue figure was identical down to the peso before and after seeding):

- `wt/business_overview.py` — `_period_totals()` (2 queries)
- `wt/sales_overview.py` — daily/monthly series, busy-times, heatmap, sales highlights, earliest-order lookup (6 queries)
- `wt/dashboard_overview.py` — category mix, item-qty-by-period, business rhythm (3 queries)
- `wt/api.py` — `/reports/sales` (orders/revenue/by-item/by-day/recent), Today's forecast-preview top-sellers, plus `is_demo` now included in the `/pos/orders` list and detail responses so a receipt/order-history view can flag it if you ever want a visible badge (not built in this pass — see Remaining risks)
- **`wt/forecasting.py` and `wt/depletion.py`** — this was the one that mattered most for not contaminating anything ML-related: Prophet/baseline training (`_daily_series`, store-level daily series, the dead-tail "effective now" detector) and the real replenishment demand model (`_trading_days`, `_item_daily_qty`, `_heads_daily`, `_intraday_share`, ingredient-consumption attribution, the backtest's own order-count observability check) all now exclude `is_demo` rows too. Without this, the demo orders — sitting exactly in the most recent, most heavily-weighted window — would have fed straight into the real Prophet model's next training run and the replenishment backtest's "was there real trading this window" check.

## 4. Combo recommendations

`associations.product_combinations()` was run scoped to a 24-day lookback (the demo window itself, not the full multi-year order history) — this matters: a pure chronological 80/20 holdout split over the *entire* order history would put 100% of the newest (demo) data in the holdout slice and none in training, making it structurally impossible for a demo-exclusive pattern to ever validate. Scoping the lookback to the window itself lets the same real, unmodified holdout check work correctly on this data.

Result: **9 candidates, 8 validated** (capped), each with real holdout-confirmed lift (3.3×–9.8×) — not fabricated association metrics, the actual mining code's own output:

| Combo | Lift | Holdout lift | Support | Confidence |
|---|---|---|---|---|
| Ranch Dip + Wings Platter 6 pcs w/ Fries | 8.07 | 9.77 | 0.0345 | 0.507 |
| Bacon and 2 Egg + Mango Shake (Tall) | 5.89 | 4.05 | 0.0345 | 0.392 |
| Onion Rings + Coke Zero | 5.65 | 3.88 | 0.0312 | 0.333 |
| Coke + 12 pcs (2 Flavors) Ala Carte Wing Rack | 5.53 | 6.09 | 0.0326 | 0.394 |
| Royal + Fish & Chips with Tartar Sauce | 4.63 | 3.33 | 0.0350 | 0.396 |
| 1 Cup Rice + Pork Tonkatsu | 4.56 | 3.73 | 0.0335 | 0.332 |
| Sprite + Creamy Sausage Pasta | 4.41 | 4.21 | 0.0335 | 0.287 |
| Sprite + Cheesy Bacon Fries | 4.11 | 4.07 | 0.0340 | 0.292 |

Each was submitted through the **real, existing** `combo_pricing.create_draft_combo()` — the same function a human clicking "Create combo idea" in the console calls — so each landed as a normal `draft_combo` row with `status = 'draft'`, ready in the owner/admin review queue (`Combos.js`'s existing UI already filters on exactly that status, so no frontend change was needed for them to appear).

## 5. A bug I found and fixed mid-implementation

My first version of the combo-drafting step didn't pass `client_request_id` to `create_draft_combo()`, so re-running the script created duplicate drafts instead of reusing them (`draft_combo`/`draft_combo_review` are both append-only — intentionally, no `UPDATE`/`DELETE` permitted). I fixed the script (deterministic `client_request_id` per item pair, so re-running now correctly reuses the existing draft), and cleaned up the 16 stray rows this produced **through the existing review workflow** — rejecting each with a note explaining exactly what happened — rather than touching the append-only table directly. Final state: 8 live drafts (ids 17–24), 16 rejected duplicates with an honest audit trail, verified stable across 3 further re-runs.

## 6. Verification performed

- Direct SQL: `stock_ledger` row count and `SUM(ingredient.on_hand_base)` identical before/after (zero inventory impact).
- Direct SQL: real-only revenue (`is_demo = 0`) identical before/after seeding (₱13,199,645.00 both times).
- Direct function calls: `business_overview._period_totals()` over the demo window returns exactly the 625 real orders, correctly excluding all 1,944 demo orders.
- Direct function call: `associations.product_combinations()` over the same window sees all 2,646 (real + demo) transactions and finds the same 9 combos the seeding script itself reported.
- `PRAGMA integrity_check` → `ok`, `PRAGMA foreign_key_check` → 0 violations, after every step.
- Full regression suite (`python -m tests.run_tests`, 1,119 tests): **1,118 passed, 1 skipped, 0 failed.**
- Idempotency: 3 consecutive re-runs of the seeding script produced identical order counts, identical revenue, identical combo set, zero duplicate drafts.

## Remaining risks / explicitly out of scope

1. **No dedicated "Demo" visual badge was built.** The `WTRORDEM-` order-number prefix is already visible everywhere an order number appears (receipts, Order History, Reports), and `is_demo` is now returned by the order list/detail API if you want to add a badge later — but I didn't add new frontend styling in this pass, since the numeric/revenue safety (the part that could actually cause harm if wrong) was the priority.
2. **This touches the real production database**, not a separate demo environment (`wt/demo_mode.py`'s fully isolated `wings_theory_demo.db` was confirmed to be the wrong mechanism for this request, since it never shows up in the real console — see the research that preceded this work).
3. If you ever want to **remove** this demo data entirely, re-running the seeding script with an empty date range isn't supported today — the straightforward removal is `DELETE FROM customer_order WHERE is_demo = 1` (cascades to `order_line`); the 8 drafted combos would need to be rejected through the review workflow (they're append-only, same as the cleanup above) rather than deleted.
