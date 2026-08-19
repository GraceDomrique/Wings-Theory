-- =============================================================================
-- 0015  Main-dish recipe corrections from the 2026-08 revision of the Wings
--       Theory Inventory & Recipe Reference, plus Banana's unit of measure.
--
-- Migration 0012 aligned the fourteen flavour sauces, the six previously-
-- missing SKUs and two specific recipe defects (Spicy Korean Wing Bowl's
-- sauce, Taiwanese Porkchop's cabbage) with the reference document, but
-- deliberately left the rest of the main-dish recipes alone: "the remaining
-- divergences between the document and the seeded main dishes are reported,
-- not silently applied, because several of them are recipe-composition
-- decisions the kitchen owns." The owner has since reviewed that specific
-- list and asked for it to be applied — this migration is that list.
--
-- Every correction below is a documented quantity, a wrong stock item, a
-- missing line, or a line the document does not carry at all. None of it
-- touches the fourteen flavour ratios, the flavour configuration, or the
-- unlimited-portion settings — 0012 already settled those.
--
-- On a fresh install this runs against an empty database and every
-- statement below matches zero rows (there is nothing yet to correct);
-- seed.py's reference.apply_configuration() carries the equivalent Python
-- logic for that path, and both converge on the same state.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Banana: convert from gram-based (seeded at 100 g/piece via
-- gapfill.PIECE_WEIGHTS) to piece-based. The document is explicit: "Banana
-- stays in whole pieces end-to-end (purchase, stock, recipe, deduction)...
-- [the estimated average weight] never overrides the piece count." Every
-- quantity that meant "grams" under the old scheme is divided by the same
-- 100 g/piece factor the ingredient was seeded with, so "how many bananas"
-- reads the same before and after — only the unit changes. Ledger history
-- is untouched (it is append-only by design); entries written before this
-- migration remain in grams, the correct record of what the ledger said at
-- the time a unit-of-measure change like this cannot rewrite.
-- -----------------------------------------------------------------------------
UPDATE ingredient
   SET base_unit = 'pc',
       pack_size = 1.0,
       pack_size_status = 'verified',
       on_hand_base = on_hand_base / 100.0,
       reserved_base = reserved_base / 100.0,
       updated_at = datetime('now')
 WHERE name = 'Banana' AND base_unit = 'g';

UPDATE fefo_batch
   SET qty_base = qty_base / 100.0
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Banana')
   AND state = 'open';

UPDATE reservation
   SET qty_base = qty_base / 100.0
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Banana')
   AND state IN ('held', 'committed');

-- Banana Milkshake deducted 120 g per order — a converted weight, exactly
-- what the document forbids. The correct figure is not a proportional
-- rescale of 120 g; it is the stated business rule, one whole piece.
UPDATE recipe_line
   SET qty_base = 1.0, qty_status = 'needs_verification',
       note = 'corrected from the reference document (2026-08): deduct exactly '
              || '1 whole banana, never a converted weight'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Banana')
   AND recipe_id IN (SELECT id FROM recipe WHERE name LIKE 'Banana MilkShake%');

-- -----------------------------------------------------------------------------
-- Staged corrections. Each stage table is a plain transcription of Section 4
-- against the currently-seeded recipes; the guard below aborts the whole
-- migration if any staged name fails to resolve, rather than silently
-- applying part of the list.
-- -----------------------------------------------------------------------------
CREATE TABLE _qty_fix_stage (
  recipe_like     TEXT NOT NULL,
  ingredient_name TEXT NOT NULL,
  new_qty         REAL NOT NULL
);
INSERT INTO _qty_fix_stage (recipe_like, ingredient_name, new_qty) VALUES
  ('Spicy Tuna Linguine Pesto%',            'Pesto Jar',                            40.0),
  ('Creamy Sausage Pasta%',                 'Hugarian Sausage',                     80.0),
  ('Creamy Chicken & Mushroom Alfredo%',    'Chicken Breast/Thigh (raw, boneless)', 100.0),
  ('Spicy Korean Wing Bowl%',               'Wings',                               300.0),
  ('Spicy Korean Wing Bowl%',               'Kimchi',                               50.0),
  ('Spicy Korean Wing Bowl%',               'Cucumber',                             30.0),
  ('Chicken Salpicao%',                     'Chicken Breast/Thigh (raw, boneless)', 180.0),
  ('Chicken Salpicao%',                     'Butter',                               15.0),
  ('Chicken Salpicao%',                     'Soy Sauce',                            10.0),
  ('Chicken Salpicao%',                     'Worchestershire',                       5.0),
  ('Chicken Salpicao%',                     'Ground Pepper',                         2.0),
  ('Hungarian Sausage (Breakfast)%',        'Palm Oil',                              5.0),
  ('Pork Tocino (Breakfast)%',              'Palm Oil',                              5.0),
  ('Spam & Egg%',                           'Spam',                                100.0),
  ('Spam & Egg%',                           'Palm Oil',                              5.0),
  ('Bacon & Egg%',                          'Bacon',                                60.0),
  ('Bacon & Egg%',                          'Palm Oil',                              5.0),
  ('Onion Rings%',                          'Flour',                                20.0),
  ('Onion Rings%',                          'White Eggs',                            0.25),
  ('Fish & Chips%',                         'Flour',                                20.0),
  ('Banana MilkShake%',                     'Fresh Milk',                          100.0),
  ('Banana MilkShake%',                     'Tube Ice',                            150.0),
  ('Banana MilkShake%',                     'Simple Syrup (house)',                 15.0),
  ('Blueberry Yogurt%',                     'Plain Yogurt',                        150.0),
  ('Blueberry Yogurt%',                     'Tube Ice',                            100.0),
  ('1 Pitcher Iced Tea%',                   'Red Tea Powder',                       25.0),
  ('1 Pitcher Iced Tea%',                   'Refined Sugar',                        40.0),
  ('1 Cup Rice%',                           'Rice',                                180.0),
  ('Ranch Dip%',                            'Mayonaise',                            25.0);

CREATE TABLE _add_stage (
  recipe_like     TEXT NOT NULL,
  ingredient_name TEXT NOT NULL,
  qty             REAL NOT NULL,
  label           TEXT NOT NULL
);
INSERT INTO _add_stage (recipe_like, ingredient_name, qty, label) VALUES
  ('Hungarian Sausage (Breakfast)%', 'Iodized Salt',    1.0,  'Iodized Salt'),
  ('Hungarian Sausage (Breakfast)%', 'Ground Pepper',   1.0,  'Ground Pepper'),
  ('Pork Tocino (Breakfast)%',       'Iodized Salt',    1.0,  'Iodized Salt'),
  ('Pork Tocino (Breakfast)%',       'Ground Pepper',   1.0,  'Ground Pepper'),
  ('Onion Rings%',                  'Cornstarch',      10.0,  'Cornstarch'),
  ('Onion Rings%',                  'Parchment Paper',  1.0,  'Parchment Paper'),
  ('Fish & Chips%',                 'Cornstarch',      15.0,  'Cornstarch'),
  ('Fish & Chips%',                 'White Eggs',       0.5,  'Egg (beaten)'),
  ('Fish & Chips%',                 'Parchment Paper',  1.0,  'Parchment Paper'),
  ('Wing Rack%',                    'Cornstarch',      60.0,  'Cornstarch'),
  ('Wing Rack%',                    'Flour',           40.0,  'All-Purpose Flour'),
  ('Wing Rack%',                    'Iodized Salt',     6.0,  'Iodized Salt'),
  ('Wing Rack%',                    'Ground Pepper',    4.0,  'Ground Pepper'),
  ('Wing Rack%',                    'Garlic powder',    4.0,  'Garlic Powder'),
  ('Wing Rack%',                    'Butter',          30.0,  'Butter'),
  ('Wing Rack%',                    'Parsley',          2.0,  'Parsley (optional garnish)'),
  ('Wing Rack%',                    'Tissue Interfold', 3.0,  'Tissue'),
  ('Wing Rack%',                    'Parchment Paper',  2.0,  'Parchment Paper'),
  ('Ranch Dip%',                    'Ground Pepper',    1.0,  'Ground Pepper');

CREATE TABLE _remove_stage (
  recipe_like     TEXT NOT NULL,
  ingredient_name TEXT NOT NULL
);
INSERT INTO _remove_stage (recipe_like, ingredient_name) VALUES
  ('Chicken Salpicao%', 'Mama Sitas Oyster Sauce Big'),
  ('Chicken Salpicao%', 'Iodized Salt'),
  ('Ranch Dip%',        'All purpose Cream'),
  ('Ranch Dip%',        'Iodized Salt');

-- Guard: abort if a staged ingredient name does not resolve, OR a staged
-- recipe pattern matches no recipe — on a database that actually has
-- ingredients/recipes to check (a fresh install has none of either yet, so
-- the guard is scoped to skip entirely there, same as migration 0012's).
CREATE TABLE _correction_guard (problem TEXT NOT NULL);

INSERT INTO _correction_guard (problem)
SELECT 'unknown ingredient: ' || ingredient_name FROM (
  SELECT ingredient_name FROM _qty_fix_stage
  UNION SELECT ingredient_name FROM _add_stage
  UNION SELECT ingredient_name FROM _remove_stage
) s
WHERE EXISTS (SELECT 1 FROM ingredient)
  AND NOT EXISTS (SELECT 1 FROM ingredient i WHERE i.name = s.ingredient_name);

INSERT INTO _correction_guard (problem)
SELECT 'no recipe matches: ' || recipe_like FROM (
  SELECT DISTINCT recipe_like FROM _qty_fix_stage
  UNION SELECT DISTINCT recipe_like FROM _add_stage
  UNION SELECT DISTINCT recipe_like FROM _remove_stage
) s
WHERE EXISTS (SELECT 1 FROM ingredient)
  AND NOT EXISTS (SELECT 1 FROM recipe r WHERE r.name LIKE s.recipe_like);

DROP TABLE _correction_guard;

-- -----------------------------------------------------------------------------
-- Apply
-- -----------------------------------------------------------------------------
UPDATE recipe_line
   SET qty_base = (
         SELECT s.new_qty FROM _qty_fix_stage s
           JOIN ingredient i ON i.name = s.ingredient_name
           JOIN recipe     r ON r.name LIKE s.recipe_like
          WHERE i.id = recipe_line.ingredient_id AND r.id = recipe_line.recipe_id
       ),
       qty_status = 'needs_verification',
       note = 'corrected from the reference document (2026-08)'
 WHERE EXISTS (
         SELECT 1 FROM _qty_fix_stage s
           JOIN ingredient i ON i.name = s.ingredient_name
           JOIN recipe     r ON r.name LIKE s.recipe_like
          WHERE i.id = recipe_line.ingredient_id AND r.id = recipe_line.recipe_id
       );

-- Two lines named the wrong stock item outright, redirected in place (same
-- technique as 0012's Korean Bowl fix) so the correction is visible on the
-- row rather than a delete-then-insert.
UPDATE recipe_line
   SET ingredient_id = (SELECT id FROM ingredient WHERE name = 'Balducci Penne Pasta'),
       qty_base = 120.0, display_label = 'Balducci Penne Pasta',
       qty_status = 'needs_verification',
       note = 'corrected from the reference document (2026-08): was generic Pasta'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Pasta')
   AND recipe_id IN (SELECT id FROM recipe WHERE name LIKE 'Creamy Sausage Pasta%');

UPDATE recipe_line
   SET ingredient_id = (SELECT id FROM ingredient WHERE name = 'Pasta'),
       qty_base = 120.0, display_label = 'Pasta (generic dry)',
       qty_status = 'needs_verification',
       note = 'corrected from the reference document (2026-08): was Rice — this is a pasta dish'
 WHERE ingredient_id = (SELECT id FROM ingredient WHERE name = 'Rice')
   AND recipe_id IN (SELECT id FROM recipe
                       WHERE name LIKE 'Creamy Chicken & Mushroom Alfredo%');

INSERT OR IGNORE INTO recipe_line
  (recipe_id, ingredient_id, display_label, qty_base, qty_status, is_optional, sort_order, note)
SELECT r.id, i.id, s.label, s.qty, 'needs_verification', 0, 91,
       'added from the reference document (2026-08)'
  FROM _add_stage s
  JOIN ingredient i ON i.name = s.ingredient_name
  JOIN recipe     r ON r.name LIKE s.recipe_like
 WHERE NOT EXISTS (SELECT 1 FROM recipe_line rl
                     WHERE rl.recipe_id = r.id AND rl.ingredient_id = i.id);

DELETE FROM recipe_line
 WHERE EXISTS (
   SELECT 1 FROM _remove_stage s
     JOIN ingredient i ON i.name = s.ingredient_name
     JOIN recipe     r ON r.name LIKE s.recipe_like
    WHERE i.id = recipe_line.ingredient_id AND r.id = recipe_line.recipe_id
 );

DROP TABLE _qty_fix_stage;
DROP TABLE _add_stage;
DROP TABLE _remove_stage;

INSERT INTO schema_version (version, name) VALUES (15, 'recipe_bank_corrections_2026_08');
