-- =============================================================================
-- 0010  The measurement half of the unlimited-wings model.
--
-- 0001 created unli_reconciliation with the *result* columns (heads,
-- theoretical, actual, variance) but nothing that says where `actual` came
-- from, so the table could only ever have been filled in by hand with a
-- number somebody already knew. There was no physical measurement in the
-- schema at all, and no code wrote a single row.
--
-- The three-stage model the manuscript describes is:
--
--   Stage 1  D  = H x lambda            provisional, at order time (already
--                                       live — see resolver.order_requirements)
--   Stage 2  Au = opening + added - closing
--                                       actual, from what the kitchen weighed
--   Stage 3  variance = Au - D, tolerance check, inventory correction,
--                                       then lambda recalibration from the
--                                       validated history
--
-- Stage 2 is what these columns add. All three are in the stream's own base
-- unit (g/ml/pc), the same canonical unit everything else in this database
-- is held in — no new unit vocabulary, no conversion of its own:
--
--   opening_base  what was weighed out onto the line at the start of service
--   added_base    every top-up issued to the line during service (0 if none)
--   closing_base  what was left over at close, weighed the same way
--
-- A container tare does not need a column: it appears in both the opening
-- and closing readings and cancels in the subtraction, so a gross weight is
-- as correct as a net one provided both readings are taken the same way.
--
-- Nothing here is nullable-and-then-required: the four original result
-- columns stay NOT NULL, and the code only ever inserts a complete row
-- (measurement + arithmetic + result together, in one transaction), so a
-- half-reconciled day cannot exist.
-- =============================================================================

-- --- Stage 2: the physical measurement -------------------------------------
ALTER TABLE unli_reconciliation ADD COLUMN opening_base REAL;
ALTER TABLE unli_reconciliation ADD COLUMN added_base   REAL NOT NULL DEFAULT 0;
ALTER TABLE unli_reconciliation ADD COLUMN closing_base REAL;

-- --- Stage 3: the verdict ---------------------------------------------------
-- tolerance_ratio is stamped onto the row rather than read live at display
-- time: the tolerance is configurable (see app_setting below), and a row
-- re-judged months later against a tolerance nobody had agreed to yet would
-- be a rewrite of history, not a report of it.
ALTER TABLE unli_reconciliation ADD COLUMN tolerance_ratio REAL;
ALTER TABLE unli_reconciliation ADD COLUMN status TEXT
  CHECK (status IS NULL OR status IN ('within_tolerance','over_tolerance','under_tolerance'));

-- Only a validated row may move a coefficient. A day inside tolerance
-- validates itself; a day outside it is held for a human, because the whole
-- point of the tolerance is that something unexplained happened and feeding
-- it straight back into lambda would bake the anomaly into the estimate.
ALTER TABLE unli_reconciliation ADD COLUMN validation_state TEXT NOT NULL DEFAULT 'validated'
  CHECK (validation_state IN ('validated','pending_review','rejected'));

-- What was actually posted to stock as a result, and the ledger row that did
-- it. correction_base is the DELTA against the provisional deduction
-- (theoretical - actual), never the actual itself — the provisional already
-- left the ledger at commit time and re-posting it would double-deduct.
ALTER TABLE unli_reconciliation ADD COLUMN correction_base REAL NOT NULL DEFAULT 0;
ALTER TABLE unli_reconciliation ADD COLUMN ledger_id INTEGER REFERENCES stock_ledger(id);

ALTER TABLE unli_reconciliation ADD COLUMN recorded_by TEXT;
ALTER TABLE unli_reconciliation ADD COLUMN reviewed_by TEXT;
ALTER TABLE unli_reconciliation ADD COLUMN reviewed_at TEXT;
ALTER TABLE unli_reconciliation ADD COLUMN note TEXT;

CREATE INDEX ix_unli_recon_date   ON unli_reconciliation (service_date DESC);
CREATE INDEX ix_unli_recon_stream ON unli_reconciliation (unli_item_id, validation_state, service_date);

-- -----------------------------------------------------------------------------
-- A single, tiny key/value table for operator-configurable numbers.
--
-- Deliberately not a column on some existing row and not a constant in
-- Python: the tolerance band is a business decision the owner is expected to
-- change ("10% is too tight for the fryer line"), and a redeploy is the wrong
-- way to change one. Values are stored as TEXT and parsed at the point of
-- use, so this stays one table rather than growing a type column.
-- -----------------------------------------------------------------------------
CREATE TABLE app_setting (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_by TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 0.10 = a reconciled day is "within tolerance" while |Au - D| / D <= 10%.
-- A starting point agreed with nobody yet, which is exactly why it is a
-- setting and not a constant.
INSERT INTO app_setting (key, value) VALUES ('unli_variance_tolerance', '0.10');
-- Minimum heads before a reconciled day is allowed to influence lambda at
-- all. One cover's leftovers is not a portion measurement.
INSERT INTO app_setting (key, value) VALUES ('unli_recalibration_min_heads', '20');

-- -----------------------------------------------------------------------------
-- The reconciled daily consumption dataset — the Isolation Forest's training
-- input. A view rather than a table so it cannot drift from the rows it
-- summarises, and so the seven-feature builder in wt/anomaly.py reads exactly
-- what the console's reconciliation history shows.
-- -----------------------------------------------------------------------------
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
       r.created_at
  FROM unli_reconciliation r
  JOIN unli_item  ui ON ui.id = r.unli_item_id
  JOIN ingredient i  ON i.id  = ui.ingredient_id
  LEFT JOIN unli_coefficient c ON c.id = r.coefficient_id;

INSERT INTO schema_version (version, name) VALUES (10, 'unli_reconciliation');
