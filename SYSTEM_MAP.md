# Wings Theory — Full System Map

**Method:** Ground-up read-only re-audit, 2026-09-28, run as ten parallel/sequential research passes over the working tree and the live `backend/wings_theory.db` (schema_version=37). No assumptions carried over from any prior document — every claim below was independently re-derived and file:line cited by the agent that produced it. Where something could not be verified, that is stated explicitly rather than guessed. This document consolidates those ten reports; see conversation history for full citation detail behind each claim.

---

## 1. Stack & Deployment

- **Backend:** FastAPI, single router file `backend/wt/api.py` (3,696 lines, 116 endpoints). Entry: `uvicorn wt.api:app`.
- **DB:** raw SQLite3, WAL mode, `BEGIN IMMEDIATE` writers, no ORM (deliberate — `reservation.py`'s guarded-UPDATE pattern needs exact control over emitted SQL/rowcounts).
- **Frontend:** hand-rolled hash-routed vanilla-JS console SPA (`backend/static/assets/console/js/`) + three standalone HTML clients: `pos.html` (cashier), `kds.html` (kitchen), `landing.html` (session handoff).
- **Deployment model:** LAN-only. Two launchers, both confirmed on disk:
  - `start_server.ps1` — production: `WT_DB=backend/wings_theory.db`, `WT_ENV=production`, binds `0.0.0.0`, LAN-IP-change watcher loop, persisted `WT_SECRET` in `backend/.secret`, crash window stays open.
  - `start_server_dev.ps1` — development: `WT_DB=backend/dev/wings_theory_dev.db`, `WT_ENV=development`, binds `127.0.0.1` with `--reload`, separate `backend/dev/.secret`. Structurally cannot resolve to the production path.
- **Environment gating (`wt/db.py`):** `WT_ENV`/`WT_DB` have no defaults and must be set explicitly; `db_path()` cross-validates the pairing (`WT_ENV=production` must resolve to exactly the production file; any other `WT_ENV` must NOT). `validate_production_startup()` additionally refuses to boot in production if `WT_SECRET` is unset or still the hardcoded dev default.

---

## 2. Live Production DB Census (read-only, 2026-09-28)

37 tables, 10 views, 17 triggers — **live schema exactly matches the migration-derived expectation, zero discrepancies.**

| table | rows | table | rows |
|---|---|---|---|
| customer_order | 33,594 | order_line | 52,243 |
| order_charge | 6,622 | reservation | 342,175 |
| stock_ledger | 680,134 | unli_session | 21,022 |
| unli_refill | 56,650 | unli_refill_line | 315,886 |
| ingredient | 122 | menu_item | 32 |
| recipe | 45 | recipe_line | 357 |
| fefo_batch | 9,191 | admin_audit_log | 9,187 |
| app_user | 5 | login_event | 10 |
| unli_coefficient | 8 | unli_flavor | 14 |

**Empty in production** (feature exists, zero live usage to date): `purchase_order*` (3 tables), `role_page_permission`, `owner_instruction`, `operational_update`, `anomaly_review`, `draft_combo*`, `ingredient_variance_review`, `unli_reconciliation`, `unmapped_recipe_line`, `ingredient_alias`.

**`customer_order` by (source, status):** 33,588 of 33,594 rows (99.98%) are `source='historical', status='served'` (2025-01-01 → 2026-09-20). Only 6 rows are genuinely organic/live — the exact 6 rows this session spent most of its time on (5 confirmed test-fixture data with a temporally-impossible timeline, 1 confirmed genuine live order).

**Integrity checks (all clean):** 0 ingredients with `on_hand_base < 0`; 0 duplicate names in `ingredient`/`menu_item`/`recipe` (despite no UNIQUE constraint enforcing it — a snapshot fact, not a guarantee); 0 orphaned `recipe_line`/`menu_item`/`order_line` FK targets.

---

## 3. Database Schema — Full Detail

37 migrations (`0001`–`0037`), applied in order by `wt/db.py:migrate()`. Every table's final cumulative state, every trigger, every view, and the full FK dependency graph were verified line-by-line against migration text **and** cross-checked live via `PRAGMA table_info`/`PRAGMA foreign_key_list` — full agreement, zero discrepancies.

### Key structural facts
- **`ingredient`** (30 cols) and **`customer_order`** (22 cols) are the two hub tables — most inbound FKs (10 and 6 respectively), zero outbound FKs of their own.
- **17 triggers total**, 16 are append-only guards (`BEFORE UPDATE`/`BEFORE DELETE ... RAISE(ABORT,...)`) on 8 append-only tables (`stock_ledger`, `order_charge`, `verification_event`, `admin_audit_log`, `login_event`, `ingredient_variance_review`, `anomaly_review`, `draft_combo`/`draft_combo_review`). The one exception is **`trg_order_financials_immutable`** — created in migration 0011 (5 columns), **dropped and re-created** by 0019 to widen the guarded set to 7 (`subtotal, discount_type, discount_amount, service_charge, total, cash_received, change_due`), fires `WHERE OLD.total IS NOT NULL`. It guards `customer_order` only — never `order_line`, which is how this session's 429→399 correction was legally possible without touching the trigger.
- **10 views**, most notably `v_recipe_expansion`/`v_recipe_leaf` (recursive sub-recipe BOM expansion, depth-capped at 8 with cycle guard) and `v_needs_verification` (redefined by 0020 to structurally exclude `role='baseline'` unli coefficients from the ordinary verification queue).
- **Asymmetric ON DELETE behavior, confirmed deliberate:** `unli_reconciliation.ledger_id`/`coefficient_id` are `NO ACTION` while `unli_item_id` on the same table is `CASCADE` — a reconciliation row survives its coefficient/ledger reference disappearing but not its parent stream.
- **Open question, not resolved by inspection:** `role_page_permission.role`'s CHECK constraint (migration 0023) still only allows `admin/manager/cashier/kitchen` — the `owner` role added later (migration 0024) was never retrofitted into this table's legal values. `wt/permissions.py` (read in the gap-fill pass) does not resolve this either — it has zero code coupling to `auth.py`'s role system at all, only a documented (not enforced-in-code) layering relationship.

### Full FK graph, all triggers, all view SQL
Captured verbatim in the conversation's DB-schema-agent report — omitted here for length; available on request if a standalone reference file is wanted.

---

## 4. Module Inventory (`backend/wt/`, all ~30 files read in full)

| Module | Role | Fan-in |
|---|---|---|
| `reservation.py` | Two-phase stock reservation (RESERVE soft-hold → COMMIT real deduction), FEFO batch management, void handling, receiving, counts, wastage. **Highest fan-in module in the codebase** — `demo_seed`, `refills`, `unli`, `purchasing`, `xlsx_history_import`, `api` all depend on it, several import its underscore-prefixed internals directly (`_ledger`, `_try_hold`, `_receive_stock_locked`) rather than duplicating logic. | highest |
| `resolver.py` | Recipe expansion (via `v_recipe_leaf`), flavour substitution, whole-order ingredient requirement aggregation, recipe cost, cycle validation. POS availability gate and the deduction path read the *same* view so they structurally cannot disagree. | high |
| `db.py` | Connection/WAL/migration/backup engine. `write_tx()` refuses nesting; `migrate()` auto-backs up (WAL-checkpointed) before applying pending migrations to an already-used DB. | high |
| `auth.py` | RBAC core — hand-rolled HS256 JWT, PBKDF2 password hashing, `CAPABILITIES` dict (action-level). | high |
| `errors.py` | Shared exception hierarchy (`WTError` + 11 subclasses), most broadly imported module (19 confirmed importers). | highest |
| `unli.py` | Three-stage Unlimited Wings pipeline: Stage 1 provisional (`D = H×λ`, posted at order time via ordinary `sale_commit`), Stage 2 refill logging (sauce only, never wings again), Stage 3 reconciliation (`actual = opening+added-closing`, `variance = actual-theoretical`, `correction = theoretical-actual`, all three distinct columns, `correction == -variance` algebraically but computed/used separately). | medium |
| `refills.py` | Session/refill event management, two-phase (submit/serve) mirroring `reservation.py`. | medium |
| `verification.py` | Owner-verification queue (`v_needs_verification`) — confirm/change/keep_estimate, append-only `verification_event` log. `unli_coefficient.lambda` is the one field a "change" never edits in place (inserts a new row instead, for historical reproducibility). | medium |
| `forecasting.py` | Backward (trailing-ledger runway) + forward (Prophet/trailing-average) demand engines, shared `fit_prophet()`, chronological rolling backtest harness. | medium |
| `depletion.py` | Forward ingredient-level replenishment chain (EWMA + DOW-shrinkage + nowcast/pace/momentum, optional Prophet override for future days only). Sole source of `suggestedOrderQty`. | medium |
| `anomaly.py` | Two independent IsolationForest passes (stock-loss, unli-consumption) + parallel rule-based fallback layers. | medium |
| `associations.py` | FP-Growth product-combination mining, two-tier threshold strategy, zero caching. | low |
| `combo_pricing.py` | Pure deterministic cost/pricing math (not ML) consuming FP-Growth output read-only. | low |
| `permissions.py` | Page-visibility grants (`verify`/`system` pages, manager role only). **No code coupling to `auth.py`** — separate mechanism, documented-not-enforced layering. | low |
| `purchasing.py` | PO lifecycle (Draft→Ordered→Partially Received→Received); freezes `pack_size`/`yield_factor` at PO creation (migration 0028) but re-reads current values at receive time — deliberate asymmetry so `qty_base` round-trips exactly. | low |
| `owner_provisioning.py` | CLI-only, one-time OWNER account creation. Not HTTP-reachable. | none (CLI) |
| `backup_restore.py` | BACKUP→VERIFY→RESTORE→VERIFY. **Confirmed: `restore()` never calls `db.migrate()`** — a restored DB keeps its backup's schema version; operator must notice `schema_up_to_date:false` and migrate separately. No comment explains why this is deliberate rather than an oversight. | none (CLI) |
| `mapping.py` | Curated ingredient-alias table. **Confirmed dead/orphaned** — zero live imports anywhere, despite no "retired" language in its own docstring (unlike `gapfill.py`/`reference.py`, which do self-flag). Likely silently superseded by `xlsx_menu_audit_import.py`. |  none |
| `raw_import.py` | CSV-era importer. Self-declared "RETIRED 2026-09" in its own docstring. Confirmed zero importers. | none |
| `export_ui.py` | CLI-only JSON export for a strict-CSP console variant. Exports only facts; everything derived is computed client-side. | none (CLI) |

Full function-level inventory, every design-rationale comment, and every remaining module (`timeutil.py`, `units.py`, `cache.py`, `reference.py`, `gapfill.py`, `data_quality.py`, `instructions.py`, `updates.py`, `demo_seed.py`, `seed.py`) is in the conversation's five module-inventory agent reports.

---

## 5. Data Seed & Import Pipeline

**Source files that actually exist on disk** (corrected from an initial wrong assumption about `backend/data/` — the real path is top-level `data/`):
- `data/Wings_Theory_Menu_Ingredient_List.xlsx` — **live**, read by `xlsx_menu_audit_import.py`.
- `data/Wings_Theory_Order_Historys.xlsx` — **live**, read by `xlsx_history_import.py`.
- `backend/seed_data/*.csv` (4 files) + `README.md` — **dead as far as the database is concerned**. Only reader is `tests/audit_data.py`, which is **not part of the automated test suite** (not in `run_tests.py`'s loader tuple). The README describing this pipeline as still-live is stale documentation.

**Live import chain:** `seed.py` (orchestrator) → `xlsx_menu_audit_import.py` (ingredient/recipe/menu import, re-validates its own load-bearing assumptions — `Base Qty == Recipe Quantity`, exact ingredient-name match — on every run, not just once) → `gapfill.py` (`apply_pre`/`apply_post`, mostly retired except sub-recipe construction) → `reference.py` (flavour/portion config).

**`IGNORED_MENU_ITEMS`** (`"Leftover Charge - Wings/Rice/Fries"`): dropped from both `menu_item` and `recipe_line` import — confirmed these 3 rows have **zero lines in sheet 6 to begin with**, so nothing is silently lost; the charge mechanism (now `customer_order.leftover_charge_total`, added this session) was and is entirely separate from the menu-import pipeline.

**Unlimited Wings historical replay — confirmed, not assumed:** `xlsx_history_import.replay_order()` calls the *exact same* `reserve_order()`/`commit_order()` functions the live POS uses — never bulk-INSERTs `stock_ledger`/`reservation`/`fefo_batch` directly. One documented, accepted gap: it records one flat-price `order_line` per unlimited table regardless of `head_count`, producing a known ~₱794k reporting gap (₱2.62M workbook vs ₱1.83M recorded) between per-diner workbook lines and this system's flat recording — explicitly called out in the module's own comments as a pre-existing business-logic shape, not something the importer papers over.

**`source_order_id` matching — precise answer:** it is the **literal `Order ID` column** from the workbook, written verbatim. What is *inferred* is not the ID value but *which DB row it should attach to* (`xlsx_history_reconcile.py`'s `run()`) — via exact-timestamp + exact-line-signature matching, accepting a link only when exactly one candidate matches. Pure-unlimited orders are deliberately never linked to a specific ID (randomized-minute timestamps make the original import's ordering unrecoverable) — left `NULL` rather than guessed.

**Currently non-functional:** `xlsx_prophet_ingest.py` targets a workbook/sheet name that no longer exists (`Wings_Theory_Order_History_Prophet_Ready.xlsx` / `"90-Day Order History"`) — read-only validation code, no data-integrity consequence, but its "validate before ingest" safety net is presently a dead path.

**Currently absent:** `backend/scripts/generate_demo_history.py` — the one sanctioned, safety-wrapped entry point for `demo_seed.py` (hardcodes dev DB path, refuses if resolved path equals production) — is deleted from the working tree right now (present in git history only).

---

## 6. ML Pipeline — Full Detail

### Prophet (`forecasting.py`, `depletion.py`)
- One shared `fit_prophet()`, three call sites (per-item, store-wide, ingredient-forward-chain), all using identical hyperparameters by design: `growth='flat'` (deliberately overridden from Prophet's own `'linear'` default — an internal backtest found this improved mean WAPE from ~57% to ~52% at 7-day horizon), `interval_width=0.8`, `daily_seasonality=False`, `yearly_seasonality=False`, `weekly_seasonality` gated on ≥2 weeks of history.
- Ingredient-level Prophet fits **only** via explicit admin action (`POST /console/forecasts/refresh-prophet-baselines`) — never inline (~1.8s/item). Persisted in an **in-process dict only** — lost on restart, no disk persistence, 48h staleness window.
- Backtest (`evaluate_demand_model`): chronological expanding-window, verified leakage-free (`train_rows = [r for r in rows if r["d"] < test_start_date]`).
- **Known risk:** `fit_prophet()`'s bare `except Exception: return None`, no logging — a real crash is indistinguishable from "not enough data" in production logs.

### Isolation Forest (`anomaly.py`) — two independent passes
- Pass A (stock-loss): 2 features, `contamination="auto"` capped post-hoc to top 10% (`MAX_STOCK_LOSS_FLAG_FRACTION`) — uncapped flagged 19.6% of real entries in testing.
- Pass B (unli-consumption, the module's own "specified model"): 7 features, capped to top 25% — a **different cap value** than Pass A with no cross-reference explaining the difference.
- **Confirmed cross-consumer inconsistency:** Pass B trains on `unli_reconciliation` rows with `validation_state <> 'rejected'` (includes unreviewed `pending_review` days), while `unli.py`'s coefficient recalibration requires `validation_state = 'validated'` for the same table — an unconfirmed measurement can shape anomaly scoring before a human signs off on it, while the coefficient engine refuses that same row.
- **No precision/recall/F1 exists for Pass B at all** — only Pass A (the older, less-emphasized pass) has `validation_metrics()`.

### FP-Growth (`associations.py`)
- Basket = one real transaction (`COALESCE(source_order_id, id)`), not raw `customer_order.id` — a documented 2026-09 bug fix (grouping by id alone silently hid all cross-category associations).
- Two-tier threshold strategy (primary/fallback), every metric independently recomputed from raw counts rather than trusted from mlxtend.
- **Not cached at all** — unlike every Prophet/IsolationForest consumer, re-mined on every call; `/console/model-details` can trigger up to 4 full mining passes per request with zero caching.

### Caching (`cache.py`)
- TTL is a memory safety net only; real invalidation is a composite fingerprint (`MAX(customer_order.id)`, `MAX(stock_ledger.id)`, etc.) baked into each cache key.
- One confirmed gap: the Prophet-backtest cache key in `/console/model-details` omits `menu_item.pos_visible` changes — toggling POS visibility doesn't invalidate a cached backtest until an unrelated order lands.

---

## 7. API & Business Logic

- **116 endpoints**, every mutating route has a capability check except the two deliberate exceptions (`POST /auth/login`, `POST /auth/change_password`).
- **Order lifecycle — confirmed NOT atomic across two steps:** order-row creation and stock reservation (`reserve_order`) are **two separate database transactions**, stitched only by application-level try/except cleanup (a compensating delete), not real ACID atomicity. The code's own comments (`api.py:1111-1140`) already acknowledge this as a known crash-recovery gap: a hard crash between the two transactions leaves an orphaned `punched` order with no reservation behind it.
- **Kitchen commit IS atomic** — `commit_order()` does the guarded stock UPDATE, FEFO depletion, ledger insert, and reservation-state flip all in one `write_tx`.
- **Guarded-UPDATE concurrency pattern:** every `on_hand_base`/`reserved_base` mutation is a single SQL statement whose WHERE clause re-checks the invariant at the instant of write (`WHERE (on_hand_base - reserved_base) >= qty`) — no SELECT-then-UPDATE race window, confirmed safe under SQLite's `BEGIN IMMEDIATE` serialization.
- **CAPABILITIES table:** `admin` = everything; `manager` = read-only across the board, zero write capabilities; `owner` (added 2026-09-11) = read-only + 2 exclusive workflow capabilities (`instruction.create`, `purchase_order.create`); `cashier`/`kitchen` scoped to their own floor operations.

---

## 8. Console Frontend

- **Definitively confirmed: no free-text or manual-price-override input exists anywhere in `pos.html`'s order-entry flow.** Full inventory of all 6 `<input>` elements in the file — 4 are login/password fields, 1 is a read-only search box over the (already-created) current-orders list, 1 is the cash-tendered amount at checkout (a payment-reconciliation input, not a pricing input). Order total is entirely server-computed.
- Frontend role-gating (`canWrite()`, `canView()`, `ALLOWED_ROLES`) is explicitly self-documented as cosmetic — every mutating endpoint re-checks server-side via `needs()` regardless of what the UI shows.
- Three deliberately separate severity vocabularies exist in `lib/severity.js` (backward-engine risk, forward-engine depletion status, anomaly high/medium) — kept un-merged on purpose after a real historical bug where `risk='out'` silently fell through to a neutral display state.

---

## 9. Known, Disclosed Limitations (for thesis documentation)

1. **₱630,390 aggregate drift** between `customer_order.total` (frozen, trigger-enforced) and `order_line`'s corrected sum, across the 21,013 orders fixed this session — evidence the immutability trigger works as designed, not a bug.
2. **~₱794k historical revenue-reporting gap** on Unlimited Wings orders (₱2.62M workbook vs ₱1.83M recorded) — a pre-existing flat-price-per-table vs. per-diner-line shape, documented in the importer's own comments as not correctable without reinventing protected business logic.
3. **Order-creation/reservation atomicity gap** — two transactions, not one; a hard crash between them can orphan a punched order.
4. **Cross-consumer validation-state inconsistency** — the consumption anomaly model trusts `pending_review` unli-reconciliation days that the coefficient recalibration engine refuses.
5. **`role_page_permission` never retrofitted for the `owner` role** — added after the table's CHECK constraint was written; not resolved by any code read in this audit.
6. **`TestPagePermissions` (8 tests) is defined but never loaded into the automated test suite's runner** — confirmed by diffing every test class against `main()`'s loader tuple.
7. **`xlsx_prophet_ingest.py` is currently non-functional** against the present dataset (targets a workbook/sheet that no longer exists) — its "validate before ingest" safety net is a dead path.
8. **`backup_restore.py`'s `restore()` never calls `db.migrate()`** — a restored DB keeps its backup's schema version; no comment in the file explains whether this is deliberate.
9. **`generate_demo_history.py`** — the one safety-wrapped entry point for synthetic data generation — is currently deleted from the working tree (present only in git history).

---

*Compiled from ten parallel/sequential read-only research passes. No file was edited and no database write occurred during this audit. Full per-module citation detail (file:line for every claim) is preserved in the conversation transcript that produced this document.*
