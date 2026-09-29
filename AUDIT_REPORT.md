# Wings Theory — Full Read-Only System Audit

**Scope:** ML (Prophet / Isolation Forest / FP-Growth), console (frontend + API), and the XLSX/CSV data pipeline, for the codebase at `C:\Users\PLPASIG\Desktop\wings-theory`.
**Method:** Read-only static audit. No files were edited. Every factual claim below is cited as `file:line`. Where no evidence was found, this is stated explicitly as **NOT FOUND** rather than assumed.
**Date of audit:** 2026-09-24.

---

## 1. System Map

**Stack (verified from imports, not assumed):** Backend is **FastAPI** — `app = FastAPI(...)` at `backend/wt/api.py:70`; single ASGI target `uvicorn wt.api:app` (`backend/wt/api.py:3`). Database is **raw SQLite3** via the stdlib `sqlite3` module — no ORM, no Alembic (rationale documented at `backend/requirements.txt:1-11`). Frontend is a hand-rolled, hash-routed, framework-free vanilla-JS SPA ("the console") under `backend/static/assets/console/js/`, using a self-hosted Chart.js.

| Directory | Purpose |
|---|---|
| `backend/wt/` | The entire application: HTTP API (`api.py`, 3685 lines, single router file), DB/migration engine (`db.py`), business logic — `resolver.py` (recipe expansion/deduction sizing), `reservation.py` (reserve/commit/FEFO stock engine), `unli.py` (unlimited-wings reconciliation), `forecasting.py` (Prophet), `anomaly.py` (Isolation Forest), `associations.py` (FP-Growth), `units.py` (unit conversion), `mapping.py`/`gapfill.py`/`seed.py`/`raw_import.py` (data import pipeline), `auth.py` (JWT/RBAC), `depletion.py` (forward-looking stock trajectory), `cache.py` (TTL+fingerprint caching). |
| `backend/migrations/` | 36 forward-only numbered `.sql` files, applied in order by `db.migrate()` (`backend/wt/db.py:178-324`), tracked in `schema_version`. |
| `backend/seed_data/` | Owner's raw CSV exports + generated "tidy" CSVs + `README.md` documenting the raw→tidy→DB pipeline; **not read by the running production server** (`backend/seed_data/README.md:5-7`). |
| `backend/static/` | Served frontend: `console.html`, `pos.html`, `kds.html`, `landing.html`, plus the `assets/console/js/` component tree, mounted at `backend/wt/api.py:3649-3662`. |
| `backend/tests/` | `run_tests.py` (452-test `unittest` suite), `audit_data.py` (standalone source-CSV-vs-DB completeness diff tool), `demo_flow.py`. |
| `backend/docs/` | Markdown narrative documentation (checklists, prior ML audits). |
| `backend/dev/`, `backend/backups/` | Runtime SQLite files / backup destination — not source. |
| `data/` | The three source `.xlsx` workbooks (see §2). |

**Entry point / startup:** `backend/start_server.ps1:56-59` runs `python -m uvicorn wt.api:app --host 0.0.0.0 --port 8000` with `WT_ENV=production`, `WT_DB` pinned to `backend/wings_theory.db`, and a persisted `WT_SECRET` (`start_server.ps1:24-40`). Startup hook `backend/wt/api.py:119-125` calls `db.validate_production_startup(conn)`. No scheduler/cron process exists anywhere in this codebase — confirmed by the sub-agent's grep and an explicit code comment (`backend/wt/anomaly.py:43-44`): "there is no scheduler in this deployment." All retraining is either per-request (Isolation Forest, FP-Growth) or triggered by an explicit admin action (Prophet baseline refresh).

**Deployment model:** LAN-only, single SQLite file, WAL mode, `BEGIN IMMEDIATE` writers (`backend/wt/db.py:8-17`). Static/health routes are unauthenticated by explicit design under this LAN threat model (`backend/wt/auth.py:11-14`).

---

## 2. XLSX/CSV Data Dictionary and Data Flow

### 2.1 Source files and who reads them

| File | Read by | Citation |
|---|---|---|
| `data/Wings_Theory_Master_Inventory.xlsx` | **Nobody at runtime.** No `openpyxl`/`pandas` call in `backend/` targets this file (confirmed by repo-wide grep). It is referenced only in code **comments** as the human source that `YIELDS`/`PACK_SIZES` (`backend/wt/seed.py:58-112`) and `RECONCILED_INGREDIENTS`/`SUB_RECIPES` (`backend/wt/gapfill.py:64-269`) were manually transcribed from. |
| `data/Wings_Theory_Order_History.xlsx` | `wt.xlsx_history_import._load_rows()` via `openpyxl.load_workbook` | `backend/wt/xlsx_history_import.py:108, 117, 163` |
| `data/Wings_Theory_Order_History_Prophet_Ready.xlsx` | Same importer (`--source prophet_ready`) plus a read-only pre-import validator | `backend/wt/xlsx_history_import.py:126, 442-478`; `backend/wt/xlsx_prophet_ingest.py:40, 79-81` |
| `backend/seed_data/Inventory_List_Tracker - Inventory.csv` (raw) | `wt.raw_import.parse_inventory()` | `backend/wt/raw_import.py:73-100` |
| `backend/seed_data/Inventory_List_Tracker - Product Item Breakdown.csv` (raw) | `wt.raw_import.parse_breakdown()` | `backend/wt/raw_import.py:103-131` |
| `backend/seed_data/inventory_tidy.csv` (generated) | `wt.seed.import_inventory()`, `wt.gapfill.apply_pre()`, `tests/audit_data.py` | `backend/wt/seed.py:235-296`; `backend/wt/gapfill.py:356-362`; `backend/tests/audit_data.py:25-28` |
| `backend/seed_data/product_item_breakdown_tidy.csv` (generated) | `wt.seed.import_recipes()`, `tests/audit_data.py` | `backend/wt/seed.py:299-409`; `backend/tests/audit_data.py:69-72` |

**Finding:** `Wings_Theory_Master_Inventory.xlsx` is a documentation artifact, not a live pipeline input. Opened directly (read-only, outside the app) to document its shape for this audit:
- Sheet `Master Inventory` (116 rows): `Item Name, Category, Total Purchase Quantity, Purchase Unit, Pack Size (Base Units), Base Unit (G/ML/PCS), Total Base Quantity, Recipe Usage Unit, Cost/Pack (PHP), Total Cost (PHP), Shelf Life / Expiry, Recipe Demand (Base Qty), Used In (# Recipes)`.
- Sheet `Recipe Detail` (458 rows): `Menu Category, Menu Item, Ingredient (as written), Quantity, Unit, Menu Price (PHP)`.
- Sheet `Sub-Recipes & Exclusions` (5 rows): `Item, Type, Detail`.

Its figures were hand-copied once into Python source. **There is no automated pipeline keeping the workbook and the code constants in sync** — an edit to the `.xlsx` has zero effect on the running system until someone manually re-transcribes it. This is worth stating precisely at a thesis defense: the spreadsheet is the nominal source of truth, but the code is the actual one.

### 2.2 Column-level data dictionary and flow

**`Inventory_List_Tracker - Inventory.csv`** (section-header format, not one-record-per-row; parsed by `backend/wt/raw_import.py:73-100`):

| Column | Meaning | Destination |
|---|---|---|
| `Category` | Section label (`Wet Foods`/`Dry Foods`/etc.) | `ingredient.category` (CHECK-constrained) via `_category()` (`backend/wt/seed.py:214-226`) |
| `Product Name` | Ingredient display name | `ingredient.name`, and the key of the `ids` dict used throughout import (`backend/wt/seed.py:264`) |
| `Product Count` | Owner-counted purchase-unit quantity | Multiplied into `on_hand_base` (see §2.4) |
| `Unit` | Purchase unit as written | `ingredient.purchase_unit`; feeds `_infer_base_unit()` (`backend/wt/seed.py:203-211`) |
| `Price (PHP)` | Price per purchase unit | `ingredient.unit_cost`, flagged `needs_verification` (`backend/wt/seed.py:269`) |
| `Expiry Date` | Owner's date guess or `N/A` | `fefo_batch.expires_at`/`expiry_status` (`backend/wt/seed.py:282-295`) |

**`Inventory_List_Tracker - Product Item Breakdown.csv`** (block-header BOM format; parsed by `backend/wt/raw_import.py:103-131`). Two block types are explicitly diverted rather than tidied: `"(Sub-Recipe Definition)"` blocks (handled instead by hand-authored `SUB_RECIPES`, `backend/wt/gapfill.py:148-269`) and `"Unlimited Wings"` ratio blocks (`backend/wt/raw_import.py:12-27, 42, 119-121`) — both are logged, never silently dropped (`backend/wt/raw_import.py:147-153`).

| Column | Meaning | Destination |
|---|---|---|
| `Product / Recipe` | Menu/recipe title | Groups rows → `recipe.name` (`backend/wt/seed.py:317-321`) |
| `Ingredient` | Free-text ingredient label | Normalized (`backend/wt/units.py:51-60`), resolved via `ALIASES`/`SUB_RECIPE_ALIASES`/`FLAVOUR_PLACEHOLDER` in `backend/wt/mapping.py`, or queued as `unmapped_recipe_line` |
| `Quantity` | Free-text quantity ("120 g", "1/2 pc", "1,000 g") | Parsed by `parse_quantity()` (`backend/wt/units.py:77-114`) → `recipe_line.qty_base` |
| `Note` | Free-text annotation | Drives `recipe_line.is_optional` when "optional" appears (`backend/wt/seed.py:398`) |

### 2.3 Loading / type conversion / unit handling

- **Quantity parsing** — `parse_quantity()`, `backend/wt/units.py:77-114`: handles thousands-comma vs decimal ambiguity (fixing a documented prior bug where `"1,000 g"` parsed as `1.0 g`, a 1000× under-deduction — docstring `units.py:82-88`), en/em-dash and `½` normalization, and flags ranges (`"35-40 g"`) and unrecognized tokens as `ambiguous` rather than guessing.
- **Unit scaling** — `_SCALE` dict, `backend/wt/units.py:30-38`: same-family only (kg↔g, l↔ml, etc.); no cross-family g↔ml conversion exists by design ("that needs a density, which is per-ingredient" — `units.py:28-29`).
- **`to_base()`** raises `UnitConversionError` rather than guessing on an unknown unit (`backend/wt/units.py:117-132`).
- **`purchase_to_base()`** — the core `qty × pack_size × yield_factor` formula (`backend/wt/units.py:135-147`); raises on `pack_size ≤ 0` or `yield_factor ≤ 0`.
- **Mojibake repair** — `_fix_mojibake()` (`backend/wt/raw_import.py:55-70`) best-effort-repairs double-encoded UTF-8 text, falling back to the original string rather than guessing wrong.

### 2.4 Validation — blanks, wrong units, duplicates, unmapped references

| Scenario | Finding |
|---|---|
| Blank `Product Count` in raw CSV | **Not validated as an error** — treated as a new section header (`backend/wt/raw_import.py:82-85`). A genuinely blank count on a real product row is silently reinterpreted as a section boundary. |
| Non-numeric `Product Count` at seed time | `float(row["Product Count"])` (`backend/wt/seed.py:245`) has **no try/except** — raises uncaught, aborting the whole seed transaction via `db.write_tx()`'s rollback (`backend/wt/db.py:148-163`). Fail-loud, but no dedicated validation message. |
| Wrong/unrecognized unit | `UnitConversionError` is caught at `backend/wt/seed.py:369-385` and the line is diverted to `unmapped_recipe_line` with `reason='ambiguous'` — a real, working guard. |
| Duplicate ingredient/menu/recipe names | **NOT FOUND — no protection.** `ingredient.name`, `menu_item.name`, `recipe.name` all lack a `UNIQUE` constraint (`backend/migrations/0001_ingredient_master.sql:31-82, 103-119, 144-155`; only the generated `*_id` codes are unique). A duplicate `Product Name` in the CSV would insert two `ingredient` rows, and the Python-side `ids` dict (`backend/wt/seed.py:236-275`), keyed by name, would silently keep only the second row's key — orphaning the first row's `ingredient`/`fefo_batch` records from every downstream lookup, with no error raised. |
| Unmapped recipe references | **Explicitly handled** — `unmapped_recipe_line` table (`backend/migrations/0001_ingredient_master.sql:298-317`) with a CHECK-constrained `reason` vocabulary; `v_menu_item_availability` blocks a menu item from being sellable if it has any open unmapped line (`0001_ingredient_master.sql:394-420`). `wt.gapfill.apply_post()` does a second-pass curated resolution (`backend/wt/gapfill.py:500-559`), never fuzzy-matching automatically. |
| Completeness audit | `backend/tests/audit_data.py` diffs tidy CSVs against the live DB bidirectionally — quantity-fidelity checks recompute `qty × pack_size × yield_factor` and compare to `on_hand_base` (lines 40-57). |
| Order-history XLSX row validation | `_load_rows()` rejects (with reasons) unparseable dates, missing items, non-numeric/non-positive quantities, negative prices (`backend/wt/xlsx_history_import.py:150-198`). The stricter `xlsx_prophet_ingest.validate_workbook()` adds plausibility bounds (`MAX_PLAUSIBLE_QTY=200`, `MAX_PLAUSIBLE_PRICE=5000.0`, `xlsx_prophet_ingest.py:64-65`), duplicate-signature detection, and cross-sheet reconciliation against the "Daily Store Demand" aggregate sheet (`xlsx_prophet_ingest.py:391-446`). |

### 2.5 Are yield factors and sub-recipes actually applied in deduction math?

**Yes — confirmed, applied exactly once, with the exact points cited.**

1. **Yield table** (`backend/wt/seed.py:106-112`): `Rice: 2.5` (raw→steamed), `Canned Tuna Big Can: 0.70`, `Mushroom Pieces and stems: 0.65`, `Shitake Mushroom: 0.65`, `Fresh Garlic Peeled: 0.95`. **Note for the thesis:** no `yield_factor` entry named "Mango" exists at 0.65 or any other value — mango is handled as a **piece-weight** conversion instead (`PIECE_WEIGHTS["Mango"] = 180` g/piece, `backend/wt/gapfill.py:102-114`), a structurally different mechanism from `YIELDS`. If citing "mango flesh yield ~65%," that figure is not present in this codebase; 0.65 belongs to the mushroom entries.
2. **On-hand stock application**: `on_hand = qty * pack * yield_f if pack else 0.0` — `backend/wt/seed.py:246`.
3. **Recipe-line application at import**: `recipe_line_to_base()` → `purchase_to_base()` (`backend/wt/units.py:157-188, 135-147`), called from `backend/wt/seed.py:369-373`, writes the pre-scaled `recipe_line.qty_base`.
4. **Deduction at order time re-reads the pre-scaled value only** — `resolver.order_requirements()` computes `SUM(l.qty_base * ol.qty)` against `v_recipe_leaf` (`backend/wt/resolver.py:202-211`), and the recursive sub-recipe view `v_recipe_expansion` scales only by `sub.yield_qty` (the sub-recipe's own batch yield) during expansion (`backend/migrations/0001_ingredient_master.sql:351`) — the raw ingredient's `yield_factor` is never referenced again after import. **No double-application found.**
5. **Receiving new stock uses the same formula**: `reservation.receive_stock()` — `base_qty = purchase_to_base(purchase_qty, row["pack_size"], row["yield_factor"])` (`backend/wt/reservation.py:616`).
6. **Costing divides by the same factors** (inverse direction): `resolver.recipe_cost()` (`backend/wt/resolver.py:343`) and `inventory_valuation()` (`backend/wt/reservation.py:1181-1203`) both use `unit_cost / (pack_size * yield_factor)` — with a code comment documenting a previously-fixed bug where dividing by `pack_size` alone (forgetting `yield_factor`) produced a wrong valuation (`reservation.py:1181-1188`).
7. **Sub-recipe composition (14 flavour sauces + 3 house preps) is a genuinely applied recursive BOM, not stored-only data**: `SUB_RECIPES` (`backend/wt/gapfill.py:148-269`) creates real `ingredient`/`recipe`/`recipe_line` rows; `v_recipe_expansion`'s recursive CTE walks and scales them at deduction time, depth-capped at 8 with a cycle guard (`backend/migrations/0001_ingredient_master.sql:340-363`). Per-order flavour choice is applied as a **live signed correction** via `resolver.flavour_substitutions()` (`backend/wt/resolver.py:112-174`), actually subtracted/added into `order_requirements()`'s deduction total based on which sauce the cashier picked (`resolver.py:250-257`) — this is a per-order application, not a static report.

### 2.6 Relevant DB schema (migration citations)

- `ingredient` — `backend/migrations/0001_ingredient_master.sql:31-82`: `pack_size` (CHECK>0), `pack_size_status`, `yield_factor` (DEFAULT 1.0, CHECK>0), `on_hand_base`/`reserved_base` (CHECK `reserved_base ≤ on_hand_base + 1e-6`, line 81).
- `recipe` / `recipe_line` — `0001_ingredient_master.sql:103-139`: `recipe_line.ingredient_id` is `NOT NULL` (explicitly "the core constraint of this phase," comment lines 121-123), `qty_base` CHECK>0.
- `unmapped_recipe_line` — `0001_ingredient_master.sql:298-317`.
- `unli_item`/`unli_coefficient`/`unli_reconciliation` — `0001_ingredient_master.sql:160-198`, `role` CHECK `baseline`/`rolling`; extended by `0010_unli_reconciliation.sql`, `0011_unli_session_refills.sql`, `0020_freeze_unli_baseline_from_verification.sql`.
- `fefo_batch` — `0001_ingredient_master.sql:275-291`, pick order strictly by `expires_at` (FEFO, not FIFO).
- `stock_ledger` — `0001_ingredient_master.sql:246-270`, append-only via triggers `trg_ledger_no_update`/`trg_ledger_no_delete`.
- Derived views: `v_recipe_expansion`/`v_recipe_leaf`/`v_ingredient_readiness`/`v_menu_item_availability`/`v_needs_verification`/`v_ingredient_available` — `0001_ingredient_master.sql:333-458`.
- Domain-specific fixes: `0018_menu_recipe_audit_2026_08.sql`, `0021_flavor_recipe_reconciliation_2026_09.sql` (the 19-ingredient/13-sauce reconciliation), `0022_zest_pack_size_2026_09.sql` (fixes a `is_deductable` regression 0021 introduced), `0032_baking_powder_gap_2026_09.sql`, `0033_ingredient_variance_review.sql`.

---

## 3. ML Models

### 3.1 Prophet (demand forecasting)

- **Fit function:** `fit_prophet()` — `backend/wt/forecasting.py:369-455`. Per-item wrapper `menu_item_demand_prophet()` (`forecasting.py:322-366`); store-wide wrapper `store_demand_prophet()` (`forecasting.py:909-956`); ingredient-forward-chain consumer `refresh_prophet_baselines()` (`backend/wt/depletion.py:476-524`); backtest harness `evaluate_demand_model()` (`forecasting.py:701-835`).
- **Input:** daily, per-menu-item (or store-wide) sales series, zero-filled across every calendar day in the lookback window (`forecasting.py:229-260, 254-260`; store-wide `860-906`), excluding voided orders (`o.status != 'void'`, `forecasting.py:248`). Lookback/horizon defaults: per-item 90/7 days (`forecasting.py:323`), ingredient chain 56/21 days (`depletion.py:129-130`), backtest 365 days (`forecasting.py:738, 857`). Univariate — only `ds`/`y` columns, no exogenous regressors (`forecasting.py:405`).
- **Preprocessing:** explicit zero-fill for real zero-sales days; no outlier clipping before fit (outliers are detected post-fit as a diagnostic only); void orders excluded.
- **Hyperparameters:** `Prophet(daily_seasonality=False, weekly_seasonality=..., yearly_seasonality=False, interval_width=0.8, growth="flat", ...)` — `forecasting.py:416-417`. `growth` is overridden from Prophet's stock `"linear"` default to `"flat"` based on an internal accuracy audit (`forecasting.py:44-63`). Weekly seasonality gated on ≥2 weeks of history (`MIN_SEASONALITY_WEEKS`, `forecasting.py:41, 401-402`); minimum sample guard `MIN_DEMAND_SAMPLE_DAYS = 7` (`forecasting.py:40`, enforced at `284, 343, 617, 925`).
- **Retrain trigger:** per-item/store forecasts are refit on every request (subject to caching below); the ingredient-forward-chain fit is refit **only** via an explicit admin action, `POST /console/forecasts/refresh-prophet-baselines` (`backend/wt/api.py:2869-2885`) — never inline, because a single fit costs ~1.8s (`api.py:2876-2879`). No cron exists anywhere in the codebase.
- **Model persistence:** **in-memory only**, no pickle/DB blob. Ingredient baselines live in a module-level dict `_prophet_store` (`depletion.py:162`), going stale after 48h (`PROPHET_STALE_AFTER_SECONDS`, `depletion.py:161, 547-548`) and lost entirely on process restart with no auto-refresh on startup.
- **Caching/invalidation (Phase 16):** `backend/wt/cache.py`'s `TTLCache` (300s TTL, 200-entry cap — `cache.py:30-31`), but invalidation is primarily **data-fingerprint-driven**, not TTL: per-item/store forecasts key on `MAX(customer_order.id)` (`api.py:2736-2739, 2760-2764`); the ingredient-forward-chain cache keys on a composite fingerprint of `MAX(customer_order.id)`, `MAX(stock_ledger.id)`, `MAX(recipe_line.id)`, `MAX(ingredient.updated_at)`, current hour, and a Prophet-refresh generation counter bumped every time `refresh_prophet_baselines()` runs (`api.py:2779-2819`; `depletion.py:169-171, 517-519`) — so a completed refresh invalidates the depletion cache on the very next request even absent a new order.
- **Output/consumer:** Per-item/store forecasts populate `GET /console/forecasts/demand` and `.../store-demand`. The ingredient-forward chain populates `demandSource: "prophet"|"ewma"` inside `GET /console/forecasts/ingredients/{pk}`, consumed by `IngredientStory.js:188-214` and rendered as the `TrajectoryChart`.
- **Finding — dead code path:** `getForecast()`/`listDemandSummaries()` (`backend/static/assets/console/js/lib/api/intelligence.js:22-33`) are defined but have **no call sites anywhere under `routes/`** — the per-item Prophet demand-forecast API appears unwired in the live console; only the replenishment table (backed by the ingredient chain, EWMA by default unless a refresh was run) is actually rendered.
- **Correctness risks:**
  - Broad `except Exception: return None` with no logging in the fit path (`forecasting.py:404-452, 65-72`) — a fit failure is indistinguishable from "Prophet not installed" to an operator.
  - Up to 48h-stale Prophet fits can be served as `demandSource: "prophet"` with no distinct staleness flag in the API response.
  - In-memory-only model store means a server restart silently reverts every ingredient to EWMA until an admin manually re-runs the refresh — no automatic re-trigger on startup.
  - **No data leakage found** in the backtest path: `_rolling_fold_starts()` builds strictly chronological expanding-window folds (`forecasting.py:582-591`), and training rows are truncated to `d < test_start_date` before each fold's fit (`forecasting.py:636-640`) — this is correct.

### 3.2 Isolation Forest (anomaly detection) — two independent passes

**3.2a Stock-loss pass** — `detect_anomalies()`, `backend/wt/anomaly.py:119-206`.
- Input: per-ledger-entry (not daily), pooled across ingredients, `reason IN ('wastage','spoilage','count_adjustment')`, 90-day lookback (`anomaly.py:119-132`).
- Features (2): `relative_mag` (|delta| ÷ per-ingredient mean |delta|) and `fraction` (|delta| ÷ balance-before) — `anomaly.py:142-149`.
- Hyperparameters: `IsolationForest(n_estimators=200, contamination="auto", random_state=42)` (`anomaly.py:62-63, 155-156`); output capped post-hoc to the worst-scoring `MAX_STOCK_LOSS_FLAG_FRACTION = 0.10` because raw `contamination="auto"` over-flagged 19.6% of a real ledger in testing (documented, `anomaly.py:65-76, 159-168`).
- A parallel rule-based layer (`RULE_FRACTION_THRESHOLD = 0.25`, any single entry removing ≥25% of prior stock) always runs, independent of the model (`anomaly.py:61, 187-202`).
- Severity cutoff hardcoded: `score < -0.15` (`anomaly.py:181`).
- Trained fresh on every call, cached via fingerprint on `MAX(stock_ledger.id)` (`api.py:2983-2989`).
- Output → `GET /console/anomalies` (`stock_loss_anomalies`) → `routes/losses/StockLoss.js`.

**3.2b Consumption pass (the "specified model")** — `detect_consumption_anomalies()`, `backend/wt/anomaly.py:296-389`.
- Input: one row per reconciled service-day per unlimited stream, from view `v_unli_daily_consumption` (`backend/migrations/0010_unli_reconciliation.sql:104-138`), `WHERE d.service_date >= ? AND d.heads > 0 AND d.validation_state <> 'rejected'` (`anomaly.py:244-260`).
- **Feature vector — 7 named features** (`FEATURE_NAMES`, `anomaly.py:104-112`, built `280-291`): `actual_per_head`, `variance_ratio`, `baseline_deviation_ratio` (rate ÷ frozen `role='baseline'` λ), `peer_rate_ratio`, `leftover_ratio`, `heads` (normalized to peer mean), `consumption_scale_ratio`. All seven derive from post-Stage-3 `unli_reconciliation` fields (`actual_base`, `heads`, `opening/added/closing_base`) — never from the raw Stage-1 `theoretical_base` alone.
- **Confirmed risk:** the WHERE clause is `validation_state <> 'rejected'` (`anomaly.py:254`), which **includes rows still `pending_review`** (over-tolerance days awaiting a manager's sign-off, `unli.py:313-320`) — i.e. the model can train on unconfirmed measurements. This is **inconsistent** with `unli.recalibrate()`, which requires `validation_state = 'validated'` explicitly (`unli.py:479`) before a day is allowed to influence the rolling λ. The anomaly-detection input is measurably less strict than the recalibration input.
- Hyperparameters: same `n_estimators=200, contamination="auto", random_state=42` (`anomaly.py:335-336`), gated by `MIN_RECONCILED_DAYS = 12` (`anomaly.py:87`); output capped to `MAX_FLAG_FRACTION = 0.25` (`anomaly.py:102, 345`); severity cutoff `-0.15` duplicated again at `anomaly.py:359`.
- Output → `GET /console/anomalies` (`consumption_anomalies`) → `StockLoss.js`; technical detail via `GET /console/model-details` → `System.js`.
- No bare `except`/silent-swallow pattern found anywhere in `anomaly.py`.

### 3.3 FP-Growth (association rules / combos)

- Module: `backend/wt/associations.py`. Transaction builder `_transactions()` (`93-128`); mining core `_mine()` calling `mlxtend.frequent_patterns.fpgrowth`/`association_rules` (`161-178`); public entry `product_combinations()` (`345-418`).
- **Transaction definition:** one real completed order, keyed by `COALESCE(customer_order.source_order_id, row id)` — not raw `customer_order.id`, to correct a historical data-import artifact that split some orders into two rows (`associations.py:11-32, 110-128`). Void orders excluded. Per-order, not per-shift or per-day.
- **Thresholds (exact):** primary tier `MIN_SUPPORT=0.002`, `MIN_CONFIDENCE=0.05`, `MIN_LIFT=1.10`, `MIN_PAIR_COUNT=10` (`associations.py:55-58`); fallback tier (used only if the primary tier finds zero pairs) `0.001/0.03/1.10/5` (`associations.py:67-70`); absolute lift floor `lift > 1.0 and lift >= min_lift` always enforced (`associations.py:223`); minimum 30 transactions before mining runs at all (`MIN_TRANSACTIONS`, `associations.py:46, 370-371`).
- **Preprocessing:** `mlxtend.preprocessing.TransactionEncoder` one-hot encoding (`associations.py:250-252`); every support/confidence/lift metric is independently recomputed from raw basket counts rather than trusted from mlxtend's own output (`associations.py:131-143, 205-223`) — a deliberate anti-black-box design choice.
- **Retrain:** re-mined on every call; **not wrapped in any TTL/fingerprint cache**, unlike Prophet and Isolation Forest (confirmed — no `TTLCache` call site references the combinations endpoint).
- **Output/UI:** `GET /console/combinations` → `routes/reports/Reports.js:16, 60, 164-260` (combo cards); technical panel via `association_model_details()` → `GET /console/model-details` → `System.js:739-742`.
- **Risks:** the two-tier fallback (thresholds loosen only when the primary tier yields zero pairs) is disclosed to the API consumer via a `threshold_tier` field (`associations.py:403`) rather than hidden, but is worth flagging as a reproducibility consideration for a thesis defense. No time-based leakage risk applies (co-occurrence mining, not sequential forecasting). No bare `except` found in the file.

### 3.4 Cross-cutting: Phase-16 caching/invalidation

`backend/wt/cache.py`'s `TTLCache` (`34-72`) is the shared mechanism. Its design intent is **data-fingerprint-driven invalidation**, with the 300s TTL/200-entry cap only as a memory safety net (`cache.py:14-22, 30-31`). Concrete invalidation keys are listed per-consumer in §3.1 and §3.2 above. FP-Growth (`product_combinations`) is the one ML surface **not** wrapped in this cache.

### 3.5 The three-stage unlimited-wings deduction pipeline

Module `backend/wt/unli.py` (header comment restates the three stages, `unli.py:1-40`).

1. **Stage 1 — provisional deduction at order time.** `resolver.order_requirements()` (`backend/wt/resolver.py:177-259`) reads only `role='rolling'` λ coefficients at the current `effective_from` (`resolver.py:231-243`), and computes `qty = lambda_value * heads * unli_qty` (`resolver.py:242`). The frozen baseline (`role='baseline'`) is explicitly never used to size a deduction (`resolver.py:196-197`). This deduction is booked as an ordinary `sale_commit` ledger reason through the same reserve/commit engine as any other order line (see §5).
2. **Stage 2 — kitchen round/refill logging** (the task brief's "tray logging"; the actual implementation tracks *refills*, not trays). Tables `unli_refill`/`unli_refill_line` (`backend/migrations/0011_unli_session_refills.sql:141-192`). Endpoints: `POST /unli/sessions/{pk}/refills` (`api.py:3477-3487`), `POST /unli/refills/{pk}/serve` (`api.py:3497-3504`), void/correct (`api.py:3512-3536`). Refills deduct **sauce only, never wings again** — the wings for the whole session already left the ledger at commit, sized `heads × λ` — deducting wings again per refill would double-count (`0011_unli_session_refills.sql:36-42`).
3. **Stage 3 — end-of-shift reconciliation.** `POST /unli/reconciliation` (`api.py:3221-3235`) → `unli.record_reconciliation()` (`unli.py:249-396`). Formula: `actual = opening_base + added_base - closing_base` (`unli.py:283-290`); `variance = actual - theoretical` (`unli.py:310`); `correction = theoretical - actual` — **never `actual` alone**, specifically to avoid double-deduction (`unli.py:16-18, 322-324`). Over-tolerance days (against `unli_variance_tolerance`, default 0.10, `unli.py:53, 311-320`) are flagged `pending_review` and require human sign-off via `POST /unli/reconciliation/{pk}/review` (`api.py:3254-3262`, `unli.py:399-423`). Uniqueness enforced at the DB level: `UNIQUE (service_date, unli_item_id)` (`backend/migrations/0001_ingredient_master.sql:197`).

**λ₀ (baseline) vs rolling-λ — confirmed as two genuinely distinct, enforced code paths:**
- Schema: `unli_coefficient.role CHECK IN ('baseline','rolling')` (`backend/migrations/0001_ingredient_master.sql:174`).
- Stage 1 deduction reads only `role='rolling'` (`resolver.py:236`).
- The anomaly `baseline_deviation_ratio` feature reads only `role='baseline'` (`anomaly.py:249-253`).
- `recalibrate()` writes only new `role='rolling'` rows, never touching baseline (`unli.py:447-536`, insert at `524-532`).
- **Migration `0020_freeze_unli_baseline_from_verification.sql`** exists specifically to stop the baseline from ever being mutated through the ordinary verification/correction flow — the `v_needs_verification` view now excludes `role='baseline'` rows entirely (`0020_...sql:61-66`), with the migration's own comment documenting the exact prior failure mode this fixes: choosing "change" on a baseline coefficient in the ordinary queue would previously have silently moved the anomaly-detection reference point (`0020_...sql:12-24`).
- Historical reproducibility: `provisional_for_date()` recomputes what a past order's deduction *actually was* by walking `role='rolling'` coefficients as of that order's timestamp (`effective_from <= order.created_at`, `unli.py:133-140`), explicitly so a later recalibration cannot retroactively change history (`unli.py:32-34`) — a correct, leakage-resistant design, noted here as a positive finding.

---

## 4. Console Modules (Frontend + API + RBAC)

### 4.1 Auth / RBAC

- **JWT issuance:** `issue_token()` — `backend/wt/auth.py:237-255`. HS256, hand-rolled (no `python-jose`), `WT_SECRET`-keyed. Claims: `sub`, `role`, `iat`, `exp` (12h TTL, `auth.py:30`), `pwd` (password-changed-at, used to invalidate tokens issued before a reset).
- **Verification:** `decode_token()` (`auth.py:258-292`); request-time identity dependency `principal()` (`api.py:205-232`) re-checks `is_active`/`password_changed_at` against the DB on every request, so a password change invalidates outstanding tokens immediately.
- **Roles (exact, from code):** `ROLES = ("admin", "manager", "cashier", "kitchen", "owner")` — `auth.py:52`. Five roles exist in the schema, not four as commonly described; `manager` is present but **operationally retired**: deactivated by `backend/migrations/0034_deactivate_manager_account.sql:20`, refused for new accounts (`api.py:463-465`), and excluded from the console's login allow-list (`ALLOWED_ROLES = ['admin','owner']`, `client.js:186`). In the currently deployed system this leaves `admin`/`owner` on the console, `cashier`/`kitchen` on POS/KDS.
- **Capability table:** `auth.CAPABILITIES` (`auth.py:88-180`), enforced via a single `needs(capability)` FastAPI dependency factory (`api.py:235-248`). Every mutating (`POST`/`PUT`/`PATCH`/`DELETE`) route in `api.py` was cross-checked against this dependency by the audit; **no unguarded mutating endpoint was found.** The only routes without a capability check are `POST /auth/login` (unauthenticated by necessity) and `POST /auth/change_password` (requires a valid token via `Depends(principal)`, but no specific capability — correct, since it is self-service).

### 4.2 Console modules

| Module | Fetches | Backend handler | Actions / role |
|---|---|---|---|
| `AppShell.js` | `verification.getQueue()` for sidebar badge | `GET /console/queues/verification` (`api.py:1927`, `queue.read`+`needs_page('verify')`) | Hash routing, re-validates route visibility on every nav (`AppShell.js:64-69`) |
| `Sidebar.js` | — | — | Renders `navGroupsForRole()`; opens `AccountModal` |
| `AccountModal.js` | `POST /auth/change_password` | `api.py:342` | Self-service password change, any authenticated role |
| `ExpiryCard.js` | (data passed in by caller) | `GET /console/expiry` (`api.py:2080`, `inventory.read`) | Opens ingredient drawer on click |
| `IngredientDrawer.js` | `GET /console/ingredients/{pk}`, `.../forecasts/ingredients/{pk}`, `.../anomalies`, `.../reconciliation/ingredients` | `api.py:1647, 2846, 2952, 2118` | Receive/count/waste/use/edit — all `admin`-only via `inventory.write`, frontend `canWrite()` gate is explicitly documented as cosmetic (`IngredientDrawer.js:146-151`) |
| `IngredientRow.js` | (pre-joined data) | `GET /console/ingredients` + `.../forecasts/depletion` | Pure presentation |
| `IngredientStory.js` | (forecast payload) | Same forecast-detail endpoint | Builds `TrajectoryChart`; shows `demandSource` (prophet/EWMA) |
| `VerificationItem.js` | Confirm/Change/Keep | `POST /console/queues/verification/resolve` (`api.py:1957`, `queue.resolve`, admin-only) | Frontend-hidden for non-writers, backend-enforced regardless |
| `AlertRow.js` | (via `Today.js`) | `GET /dashboard/summary` (`api.py:2360`, `inventory.read`) | Pure presentation |
| POS (`/pos/orders`) | — | `api.py:968`, `order.create`, cashier-only | Order creation + reservation |
| KDS (`/kds/orders/.../start`) | — | `api.py:1151`, `order.status`, kitchen-only | Commits deduction |

**No frontend-gating-without-backend-enforcement gap was found** — every write action traced above has an independent server-side capability check.

### 4.3 Stock deduction atomicity

- **Transaction mechanism:** `write_tx()` (`backend/wt/db.py:148-164`) opens `BEGIN IMMEDIATE` (`db.py:156`), taking SQLite's write lock up front; nesting is explicitly rejected. This serializes all writers on the single SQLite file — no multi-writer race window at the engine level.
- **Reserve (Stage: order creation),** `reservation.py:265-342`: per-ingredient guarded UPDATE — `UPDATE ingredient SET reserved_base = reserved_base + :qty ... WHERE (on_hand_base - reserved_base) >= :qty - :eps` (`reservation.py:238-247`) — condition and mutation in one atomic statement, not read-then-write.
- **Documented gap:** order-row creation (`api.py:993`, inside `write_tx`) and `reserve_order()` (`api.py:1112`, a *separate* `write_tx`) are two distinct transactions, not one atomic unit. If `reserve_order` throws, the handler compensates by deleting the order row in a third `write_tx` (`api.py:1136-1137`) — a documented saga/compensating-transaction pattern, not true ACID atomicity across the two steps. A process crash between the two `write_tx` calls would leave an orphaned, unreserved order row; **no cleanup/reconciliation job for such orphans was found** in this audit.
- **Commit (kitchen "start"),** `commit_order()`, `reservation.py:348-422`: guarded UPDATE `on_hand_base = MAX(0, on_hand_base - :qty), reserved_base = MAX(0, reserved_base - :qty) WHERE reserved_base >= :qty - :eps` (`reservation.py:378-387`), then FEFO batch depletion (`deplete_fefo()`, `reservation.py:121-176`, also guarded per-batch) and a `stock_ledger` insert, all in the same transaction.
- **Concurrency conclusion:** given SQLite's `BEGIN IMMEDIATE` exclusive-writer serialization plus guarded UPDATEs, stock cannot go negative and cannot be overcommitted under this design. Partial failure within a single reserve/commit call rolls back cleanly. The one open gap is the cross-transaction order-creation/reservation stitching described above.

### 4.4 Charts

- **TrajectoryChart** (`components/charts/TrajectoryChart.js:111-216`): "Measured stock" (history) vs "Projected stock" (forecast) with an uncertainty band, sourced from `GET /console/forecasts/ingredients/{pk}` (`api.py:2846`, handled in `depletion.py`). Confirmed Prophet/EWMA-sourced: `depletion.py:445-452` sets `demand_source = "prophet"` only when a cached Prophet fit exists, else `"ewma"`; band drawn only when ≥7 trading days exist (`MIN_TRADING_DAYS = 7`, `depletion.py:132`) — no fabricated data.
- **BarChart** / **LineChart** (`components/charts/BarChart.js`, `LineChart.js`): generic, data supplied by callers; `LineChart.js:22-30` explicitly refuses to draw an invented flat/zero forecast line when no real data exists ("A forecast line here would be invented, not measured").
- **Anomaly data:** `GET /console/anomalies` (`api.py:2952`) confirmed to use scikit-learn `IsolationForest` (`anomaly.py:51, 155, 335`).

---

## 5. End-to-End Order Trace

Tracing one Unlimited Wings order from POS tap to dashboard reflection, hop by hop:

1. **Order created (POS tap):** `POST /pos/orders` (`backend/wt/api.py:968-971`, capability `order.create`, cashier-only). Inside one `write_tx` (`api.py:993`): a `customer_order` row is inserted (`api.py:997-1001`), each line validated against `v_menu_item_availability` (`api.py:1040-1048`), and an unlimited line is guarded to `qty=1` with `head_count ≥ 1` (`api.py:1023-1039`) — an unlimited item is sized by head count, never by cart quantity, to prevent a documented double-counting failure mode (comment, `api.py:1015-1022`). A `unli_session` row opens in the same transaction (`api.py:1093-1100`).
2. **Recipe lookup / deduction sizing:** `reserve_order()` → `resolver.order_requirements()` (`backend/wt/resolver.py:177-259`), called just outside the creation transaction (`api.py:1112`). For ordinary lines, it sums `v_recipe_leaf.qty_base * ol.qty` (`resolver.py:200-211`) — this view already has yield factors and sub-recipe expansion baked in (§2.5). For the unlimited line, it reads the current **rolling** λ coefficient and computes `lambda_value * heads * unli_qty` (`resolver.py:231-243`) — this is Stage 1 of the three-stage pipeline (§3.5). The cashier's chosen sauce flavour is then applied as a signed correction via `flavour_substitutions()` (`resolver.py:250-257`).
3. **Ingredient reservation (soft hold):** `reservation.reserve_order()` (`reservation.py:265-342`) takes a guarded per-ingredient hold (`_try_hold()`, `reservation.py:236-248`) in its own `write_tx`. If this fails, the order row is deleted in a compensating transaction (`api.py:1135-1140`) — see §4.3 for the atomicity caveat here.
4. **Kitchen commits the deduction:** `POST /kds/orders/{order_pk}/start` (`api.py:1151-1157`, capability `order.status`, kitchen-only) calls `commit_order()` (`reservation.py:348-422`), which atomically decrements `ingredient.on_hand_base`/releases the hold (`reservation.py:378-387`), draws down FEFO batches (`deplete_fefo()`, `reservation.py:121-176`), and appends a `stock_ledger` row with `reason='sale_commit'` (`reservation.py:398-406`). This is the moment physical inventory actually changes.
5. **Inventory state updated:** `ingredient.on_hand_base` and `fefo_batch.qty_base` now reflect the sale; the append-only `stock_ledger` (triggers forbid update/delete, `0001_ingredient_master.sql:267-270`) carries the audit trail.
6. **Later — kitchen refill (Stage 2):** if the table's unlimited order needs a sauce refill, `POST /unli/sessions/{pk}/refills` / `.../serve` (`api.py:3477-3504`) deducts sauce only, never wings again (`0011_unli_session_refills.sql:36-42`).
7. **End-of-shift reconciliation (Stage 3):** `POST /unli/reconciliation` (`api.py:3221-3235`) computes `actual = opening + added - closing` and books `correction = theoretical - actual` against the ledger (`unli.py:283-324`) — this is the point where the provisional Stage-1 estimate is trued up.
8. **ML inputs updated:**
   - The new `customer_order`/`order_line` rows immediately bump `MAX(customer_order.id)`, which is the cache-invalidation key for Prophet's per-item/store forecasts (`api.py:2736-2739, 2760-2764`) and part of the ingredient-forward-chain fingerprint (`api.py:2779-2819`) — the next dashboard/forecast request recomputes rather than serving a stale cache entry.
   - The new `stock_ledger` row bumps `MAX(stock_ledger.id)`, invalidating both the stock-loss and (once reconciled) consumption Isolation Forest caches (`api.py:2983-2993`).
   - Once Stage 3 reconciliation commits, the new `unli_reconciliation` row is picked up by the consumption anomaly pass's cache key (`MAX(unli_reconciliation.id)`, `api.py:2984, 2990-2993`) and by `v_unli_daily_consumption` for the next anomaly run — but note the finding in §3.2b: the anomaly model will consult it even while still `pending_review`, before a manager has confirmed it.
   - FP-Growth re-mines transactions fresh on every request (no cache), so the new order is visible to `GET /console/combinations` immediately once it is non-void.
9. **Dashboard/forecast/alerts reflect it:** `GET /dashboard/summary` (`api.py:2360`, `inventory.read`) and `GET /console/forecasts/ingredients/{pk}` (`api.py:2846`) now serve figures computed from the updated ledger/order data (subject to the cache-fingerprint recompute described above); the `TrajectoryChart` on the ingredient drawer (`IngredientStory.js:188-214`) will show the new on-hand level the next time it's opened.

---

## 6. Issues Found

| Severity | Location | What's wrong | Why it matters |
|---|---|---|---|
| **High** | `backend/wt/anomaly.py:254` vs `backend/wt/unli.py:479` | The consumption Isolation Forest trains on `unli_reconciliation` rows in `pending_review` state (not yet manager-validated), while `recalibrate()` requires `validation_state='validated'` for the same kind of row before it's allowed to influence rolling λ. | An unconfirmed, possibly-wrong reconciliation figure can shape anomaly scoring before a human has signed off on it — inconsistent trust levels for the same data across two ML consumers. Worth a direct question at defense: "why does the anomaly model trust unvalidated data that the recalibration model doesn't?" |
| **Medium** | `backend/migrations/0001_ingredient_master.sql:31-82, 103-119, 144-155` | No `UNIQUE` constraint on `ingredient.name`, `menu_item.name`, or `recipe.name`. | A duplicate name in a re-imported CSV silently orphans the earlier row (its `ingredient`/`fefo_batch` data becomes unreachable from any name-keyed lookup) with no error raised — a real data-integrity gap in the seed pipeline. |
| **Medium** | `backend/wt/api.py:993, 1112, 1136` | Order-row creation and stock reservation are two separate `write_tx` transactions stitched by a catch-and-compensate delete, not one atomic unit. | A process crash between the two transactions leaves an orphaned order row with no reservation and no compensating cleanup — no reconciliation job for this state was found in the audit. Low likelihood, but worth naming as a known limitation rather than an oversight, since the code itself documents the tradeoff. |
| **Medium** | `backend/wt/forecasting.py:404-452, 65-72` | Prophet's fit path wraps everything in a bare `except Exception: return None` with no logging. | A genuine fit failure (bad data, library crash) is indistinguishable from "not enough history yet" or "Prophet unavailable" — makes failures invisible to an operator and hard to diagnose after the fact. |
| **Medium** | `backend/wt/depletion.py:161-162, 515-516` | Prophet model state is in-process memory only; lost on restart, no auto-refresh on startup, up to 48h staleness window with no distinct "stale" flag in the API response. | After any server restart, every ingredient silently reverts to EWMA-based forecasting until an admin manually triggers a refresh — a plausible real-world failure mode for an unattended LAN deployment. |
| **Low** | `backend/static/assets/console/js/lib/api/intelligence.js:22-33` | `getForecast()`/`listDemandSummaries()` (per-item Prophet forecast API) have no call sites anywhere under `routes/`. | The per-item Prophet forecast appears to be dead/unwired UI-side — only the replenishment table (ingredient-forward chain) is actually shown. Worth confirming with the team whether this is intentional or an incomplete feature. |
| **Low** | `backend/wt/anomaly.py:181, 359` | The `-0.15` severity cutoff is a hardcoded magic number duplicated in two separate functions rather than a single named constant. | Changing severity sensitivity requires remembering to edit both places; a maintenance/consistency risk, not a functional bug today. |
| **Low** | `backend/wt/raw_import.py:82-85` | A blank `Product Count` cell in the raw inventory CSV is indistinguishable from a section-header row. | A genuinely blank count on a real product silently becomes a phantom section header rather than raising a validation error — could hide a data-entry mistake in the source CSV. |
| **Informational** | `data/Wings_Theory_Master_Inventory.xlsx` | Never read by any running code; its values were manually transcribed once into `backend/wt/seed.py`/`gapfill.py`. | Not a bug, but important for accurately describing the system's architecture: the spreadsheet is not a live data source, despite being the nominal "source of truth" document. Editing it has no effect until someone manually re-transcribes the change. |
| **Informational** | `backend/wt/seed.py:106-112` vs `backend/wt/gapfill.py:102-114` | No "mango flesh yield ~65%" `yield_factor` value exists anywhere in the code; mango uses a `PIECE_WEIGHTS` (180g/piece) mechanism instead, and the 0.65 figure belongs to mushroom entries. | If this figure appears in the thesis narrative, it should be corrected or its source clarified — it does not match what the code implements. |

---

## 7. Open Questions Not Resolved From the Code

- **Why the Master Inventory workbook is kept in `data/` at all if nothing reads it** — is it meant purely as the owner's editable reference, with a documented (but not automated) process for a developer to re-transcribe changes into `seed.py`/`gapfill.py`? NOT FOUND: no `docs/` file describing an update procedure for this workbook was located.
- **Whether the orphaned-order-row gap (§4.3, §6) has ever occurred in production** — the code documents the theoretical failure mode and its compensating-delete mitigation, but no monitoring/alerting or cleanup job for a crash occurring inside that exact window was found. Whether this is accepted risk or an oversight would need to be answered by the development team, not the code.
- **Whether `docs/PROPHET_ACCURACY_OPTIMIZATION_AUDIT.md`'s numeric justification for `growth="flat"`** matches what's re-derivable from `evaluate_demand_model()` today — the ML-audit sub-agent did not exhaustively re-run or re-verify the backtest numbers cited in that document against current code, only confirmed the backtest methodology itself is leakage-free.
- **Whether the `manager` role's continued presence in `auth.CAPABILITIES`** (despite being deactivated at the account level) is planned to be fully removed, or is intentionally kept as a "capability" scaffold for a future reactivation — this is a product decision, not something inferable from the code.
