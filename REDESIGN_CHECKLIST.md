# Admin Console Redesign — Implementation Checklist

Living tracker for the approved 8-section redesign. Updated as work lands — not a
plan, a status board. "Verified" means checked live in the browser or via direct
API calls against real seeded data, not just "renders" or "passes a syntax check."

Status values: `done` / `refine` (implemented, needs polish) / `todo` / `blocked`.

---

## 2026-10-15 — End-to-end verification pass

This round was explicitly **verification and defect-fixing, not further redesign**,
per the request. Below is the full result, organized exactly as asked: Implemented
and tested / Implemented but not fully tested / Confirmed defects fixed /
Remaining limitations.

### ⚠️ Incident during this round: dev database data loss and recovery

Before verification could proceed, `backend/dev/preview.db` — the database the
preview server and all of today's testing runs against — was found to have lost
all its data (0 orders, 0 users, 0 ledger rows), while keeping the correct schema
(v55). **Production `wings_theory.db` was never touched and is not implicated.**
Root cause was not fully pinned down (best evidence points to a server-restart /
migration-lock interaction sometime between this session's earlier testing and this
round starting), but a clean backup existed: `backend/backups/preview.pre_migrate_to_v0055.20261014_203257.db`
(made automatically by the migration system's own pre-migrate backup step, dated the
evening before, schema v54, with all 33,592 orders / 4 users / 680,082 ledger rows
intact). With the user's explicit confirmation, this was restored, and the server's
normal migration path brought it cleanly from v54 to v55 (confirmed: no data loss,
`auth_session` table — migration 0055's only change — present post-migration). All
verification below is against this restored, real dataset.

### Implemented and tested

**1. Menu Pricing** — PATCH `/console/menu-items/{pk}/price` changes a price; GET
`/pos/menu` immediately reflects it (confirmed live: White Truffle Pasta ₱220→₱275→
reverted to ₱220); `order_line.unit_price` is written once at order creation and
there is no code path that ever updates it afterward (confirmed: a historical
order's line item kept its original ₱220.00 price unchanged after the menu price
was changed). `trg_order_financials_immutable` (migration 0011/0019) independently
blocks any UPDATE to a settled order's subtotal/total/etc. at the database level.

**2. Recipe Editing** — PATCH `/console/recipes/{pk}` with a changed ingredient
quantity recalculates cost correctly (confirmed live: doubling White Truffle
Pasta's Butter line from 10g→20g moved total_cost from ₱74.26→₱79.06, exactly the
expected +₱4.80; reverted cleanly). Confirmed the edit writes **zero** stock_ledger
rows and leaves a historical order_line's price untouched — a recipe edit only
changes the recipe going forward. `reserve_order()` snapshots quantities into the
`reservation` table at POS-punch time and `commit_order()` deducts from that
snapshot, never re-reading `recipe_line` live (confirmed by source read, consistent
with no ledger rows appearing from the edit) — in-flight orders are safe by
construction, not by a special-cased check.

**3. Receiving** — POST `/console/ingredients/{pk}/receive` opens a real FEFO batch
and writes a `purchase_receipt` ledger row atomically (confirmed live: receiving
2kg of Salt moved on_hand_base from 400,049g→402,049g exactly, created
`fefo_batch` id 9192 with the given lot number, and a matching ledger row). PO-line
receiving (`POST /console/purchase-orders/items/{id}/receive`) is idempotent via
`client_request_id`: firing the exact same request twice returns `"duplicate":
true` on the second call with `received_qty_base` unchanged (confirmed live — no
double-credit). Over-receipt beyond a line's ordered quantity is refused outright
(confirmed: requesting 5000 when only 2000 remained was rejected). The UI's submit
button also disables synchronously before any async work, closing the
accidental-double-click path independent of the server-side idempotency key.

**4. Order Traceability** — the order-number lookup fix from a prior round (ledger
`ref_id` resolved against both an order's current and legacy number) was tested
across every edge case asked for: a historical order with only a legacy number
(13 ledger entries found), a **brand-new order created this round with no legacy
number at all** (1 entry found — confirms the fix also covers orders created after
the renumbering, not just the ones it was originally found against), a search
string matching no real order (200 OK, 0 entries, no error), and numeric-tail
partial search (6 entries, consistent with existing documented LIKE behavior).
Both the Order Details modal's "Ingredients used" section and the Stock Activity
page's order-grouped view were confirmed live to show the correct real deduction
for the new order.

**5. Order History pagination** — confirmed the defect was real: the dataset spans
2025-01-01 to today, but the old "most recent 5000 of 33,593" approach under "All
time" only reached back to 2026-07-06 — **over a year of real order history was
permanently unreachable**, with no way to page further and search only ever
matching the one page already loaded in the browser. Fixed with real server-side
pagination and search (`GET /pos/orders` gained `offset` and `q` params; `q`
matches `order_no`, `table_label`, or any item name via an EXISTS subquery, scoped
by whatever date range is active). Confirmed live: paging reaches the actual oldest
order on record (`WTRORD-2025000001`, 2025-01-01) by both offset-paging and by
searching for it directly under "All time," something the old implementation could
never do. The frontend now loads at most 100 rows at a time (was up to 5000) with
Previous/Next controls, verified at desktop and tablet width. Fixed a secondary
bug found in the same code: `total_count` previously ignored the active date
filter entirely and always reported the grand total regardless of range — now it
reflects the same WHERE clause as the row query (confirmed: a date-scoped query
now reports an accurate count instead of the unconditional 33,593).

**6. Permissions** — tested via direct API calls (not just UI, so a missing
server-side check behind a hidden button couldn't hide): unauthenticated requests
→ 401 (no token, garbage token, both tested); Admin attempting the owner-only PO
draft endpoint → 403; Admin editing a menu price → 200 (admin-only, correctly
allowed); Admin attempting to punch a POS order (`order.create`, cashier-only) →
403 (an incidental extra confirmation of role separation); Owner successfully
drafting a PO → 200; a second device's session token revoked on password change
→ 401 on replay, including the very token used to request the change; logout
revokes that session's token immediately → 401 on replay. Code-read confirmed
`purchase_order.create` is owner-only, `inventory.write`/`recipe.write` are
admin-only, and the menu price endpoint requires `recipe.write` — all in
`auth.py`'s `CAPABILITIES` table, unchanged this round.

**7. ML Integrity** — Isolation Forest (stock-loss, unli-consumption,
flavor-consumption) artifacts are present on disk and match their registry
entries; startup validation did not flag them. FP-Growth combo mining is
confirmed **live-computed from real order history on every page load**, not
dependent on a persisted artifact at all ("FP-Growth over 20973 real orders (last
365 days)" shown in the UI's own technical-details panel) — so the registry issue
below does not affect the feature a user actually sees. The ingredient demand
forecast endpoint (`/console/forecasts/demand`) serves a real trailing-average/
seasonal-baseline result without erroring. No model artifact file was written
during this entire verification pass (confirmed by file-mtime check) — nothing
here triggered retraining, intentionally, per the explicit instruction not to.

**9. Security** (continued) — see Permissions above for the auth/session tests;
all passed.

### Confirmed defects fixed this round

- **Order History's 5,000-row cap silently made >1 year of real order history
  permanently unreachable.** Fixed with real server `offset`/`q` pagination and
  search (see item 5 above). This is the one item explicitly requested as new
  work in this round's instructions, not a re-verification of prior work.
- **`total_count` on `GET /pos/orders` ignored the active date filter**, always
  reporting the unconditional grand total. Found and fixed as part of the same
  change (now shares the row query's WHERE clause).
- **Dev database data loss** (see incident note above) — restored from an
  automatic pre-migration backup with the user's confirmation; zero data loss
  in the end, but this cost significant time mid-round and should be understood
  before it's trusted not to recur.

### Implemented but not fully tested

- **Hybrid forecast selection logic** (Prophet vs. seasonal baseline) was not
  exercised directly this round beyond confirming the baseline endpoint serves
  correctly — no test specifically forced the hybrid selector to choose Prophet's
  branch and verified that branch's output, partly because of the artifact issue
  below.
- **Replenishment backtest** was not re-run live this round; its correctness
  rests on the regression suite's `TestReplenishmentBacktestHistoricalReconstruction`
  /`TestReplenishmentBacktestFindings` classes, which passed (see Regression Tests
  below) but were not independently re-verified against the restored live data.
- **A full re-verification of every one of the 8 admin sections at tablet width**
  was not repeated this round — Today, Inventory, Menu, Orders, Purchase, Combos,
  Users & Access, Instructions, and Account Settings were all spot-checked live at
  desktop width this round (and tablet for Orders specifically), but a systematic
  tablet pass across literally every remaining page was not redone here — it was
  done in the prior redesign round and nothing touched this round should have
  affected layout on pages other than Orders and Menu.

### Remaining limitations — do not treat as production-ready without addressing

- **`demand_prophet` and `fpgrowth_combos` model-registry entries are broken**:
  startup validation failed for both (artifact files the registry points to are
  missing from disk, and their recorded rollback candidates are *also* missing —
  confirmed via the Activity Log and direct file listing). The actual files
  present in `ml_artifacts/` (`demand_prophet__2b2a7617aac9.joblib`,
  `fpgrowth_combos__8147c0d858f2.json`, dated 2026-10-13) don't match **any**
  training run recorded in `ml_training_run`, meaning there's an untracked
  training event somewhere in this dev environment's history. **This predates
  this session's work** — it is not something caused by today's testing. Per the
  explicit instruction not to trigger retraining, and because fabricating a
  `ml_training_run` row to "explain" the orphaned files would mean inventing
  provenance data (also explicitly prohibited), **this was deliberately left
  unfixed and is flagged here for an owner/ML-engineer decision**: either locate
  a matching artifact backup, or deliberately retrain Prophet/FP-Growth and record
  it as a real, new, traceable training run. In practice this is lower-impact
  than it sounds — the two live features that matter to users (demand forecasts,
  combo associations) both currently compute from a seasonal baseline / live
  FP-Growth rather than depending on these specific stored artifacts — but the
  model-status dashboard and anything that specifically calls the Prophet
  artifact directly should be assumed broken until this is resolved.
- **Root cause of the dev-database data loss was not conclusively identified.**
  The restore fixed the symptom; it did not rule out a recurring trigger. If this
  happens again, the same recovery path works (an automatic pre-migrate backup
  exists before any schema change), but it's worth someone with more context on
  the migration-lock/server-restart interaction looking at this directly.
- Test/QA data created during this round, left in place as real (not fabricated)
  records, consistent with "don't retroactively modify or delete":
  - Order `WTRORD-2026016708` (QA-TEST table, 1 Coke, committed) — a real order
    now in history, not cleaned up (orders are append-only by design; there is
    no "delete a test order" action, which is correct).
  - Purchase Order PO-2 (QA Test Supplier, Salt, partially received 1kg) — left
    `partially_received` since cancelling a PO in that state is correctly refused
    by the system (can't un-receive real stock by cancelling). The 1kg of Salt
    received is real, correctly-ledgered stock, not fake data.
- Four dev-only account passwords were reset to a shared QA password
  (`DevQaPass123!`) to make this round's login-dependent tests possible — this is
  the `dev/preview.db` sandbox only, never production, and mirrors what
  `reset_prod_passwords.py`'s own docstring describes doing for the real
  database when an owner needs to.

### Regression Tests (item 10)

Full suite (`python tests/run_tests.py`, 1130 tests) was run **twice** this
session: once before the `list_orders` pagination/search change — **1130 tests,
1 skipped, 0 failures, exit code 0** — and once after it, against the now-restored
real data — **1130 tests, 1 skipped, 0 failures, exit code 0** again. Both runs use
fully isolated tempfile SQLite databases per test class (confirmed by reading
`tests/run_tests.py`'s own setUpClass/tearDownClass pattern), so neither run could
have read or written `dev/preview.db` — the regression suite and the live-data
verification above are two genuinely independent checks, not the same test
counted twice. The pagination/search change introduced no regressions.

---

## Prior rounds (2026-10-14/15) — redesign checklist, superseded by the verification pass above where overlapping

## 1. Today
- Layout, KPI reorder, quick actions: **done**

## 2. Inventory
- Unified workspace (4 KPI cards, Needs Attention panel, Quick Actions, table, drawer): **done**
- Order-grouped Stock Activity toggle: **done** — order-number filter confirmed working this round (see Order Traceability above)

## 3. Menu & Recipes
- Menu page (compact cards, consolidated Menu Problems, search/filter): **done**
- Recipes & Costing: **done** — cost recalculation re-verified this round
- Combos: **done** — FP-Growth association mining confirmed live-computed this round

## 4. Purchasing
- What to Buy: **done**
- Purchase Orders: **done** — receiving/idempotency/FEFO/ledger all re-verified live this round
- Demand Forecasts: **done**, baseline endpoint re-verified; Prophet artifact issue noted above

## 5. Sales & Orders
- Overview: **done**
- Orders: **done** — real server-side pagination/search implemented and verified this round (see above)
- Reports: **done**

## 6. Management
- Setup & Verification: **done**
- Users & Access: **done** — plain-English summary + Advanced Permissions section
- Audit Trail: **done**

## 7. Instructions
- **done**, re-confirmed renders correctly this round

## 8. Account Settings
- **done**, re-confirmed renders correctly this round, password-change flow fully tested this round (see Security above)

---

## Next unfinished task
1. Get an owner/ML-engineer decision on the broken `demand_prophet`/`fpgrowth_combos`
   registry entries (see Remaining Limitations above) — do not silently retrain or
   fabricate provenance to resolve it.
2. Investigate the dev-database data-loss root cause if it recurs.

(Regression suite confirmed clean both before and after this round's changes —
see Regression Tests above. No other item from this round is outstanding.)
