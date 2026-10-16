# Wings Theory ML Evaluation — Thesis Chapter 4 Results

**Run ID:** `20261013_125802`
**Generated:** 2026-10-13 (UTC)
**Database evaluated:** `backend/wings_theory.db` (production, opened read-only — this run wrote nothing to it)
**Code version:** working tree at commit `045597f` + two additive changes made for this evaluation (see "Changes made to enable this evaluation" below)
**Raw data:** [`raw_results.json`](raw_results.json) · [`table_a_forecasting.csv`](table_a_forecasting.csv) · [`table_b_anomaly.csv`](table_b_anomaly.csv) · [`table_c_fpgrowth.csv`](table_c_fpgrowth.csv) · [`table_d_summary.csv`](table_d_summary.csv)
**Script:** `backend/scripts/run_thesis_ml_evaluation.py`

Every number below was produced by actually executing this codebase's own, already-committed evaluation functions against the live production database — nothing was estimated, assumed, or hand-adjusted. Where a metric could not be validly computed, it is marked **NOT MEASURABLE** with the precise reason and what would be needed to compute it, rather than filled in with a guess.

---

## 0. Changes made to enable this evaluation

Two small, additive changes were made to the codebase, both verified against the full regression suite (see §8):

1. **Added Forecast Bias to the forecasting backtest.** `wt/forecasting.py`'s existing rolling-backtest scorer (`_score()`) computed MAE/RMSE/WAPE/sMAPE/MAPE/MASE per fold but never signed bias (mean(predicted − actual)), which this evaluation's scope requires. Added `_bias()` and threaded `bias`/`mean_bias` through the per-fold, per-item, and fleet-summary aggregation. This is a pure additive field — it does not change which method gets selected per item (`_rank_key` still reads only `wape`/`mae`), and does not touch any production-serving code path (`depletion.py` never calls this backtest function).
2. **Wrote `backend/scripts/run_thesis_ml_evaluation.py`**, a new orchestration script that calls the existing evaluation functions below, nothing else. It opens the database `mode=ro` (SQLite read-only URI) — it is structurally incapable of writing to it.

No trained model artifact, threshold, production config, stock quantity, or inventory record was changed. No model was retrained. No existing data was deleted, relabeled, or replaced.

---

## 1. Model inventory and classification

| Component | Type | Trained ML model? |
|---|---|---|
| Prophet demand forecasting (item-level) | Trained ML model (Facebook/Meta Prophet, Bayesian structural time series) | Yes |
| Naive (previous-period) forecast | Heuristic baseline | No |
| Trailing moving-average forecast | Statistical baseline | No |
| Day-of-week seasonal forecast | Statistical baseline | No |
| Hybrid forecast selection (per item, lowest mean WAPE wins) | Rule-based model selection over the above four | No (selector, not a model) |
| `stock_loss_isoforest` (Pass A) | Trained ML model (scikit-learn IsolationForest) + a deterministic `large_fraction` rule layer | Yes (forest) / No (rule layer) |
| `unli_consumption_isoforest` (Pass B) | Trained ML model (IsolationForest) + deterministic variance/MAD rule layer | Yes / No |
| `flavor_consumption_isoforest` | Trained ML model (IsolationForest) + deterministic rule layer | Yes / No |
| Replenishment critical/out early-warning flag | Deterministic heuristic rule over forecasted demand vs. on-hand stock (not a trained model) | No |
| FP-Growth association rule mining (`fpgrowth_combos`) | Trained ML model (frequent-itemset mining, mlxtend) | Yes |
| Combo pricing | Deterministic arithmetic (target food-cost % over ingredient cost) | No |

The replenishment early-warning flag is included in Table B below for completeness (it has real ledger-grounded ground truth and a measurable confusion matrix), but it is explicitly labeled a heuristic, **not** a trained model — do not report its numbers as "an ML model's accuracy."

---

## 2. Dataset provenance

Read directly from the live database at evaluation time:

| Table | Total rows | Real | Synthetic/other | Note |
|---|---|---|---|---|
| `customer_order` | 33,596 | 33,588 (`source='historical'`) | 8 (`source IS NULL`) | The 8 non-historical rows are residual manual test orders from prior dev work, not evaluation data. |
| `stock_ledger` | 689,416 | 659,948 (`sale_commit` + `void_credit`) | 29,468 (`wastage` 18,840 + `purchase_receipt` 9,083 + `count_adjustment` 1,541 + `spoilage` 4) | The wastage/purchase_receipt/count_adjustment/spoilage rows are the historical-import seeder's RNG-fabricated history (by design — see `demo_seed.py`'s own docstrings), not real restaurant operations. |
| `anomaly_review` | 1,216 | — | 1,216 reviewed, but **all** of them review synthetic-sourced ledger rows | Every human review on file reviewed a row the system itself now excludes from the model's real candidate population — see §4.1. |
| `unli_reconciliation` | 0 | 0 | — | Zero rows. Pass B's primary data source is completely empty of real data. |
| `unli_consumption_validation_sample` | 0 | 0 | — | Zero labeled samples exist for Pass B's scientific validation. |
| `unli_flavor_validation_sample` | 0 | 0 | — | Zero labeled samples exist for the flavor-level pass. |
| `ingredient.lead_time_days` | 122 | 0 measured | 122 hard-coded (2 days × 37 wet-food, 4 days × 85 others) | Confirms the 2026-10-05 forensic finding: no supplier lead-time has ever been measured from real data. Any replenishment-timing number downstream of this inherits an unvalidated assumption. |

**No real-data evaluation below is contaminated with synthetic data.** Every function used already structurally excludes synthetic/seeded rows from training and from ground truth (see `_REAL_USER_CLAUSE`, `_REAL_RECORDER_CLAUSE`, `SYNTHETIC_SEED_USER_IDS` in `wt/anomaly.py`) — this was verified by inspecting the excluding SQL itself, not assumed from a comment.

---

## TABLE A — Forecasting Performance

**Method:** chronological expanding-window rolling backtest (`forecasting.evaluate_demand_model`), **never** a random split. 16 of 32 POS-visible menu items had enough real sales history (≥21-day initial training window, ≥28 days total) to backtest; the other 16 are listed as `items_excluded_for_capacity` (not silently dropped — see raw JSON). Each item: up to 4 most-recent folds, 7-day forecast horizon, every fold scored for all four methods on data none of them saw while fitting. Evaluated date range: 2025-09-22 → 2026-09-21 (365 real days).

| Model | Dataset | Items Scored | Mean MAE | Mean WAPE (%) | Mean Bias | Samples (items) |
|---|---|---|---|---|---|---|
| Naive (previous-period) | REAL, item-level | 16 | 2.4978 | 75.17 | +0.3772 | 16 |
| Trailing moving-average | REAL, item-level | 16 | 1.7573 | 54.97 | −0.0469 | 16 |
| Day-of-week seasonal | REAL, item-level | 16 | 1.6424 | **50.16** | −0.5073 | 16 |
| **Prophet** | REAL, item-level | 16 | 1.5774 | 50.91 | −0.1451 | 16 |

**Prophet vs. best baseline (day-of-week seasonal):** absolute WAPE difference **−0.75 pts** (Prophet is *worse*), i.e. **−1.5%**. `prophet_actually_better: false`. Per-item method selection (lowest mean WAPE wins, no method assumed superior): day-of-week-seasonal selected for 10/16 items, Prophet for 3/16, trailing moving-average for 2/16, naive for 1/16.

Interpretation of why WAPE looks large everywhere (75–105% for several items) while MASE is <1 for most: per-item daily volumes are small (most items sell 1–3 units/day), so a single-unit miss is a large percentage error. MASE (ratio to a naive lag-1 in-sample baseline) is the more meaningful number for these series and sits below 1.0 for the great majority of item/method combinations — i.e. every method, including the baselines, beats "assume today repeats yesterday" even though WAPE looks dramatic. **No confusion matrix applies here** — this is a continuous-error forecasting task, not a classification task; WAPE/MAE are never reported here as "accuracy."

Store-level / aggregated total-demand forecasting is **NOT MEASURABLE as a distinct series**: the codebase's forecasting evaluation operates per menu item plus the unlimited-wings head count (which is itself scored as one of the 16 items, "Unlimited Wings — 14 Flavors," where Prophet *did* win: WAPE 11.88% vs. 14.69% for the best baseline). No separate aggregate-across-all-items series exists in the code to evaluate; one would need to be defined (e.g. total daily covers or total daily revenue) and is not currently part of any evaluation function.

---

## TABLE B — Anomaly Detection Performance

| Model | Dataset | TP | TN | FP | FN | Precision | Recall | F1 | Accuracy | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| `stock_loss_isoforest` (Pass A) | REAL, human-reviewed | — | — | — | — | — | — | — | — | **NOT MEASURABLE** |
| `unli_consumption_isoforest` (Pass B) | REAL, human-reviewed | — | — | — | — | — | — | — | — | **NOT MEASURABLE** |
| `flavor_consumption_isoforest` | REAL, human-reviewed | — | — | — | — | — | — | — | — | **NOT MEASURABLE** |
| Replenishment early-warning (ewma demand) — *heuristic, not a trained model* | REAL, ledger backtest | 0 | 1,431 | 513 | 0 | 0.0000 | **n/a** (0/0) | n/a | 0.7361 | measured |
| Replenishment early-warning (prophet demand) — *heuristic, not a trained model* | REAL, ledger backtest | 0 | 1,431 | 513 | 0 | 0.0000 | **n/a** (0/0) | n/a | 0.7361 | measured |

### Why each Isolation Forest pass is NOT MEASURABLE

- **Pass A (`stock_loss_isoforest`):** `anomaly.validation_metrics()` returns `available: false`. Confusion matrix is `{tp:0, fp:0, fn:0, tn:0}` — **0 of the required 20** confirmed labels. All 1,216 rows in `anomaly_review` reviewed ledger rows the system itself now flags as synthetic-sourced (`SYNTHETIC_SEED_USER_IDS`), so none of those 1,216 reviews count as ground truth for the model's real candidate population. **What's needed:** ≥20 human reviews (`confirmed_anomaly`/`false_positive`) of *real* (non-seeded) `wastage`/`purchase_receipt`/`count_adjustment`/`spoilage` ledger rows, with at least some on both the flagged and unflagged side.
- **Pass B (`unli_consumption_isoforest`):** `scientific_consumption_validation_metrics()` returns `available: false`, `0 labeled sample(s)`. `unli_reconciliation` (the real-data source) has **zero rows** — the owner's daily unlimited-wings reconciliation workflow has never been used. Readiness state: `SIMULATION_ONLY`. **What's needed:** the reconciliation workflow run daily for ≥14 days of real service, then ≥20 scientifically-sampled human reviews.
- **Flavor-level pass:** `scientific_flavor_validation_metrics()` returns `available: false`, `0 labeled sample(s)`, readiness `SIMULATION_ONLY`. Same gap as Pass B, one level more granular (per-flavor reconciliation, never used with real data).
- **Model registry confirms all three are deployed in `SIMULATED` mode**, trained on 1,846 / 343 / 1,372 synthetic records respectively, 0 real records each (see §6). This is the system working as designed — it correctly refuses to call itself `REAL`-mode-ready — not a concealed gap.

### Replenishment early-warning flag (included for transparency, not a trained model)

180-day lookback, 18 evaluation points (every 10 days), 1,944 total ingredient-evaluations, 7-day check window, computed entirely from real `stock_ledger`/`purchase_order` history (`depletion.replenishment_backtest`). **In this specific 180-day window there were zero real stockouts at all** (`missed: 0`, `caught: 0`) — so Recall is undefined (0/0, correctly reported as `null`, not fabricated as 0% or 100%). Precision is a real, measured **0.0%**: every one of the 513 critical/out flags raised in this window turned out to be a false alarm (416 resolved by an actual restock inside the window — consistent with the warning being *acted on*, not necessarily wrong; 97 "genuine" false alarms with no restock and no real stockout). Switching the demand model input from `ewma` to `prophet` produced **identical** confusion-matrix results (513/0/0/1,431) — the critical/out threshold logic is not materially sensitive to which demand model backs it on this data. This matches but updates the 2026-09-28 finding (4.2% precision / 25% recall on a different window) — the current 180-day window simply contains no real stockout events to measure recall against, which is itself informative about current stock-management practice, not a code defect.

---

## TABLE C — FP-Growth Performance

| Method | Transactions | Unique Products | Avg Items/Txn | Support | Confidence | Lift | Frequent Itemsets | Rules | Final Recommendations | Holdout Result |
|---|---|---|---|---|---|---|---|---|---|---|
| FP-Growth (mlxtend) | 21,058 | 32 | 1.55 | 0.00059 | 0.05 | 1.10 | 364 | 7 | **1** | 7 candidates → 1 validated, 6 rejected |

**Chronological 80/20 holdout** (16,846 train / 4,212 holdout transactions, real orders only, train strictly precedes holdout in time): of 7 in-sample candidate pairs that cleared the primary mining thresholds, **1 held up** on the unseen holdout slice:

- **Sprite + Creamy Sausage Pasta** — in-sample lift 1.539 (pair count 22) → holdout lift **1.759** (holdout pair count 5). `status: holdout_validated`.
- The other 6 were rejected for `insufficient_holdout_occurrences` (seen together 0–2× in the holdout window, below the required 5×) — a real finding (the pattern didn't repeat), not a code failure.

**Temporal stability (3 nested expanding-window folds, independent of the 80/20 check):** of 26 candidate pairs ever identified across any fold's training window, **1 validated_stable**, 2 `unstable_failed`, 23 `insufficient_evidence` (too few holdout occurrences within that fold to judge either way). The 1 stable pair is the same Sprite/Creamy-Sausage-Pasta combo.

**Precision@K:** not computed — no labeled "would the customer actually have bought this if recommended" prediction task exists in the system (combos are offered, not yet tracked for accept/decline at the item-recommendation level). This is explicitly **NOT MEASURABLE** without instrumenting combo-offer acceptance as ground truth.

**Recommendation coverage:** 1 of 32 products (Sprite, paired with Creamy Sausage Pasta) has a currently-live, holdout-validated recommendation — i.e. ~3% direct SKU coverage. No classification Accuracy/F1 is assigned to FP-Growth anywhere in this report, consistent with the instructions — lift/support/confidence/holdout-survival are the only metrics reported.

---

## TABLE D — Overall Evaluation Summary

| Model | Primary Metric | Actual Result | Dataset Provenance | Limitations |
|---|---|---|---|---|
| Prophet demand forecasting | Fleet-mean WAPE vs. best baseline | Prophet 50.91% vs. day-of-week-seasonal 50.16% → **Prophet is 0.75 pts worse (−1.5%)** | REAL (365-day chronological rolling backtest, 16 items, 4 folds each) | Low per-item daily volume inflates WAPE for all methods equally; Prophet wins on only 3/16 items (mostly the highest-volume one, Unlimited Wings). Not beneficial over the simpler seasonal baseline on this data as it stands today. |
| `stock_loss_isoforest` | Precision/Recall | **NOT MEASURABLE** (0/20 real labels) | All 1,216 existing reviews are of synthetic-sourced rows | Needs ≥20 real-data human reviews before any claim can be made. |
| `unli_consumption_isoforest` | Precision/Recall | **NOT MEASURABLE** (0 reconciled real days) | `unli_reconciliation` has 0 rows — fully dormant for real data | Needs the owner's reconciliation workflow actually used, ≥14 real days, then ≥20 reviewed samples. |
| `flavor_consumption_isoforest` | Precision/Recall | **NOT MEASURABLE** (0 real observations) | Same gap, one level more granular | Same as above plus ≥8-flavor coverage requirement. |
| Replenishment early-warning (heuristic, not ML) | Precision / Recall | Precision **0.0%** (513 FP / 0 TP); Recall **undefined** (0 real stockouts in window) | REAL (180-day ledger backtest) | Zero real stockouts occurred in this window at all — the flag has never once been confirmed right in the period measured; it also has never been proven wrong by a missed stockout, because none occurred. |
| FP-Growth association mining | Holdout-validated combo count | **1** (Sprite + Creamy Sausage Pasta, holdout lift 1.759) | REAL (chronological 80/20 holdout + 3-fold temporal stability, both independently confirming the same pair) | 1/32 products have a validated pairing; small average basket size (1.55 items/order) is the binding constraint on finding more, not the thresholds (confirmed via `threshold_sensitivity`). |

---

## 3. Thesis-ready interpretation, per model

### Prophet demand forecasting
1. **What it does:** fits a Bayesian additive time-series model (trend + weekly seasonality) per menu item to forecast daily unit demand 7 days ahead.
2. **How measured:** chronological expanding-window backtest — train on everything before a cutoff, forecast the next 7 real days, score against what actually sold, roll the cutoff forward, repeat. Never a random split (would leak future information into training).
3. **Result:** on *this* data, Prophet's fleet-mean WAPE (50.91%) is measurably *worse* than the simplest alternative, a day-of-week seasonal average (50.16%) — a −1.5% regression, reported honestly by the code's own `prophet_vs_baseline` field rather than rounded up.
4. **Real or simulated:** 100% real — every fold trains and tests on actual historical `customer_order` rows.
5. **Baseline comparison:** loses to day-of-week-seasonal fleet-wide; wins on 3/16 individual items, most notably the highest-volume series (Unlimited Wings: 11.88% vs. 14.69%).
6. **Limitations:** menu items other than Unlimited Wings sell too few daily units for Prophet's extra model complexity to pay off; growth-mode selection (flat vs. linear trend) is itself tuned per-item from a leakage-free earlier slice, so this is not an undertuned model — it's a genuine finding that simpler methods currently win on most items.
7. **Deployment readiness:** already deployed (model registry: `demand_prophet`, mode `REAL`, trained on 32 real-order-based records) and already correctly falls back to baseline methods per-item via the existing selection logic — the system is not forcing Prophet where it underperforms.

### stock_loss_isoforest / unli_consumption_isoforest / flavor_consumption_isoforest
1. **What they do:** unsupervised scikit-learn IsolationForest models flag statistically unusual stock-ledger rows (Pass A), unlimited-wings daily consumption days (Pass B), or per-flavor consumption days (flavor pass), layered under a deterministic rule-based catch for extreme cases.
2. **How measured:** the codebase's own `scientific_*_validation_metrics()` functions compute TP/FP/FN/TN strictly from human-labeled samples, keyed to a specific (model_version, eligibility_version) pair, and refuse to report a number below a fixed minimum-evidence floor.
3. **Result:** all three report `available: false`. No precision/recall/F1/accuracy exists for any of them today.
4. **Real or simulated:** the models themselves are currently trained in `SIMULATED` mode on fabricated data (1,846 / 343 / 1,372 synthetic records). The *validation* gap is separate and worse: even the synthetic-trained models have zero real-world ground truth to be checked against.
5. **Baseline comparison:** not applicable — no baseline anomaly detector exists to compare against in this codebase.
6. **Limitations:** Pass A's only labels (1,216 reviews) are of synthetic rows and don't count; Pass B and the flavor pass have literally zero real observations in their source tables.
7. **Deployment readiness:** **not validated for deployment as scientific accuracy claims.** The system's own readiness ladder agrees — all three report `INSUFFICIENT_REAL_DATA`/`SIMULATION_ONLY`, never `READY_REAL_DATA`. They may still be operationally useful as a first-pass screening heuristic (that's a judgment call for the owner, not something this evaluation can certify), but no thesis claim of X% precision/recall can be made for any of them today.

### Replenishment critical/out early-warning flag
1. **What it does:** a deterministic rule (not a trained model) that flags an ingredient `critical`/`out` when its EWMA- or Prophet-based demand forecast projects depletion before the next expected delivery.
2. **How measured:** historical simulation at 18 past points over the last 180 days, replaying `ingredient_forecast()` with only data that existed at that point, then checking real ledger balances 7 days later.
3. **Result:** Precision 0.0% (0 TP / 513 FP); Recall undefined (0 real stockouts occurred in the window to catch or miss). Service level 100% — no ingredient actually ran out in this period.
4. **Real or simulated:** 100% real ledger data.
5. **Baseline comparison:** demand-model choice (ewma vs. prophet) made no difference to this classification outcome on this data.
6. **Limitations:** a 100% service level means there's no genuine stockout signal in this window to validate recall against at all; of the 513 flags, 416 coincide with an actual restock inside the window (consistent with, not proof of, the warning being useful) and 97 have no such explanation.
7. **Deployment readiness:** functioning as an early-warning signal with a high false-alarm rate by design (erring toward caution); not a scientific claim of accuracy, and the thesis should not describe this as "an ML model's" performance since it's a rule, not a fitted model.

### FP-Growth association rule mining
1. **What it does:** mines frequent 2-item co-purchase patterns from real completed orders (frequent-itemset mining via mlxtend's FP-Growth implementation).
2. **How measured:** two independent, real-data validation checks — a chronological 80/20 train/holdout split (never random) and a separate 3-fold expanding-window temporal-stability analysis — both checking whether an in-sample pattern survives on transactions the miner never trained on.
3. **Result:** of 7 in-sample candidates, exactly 1 survives both checks: Sprite + Creamy Sausage Pasta (holdout lift 1.759, confirmed stable across all 3 temporal folds).
4. **Real or simulated:** 100% real — all 21,058 transactions are real completed orders.
5. **Baseline comparison:** not applicable (no competing association-mining baseline exists in this codebase).
6. **Limitations:** average basket size is only 1.55 items/order — most customers simply don't buy two things together often enough for any pattern to clear the holdout bar, which is the binding constraint, confirmed by this evaluation's own threshold-sensitivity check, not an artifact of overly strict thresholds.
7. **Deployment readiness:** the one validated combo is genuinely deployment-ready by this evaluation's own double-check; the system already serves only holdout-validated combos live (`active_combo_recommendations`), so no unvalidated pattern is currently being presented to customers.

---

## 4. What could not be evaluated, and exactly what is needed

| Gap | What's missing | Exact requirement to close it |
|---|---|---|
| Pass A (stock-loss) Precision/Recall | Real-data human labels | ≥20 `confirmed_anomaly`/`false_positive` reviews of **real** (non-seeded) wastage/purchase_receipt/count_adjustment/spoilage ledger rows, via the existing review UI, with labels on both the flagged and unflagged side. |
| Pass B (unli consumption) Precision/Recall | Real reconciliation data at all | The owner's daily unlimited-wings reconciliation workflow must actually be used for ≥14 real service days before the model can even train on real data, then ≥20 scientifically-sampled reviews. |
| Flavor-pass Precision/Recall | Real per-flavor reconciliation data | Same as Pass B, plus coverage across ≥8 distinct flavors. |
| FP-Growth Precision@K | A labeled recommendation-acceptance task | Instrument whether a customer who was shown/offered a combo actually bought both items, as a tracked event — does not currently exist in the schema. |
| Store-level/aggregated forecasting as its own series | A defined aggregate series and an evaluation path for it | Decide what "store-level" means (total daily units? revenue? covers?) and extend `evaluate_demand_model` (or a new function) to backtest that series the same way per-item series are backtested today. |
| Replenishment early-warning Recall (this window) | Real stockout events | None occurred in the 180-day window evaluated; a longer lookback or waiting for a future genuine stockout would supply this. |

---

## 5. Regression testing

Full backend suite (`python -m tests.run_tests`, 1,119 tests) run after the two additive changes above: **1,118 passed, 1 failed, 1 skipped**. The one failure (`test_admin_direct_api_attempt_to_create_owner_is_rejected`, an RBAC authorization test) is **pre-existing and unrelated** — it concerns admin/owner account-creation permissions, not forecasting or anomaly code, and this evaluation's only code change was an additive `bias` field inside `wt/forecasting.py`'s private scoring helper. It is flagged here for disclosure, not fixed, since it is out of this evaluation's scope.

---

## 6. ML model registry snapshot (versions, dataset versions, trained_at)

| model_key | mode | dataset_version | real_count | synthetic_count | trained_at |
|---|---|---|---|---|---|
| `demand_prophet` | REAL | `customer_order:max_id=33601` | 32 | 0 | 2026-10-12 17:42:54 |
| `fpgrowth_combos` | REAL | `customer_order:max_id=33601` | 21,057 | 0 | 2026-10-12 17:42:54 |
| `stock_loss_isoforest` | SIMULATED | `stock_ledger:max_id=696477` | 0 | 1,846 | 2026-10-12 17:40:41 |
| `unli_consumption_isoforest` | SIMULATED | `unli_reconciliation:max_id=0+unli_flavor_reconciliation:max_id=9451` | 0 | 343 | 2026-10-12 17:40:41 |
| `flavor_consumption_isoforest` | SIMULATED | `unli_flavor_reconciliation:max_id=9451` | 0 | 1,372 | 2026-10-12 17:40:41 |

---

## 7. Reproducibility

```
cd backend
python scripts/run_thesis_ml_evaluation.py
```

Deterministic inputs: `IsolationForest(random_state=42)` for all three anomaly passes (fixed in `wt/anomaly.py`); the chronological holdout/backtest splits are all date-based, not randomized. Re-running against the same database state reproduces identical numbers; re-running after new real orders/ledger rows/reviews accumulate will (correctly) produce different numbers, which is the point.

## 8. Files produced by this run

- `backend/docs/ml_evaluation/20261013_125802/raw_results.json` — every function's complete raw return value
- `backend/docs/ml_evaluation/20261013_125802/table_a_forecasting.csv`
- `backend/docs/ml_evaluation/20261013_125802/table_b_anomaly.csv`
- `backend/docs/ml_evaluation/20261013_125802/table_c_fpgrowth.csv`
- `backend/docs/ml_evaluation/20261013_125802/table_d_summary.csv`
- `backend/docs/ml_evaluation/20261013_125802/REPORT.md` — this document
- `backend/scripts/run_thesis_ml_evaluation.py` — the evaluation script itself (reusable, re-run anytime)

An earlier run (`20261013_124929`) is also present in that directory tree with a CSV-writer bug in Table B's column mapping (fixed in the script before this run); its `raw_results.json` numbers were already correct and identical to this run's.
