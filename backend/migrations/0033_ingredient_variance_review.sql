-- =============================================================================
-- 0033  Ingredient variance review — the causal-classification gap the
--       2026-09 Phase 5 audit confirmed (see wt/reservation.py's
--       _repeated_variance_signals / ingredient_reconciliation_report).
--
-- The system can already detect a REPEATED physical-count variance (three
-- independent business dates, same direction, each >= VARIANCE_FLAG_RATIO)
-- and can already record a HUMAN CALIBRATION correction (verification_event,
-- migration 0013) for pack_size/yield_factor/unit_cost/recipe_line.qty_base/
-- unli_coefficient.lambda/fefo_batch.expires_at. Neither of those is the
-- same thing as this: a reviewer's suspected OPERATIONAL CAUSE for a
-- discrepancy between theoretical and physical stock -- waste, a portion
-- that ran heavy, a miscount, a recipe/yield that needs revisiting, or a
-- receiving problem. verification_event's shape (field/row_id/previous_
-- value/new_value) has no natural home for that: nothing about a variance
-- review confirms or changes an operational value, so forcing it through
-- that table would mean populating previous_value/new_value with nothing
-- meaningful. This is a distinct, append-only annotation of a signal, not a
-- correction of a model input -- see wt/verification.py's own module
-- docstring for why unli_coefficient changes append rather than overwrite;
-- the same append-only reasoning applies here for a different reason (the
-- classification is a human's hypothesis, not a fact the system can revise
-- an ingredient/recipe value to match).
--
-- Deliberately references the evidence (a real stock_ledger row this
-- ingredient already has, reason='count_adjustment') rather than freezing a
-- copy of the variance numbers -- consistent with how nothing else in this
-- schema duplicates a figure that can be read live off stock_ledger.
--
-- Schema-only: no data is seeded, so this is safe on a fresh install without
-- any of the guarding migration 0032 needed (nothing here is a global INSERT
-- that could trip seed.py's "already seeded" check). Idempotency here comes
-- from the same place every other migration's does -- db.migrate() only
-- ever applies a version once (schema_version) -- not from defensive
-- IF NOT EXISTS clauses, matching every other CREATE TABLE in this project.
-- =============================================================================

CREATE TABLE ingredient_variance_review (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  ingredient_id      INTEGER NOT NULL REFERENCES ingredient(id)   ON DELETE RESTRICT,
  -- The stock_ledger row (reason='count_adjustment') that was the most
  -- recent physical evidence at the moment this review was recorded --
  -- not a foreign key to a "signal" (nothing durable represents one; the
  -- repeated-direction pattern is computed live) but to a real, permanent,
  -- append-only ledger row that will never move or disappear.
  evidence_ledger_id INTEGER NOT NULL REFERENCES stock_ledger(id) ON DELETE RESTRICT,
  reason             TEXT    NOT NULL CHECK (reason IN (
                       'waste', 'portion_variance', 'counting_error',
                       'recipe_yield_issue', 'receiving_issue', 'other')),
  note               TEXT,
  user_id            TEXT,
  -- Same idempotency convention as purchase_order_receipt.client_request_id
  -- (migration 0028) / customer_order.client_request_id (migration 0009): a
  -- retried submission of the exact same review is a no-op replay, not a
  -- second row. A reviewer who changes their mind submits a NEW request
  -- (client_request_id omitted or different) and gets a new row -- the
  -- earlier review is never edited or removed.
  client_request_id  TEXT UNIQUE,
  created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_variance_review_ingredient
  ON ingredient_variance_review (ingredient_id, created_at);

-- Append-only, matching stock_ledger / verification_event: a review is a
-- record of what a human concluded at that moment, never edited in place.
CREATE TRIGGER trg_variance_review_no_update
BEFORE UPDATE ON ingredient_variance_review
BEGIN SELECT RAISE(ABORT, 'ingredient_variance_review is append-only'); END;
CREATE TRIGGER trg_variance_review_no_delete
BEFORE DELETE ON ingredient_variance_review
BEGIN SELECT RAISE(ABORT, 'ingredient_variance_review is append-only'); END;
