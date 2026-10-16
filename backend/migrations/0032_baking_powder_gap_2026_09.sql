-- =============================================================================
-- 0032  Baking Powder: a source-confirmed gap between the data/ Master
--       Inventory workbook's Recipe Detail sheet and the production BOM.
--
-- The 2026-09 Phase 3 data-consolidation audit (data/Wings_Theory_Master_
-- Inventory.xlsx, "Recipe Detail" sheet) found Baking Powder used in nearly
-- every wing-flavour line plus two standalone dishes, but no ingredient (or
-- alias) named Baking Powder existed anywhere in this database, and unlike
-- the wing flavours -- whose shared breading/frying ingredients are tracked
-- once per order via unli_coefficient rather than per-flavour recipe_line,
-- which is why they are deliberately absent from the 14 flavour sub-recipes
-- -- Onion Rings and Fish & Chips are ordinary static recipes with no such
-- mechanism standing in for it. Their recipe_line rows are simply missing
-- this one ingredient. This migration adds only that.
--
-- Everything else the workbook lists Baking Powder in (the 14 flavour
-- sub-recipes) is deliberately NOT touched here -- that consumption already
-- has a home (unli_coefficient) and adding a redundant recipe_line would
-- double-count it. See the Phase 3 audit for the full reconciliation.
--
-- Pack size (100 g can) and unit cost (P45) are transcribed from the same
-- workbook row, exactly as migration 0021's RECONCILED_INGREDIENTS did for
-- 18 other ingredients from this same source -- both flagged
-- needs_verification, never verified, because the workbook's own
-- "ASSUMPTIONS & METHODOLOGY" note discloses these are standard foodservice
-- placeholders, not a supplier quote. Leaving pack_size NULL instead would
-- set pack_size_status='unset' and make this ingredient non-deductable
-- (v_ingredient_readiness), which is worse than an unverified number -- see
-- migration 0022's note on the same tradeoff for the flavour zests.
--
-- No supplier, reorder point, lead time, or shelf life is set here -- none
-- of those has a source value, so they stay at schema defaults / NULL
-- exactly as gapfill.py's _create_ingredient leaves them for every other
-- sourced-but-unconfirmed addition.
--
-- Idempotent: every statement below matches zero rows on a second run.
-- =============================================================================

-- Guarded on the two target recipes already existing (same pattern as
-- 0018's Coke Zero insert) so this is a true no-op on a fresh install --
-- import_recipes() hasn't run yet at migration time, and unconditionally
-- inserting Baking Powder here would leave a from-scratch database with one
-- ingredient row before seed.py's own import ever runs, which trips
-- seed.run()'s "already seeded" short-circuit (ingredient count > 0) and
-- skips seeding entirely -- no users, no recipes, nothing. Caught by the
-- full test suite failing suite-wide on setup, not by anything specific to
-- Baking Powder.
INSERT INTO ingredient
  (ingredient_id, name, category, kind, base_unit, purchase_unit,
   pack_size, pack_size_status, yield_factor, yield_factor_status,
   on_hand_base, unit_cost, unit_cost_status, lead_time_days)
SELECT 'ING-0131', 'Baking Powder', 'dry_food', 'raw', 'g', '100g can',
       100.0, 'needs_verification', 1.0, 'needs_verification',
       0, 45.0, 'needs_verification', 3
 WHERE NOT EXISTS (SELECT 1 FROM ingredient WHERE name = 'Baking Powder')
   AND NOT EXISTS (SELECT 1 FROM ingredient_alias WHERE alias_norm = 'baking powder')
   AND EXISTS (SELECT 1 FROM recipe WHERE name IN
        ('Onion Rings (P99)', 'Fish & Chips with Tartar Sauce (P199)'));

INSERT INTO recipe_line
  (recipe_id, ingredient_id, display_label, qty_base, qty_status, is_optional, sort_order, note)
SELECT r.id, i.id, 'Baking Powder', 2.0, 'needs_verification', 0, 91,
       'source-backed gap (Phase 3 audit, 2026-09): data/Wings_Theory_Master_Inventory.xlsx Recipe Detail sheet lists Baking Powder 2 g for Onion Rings; no equivalent line existed'
  FROM recipe r JOIN ingredient i ON i.name = 'Baking Powder'
 WHERE r.name = 'Onion Rings (P99)'
   AND NOT EXISTS (SELECT 1 FROM recipe_line rl WHERE rl.recipe_id = r.id AND rl.ingredient_id = i.id);

INSERT INTO recipe_line
  (recipe_id, ingredient_id, display_label, qty_base, qty_status, is_optional, sort_order, note)
SELECT r.id, i.id, 'Baking Powder', 3.0, 'needs_verification', 0, 91,
       'source-backed gap (Phase 3 audit, 2026-09): data/Wings_Theory_Master_Inventory.xlsx Recipe Detail sheet lists Baking Powder 3 g for Fish & Chips; no equivalent line existed'
  FROM recipe r JOIN ingredient i ON i.name = 'Baking Powder'
 WHERE r.name = 'Fish & Chips with Tartar Sauce (P199)'
   AND NOT EXISTS (SELECT 1 FROM recipe_line rl WHERE rl.recipe_id = r.id AND rl.ingredient_id = i.id);
