# Synthetic dataset integration — 2026-10-12

Scope: replace the obsolete synthetic data (previously imported from the retired `Wings_Theory_1_Year_Historical_Collection_simulatedd.xlsx`) with the two current workbooks — `Wings_Theory_Regenerated_Consistent_Inventory.xlsx` (primary: receiving/wastage/stock counts) and `Wings_Theory_1_Year_Historical.xlsx` (supplementary: unli flavor reconciliation only) — **inside an isolated database copy**. Both workbooks are explicitly synthetic by their own declaration; nothing here is presented or stored as real.

**The live database (`backend/wings_theory.db`) was not modified.** Everything below ran against `backend/isolated_work/isolated_copy.db`, a byte-for-byte copy of the live DB taken after a verified backup. The live DB stays exactly as it was (including its *old* synthetic data) until you approve applying this replacement to it.

## Files changed

| File | Change |
|---|---|
| [backend/wt/simulated_workbook_import.py](../backend/wt/simulated_workbook_import.py) | Split single-workbook design into `inventory_path` + `historical_path`; added `replace_existing` (default `True`) which purges every row this module previously wrote (identified solely by its sentinel `user_id`/`recorded_by`/`created_by`) before re-importing, including a scoped, restored lift of the `stock_ledger`/`unli_flavor_reconciliation`/`ingredient_variance_review` append-only triggers for exactly those synthetic rows. +198/-? lines. |
| [backend/wt/demo_validation.py](../backend/wt/demo_validation.py) | Fixed a real defect (see below): the stock-loss evaluation harness hardcoded the old workbook's positive label (`INJECTED_STOCK_LOSS`); the new workbook uses `SYNTHETIC_LOSS`. Now checks both. Updated default source paths to the new per-sheet files. |
| [backend/tests/run_tests.py](../backend/tests/run_tests.py) | Updated the one call site using the old single-path signature; rewrote the idempotency test to cover both `replace_existing=False` (pure dedup no-op) and `replace_existing=True` (purge + reimport, same end state); added a regression test guarding the label-convention fix. |

Not committed to git yet — left as working-tree changes for your review before you decide to commit.

## Integration defect found and fixed

`Wings_Theory_Regenerated_Consistent_Inventory.xlsx`'s Stock Counts sheet labels an injected loss `SYNTHETIC_LOSS` (confirmed against that workbook's own "Validation Summary" sheet: *"Injected stock loss events: 587"*), not `INJECTED_STOCK_LOSS` (the retired workbook's convention). The evaluation code had the old string hardcoded, so every score against the new workbook silently reported 0 ground-truth positives (precision 0.0, recall `null`) — not because the model found nothing, but because the harness was checking the wrong label. Fixed by checking a set of recognized positive labels instead of one hardcoded string; added `test_stock_loss_workbook_eval_recognizes_this_workbooks_own_positive_label` to guard against a silent regression.

No other integration defects found. Anomaly threshold recalibration was considered (your directive #7) but **not applied**: the shipped default thresholds (precision 0.391, recall 0.99, F1 0.561) are reasonably close to the chronologically-calibrated optimum (precision 0.412, recall 0.958, F1 0.576) — recalibrating production constants from one synthetic run risked overfitting to this particular synthetic distribution without a clear, demonstrated defect to justify it (unlike the 2026-10-08 receiving-balance fix, which *was* a demonstrated defect).

## Records replaced/imported (isolated copy only)

| Table | Before | Purged (old synthetic) | Imported (new) | After |
|---|---|---|---|---|
| `stock_ledger` | 682,105 | 1,944 (648 wastage + 1,296 counts) | 9,249 (7,737 wastage + 1,512 counts) | 689,410 |
| `purchase_order` / `_item` | 1,296 | 1,296 | 3,020 | 3,020 |
| `purchase_order_receipt` | 1,296 | 1,296 | ≤3,020 (short/zero-receipt rows get no receipt event) | matches receiving rows with qty > 0 |
| `unli_flavor_reconciliation` | 1,460 | 1,460 | 1,460 | 1,460 |
| `customer_order` (real sales) | 33,595 | — | — | 33,595 (unchanged) |
| `ingredient.on_hand_base`/`reserved_base` | — | — | — | bit-for-bit unchanged (verified by snapshot comparison before/after) |

Note: the live DB's `purchase_order`/`unli_flavor_reconciliation` tables already held this exact "old synthetic" data from a prior session — a correction to my own earlier report, which had missed that these tables existed and were populated.

Zero rejected rows across all four imported sheets (3,020 receiving + 7,737 wastage + 1,512 counts + 1,460 reconciliation, 100% accepted). Zero FK integrity errors. Idempotency verified both ways: re-running with `replace_existing=False` is a pure no-op (0 inserted, all duplicates); re-running with `replace_existing=True` (default) purges and reimports to an identical end state. The append-only guard on `stock_ledger`/`unli_flavor_reconciliation` is confirmed restored and enforced immediately after each purge.

## Tests

**1,101 passed, 0 failed, 1 skipped** (`backend/tests/run_tests.py`, includes the 1 new regression test added this session).

## ML evaluation results (against the isolated, post-import copy)

Real-data components (A1–A3, A7) are **unaffected by this import** — they read `customer_order`/menu data, which this importer never touches — numbers match the pre-import baseline exactly, confirming no cross-contamination:

- Demand forecast (per-item): Prophet fleet WAPE 50.91 vs. best baseline (day-of-week) 50.16 — Prophet still not better (−1.5%).
- Demand forecast (store aggregate): Prophet wins all 3 metrics.
- Replenishment backtest (60-day window): 0 caught, 0 missed, 168 false alarms — no real stockouts in this window to catch; unaffected by synthetic wastage/counts.
- FP-Growth: 1 holdout-validated combo (Sprite + Creamy Sausage Pasta, holdout lift 1.76).

Synthetic-ground-truth components (against the **new** workbooks, correctly isolated — `production_validated: false` on every result):

| Pass | n | Positives | Default thresholds | Calibrated (chronological 60/40) |
|---|---|---|---|---|
| **A — Stock-loss** | 9,249 | 579 (`SYNTHETIC_LOSS`) | precision 0.391, recall 0.990, F1 0.561 | precision 0.412, recall 0.958, F1 0.576, ROC-AUC 0.955 |
| **B — Unli consumption (day-aggregate)** | 365 | 52 | precision 0.611, recall 0.846, F1 0.710 | precision 0.562, recall 0.750, F1 0.643, ROC-AUC 0.910 |
| **C — Flavor-level consumption** | 1,460 | 55 | precision 0.195, recall 0.909, F1 0.322 | precision 0.700, recall 0.519, F1 0.596, ROC-AUC 0.982 |

Pass A's numbers improved substantially once the label defect was fixed (from an unusable 0/null/null to a working 0.391/0.99/0.561) — this is the real result of the new workbook's data, not an artifact.

Real-label validation (A4/A5/A6) remains correctly **unavailable/dormant** — 0 confirmed human-reviewed labels exist on real rows, and the newly-imported rows are correctly excluded from that eligibility pool (verified: `app_user` has no account named `simulated_workbook_import`, and `detect_anomalies()`'s own output never attributes a flag to that sentinel).

Full machine-readable results: [ml_eval_report_isolated_2026-10-12.json](ml_eval_report_isolated_2026-10-12.json)

## Remaining blockers

1. **Your explicit approval is required before applying this to the live `wings_theory.db`.** Nothing above has touched it.
2. The code changes (`simulated_workbook_import.py`, `demo_validation.py`, `run_tests.py`) are uncommitted working-tree changes — your call on when to commit.
3. Real-label validation (A4/A5/A6) still has zero genuine human-reviewed labels — unchanged by this work, since synthetic data is correctly excluded from that pool by design.
4. `backend/reset_prod_passwords.py` (plaintext live credentials) is still sitting uncommitted from the prior session — still needs your decision.
5. The two pre-existing data-integrity bugs from the 2026-10-05 forensic audit (7 stray non-historical `customer_order` rows; unverified `anomaly_review` provenance) are untouched, out of scope here.
6. If you approve applying this to the live DB, the exact same `simulated_workbook_import.run()` call (against `db.get_conn()` instead of the isolated copy) reproduces this; I have not run it against live.
