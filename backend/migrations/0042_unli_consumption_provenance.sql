-- =============================================================================
-- 0042  Expose reconciliation provenance to the consumption Isolation Forest.
--
-- 2026-10 audit (following the stock-loss provenance correction): unli_
-- reconciliation already has a recorded_by column (migration 0010) --
-- record_reconciliation() (wt/unli.py) stamps it with the authenticated
-- caller's real username on every LIVE call (POST /unli/reconciliation,
-- user_id=p["sub"]), and demo_seed._seed_unlimited_reconciliation() stamps
-- the identical column with demo_seed.USER_ID ('demo_seed', or
-- 'xlsx_history_import' when invoked from that importer's swapped-USER_ID
-- window) on every SYNTHETIC call -- the exact same reliable real-vs-
-- synthetic signal stock_ledger.user_id already provided for the stock-loss
-- pass. The gap: v_unli_daily_consumption (0010), the view wt/anomaly.py's
-- detect_consumption_anomalies() actually reads, never selected recorded_by
-- at all, and that function's own query carried no eligibility filter
-- whatsoever -- so a demo-seeded reconciliation day (actual_base =
-- theoretical_base x a RANDOM ratio, see demo_seed._seed_unlimited_
-- reconciliation's own docstring -- not a real scale reading) would have
-- been scored and could have been flagged as if it were a genuine
-- kitchen measurement, with nothing in the code to catch it.
--
-- Found by direct code inspection, not by data: the live database this was
-- checked against happens to hold zero unli_reconciliation rows of either
-- kind right now (no kitchen has reconciled a day yet, and this particular
-- snapshot's synthetic reconciliation step produced none either), so there
-- is no observable change in detect_consumption_anomalies()'s output today
-- (0 eligible before, 0 eligible after). The defect is real regardless --
-- the moment either a real or a synthetic reconciliation row exists, it
-- would previously have been scored identically. Fixed the same minimal way
-- as the stock-loss pass: expose the existing column, filter on it, no new
-- schema. See wt/anomaly.py's _REAL_RECORDER_CLAUSE for the actual filter.
-- =============================================================================

DROP VIEW v_unli_daily_consumption;

CREATE VIEW v_unli_daily_consumption AS
SELECT r.id                AS reconciliation_id,
       r.service_date,
       r.unli_item_id,
       ui.item_key,
       ui.label,
       i.id                AS ingredient_id,
       i.name              AS ingredient_name,
       i.base_unit,
       r.heads,
       r.theoretical_base,
       r.actual_base,
       r.variance_base,
       r.variance_ratio,
       r.opening_base,
       r.added_base,
       r.closing_base,
       r.tolerance_ratio,
       r.status,
       r.validation_state,
       r.correction_base,
       -- per-head actual consumption: the quantity lambda itself is measured
       -- in, so a reconciled day and a coefficient are directly comparable.
       CASE WHEN r.heads > 0 THEN r.actual_base / r.heads END AS actual_per_head,
       -- how much of what was put out came back: a real operational signal
       -- that exists only because the measurement exists.
       CASE WHEN (COALESCE(r.opening_base,0) + r.added_base) > 0
            THEN COALESCE(r.closing_base,0) / (COALESCE(r.opening_base,0) + r.added_base) END
            AS leftover_ratio,
       c.lambda_value      AS lambda_used,
       r.recorded_by,
       r.created_at
  FROM unli_reconciliation r
  JOIN unli_item  ui ON ui.id = r.unli_item_id
  JOIN ingredient i  ON i.id  = ui.ingredient_id
  LEFT JOIN unli_coefficient c ON c.id = r.coefficient_id;

INSERT INTO schema_version (version, name) VALUES (42, 'unli_consumption_provenance');
