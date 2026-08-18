-- =============================================================================
-- 0012  Align the database with the Wings Theory Inventory & Recipe Reference
--       (capstone working document), and configure the unlimited system with
--       practical initial values.
--
-- What this migration is, and is not
-- ----------------------------------
-- It is a DATA migration. No table is created or altered except two additive
-- provenance columns on `ingredient`. Every mechanism — BOM resolution, the
-- ledger, FEFO, reconciliation, sessions, refills, order charges — is left
-- exactly as it was; this only changes the numbers those mechanisms operate on.
--
-- The four real defects it corrects
-- ---------------------------------
-- 1. EVERY flavour sub-recipe declared yield_qty = 1000 g while its ingredient
--    lines summed to between 14 g and 200 g. A recipe claiming to produce a
--    kilo of sauce out of 14 g of salt, pepper and garlic powder is not a
--    rounding problem — it made every flavour deduction wrong by a factor of
--    5 to 70. The reference document supplies all fourteen as 100 g master
--    ratios whose ingredients sum to exactly 100 g, so yield and lines now
--    agree and `qty_base * portion / yield_qty` finally means what it says.
--
-- 2. Spicy Korean Wing Bowl consumed 200 g of KOREAN BBQ sauce. The document
--    specifies Korean Yangnyeum scaled to 40 g. Both the flavour and the
--    quantity were wrong, and after fix 1 the 200 g line would have deducted
--    two full sauce batches for one bowl.
--
-- 3. Taiwanese Porkchop had no Shredded Cabbage line although the document
--    lists 30 g and the SKU exists and is stocked.
--
-- 4. White Vinegar is required by two flavour ratios (Mango Habanero 5 g,
--    Panda Express Orange Chicken 10 g) and did not exist as an ingredient.
--
-- What it deliberately does NOT do
-- --------------------------------
-- The six SKUs the document lists as "referenced in recipes but NOT currently
-- in inventory" — Onion, Cucumber, Shredded Cabbage, Fresh Milk, Blueberry,
-- Yogurt — ALL ALREADY EXIST, added by the 2026-08-11 spreadsheet revision,
-- and all are already wired into their recipes. They are re-priced here
-- against the supplied benchmarks, not created. Creating second copies would
-- have produced exactly the SKU-name collision the brief forbids.
--
-- It does not rewrite the main-dish recipes. The brief scopes "use the PDF
-- recipes exactly" to the fourteen flavours; the remaining divergences between
-- the document and the seeded main dishes are reported, not silently applied,
-- because several of them are recipe-composition decisions the kitchen owns.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Cost provenance, stated separately from the existing verification vocabulary
--
-- `unit_cost_status` already distinguishes verified / needs_verification /
-- unset, and v_needs_verification is built on it, so it is left alone. These
-- two columns record what the brief asks for in its own words — where a price
-- came from, and whether anyone has confirmed it with the actual supplier —
-- without redefining a vocabulary the console and its queue already depend on.
-- Backfilled from unit_cost_status so no row is left making a claim about
-- itself that the older column contradicts.
-- -----------------------------------------------------------------------------
ALTER TABLE ingredient ADD COLUMN price_status TEXT NOT NULL DEFAULT 'unset'
  CHECK (price_status IN ('verified', 'estimated', 'unset'));

ALTER TABLE ingredient ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'not_required'
  CHECK (verification_status IN ('confirmed', 'pending_supplier_confirmation', 'not_required'));

UPDATE ingredient SET
  price_status = CASE WHEN unit_cost IS NULL THEN 'unset'
                      WHEN unit_cost_status = 'verified' THEN 'verified'
                      ELSE 'estimated' END,
  verification_status = CASE WHEN unit_cost_status = 'verified' THEN 'confirmed'
                             ELSE 'pending_supplier_confirmation' END;

-- -----------------------------------------------------------------------------
-- White Vinegar — the one genuinely missing ingredient
--
-- Required by Mango Habanero (5 g / 100 g batch) and Panda Express Orange
-- Chicken (10 g). Document Section 2: bottles, 1 bottle = 1000 ml [E], P45.
-- Opens at zero stock with no FEFO batch: nobody has counted any, and seeding
-- a quantity would put an invented number into the balance the whole ledger is
-- reconciled against.
-- -----------------------------------------------------------------------------
-- `WHERE EXISTS (SELECT 1 FROM ingredient)` is load-bearing, not defensive.
-- Migrations run against an EMPTY database on a fresh install — db.reset()
-- migrates before seed.py imports anything — so without that clause this
-- INSERT would put a single row into an empty ingredient table, and seed.run()
-- would then see a non-zero count, print "database already seeded" and refuse
-- to import. Fresh installs get White Vinegar from gapfill.ADDITIONS instead;
-- this branch exists solely to bring an already-populated database up to date.
INSERT INTO ingredient
  (ingredient_id, name, category, kind, base_unit, purchase_unit, pack_size,
   pack_size_status, yield_factor, yield_factor_status, on_hand_base, unit_cost,
   unit_cost_status, lead_time_days, expiry_mode, inventory_type,
   price_status, verification_status)
SELECT 'ING-' || printf('%04d', (SELECT COUNT(*) FROM ingredient) + 1),
       'White Vinegar', 'dry_food', 'raw', 'ml', 'bottles', 1000.0,
       'needs_verification', 1.0, 'needs_verification', 0.0, 45.0,
       'needs_verification', 4, 'fixed_date', 'food_ingredient',
       'estimated', 'pending_supplier_confirmation'
WHERE EXISTS (SELECT 1 FROM ingredient)
  AND NOT EXISTS (SELECT 1 FROM ingredient WHERE name = 'White Vinegar');

-- -----------------------------------------------------------------------------
-- Benchmark prices for the six document-flagged SKUs
--
-- Applied only where the existing purchase unit matches the benchmark's, so a
-- price is never silently attached to a different pack size than the one it was
-- quoted for. Onion already carried P140/kg and is unchanged.
--
-- NOT applied to Blueberry: the stocked SKU is "Blueberry Puree", a 750 ml
-- bottle, and the benchmark is a 500 g frozen pack. Those are different
-- products in different measurement families; converting one to the other
-- would require a density nobody has supplied, and rewriting an ingredient's
-- base_unit while it holds stock and an open FEFO batch is precisely the kind
-- of silent restatement this codebase refuses to make. Reported instead.
-- -----------------------------------------------------------------------------
UPDATE ingredient SET unit_cost = 135.0, unit_cost_status = 'needs_verification',
       price_status = 'estimated', verification_status = 'pending_supplier_confirmation',
       updated_at = datetime('now')
 WHERE name = 'Cucumber';

UPDATE ingredient SET unit_cost = 105.0, unit_cost_status = 'needs_verification',
       price_status = 'estimated', verification_status = 'pending_supplier_confirmation',
       updated_at = datetime('now')
 WHERE name = 'Shredded Cabbage';

UPDATE ingredient SET unit_cost = 130.0, unit_cost_status = 'needs_verification',
       price_status = 'estimated', verification_status = 'pending_supplier_confirmation',
       updated_at = datetime('now')
 WHERE name = 'Fresh Milk';

-- purchase_unit said 'liters' against a gram base unit; the benchmark is per
-- kilo and the pack size is already 1000, so the label is corrected to match
-- what the number actually describes.
UPDATE ingredient SET unit_cost = 688.0, purchase_unit = 'kg',
       unit_cost_status = 'needs_verification',
       price_status = 'estimated', verification_status = 'pending_supplier_confirmation',
       updated_at = datetime('now')
 WHERE name = 'Plain Yogurt';

UPDATE ingredient SET price_status = 'estimated',
       verification_status = 'pending_supplier_confirmation', updated_at = datetime('now')
 WHERE name IN ('Onion', 'Blueberry Puree');

-- -----------------------------------------------------------------------------
-- The fourteen flavour master ratios, per 100 g of finished sauce
--
-- Transcribed from Section 3 of the reference document. Every ratio sums to
-- exactly 100 g, which is what makes yield_qty = 100 honest.
--
-- Three ingredients are stocked in millilitres while the document states them
-- in grams — Evaporated Milk, Condense Milk and Soy Sauce. Their gram figures
-- are recorded 1:1 as millilitres. That is an approximation of roughly 3% for
-- the milks and under 10% for soy sauce, and it is deliberate: units.to_base()
-- correctly refuses a g<->ml conversion without a per-ingredient density, and
-- no density has been supplied. Every line lands as needs_verification anyway,
-- so this sits in the same queue as the rest of the ratio.
--
-- Mango Habanero's "Buffalo/Hot Sauce" is written as an either/or in the
-- document. It stays mapped to the generic Hot Sauce SKU, which is what the
-- existing recipe used, and the two SKUs stay distinct per the brief.
-- -----------------------------------------------------------------------------
CREATE TABLE _flavor_ratio_stage (
  sauce_name      TEXT NOT NULL,
  ingredient_name TEXT NOT NULL,
  qty_base        REAL NOT NULL,
  sort_order      INTEGER NOT NULL
);

INSERT INTO _flavor_ratio_stage (sauce_name, ingredient_name, qty_base, sort_order) VALUES
  -- Classic Buffalo (Regular)
  ('Classic Buffalo Sauce', 'Buffalo Hot Sauce',        70, 1),
  ('Classic Buffalo Sauce', 'Butter',                   25, 2),
  ('Classic Buffalo Sauce', 'Garlic powder',             3, 3),
  ('Classic Buffalo Sauce', 'Honey',                     2, 4),
  -- Mango Habanero (Regular)
  ('Mango Habanero Sauce',  'Mango',                    50, 1),
  ('Mango Habanero Sauce',  'Hot Sauce',                20, 2),
  ('Mango Habanero Sauce',  'Honey',                    20, 3),
  ('Mango Habanero Sauce',  'Chili Flakes Dehydrated',   5, 4),
  ('Mango Habanero Sauce',  'White Vinegar',             5, 5),
  -- Salted Egg (Regular)
  ('Salted Egg Sauce',      'Salted Eggs',              35, 1),
  ('Salted Egg Sauce',      'Evaporated Milk',          25, 2),
  ('Salted Egg Sauce',      'Butter',                   20, 3),
  ('Salted Egg Sauce',      'Condense Milk',            10, 4),
  ('Salted Egg Sauce',      'Garlic Flakes',            10, 5),
  -- Lemon Glazed (Regular)
  ('Lemon Glazed Sauce',    'Lemon',                    25, 1),
  ('Lemon Glazed Sauce',    'Honey',                    35, 2),
  ('Lemon Glazed Sauce',    'Butter',                   20, 3),
  ('Lemon Glazed Sauce',    'Soy Sauce',                10, 4),
  ('Lemon Glazed Sauce',    'Cornstarch',               10, 5),
  -- Original (Regular, dry rub)
  ('Original Dry Rub',      'Iodized Salt',             25, 1),
  ('Original Dry Rub',      'Ground Pepper',            15, 2),
  ('Original Dry Rub',      'Garlic powder',            25, 3),
  ('Original Dry Rub',      'Paprika',                  20, 4),
  ('Original Dry Rub',      'Cornstarch',               15, 5),
  -- Korean BBQ (Regular)
  ('Korean BBQ Sauce',      'Gochujang Big',            35, 1),
  ('Korean BBQ Sauce',      'Soy Sauce',                20, 2),
  ('Korean BBQ Sauce',      'Honey',                    25, 3),
  ('Korean BBQ Sauce',      'Fresh Garlic Peeled',      10, 4),
  ('Korean BBQ Sauce',      'Sesame Seed',              10, 5),
  -- Garlic Parmesan (Regular)
  ('Garlic Parmesan Sauce', 'Butter',                   40, 1),
  ('Garlic Parmesan Sauce', 'Fresh Garlic Peeled',      25, 2),
  ('Garlic Parmesan Sauce', 'Parmesan Cheese',          25, 3),
  ('Garlic Parmesan Sauce', 'Parsley',                   5, 4),
  ('Garlic Parmesan Sauce', 'Iodized Salt',              5, 5),
  -- Soy Garlic (Regular)
  ('Soy Garlic Sauce',      'Soy Sauce',                40, 1),
  ('Soy Garlic Sauce',      'Fresh Garlic Peeled',      25, 2),
  ('Soy Garlic Sauce',      'Honey',                    25, 3),
  ('Soy Garlic Sauce',      'Butter',                   10, 4),
  -- Teriyaki (Regular)
  ('Teriyaki Sauce',        'Soy Sauce',                45, 1),
  ('Teriyaki Sauce',        'Honey',                    30, 2),
  ('Teriyaki Sauce',        'Fresh Garlic Peeled',      10, 3),
  ('Teriyaki Sauce',        'Cornstarch',               10, 4),
  ('Teriyaki Sauce',        'Sesame Seed',               5, 5),
  -- Honey Sriracha (Regular)
  ('Honey Sriracha Sauce',  'Honey',                    50, 1),
  ('Honey Sriracha Sauce',  'Sriracha',                 35, 2),
  ('Honey Sriracha Sauce',  'Butter',                   15, 3),
  -- Snow Cheese (Premium)
  ('Snow Cheese Sauce',     'Cheese Powder',            35, 1),
  ('Snow Cheese Sauce',     'Condense Milk',            25, 2),
  ('Snow Cheese Sauce',     'Evaporated Milk',          20, 3),
  ('Snow Cheese Sauce',     'Butter',                   20, 4),
  -- Panda Express Orange Chicken (Premium)
  ('Orange Chicken Sauce',  'Orange',                   30, 1),
  ('Orange Chicken Sauce',  'Soy Sauce',                20, 2),
  ('Orange Chicken Sauce',  'Refined Sugar',            25, 3),
  ('Orange Chicken Sauce',  'White Vinegar',            10, 4),
  ('Orange Chicken Sauce',  'Cornstarch',               10, 5),
  ('Orange Chicken Sauce',  'Fresh Garlic Peeled',       5, 6),
  -- Korean Yangnyeum (Premium)
  ('Korean Yangneum Sauce', 'Gochujang Big',            40, 1),
  ('Korean Yangneum Sauce', 'Honey',                    30, 2),
  ('Korean Yangneum Sauce', 'Soy Sauce',                15, 3),
  ('Korean Yangneum Sauce', 'Fresh Garlic Peeled',      10, 4),
  ('Korean Yangneum Sauce', 'Sesame Seed',               5, 5),
  -- Cheese (Premium)
  ('Cheese Wing Sauce',     'Cheese Bars',              45, 1),
  ('Cheese Wing Sauce',     'Cheese Powder',            20, 2),
  ('Cheese Wing Sauce',     'Evaporated Milk',          25, 3),
  ('Cheese Wing Sauce',     'Butter',                   10, 4);

-- Guard: if any staged ingredient name fails to resolve, the migration must
-- ABORT rather than silently write a flavour that is short an ingredient —
-- that is the same class of bug as the 1000 g yield defect above.
--
-- The abort is a real constraint violation, not a logged row: _flavor_guard
-- has a NOT NULL column and this statement tries to write NULL into it for
-- every unresolved name. Zero unresolved names selects zero rows and the
-- INSERT is a no-op. An earlier draft "guarded" by inserting a marker row
-- into schema_version, which aborts nothing and poisons MAX(version) so no
-- later migration would ever run again.
--
-- On an empty database (fresh install) nothing resolves, so the guard is
-- scoped to skip entirely when there are no ingredients yet.
CREATE TABLE _flavor_guard (unresolved_ingredient TEXT NOT NULL);

INSERT INTO _flavor_guard (unresolved_ingredient)
SELECT NULL
  FROM _flavor_ratio_stage s
 WHERE EXISTS (SELECT 1 FROM ingredient)
   AND NOT EXISTS (SELECT 1 FROM ingredient i WHERE i.name = s.ingredient_name);

DROP TABLE _flavor_guard;

DELETE FROM recipe_line
 WHERE recipe_id IN (SELECT r.id FROM recipe r
                       JOIN ingredient i ON i.id = r.produces_ingredient_id
                      WHERE i.name IN (SELECT DISTINCT sauce_name FROM _flavor_ratio_stage));

INSERT INTO recipe_line
  (recipe_id, ingredient_id, display_label, qty_base, qty_status, is_optional, sort_order, note)
SELECT r.id, ing.id, s.ingredient_name, s.qty_base, 'needs_verification', 0, s.sort_order,
       'Section 3 master ratio, per 100 g finished sauce — proposed, pending kitchen validation'
  FROM _flavor_ratio_stage s
  JOIN ingredient sauce ON sauce.name = s.sauce_name
  JOIN recipe     r     ON r.produces_ingredient_id = sauce.id
  JOIN ingredient ing   ON ing.name = s.ingredient_name;

-- yield and lines now agree: 100 g in, 100 g out.
UPDATE recipe SET yield_qty = 100.0, yield_unit = 'g', updated_at = datetime('now')
 WHERE produces_ingredient_id IN (SELECT id FROM ingredient
                                   WHERE name IN (SELECT DISTINCT sauce_name FROM _flavor_ratio_stage));

DROP TABLE _flavor_ratio_stage;

-- -----------------------------------------------------------------------------
-- Defect 2: Spicy Korean Wing Bowl referenced the wrong sauce, in the wrong
-- quantity. Document: "Korean Yangnyeum Sauce (Sec. 3 ratio, scaled to 40 g)".
-- -----------------------------------------------------------------------------
UPDATE recipe_line
   SET ingredient_id = (SELECT id FROM ingredient WHERE name = 'Korean Yangneum Sauce'),
       qty_base      = 40.0,
       display_label = 'Korean Yangnyeum Sauce',
       qty_status    = 'needs_verification',
       note          = 'corrected in 0012: was Korean BBQ Sauce 200 g'
 WHERE recipe_id = (SELECT id FROM recipe WHERE name LIKE 'Spicy Korean Wing Bowl%')
   AND ingredient_id = (SELECT id FROM ingredient WHERE name = 'Korean BBQ Sauce');

-- -----------------------------------------------------------------------------
-- Defect 3: Taiwanese Porkchop is served with shredded cabbage (document lists
-- 30 g) and the SKU is stocked, but the recipe had no line for it.
-- -----------------------------------------------------------------------------
INSERT OR IGNORE INTO recipe_line
  (recipe_id, ingredient_id, display_label, qty_base, qty_status, is_optional, sort_order, note)
SELECT r.id, i.id, 'Shredded Cabbage', 30.0, 'needs_verification', 0, 90,
       'added in 0012 from the reference document — the SKU was stocked but unreferenced'
  FROM recipe r, ingredient i
 WHERE r.name LIKE 'Taiwanese Porkchop%' AND i.name = 'Shredded Cabbage'
   AND NOT EXISTS (SELECT 1 FROM recipe_line rl
                    WHERE rl.recipe_id = r.id AND rl.ingredient_id = i.id);

-- -----------------------------------------------------------------------------
-- Unlimited flavour configuration
--
-- display_name takes the document's customer-facing flavour name; the
-- ingredient SKU name is untouched. flavor_type maps the document's own
-- Regular/Premium column onto included/special.
--
-- The four premium prices are INITIAL SYSTEM RECOMMENDATIONS, not
-- business-supplied figures. They stay editable by any user holding
-- recipe.write, through the console's Flavors card.
-- -----------------------------------------------------------------------------
UPDATE unli_flavor SET flavor_type = 'included', addon_price = 0, addon_menu_item_id = NULL,
       updated_by = 'migration 0012', updated_at = datetime('now');

UPDATE unli_flavor SET display_name = 'Classic Buffalo'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Classic Buffalo Sauce');
UPDATE unli_flavor SET display_name = 'Mango Habanero'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Mango Habanero Sauce');
UPDATE unli_flavor SET display_name = 'Salted Egg'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Salted Egg Sauce');
UPDATE unli_flavor SET display_name = 'Lemon Glazed'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Lemon Glazed Sauce');
UPDATE unli_flavor SET display_name = 'Original'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Original Dry Rub');
UPDATE unli_flavor SET display_name = 'Korean BBQ'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Korean BBQ Sauce');
UPDATE unli_flavor SET display_name = 'Garlic Parmesan'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Garlic Parmesan Sauce');
UPDATE unli_flavor SET display_name = 'Soy Garlic'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Soy Garlic Sauce');
UPDATE unli_flavor SET display_name = 'Teriyaki'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Teriyaki Sauce');
UPDATE unli_flavor SET display_name = 'Honey Sriracha'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Honey Sriracha Sauce');

UPDATE unli_flavor SET display_name = 'Snow Cheese', flavor_type = 'special', addon_price = 30.0
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Snow Cheese Sauce');
UPDATE unli_flavor SET display_name = 'Panda Express Orange Chicken', flavor_type = 'special', addon_price = 15.0
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Orange Chicken Sauce');
UPDATE unli_flavor SET display_name = 'Korean Yangnyeum', flavor_type = 'special', addon_price = 20.0
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Korean Yangneum Sauce');
UPDATE unli_flavor SET display_name = 'Cheese', flavor_type = 'special', addon_price = 30.0
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Cheese Wing Sauce');

UPDATE unli_flavor SET sort_order = (
  SELECT COUNT(*) FROM unli_flavor f2 WHERE f2.display_name <= unli_flavor.display_name);

-- -----------------------------------------------------------------------------
-- Unlimited portion settings
--
-- The 10 g-per-piece figure introduced in 0011 was derived from the Wing Rack
-- BOM and is superseded. The reference document sizes a 3-piece portion at
-- 35-40 g of finished sauce (Sulit Deal 99), so the unlimited default is 40 g
-- PER HEAD PER REFILL — one head, one round, 3 wings, 40 g of sauce.
--
-- The old key is dropped rather than left behind: two settings that both claim
-- to size the same deduction is how one of them silently goes stale.
-- -----------------------------------------------------------------------------
DELETE FROM app_setting WHERE key = 'unli_sauce_g_per_piece';
INSERT INTO app_setting (key, value, updated_by)
VALUES ('unli_sauce_g_per_head', '40', 'migration 0012')
ON CONFLICT(key) DO UPDATE SET value = '40', updated_by = 'migration 0012',
                               updated_at = datetime('now');

-- Pieces per head is the shop's stated business rule and is unchanged.
UPDATE app_setting SET value = '3' WHERE key = 'unli_pieces_per_head';

INSERT INTO schema_version (version, name) VALUES (12, 'pdf_reference_alignment');
