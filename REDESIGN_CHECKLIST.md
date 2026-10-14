# Admin Console Redesign — Implementation Checklist

Living tracker for the approved 8-section redesign. Updated as work lands — not a
plan, a status board. "Verified" means checked live in the browser (desktop +
tablet), not just "renders" or "passes a syntax check."

Status values: `done` / `refine` (implemented, needs polish) / `todo` / `blocked`.

---

## 1. Today
- Layout, KPI reorder, quick actions: **done** (prior rounds)
- Tablet verified: **done** (prior rounds, not reconfirmed since CSS token updates — low risk, no structural change since)

## 2. Inventory
- Unified workspace (4 KPI cards, Needs Attention panel, Quick Actions, table, drawer): **done**
- Order-grouped Stock Activity toggle: **done** — the "Order #" filter inside it was silently broken for every real order until this round's backend fix (see Bugs below); the grouped view itself (which reads `ref_id` as-is, never through this filter) was unaffected
- Physical Stock Count full-screen flow: **done** (prior round)
- Tablet verified: **done** (prior round)

## 3. Menu & Recipes
- **Menu** page (rebuilt 2026-10-15):
  - Compact 3-card summary (Available/Blocked/Total), icon-chip RailCards matching Inventory's density: **done**, verified desktop + tablet
  - Consolidated Menu Problems card, top-3 + "View all N issues": **done** — fixed a real grouping bug during this pass (see below), verified live: Butter correctly rolls up 9 dishes into 1 row
  - Clickable problem rows expanding inline to the affected dish names: **done**, verified
  - Search (global topbar search, now enabled for this route) + status filter chips (All/Available/Blocked) on the table: **done**, verified — search matches name or category, filter chips combine with it
  - Admin price edit: **done** (existing, unchanged)
  - Add/Edit menu item controls: **blocked, not fabricated** — there is no backend endpoint to create a standalone `menu_item` row; the only real creation path is publishing a Combo (`POST /console/combos/publish`). Did not invent a fake "+ Add item" button. If a direct create-menu-item flow is wanted, that is new backend work, not a frontend gap.
- **Recipes & Costing**: searchable ingredient picker, add/edit recipe: **done**, tablet-verified this round
- **Combos**: two-column builder, FileUpload, three-section layout (Opportunities/Drafts & Reviews/Published — already matches spec under different labels): **done**, tablet-verified this round including the builder modal itself
- Tablet verified: **done**, all three pages this round

## 4. Purchasing
- What to Buy: table + detail drawer: **done**, tablet verified **done**
- Purchase Orders page: **done** — re-read the actual current file this round (the checklist's prior note that this was only bug-fixed was stale/wrong); it already has status-tabbed list (Draft/In Progress/Completed/Cancelled/History), a detail panel per order, partial-receiving with per-line lot number + expiry inputs, and owner-only drafting. Verified live + tablet this round, corrected the checklist instead of redoing working code.
- Demand Forecasts: reviewed, already compliant: **done**

## 5. Sales & Orders
- Overview: **done**
- Orders (OrderHistory.js): **done** — search, status filter, and a time-range selector already exist (this checklist's prior "todo — not redesigned" note was stale); added the "Ingredients Used" section to the shared order-detail modal this round (`orderDetailModal.js`), pulling real stock-ledger entries for the order via `listMovements({orderNo})`. Evaluated the 5,000-row cap: `list_orders` has no `offset` param, so real pagination would be new backend work, not a frontend gap — and the page already handles the cap honestly (range picker to narrow the window, explicit "showing X of Y, narrow the range" message instead of silent truncation). Treating this as an intentional, reasonable tradeoff rather than building speculative pagination for a read-only BI page.
- Reports: **done**, tablet verified **done**

## 6. Management
- Setup & Verification: table + drawer: **done**, tablet verified **done**
- Users & Access (rebuilt 2026-10-15):
  - Plain-English capability summary, shown by default: **done** — reference mode summarizes each role's full/partial access by area; editing mode summarizes one account's overrides against its role default in plain language
  - Raw capability-ID matrix moved under a collapsed "Advanced Permissions" section (auto-expands when an account is picked for editing, so the editor is never more than one click away): **done**, verified live both in reference mode and while editing cashier1
- Audit Trail: dedup + filters: **done**

## 7. Instructions
- Desktop split panel / tablet stack: **done**, tablet verified **done**

## 8. Account Settings
- Pinned destination, reused password-change logic: **done**

---

## Global / cross-cutting
- Design tokens (charcoal/ivory/burnt orange/slate blue/green/amber/red): **done**, contrast-checked
- No numeric spinners: **done** (global CSS rule)
- Custom FileUpload (no raw browser control): **done**
- Searchable ingredient pickers: **done** (Recipes, Verification)
- RailCard status-card redesign: **done**
- Visual density pass (oversized/repetitive panels): Menu page and Users & Access both tightened this round. Purchase Orders and Orders were re-checked and are already reasonably dense (tabbed table + single detail panel, not stacked cards) — no rework needed.

## Bugs found and fixed this round
- **Menu Problems grouping silently failed to group anything**: it keyed a `Map` on `str()`'s return value, which is a DOM Text node (a distinct object every call), not a string — so "Butter" blocking 9 dishes rendered as 9 separate one-dish groups instead of one nine-dish group. Fixed by keying on the raw string. Same class of bug as the earlier `[object Text]` issue, just silent instead of visible — worth remembering as a standing trap with `format.js`'s node-returning helpers (`str`, `qty`, `money`, `date`, etc.) in any context that needs a plain string (template literals, Map/Set keys, comparisons).
- **The global search box in the topbar was only ever wired to the `onhand` route** (`setSearchVisible(id === 'onhand')`); Menu's new search needed it too, so this is now `id === 'onhand' || id === 'menu'`, and the placeholder text is now route-aware (`setSearchPlaceholder`) instead of a hardcoded "Search ingredients…" that would have been wrong on every other page it's enabled for.
- **Significant pre-existing backend bug, found while building "Ingredients Used," not introduced by this round**: `GET /console/ledger?order_no=` (used by both the new Ingredients Used section and the existing order-grouped Stock Activity filter) matched a ledger line's `ref_id` against the order's *current* `order_no` — but a historical order-numbering migration rewrote every pre-existing order's `order_no` to a new scheme (`WTRORD-YYYYNNNNNN`) and moved its old code to `legacy_order_no`, without (correctly) rewriting the immutable `stock_ledger.ref_id` values that were already written under the old scheme (`WT-NNNNN`). Verified directly against the dev DB: **100% of the 33,590 orders in the dataset have `order_no != legacy_order_no`**, and every single committed order's ledger lines are keyed to the *legacy* code — meaning this filter, and by extension any ingredient-traceability lookup by today's order number, matched **zero rows for any real order** before this fix. Fixed in `backend/wt/api.py`'s `list_ledger` by resolving the given `order_no` to the order it names and matching `ref_id` against both of that order's codes (current and legacy) — no ledger row touched, only the lookup widened; falls back to the original raw-text LIKE search when the input doesn't name a real order. Verified live: searching Stock Activity by the new-format order number `WTRORD-2026016703` now correctly surfaces `Order #WT-34516`'s 13 ledger entries, and the new Ingredients Used section shows real deductions (Worcestershire Sauce, Salt, Paprika, Hot Sauce, Garlic Powder, Cornstarch, …) instead of "No ledger entries found." Backend regression suite (`python tests/run_tests.py`, 1130 tests) launched after this change — see Non-negotiables below for result status at time of writing.

## Non-negotiables (re-confirmed each round, not re-tested unless touched)
- Owner-only PO drafting: unchanged, confirmed in auth.py CAPABILITIES
- FEFO / append-only ledger / reserve→commit (no double deduct): unchanged, confirmed in reservation.py
- ML provenance (REAL/SIMULATED): unchanged, not touched
- No DB resets, no retroactive historical edits: unchanged — the order-number ledger fix above explicitly does NOT rewrite any `stock_ledger` row; it only widens a read-side lookup
- Backend regression suite: **ran clean** after the `list_ledger` fix — `python tests/run_tests.py`, 1130 tests, 1 skipped, exit code 0, no failures. The fix is verified, not just assumed.

---

## Next unfinished task
All items from this round's scope are now resolved or deliberately deferred with a
stated reason (see each section above). Re-checked Today and Inventory live this
round against the current design tokens — both render cleanly, dense, no drift.

Nothing is currently flagged as a required next step. If further work is wanted:
- Spec's original ask for a full form-field consistency audit across every page
  (height/padding/focus-ring/error-state) has only been done incidentally, on pages
  that were otherwise touched — never as its own systematic pass.
- No page in the console currently lets Admin/Owner create a standalone menu item
  outside of Combo Builder (see section 3) — worth a product decision, not a code gap.
