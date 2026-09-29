-- =============================================================================
-- 0020  Stop the frozen unli baseline from ever reaching the verification queue.
--
-- 2026-09 audit finding: seed.py creates BOTH the 'baseline' and 'rolling'
-- unli_coefficient row for a stream with status='needs_verification' --
-- reasonable for 'rolling' (it starts as an estimate awaiting confirmation),
-- wrong for 'baseline', whose entire purpose is to be the frozen reference
-- everything else is measured against (see unli.recalibrate()'s own comment:
-- "the baseline coefficient is never touched... a baseline that moves with
-- the data measures nothing").
--
-- v_needs_verification's unli_coefficient.lambda branch filtered only on
-- status='needs_verification', with no role condition, so that baseline row
-- surfaced in the ordinary verification queue like any other pending
-- estimate. verification.resolve()'s "change" action inserts a new
-- unli_coefficient row carrying the SAME role as the one being corrected (by
-- design, for 'rolling': "insert a new row... so only orders from this
-- moment on see the new rate") -- so choosing "change" on this one queue
-- item, during otherwise completely routine verification work, would insert
-- a new 'baseline' row. Every reader of "the" baseline
-- (api.py/anomaly.py/export_ui.py) takes the most recent by effective_from,
-- so that new row silently became the anomaly-detection reference from that
-- moment on. Exactly the failure mode the spec requires never happen: a
-- normal correction flow silently moving the frozen baseline.
--
-- Two independent layers, so a future change to either alone can't
-- reintroduce this:
--   1. The view never surfaces a baseline-role coefficient at all, so it is
--      structurally unreachable through the queue.
--   2. wt/verification.py's resolve() (application-layer fix, not part of
--      this migration) refuses a "change" action on a baseline-role
--      coefficient outright, in case anything else ever routes here.
-- =============================================================================

DROP VIEW v_needs_verification;

CREATE VIEW v_needs_verification AS
  SELECT 'ingredient.pack_size'    AS field, i.ingredient_id AS entity_id, i.name AS entity_name,
         CAST(i.pack_size AS TEXT) AS current_value,
         i.purchase_unit || ' -> ' || i.base_unit AS context, i.id AS row_id
    FROM ingredient i WHERE i.pack_size_status = 'needs_verification'
UNION ALL
  SELECT 'ingredient.yield_factor', i.ingredient_id, i.name,
         CAST(i.yield_factor AS TEXT), 'raw -> usable', i.id
    FROM ingredient i WHERE i.yield_factor_status = 'needs_verification'
UNION ALL
  SELECT 'ingredient.unit_cost', i.ingredient_id, i.name,
         CAST(i.unit_cost AS TEXT), 'per ' || i.purchase_unit, i.id
    FROM ingredient i WHERE i.unit_cost_status = 'needs_verification'
UNION ALL
  SELECT 'recipe_line.qty_base', r.recipe_id, r.name || ' / ' || rl.display_label,
         CAST(rl.qty_base AS TEXT), 'per ' || r.yield_unit, rl.id
    FROM recipe_line rl JOIN recipe r ON r.id = rl.recipe_id
   WHERE rl.qty_status = 'needs_verification'
UNION ALL
  SELECT 'fefo_batch.expires_at', COALESCE(b.lot_no,'batch#'||b.id), i.name,
         COALESCE(b.expires_at,'(none)'), 'received ' || b.received_at, b.id
    FROM fefo_batch b JOIN ingredient i ON i.id = b.ingredient_id
   WHERE b.expiry_status = 'needs_verification' AND b.state = 'open'
UNION ALL
  -- role != 'baseline' is the fix: a frozen reference has nothing to
  -- "verify" against and must never be offered as a correctable estimate.
  SELECT 'unli_coefficient.lambda', u.item_key, u.label,
         CAST(c.lambda_value AS TEXT), c.role || ' coefficient', c.id
    FROM unli_coefficient c JOIN unli_item u ON u.id = c.unli_item_id
   WHERE c.status = 'needs_verification' AND c.role != 'baseline';

-- Repair existing data: any baseline row a prior seed run left pending is
-- retroactively treated as confirmed at seed time, matching what it should
-- have been from the start -- its lambda_value is untouched, only status
-- changes, so no historical anomaly comparison is affected.
UPDATE unli_coefficient SET status = 'verified'
 WHERE role = 'baseline' AND status = 'needs_verification';

INSERT INTO schema_version (version, name) VALUES (20, 'freeze_unli_baseline_from_verification');
