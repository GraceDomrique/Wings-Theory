-- =============================================================================
-- 0018  Menu / recipe audit against the physical Wings Theory menu card,
--       2026-08. Three corrections, each one a defect rather than a
--       preference — see the note above each.
--
-- Everything the audit found that was ALREADY correct is deliberately absent
-- from this file: the fourteen flavour ratios (0012), the main-dish quantities
-- the owner reviewed (0015), all twenty-eight existing menu prices (they match
-- the printed card), and the four SKU cost benchmarks (0012) that are already
-- carried as needs_verification. None of those are touched here.
--
-- On a fresh install every statement below matches zero rows and the two
-- inserts create the Coke Zero SKU from scratch; seed.py's reference pass
-- converges on the same state.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. White Eggs: a piece count sitting in a gram-based field.
--
-- White Eggs is stocked in GRAMS (base_unit 'g', pack_size 50 — one whole egg,
-- gapfill.PIECE_WEIGHTS). Every other recipe that uses it respects that:
-- Pork Tonkatsu's "½ pc (25 g)" is stored as 25.0, the silogs' "2 pcs (100 g)"
-- as 100.0. Migration 0015 staged Onion Rings at 0.25 and Fish & Chips at 0.5
-- — those are PIECE counts, written into the gram column. As stored they mean
-- a quarter of a gram and half a gram of egg, which is not a portion any
-- kitchen can produce and is roughly 1/200th of the intended deduction.
--
-- The owner's reviewed figures (¼ egg and ½ egg) are preserved exactly; only
-- the unit is corrected, at this system's own 50 g/egg. qty_status goes back
-- to needs_verification so the two lines surface in the existing queue rather
-- than being presented as settled.
-- -----------------------------------------------------------------------------
UPDATE recipe_line
   SET qty_base = 12.5, qty_status = 'needs_verification',
       note = 'unit corrected (2026-08 audit): 0.25 was a PIECE count in a gram-based field; 0.25 pc x 50 g/egg = 12.5 g'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'White Eggs')
   AND qty_base = 0.25
   AND recipe_id IN (SELECT id FROM recipe WHERE name LIKE 'Onion Rings%');

UPDATE recipe_line
   SET qty_base = 25.0, qty_status = 'needs_verification',
       note = 'unit corrected (2026-08 audit): 0.5 was a PIECE count in a gram-based field; 0.5 pc x 50 g/egg = 25 g'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'White Eggs')
   AND qty_base = 0.5
   AND recipe_id IN (SELECT id FROM recipe WHERE name LIKE 'Fish & Chips%');

-- -----------------------------------------------------------------------------
-- 2. Coke Zero in Can — stocked, on the printed menu, unsellable.
--
-- The ingredient exists (ING-0046, inventory_type 'sellable_product', a real
-- FEFO batch on hand) and the printed card carries it as a sticker over the
-- Coke line in SOFTDRINKS IN CAN, at the same ₱60 as Sprite/Coke/Royal. But
-- no menu_item and no recipe ever pointed at it, so it could not be rung up
-- and its stock could never be deducted — a SKU the shop owns and sells with
-- no path through the POS at all.
--
-- Modelled exactly like the other three cans: a one-line menu recipe that
-- deducts one piece. The existing Coke in Can item is deliberately left in
-- place — the sticker on the physical card is ambiguous about whether Coke
-- was replaced or Coke Zero was added, and deleting a live menu item on that
-- reading would be inventing a decision the owner has not made.
-- -----------------------------------------------------------------------------
INSERT INTO recipe (recipe_id, name, kind, yield_qty, yield_unit, is_active)
SELECT 'REC-0047', 'Coke Zero in Can (P60)', 'menu', 1.0, 'serving', 1
 WHERE EXISTS (SELECT 1 FROM ingredient WHERE name = 'Coke Zero in Can')
   AND NOT EXISTS (SELECT 1 FROM recipe WHERE name = 'Coke Zero in Can (P60)');

INSERT INTO recipe_line
  (recipe_id, ingredient_id, display_label, qty_base, qty_status, is_optional, sort_order, note)
SELECT r.id, i.id, 'Coke Zero in Can', 1.0, 'verified', 0, 0,
       'one can per serving — a resale SKU, not a preparation'
  FROM recipe r JOIN ingredient i ON i.name = 'Coke Zero in Can'
 WHERE r.name = 'Coke Zero in Can (P60)'
   AND NOT EXISTS (SELECT 1 FROM recipe_line rl WHERE rl.recipe_id = r.id);

INSERT INTO menu_item (menu_item_id, name, category, price, recipe_id, is_unli, pos_visible)
SELECT 'MEN-0030', 'Coke Zero in Can', 'Drinks', 60.0, r.id, 0, 1
  FROM recipe r
 WHERE r.name = 'Coke Zero in Can (P60)'
   AND NOT EXISTS (SELECT 1 FROM menu_item WHERE name = 'Coke Zero in Can');

-- -----------------------------------------------------------------------------
-- 3. QA test artefacts left behind by the console's create-recipe endpoint.
--
-- 'QA Test Ingredient MYHMX' has pack_size NULL / pack_size_status 'unset', so
-- v_ingredient_readiness reports it permanently blocked and it sits in the
-- readiness list forever as a real problem that is not one. Guarded: removed
-- only when nothing outside its own throwaway recipe references it, so a
-- database where someone actually used these rows keeps them.
-- -----------------------------------------------------------------------------
DELETE FROM recipe_line
 WHERE recipe_id IN (SELECT id FROM recipe WHERE name LIKE 'QA Test Recipe %')
   AND NOT EXISTS (SELECT 1 FROM menu_item m
                    WHERE m.recipe_id = recipe_line.recipe_id);

DELETE FROM recipe
 WHERE name LIKE 'QA Test Recipe %'
   AND id NOT IN (SELECT recipe_id FROM recipe_line)
   AND id NOT IN (SELECT COALESCE(recipe_id, -1) FROM menu_item);

DELETE FROM ingredient
 WHERE name LIKE 'QA Test Ingredient %'
   AND id NOT IN (SELECT ingredient_id FROM recipe_line)
   AND id NOT IN (SELECT ingredient_id FROM stock_ledger)
   AND id NOT IN (SELECT ingredient_id FROM fefo_batch)
   AND id NOT IN (SELECT ingredient_id FROM reservation)
   AND id NOT IN (SELECT ingredient_id FROM ingredient_alias)
   AND id NOT IN (SELECT COALESCE(ingredient_id, -1) FROM unli_item)
   AND id NOT IN (SELECT COALESCE(ingredient_id, -1) FROM unli_flavor);

INSERT INTO schema_version (version, name) VALUES (18, 'menu_recipe_audit_2026_08');
