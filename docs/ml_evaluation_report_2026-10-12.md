# ML evaluation report — 2026-10-12

Scope note: the two workbooks named in the original brief (`Wings_Theory_Regenerated_Consistent_Inventory.xlsx`, `Wings_Theory_1_Year_Historical.xlsx`) both self-declare as **synthetic test fixtures**, not real operational data (see their own "Read Me"/"START HERE" sheets: *"Synthetic functional test fixture only. Not real Wings Theory operational measurements."* / *"All operational data generated and labeled synthetic."*). Per your direction, this session finished and ran the already-built synthetic-ground-truth evaluation pipeline instead of importing them as real. **The live database (`backend/wings_theory.db`) was not written to at all in this session** — every number below came from either a read-only connection to it or a throwaway isolated database.

## Files changed

Committed as `c756197` "Phase 23C: checkpoint WIP" — captured prior-session work that was already built and test-passing but never committed:
- 6 new migrations (`0044`–`0049`): unli flavor reconciliation, validation sample, uniqueness constraint, ingredient minimum order qty, combo publish, unli reconciliation revision.
- New modules: [backend/wt/combos.py](backend/wt/combos.py), [backend/wt/simulated_workbook_import.py](backend/wt/simulated_workbook_import.py).
- Completed the `backend/static/` → `frontend/` asset restructuring (151 files).
- Excluded from commit: `backend/reset_prod_passwords.py` — contains live account passwords in plaintext; left untracked. **You should decide how to handle this file** (rotate those passwords out-of-band, or store it somewhere other than the repo).

No other files were modified this session beyond the evaluation driver scripts (kept in scratchpad, not the repo).

## Records replaced/imported

**None.** No rows were inserted, updated, or deleted in `wings_theory.db`. `customer_order` is unchanged: 33,588 real historical rows + 7 pre-existing stray non-historical rows (BUG-1 from the 2026-10-05 forensic audit, still unresolved — not touched this session, out of scope for "finish the eval pipeline").

## Tests

**1,100 passed, 1 skipped, 0 failed** (`backend/tests/run_tests.py`, pre-existing suite, run before committing the checkpoint).

## ML evaluation results

### (A) Real-data evaluations — read-only against the live DB

| Component | Result |
|---|---|
| Demand forecast (per-item) | Fleet mean WAPE: naive 75.2, trailing-avg 55.0, day-of-week 50.2, **Prophet 50.9**. Prophet is *not* better than the day-of-week baseline (−1.5%) — consistent with the 2026-09-28 finding; Prophet still ties/loses to the seasonal baseline. |
| Demand forecast (store aggregate) | Prophet wins on all 3 metrics here (orders/items/sales), WAPE 10.0–10.6 vs. 11.3–12.2 for day-of-week. |
| Replenishment backtest (60-day lookback, 6 points) | 0 caught, 0 missed, 168 false alarms (106 resolved by a real restock, 62 genuine). No real stockouts occurred in this particular 60-day window, so precision/recall on "catching a real stockout" isn't computable here — this is a narrower window than the 180-day lookback the 2026-09-28 memory used (which found 4.2% precision / 25% recall); not a regression, a different window. |
| Stock-loss anomaly — real-label validation | **Unavailable** — 0/20 required confirmed human labels on real (non-synthetic) ledger rows. |
| Stock-loss anomaly — scientific validation | **Unavailable** — same reason, 0 labeled samples. |
| Unli consumption anomaly (Pass B, generic) | **Dormant** — `unli_reconciliation` has 0 real rows; `records_evaluated: 0`. |
| FP-Growth / combo mining | 21,109 real transactions, 8 in-sample candidates, chronological 80/20 holdout: **1 validated** (Sprite + Creamy Sausage Pasta, lift 1.76 on holdout), 7 rejected for insufficient holdout occurrences. Improvement over the 2026-09-28/10-01 baseline (then: 0 survived holdout). |
| Combo pricing | Deterministic math, already covered by the passing unit tests — not a trained model. `draft_combo`/`draft_combo_review`: still 0 rows each, so attach-rate still has no data. |

### (B) Synthetic-ground-truth evaluations — isolated throwaway DB, workbook `Wings_Theory_1_Year_Historical_Collection_simulatedd.xlsx`, real ingredient/flavor reference rows only, `production_validated: false` on every result

| Pass | Default thresholds (as shipped) | Calibrated (chronological 60/40 split) |
|---|---|---|
| **A — Stock-loss IsolationForest** (n=1,944; 65 injected anomalies) | precision 0.056, recall 0.877, F1 0.106 | precision 0.093, recall 0.778, F1 0.165, ROC-AUC 0.817 |
| **B — Unli consumption (day-aggregate)** (n=365; 52 anomalous days) | precision 0.611, recall 0.846, F1 0.710 | precision 0.562, recall 0.750, F1 0.643, ROC-AUC 0.910 |
| **C — Flavor-level consumption** (n=1,460; 55 injected anomalies) | precision 0.195, recall 0.909, F1 0.322 | precision 0.700, recall 0.519, F1 0.596, ROC-AUC 0.982 |

Reading these: Pass A's shipped thresholds are tuned for high recall at the cost of precision (catches 88% of injected losses but with ~17 false alarms per true catch); Pass C's calibrated threshold trades the opposite way. These numbers describe how well the real detection code recovers *injected, synthetic* anomalies — they say nothing about real-world accuracy, which still has zero genuine labels to measure against (see A4/A5/A6 above).

Full machine-readable report: [ml_eval_report_2026-10-12.json](ml_eval_report_2026-10-12.json)

## Unresolved blockers

1. **No real human-reviewed labels exist** for either IsolationForest pass — real-data precision/recall for stock-loss and unli consumption remain structurally unavailable until the console's Validation Queue gets used.
2. **`unli_reconciliation` has 0 real rows** — Pass B (generic unli consumption) is fully dormant, not just unvalidated.
3. **BUG-1** (7 stray non-historical `customer_order` rows, flagged 2026-10-05): still present, not in this session's scope.
4. **BUG-2** (1,216 `anomaly_review` rows with unverified provenance, flagged 2026-10-05): still unresolved; not relied on by anything in this report (the A4/A5 "unavailable" results already exclude them).
5. **`ingredient.lead_time_days` is 100% hard-coded**, never measured from real supplier data — unchanged.
6. **Combo attach-rate has no data** — `draft_combo`/`draft_combo_review` both still empty.
7. **`backend/reset_prod_passwords.py`** contains live plaintext passwords and is intentionally left uncommitted — needs your decision on handling.
8. **The original brief's premise doesn't hold**: both named workbooks are synthetic fixtures by their own declaration. If you have separate, genuinely real operational data to bring in (not these two files), that's a distinct task from what ran here.
