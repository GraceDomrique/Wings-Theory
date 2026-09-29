-- =============================================================================
-- 0021  Reconcile the 14 flavor sauce sub-recipes against the verified
-- Wings_Theory_Master_Inventory.xlsx source of truth.
--
-- Background: gapfill.py originally seeded these 13 sauces (Original Dry Rub
-- excluded by explicit business decision) as a "100 g nominal batch" -- an
-- assumption stamped needs_verification on every line, never confirmed
-- against real kitchen recipes. The workbook's "Recipe Detail" sheet gives
-- the actual per-one-6-pc-order recipe for each flavor (breading + sauce);
-- this migration reconciles only the sauce-specific portion of that recipe
-- (breading/wings/oil are shared across all flavors and already modeled
-- elsewhere). Ingredient identity/unit for every new raw ingredient is taken
-- from the workbook's "Master Inventory" sheet, the authoritative source for
-- name, base unit, pack size, and cost.
--
-- Model change: each of these 13 sub-recipes moves from yield_qty=100 (a
-- fabricated round-number batch) to yield_qty=1, yield_unit='order' -- one
-- recipe execution IS one 6-pc order's worth of sauce, in the workbook's
-- exact per-ingredient units (grams and millilitres side by side, no density
-- conversion invented; see Mango Habanero, which stays mixed-unit on
-- purpose). v_recipe_expansion computes
-- parent_qty_base * child_qty_base / sub.yield_qty, so with yield_qty=1 the
-- parent-side recipe_line that references the sauce ingredient must itself
-- read in units of "how many orders' worth of sauce", not grams of a 100 g
-- batch -- see the five-line rescale at the end.
--
-- Water is intentionally excluded, per the workbook's own methodology sheet
-- ("Sub-Recipes & Exclusions": "Plain tap/utility water... is not a
-- purchased or trackable SKU"). "Or" wording ("Honey or corn syrup",
-- "Vinegar or lime juice", "Corn syrup/honey") is resolved per that same
-- sheet's documented methodology, confirmed by the business owner.
--
-- Unlimited Wings is untouched: unli.py deducts a single generic `Wings`
-- ingredient by head-count x lambda coefficient and never reads these
-- sub-recipes at all -- unli_flavor only tags a flavor's display name to an
-- ingredient for refill-tracking UI, not for stock deduction.
--
-- Original Dry Rub (produces_ingredient 'Original Dry Rub') is deliberately
-- excluded from every statement below, by explicit business decision.
--
-- On a fresh install this runs against an empty database (the test suite's
-- fresh_db() migrates a bare schema, never seeded data; seed.run(fresh=True)
-- also migrates an empty schema before its own Python seeding step runs).
-- EVERY statement below -- including the new-ingredient inserts -- is
-- therefore guarded to match zero rows unless this database was already
-- seeded (anchor check: 'Classic Buffalo Sauce' sub-recipe already exists),
-- exactly like migration 0015's convention ("On a fresh install this runs
-- against an empty database and every statement below matches zero rows").
-- This matters beyond tidiness: seed.py's run() treats ANY pre-existing
-- ingredient row as "already seeded" and aborts its entire seeding pass
-- (import_inventory, gapfill, menu, users -- everything) -- so an unguarded
-- insert here would silently break every fresh install and every
-- seed.run(fresh=True)-based test. gapfill.py's SUB_RECIPES is NOT updated
-- by this migration -- that is a known, separate follow-up for the
-- fresh-install path; this migration's job is reconciling the
-- already-seeded production data in wings_theory.db.
-- -----------------------------------------------------------------------------

-- -----------------------------------------------------------------------------
-- 1. New raw ingredients, from Master Inventory. Only created because no
--    existing ingredient is the same purchased item (checked against the full
--    ingredient table first -- e.g. the existing "Hot Sauce"/g and
--    "Buffalo Hot Sauce"/g are different products from the workbook's
--    ml-based "Hot Sauce (Cayenne)"). Guarded by name so a second run (or a
--    database that already has one of these from some other path) does not
--    insert a duplicate, AND by the already-seeded anchor described above.
-- -----------------------------------------------------------------------------
INSERT INTO ingredient (ingredient_id, name, category, kind, base_unit, purchase_unit,
                         pack_size, pack_size_status, unit_cost, unit_cost_status)
SELECT v.ingredient_id, v.name, v.category, 'raw', v.base_unit, v.purchase_unit,
       v.pack_size, v.pack_size_status, v.unit_cost, v.unit_cost_status
  FROM (
    SELECT 'ING-0113' AS ingredient_id, 'Hot Sauce (Cayenne)'        AS name, 'dry_food' AS category, 'ml' AS base_unit, '700ml bottle' AS purchase_unit, 700.0  AS pack_size, 'verified' AS pack_size_status, 280.0 AS unit_cost, 'verified' AS unit_cost_status
    UNION ALL SELECT 'ING-0114', 'Sriracha Sauce',                'dry_food', 'ml', '730ml bottle', 730.0,  'verified', 280.0, 'verified'
    UNION ALL SELECT 'ING-0115', 'Fresh Ginger',                  'dry_food', 'g',  '500g pack',    500.0,  'verified', 90.0,  'verified'
    UNION ALL SELECT 'ING-0116', 'Salted Egg Yolk, Frozen',       'dry_food', 'pc', '30-pc box',    30.0,   'verified', 280.0, 'verified'
    UNION ALL SELECT 'ING-0117', 'Mango Nectar/Puree',            'dry_food', 'ml', '1L tetra',     1000.0, 'verified', 150.0, 'verified'
    UNION ALL SELECT 'ING-0118', 'Habanero Hot Sauce',            'dry_food', 'ml', '148ml bottle', 148.0,  'verified', 250.0, 'verified'
    UNION ALL SELECT 'ING-0119', 'Curry Leaves, Dried',           'dry_food', 'g',  '25g pack',     25.0,   'verified', 60.0,  'verified'
    UNION ALL SELECT 'ING-0120', 'Calamansi/Lemon Juice',         'dry_food', 'ml', '1L bottle',    1000.0, 'verified', 180.0, 'verified'
    UNION ALL SELECT 'ING-0121', 'Korean BBQ Sauce, Bottled',     'dry_food', 'ml', '1kg bottle',   1000.0, 'verified', 320.0, 'verified'
    UNION ALL SELECT 'ING-0122', 'Sesame Oil',                    'dry_food', 'ml', '150ml bottle', 150.0,  'verified', 110.0, 'verified'
    UNION ALL SELECT 'ING-0123', 'Brown Sugar',                   'dry_food', 'g',  '1kg pack',     1000.0, 'verified', 75.0,  'verified'
    UNION ALL SELECT 'ING-0124', 'Mirin (Japanese Cooking Wine)', 'dry_food', 'ml', '500ml bottle', 500.0,  'verified', 220.0, 'verified'
    UNION ALL SELECT 'ING-0125', 'Powdered Sugar',                'dry_food', 'g',  '1kg pack',     1000.0, 'verified', 90.0,  'verified'
    UNION ALL SELECT 'ING-0126', 'Orange Juice',                  'dry_food', 'ml', '1L tetra',     1000.0, 'verified', 120.0, 'verified'
    UNION ALL SELECT 'ING-0127', 'Rice Vinegar',                  'dry_food', 'ml', '750ml bottle', 750.0,  'verified', 90.0,  'verified'
    UNION ALL SELECT 'ING-0128', 'Banana Ketchup',                'dry_food', 'g',  '1kg pouch',    1000.0, 'verified', 90.0,  'verified'
    UNION ALL SELECT 'ING-0129', 'Cheddar Cheese, Shredded',      'dry_food', 'g',  '1kg bag',      1000.0, 'verified', 480.0, 'verified'
    -- Not a separately purchased SKU in Master Inventory (shaved from whole
    -- Lemon/Orange, which ARE listed) -- physical constants left unset
    -- rather than fabricated, per explicit instruction not to derive a cost
    -- from the whole-fruit price for this task.
    UNION ALL SELECT 'ING-0130', 'Lemon Zest',                    'dry_food', 'g',  'g',            NULL,   'unset',    NULL,  'unset'
    UNION ALL SELECT 'ING-0131', 'Orange Zest',                   'dry_food', 'g',  'g',            NULL,   'unset',    NULL,  'unset'
  ) v
 WHERE NOT EXISTS (SELECT 1 FROM ingredient i WHERE i.name = v.name)
   AND EXISTS (SELECT 1 FROM recipe WHERE kind = 'sub_recipe' AND name = 'Classic Buffalo Sauce');

-- -----------------------------------------------------------------------------
-- 2. Sub-recipe batch model: 100 g nominal -> 1 order (exact workbook units).
--    Matches zero rows on a database that has none of these recipes yet.
-- -----------------------------------------------------------------------------
UPDATE recipe SET yield_qty = 1, yield_unit = 'order', updated_at = datetime('now')
 WHERE kind = 'sub_recipe'
   AND name IN (
     'Classic Buffalo Sauce', 'Mango Habanero Sauce', 'Salted Egg Sauce',
     'Lemon Glazed Sauce', 'Korean BBQ Sauce', 'Garlic Parmesan Sauce',
     'Soy Garlic Sauce', 'Teriyaki Sauce', 'Honey Sriracha Sauce',
     'Snow Cheese Sauce', 'Orange Chicken Sauce', 'Korean Yangneum Sauce',
     'Cheese Wing Sauce'
   );   -- excludes 'Original Dry Rub'

-- -----------------------------------------------------------------------------
-- 3. Replace each sauce's recipe lines with the verified workbook quantities.
--    Old needs_verification 100g-batch lines are removed first. Both the
--    DELETE and the guarded INSERT below match zero rows when the named
--    recipe does not exist (fresh/test database), so this is safe to run
--    against any starting state.
-- -----------------------------------------------------------------------------
DELETE FROM recipe_line
 WHERE recipe_id IN (
   SELECT id FROM recipe WHERE kind = 'sub_recipe' AND name IN (
     'Classic Buffalo Sauce', 'Mango Habanero Sauce', 'Salted Egg Sauce',
     'Lemon Glazed Sauce', 'Korean BBQ Sauce', 'Garlic Parmesan Sauce',
     'Soy Garlic Sauce', 'Teriyaki Sauce', 'Honey Sriracha Sauce',
     'Snow Cheese Sauce', 'Orange Chicken Sauce', 'Korean Yangneum Sauce',
     'Cheese Wing Sauce'
   )
 );

INSERT INTO recipe_line (recipe_id, ingredient_id, display_label, qty_base, qty_status, sort_order)
SELECT r.id, ing.id, v.display_label, v.qty_base, v.qty_status, v.sort_order
  FROM (
    -- Classic Buffalo Sauce
    SELECT 'Classic Buffalo Sauce' AS recipe_name, 'Hot Sauce (Cayenne)' AS ingredient_name, 'Hot sauce' AS display_label, 45.0 AS qty_base, 'verified' AS qty_status, 1 AS sort_order
    UNION ALL SELECT 'Classic Buffalo Sauce', 'Butter',           'Butter', 18.0, 'verified', 2
    UNION ALL SELECT 'Classic Buffalo Sauce', 'Worchestershire',  'Worcestershire sauce', 3.0, 'verified', 3

    -- Mango Habanero Sauce -- mixed units preserved exactly, no fabricated yield conversion.
    UNION ALL SELECT 'Mango Habanero Sauce', 'Mango Nectar/Puree', 'Mango nectar/puree', 55.0, 'verified', 1
    UNION ALL SELECT 'Mango Habanero Sauce', 'Habanero Hot Sauce', 'Habanero sauce', 10.0, 'verified', 2
    UNION ALL SELECT 'Mango Habanero Sauce', 'Honey',              'Honey or corn syrup', 8.0, 'verified', 3
    UNION ALL SELECT 'Mango Habanero Sauce', 'Butter',             'Butter', 8.0, 'verified', 4
    UNION ALL SELECT 'Mango Habanero Sauce', 'White Vinegar',      'Vinegar or lime juice', 3.0, 'verified', 5

    -- Salted Egg Sauce
    UNION ALL SELECT 'Salted Egg Sauce', 'Salted Egg Yolk, Frozen', 'Salted egg yolk', 2.0, 'verified', 1
    UNION ALL SELECT 'Salted Egg Sauce', 'Butter',                  'Butter', 15.0, 'verified', 2
    UNION ALL SELECT 'Salted Egg Sauce', 'Fresh Garlic Peeled',     'Garlic', 5.0, 'verified', 3
    UNION ALL SELECT 'Salted Egg Sauce', 'Evaporated Milk',         'Evaporated milk/cream', 20.0, 'verified', 4
    UNION ALL SELECT 'Salted Egg Sauce', 'Chili Flakes Dehydrated', 'Chili flakes', 1.0, 'verified', 5
    UNION ALL SELECT 'Salted Egg Sauce', 'Curry Leaves, Dried',     'Curry leaves', 2.0, 'verified', 6

    -- Lemon Glazed Sauce -- Water line dropped (untracked, see header).
    UNION ALL SELECT 'Lemon Glazed Sauce', 'Calamansi/Lemon Juice', 'Lemon juice', 20.0, 'verified', 1
    UNION ALL SELECT 'Lemon Glazed Sauce', 'Lemon Zest',            'Lemon zest', 1.0, 'verified', 2
    UNION ALL SELECT 'Lemon Glazed Sauce', 'Honey',                 'Honey', 18.0, 'verified', 3
    UNION ALL SELECT 'Lemon Glazed Sauce', 'Butter',                'Butter', 8.0, 'verified', 4
    UNION ALL SELECT 'Lemon Glazed Sauce', 'Fresh Garlic Peeled',   'Garlic', 2.0, 'verified', 5
    UNION ALL SELECT 'Lemon Glazed Sauce', 'Cornstarch',            'Cornstarch', 2.0, 'verified', 6

    -- Korean BBQ Sauce -- "Korean BBQ sauce" maps to the new raw bottled
    -- ingredient, NOT this recipe's own sub-recipe output.
    UNION ALL SELECT 'Korean BBQ Sauce', 'Korean BBQ Sauce, Bottled', 'Korean BBQ sauce', 45.0, 'verified', 1
    UNION ALL SELECT 'Korean BBQ Sauce', 'Soy Sauce',                 'Soy sauce', 5.0, 'verified', 2
    UNION ALL SELECT 'Korean BBQ Sauce', 'Honey',                     'Honey/brown sugar', 8.0, 'verified', 3
    UNION ALL SELECT 'Korean BBQ Sauce', 'Fresh Garlic Peeled',       'Garlic', 4.0, 'verified', 4
    UNION ALL SELECT 'Korean BBQ Sauce', 'Sesame Oil',                'Sesame oil', 2.0, 'verified', 5
    UNION ALL SELECT 'Korean BBQ Sauce', 'Sesame Seed',               'Sesame seeds', 2.0, 'verified', 6

    -- Garlic Parmesan Sauce
    UNION ALL SELECT 'Garlic Parmesan Sauce', 'Butter',          'Butter', 18.0, 'verified', 1
    UNION ALL SELECT 'Garlic Parmesan Sauce', 'Fresh Garlic Peeled', 'Garlic', 6.0, 'verified', 2
    UNION ALL SELECT 'Garlic Parmesan Sauce', 'Parmesan Cheese',  'Parmesan cheese', 18.0, 'verified', 3
    UNION ALL SELECT 'Garlic Parmesan Sauce', 'Parsley',          'Parsley', 2.0, 'verified', 4
    UNION ALL SELECT 'Garlic Parmesan Sauce', 'Ground Pepper',    'Black pepper', 1.0, 'verified', 5

    -- Soy Garlic Sauce
    UNION ALL SELECT 'Soy Garlic Sauce', 'Soy Sauce',            'Soy sauce', 25.0, 'verified', 1
    UNION ALL SELECT 'Soy Garlic Sauce', 'Honey',                'Honey', 12.0, 'verified', 2
    UNION ALL SELECT 'Soy Garlic Sauce', 'Brown Sugar',          'Brown sugar', 6.0, 'verified', 3
    UNION ALL SELECT 'Soy Garlic Sauce', 'Fresh Garlic Peeled',  'Garlic', 6.0, 'verified', 4
    UNION ALL SELECT 'Soy Garlic Sauce', 'Fresh Ginger',         'Ginger', 3.0, 'verified', 5
    UNION ALL SELECT 'Soy Garlic Sauce', 'Mirin (Japanese Cooking Wine)', 'Mirin', 15.0, 'verified', 6
    UNION ALL SELECT 'Soy Garlic Sauce', 'Sesame Oil',           'Sesame oil', 3.0, 'verified', 7
    UNION ALL SELECT 'Soy Garlic Sauce', 'Sesame Seed',          'Sesame seeds', 2.0, 'verified', 8

    -- Teriyaki Sauce -- Water line dropped.
    UNION ALL SELECT 'Teriyaki Sauce', 'Soy Sauce',               'Soy sauce', 25.0, 'verified', 1
    UNION ALL SELECT 'Teriyaki Sauce', 'Mirin (Japanese Cooking Wine)', 'Mirin', 15.0, 'verified', 2
    UNION ALL SELECT 'Teriyaki Sauce', 'Brown Sugar',             'Brown sugar', 12.0, 'verified', 3
    UNION ALL SELECT 'Teriyaki Sauce', 'Fresh Ginger',            'Ginger', 2.0, 'verified', 4
    UNION ALL SELECT 'Teriyaki Sauce', 'Fresh Garlic Peeled',     'Garlic', 2.0, 'verified', 5
    UNION ALL SELECT 'Teriyaki Sauce', 'Cornstarch',              'Cornstarch', 2.0, 'verified', 6
    UNION ALL SELECT 'Teriyaki Sauce', 'Sesame Seed',             'Sesame seeds', 2.0, 'verified', 7

    -- Honey Sriracha Sauce
    UNION ALL SELECT 'Honey Sriracha Sauce', 'Sriracha Sauce',        'Sriracha sauce', 35.0, 'verified', 1
    UNION ALL SELECT 'Honey Sriracha Sauce', 'Honey',                 'Honey', 12.0, 'verified', 2
    UNION ALL SELECT 'Honey Sriracha Sauce', 'Butter',                'Butter', 8.0, 'verified', 3
    UNION ALL SELECT 'Honey Sriracha Sauce', 'Calamansi/Lemon Juice', 'Lime/calamansi juice', 4.0, 'verified', 4
    UNION ALL SELECT 'Honey Sriracha Sauce', 'Garlic powder',        'Garlic powder', 1.0, 'verified', 5

    -- Snow Cheese Sauce
    UNION ALL SELECT 'Snow Cheese Sauce', 'Cheese Powder',   'Cheese powder', 18.0, 'verified', 1
    UNION ALL SELECT 'Snow Cheese Sauce', 'Milk Powder',     'Milk powder', 5.0, 'verified', 2
    UNION ALL SELECT 'Snow Cheese Sauce', 'Powdered Sugar',  'Powdered sugar', 3.0, 'verified', 3
    UNION ALL SELECT 'Snow Cheese Sauce', 'Parmesan Cheese', 'Parmesan cheese', 5.0, 'verified', 4

    -- Orange Chicken Sauce -- Water line dropped.
    UNION ALL SELECT 'Orange Chicken Sauce', 'Orange Juice',         'Orange juice', 35.0, 'verified', 1
    UNION ALL SELECT 'Orange Chicken Sauce', 'Orange Zest',          'Orange zest', 1.0, 'verified', 2
    UNION ALL SELECT 'Orange Chicken Sauce', 'Soy Sauce',            'Soy sauce', 8.0, 'verified', 3
    UNION ALL SELECT 'Orange Chicken Sauce', 'Brown Sugar',          'Brown sugar', 12.0, 'verified', 4
    UNION ALL SELECT 'Orange Chicken Sauce', 'Rice Vinegar',         'Rice vinegar', 5.0, 'verified', 5
    UNION ALL SELECT 'Orange Chicken Sauce', 'Fresh Garlic Peeled',  'Garlic', 3.0, 'verified', 6
    UNION ALL SELECT 'Orange Chicken Sauce', 'Fresh Ginger',         'Ginger', 2.0, 'verified', 7
    UNION ALL SELECT 'Orange Chicken Sauce', 'Cornstarch',           'Cornstarch', 3.0, 'verified', 8
    UNION ALL SELECT 'Orange Chicken Sauce', 'Chili Flakes Dehydrated', 'Chili flakes', 1.0, 'verified', 9

    -- Korean Yangneum Sauce
    UNION ALL SELECT 'Korean Yangneum Sauce', 'Gochujang Big',      'Gochujang', 25.0, 'verified', 1
    UNION ALL SELECT 'Korean Yangneum Sauce', 'Banana Ketchup',     'Ketchup', 15.0, 'verified', 2
    UNION ALL SELECT 'Korean Yangneum Sauce', 'Honey',               'Corn syrup/honey', 15.0, 'verified', 3
    UNION ALL SELECT 'Korean Yangneum Sauce', 'Soy Sauce',           'Soy sauce', 8.0, 'verified', 4
    UNION ALL SELECT 'Korean Yangneum Sauce', 'Rice Vinegar',        'Rice vinegar', 4.0, 'verified', 5
    UNION ALL SELECT 'Korean Yangneum Sauce', 'Fresh Garlic Peeled', 'Garlic', 4.0, 'verified', 6
    UNION ALL SELECT 'Korean Yangneum Sauce', 'Fresh Ginger',        'Ginger', 2.0, 'verified', 7
    UNION ALL SELECT 'Korean Yangneum Sauce', 'Sesame Seed',         'Sesame seeds', 2.0, 'verified', 8

    -- Cheese Wing Sauce -- Milk confirmed as Fresh Milk by business decision.
    UNION ALL SELECT 'Cheese Wing Sauce', 'Cheese Sauce (house)',       'Cheese sauce', 35.0, 'verified', 1
    UNION ALL SELECT 'Cheese Wing Sauce', 'Cheddar Cheese, Shredded',   'Cheddar cheese', 15.0, 'verified', 2
    UNION ALL SELECT 'Cheese Wing Sauce', 'Fresh Milk',                 'Milk', 10.0, 'verified', 3
    UNION ALL SELECT 'Cheese Wing Sauce', 'Butter',                     'Butter', 5.0, 'verified', 4
  ) v
  JOIN recipe r     ON r.name = v.recipe_name AND r.kind = 'sub_recipe'
  JOIN ingredient ing ON ing.name = v.ingredient_name;

-- -----------------------------------------------------------------------------
-- 4. Five recipe_lines in OTHER (unrelated) menu recipes reference these sauce
--    ingredients directly, at a flat gram quantity that only made sense
--    against the old yield_qty=100 nominal batch (e.g. 35 meant "35% of a
--    100g batch"). Rescaled to the equivalent fraction of one full order
--    under the new yield_qty=1 model, so their actual sauce portion is
--    unchanged -- only the multiplier that expresses it changes. Flagged
--    needs_verification (not verified): the *portion size* these combo items
--    intend is a judgment call carried over from the old model, not itself
--    confirmed by the workbook. Plain UPDATEs match zero rows if the target
--    line does not exist, so these are safe on a fresh database too.
-- -----------------------------------------------------------------------------
UPDATE recipe_line SET qty_base = 0.35, qty_status = 'needs_verification'
 WHERE id IN (
   SELECT rl.id FROM recipe_line rl
     JOIN recipe r ON r.id = rl.recipe_id
     JOIN ingredient i ON i.id = rl.ingredient_id
    WHERE r.name = 'Sulit Deals 99: (3 pcs Wings + Rice)'
      AND i.name = 'Classic Buffalo Sauce' AND rl.qty_base = 35
 );

UPDATE recipe_line SET qty_base = 0.60, qty_status = 'needs_verification'
 WHERE id IN (
   SELECT rl.id FROM recipe_line rl
     JOIN recipe r ON r.id = rl.recipe_id
     JOIN ingredient i ON i.id = rl.ingredient_id
    WHERE r.name = 'Wing Rack 12 pcs (2 flavors) (P339)'
      AND i.name = 'Classic Buffalo Sauce' AND rl.qty_base = 60
 );

UPDATE recipe_line SET qty_base = 0.60, qty_status = 'needs_verification'
 WHERE id IN (
   SELECT rl.id FROM recipe_line rl
     JOIN recipe r ON r.id = rl.recipe_id
     JOIN ingredient i ON i.id = rl.ingredient_id
    WHERE r.name = 'Wings Platter w/ Fries on Side'
      AND i.name = 'Classic Buffalo Sauce' AND rl.qty_base = 60
 );

UPDATE recipe_line SET qty_base = 0.40, qty_status = 'needs_verification'
 WHERE id IN (
   SELECT rl.id FROM recipe_line rl
     JOIN recipe r ON r.id = rl.recipe_id
     JOIN ingredient i ON i.id = rl.ingredient_id
    WHERE r.name LIKE 'Spicy Korean Wing Bowl%'
      AND i.name = 'Korean Yangneum Sauce' AND rl.qty_base = 40
 );

INSERT INTO schema_version (version, name) VALUES (21, 'flavor_recipe_reconciliation_2026_09');
