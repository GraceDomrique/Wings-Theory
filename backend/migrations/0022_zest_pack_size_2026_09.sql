-- =============================================================================
-- 0022  Give Lemon Zest / Orange Zest a real, honest pack size so they can
-- actually be deducted -- fixing a live-service bug migration 0021 introduced
-- as a side effect.
--
-- Background: migration 0021 added these two as raw ingredients with
-- pack_size_status='unset' and pack_size=NULL, on purpose -- the workbook's
-- own methodology sheet says they are "not a separately purchased SKU"
-- (shaved from whole lemons/oranges, which ARE listed with their own price),
-- and the instruction for that migration was explicit: do not derive a
-- fabricated pack cost from the whole-fruit price.
--
-- What that migration's author did not chase to its consequence:
-- v_ingredient_readiness.is_deductable is FALSE for any raw ingredient with
-- pack_size_status='unset' (see that view), and expand_recipe()/
-- _refill_requirements() refuse to deduct a recipe with even one
-- non-deductable leaf. Lemon Zest is a leaf of "Lemon Glazed Sauce" and
-- Orange Zest a leaf of "Orange Chicken Sauce" -- so as of migration 0021,
-- BOTH flavors are unorderable in the live POS: any attempt to serve them
-- raises InsufficientStockError. This was caught while replaying real
-- order history that happens to select those flavors, not by inspection --
-- see the change report for how it surfaced.
--
-- Fix, without fabricating anything: track both directly in the unit they
-- are already consumed in (grams) with pack_size = 1 gram. This is not an
-- invented conversion ratio -- "1 gram per gram" carries no assumption --
-- it just means receiving events for these two are entered in whole grams
-- rather than a purchased pack, which is operationally exactly how a prep
-- item shaved from a garnish fruit is actually restocked (weighed, not
-- counted in boxes). unit_cost stays NULL/'unset' -- genuinely unknown,
-- and unlike pack_size it does not gate is_deductable (see the view), so
-- leaving it unset does not reintroduce the same bug for a different
-- reason. purchase_unit is set to 'g' to match, replacing the placeholder
-- 'g' string 0021 already used there for want of a real purchase unit.
--
-- Guarded exactly like every ingredient-data migration before it: matches
-- zero rows on a fresh/test database (WHERE name IN (...) AND
-- pack_size_status = 'unset' means it only ever touches the specific two
-- rows 0021 created, and only while they are still in the state 0021 left
-- them in -- a second run, or a database where an admin already fixed this
-- by hand, changes nothing).
-- -----------------------------------------------------------------------------
UPDATE ingredient
   SET pack_size = 1.0,
       pack_size_status = 'verified',
       purchase_unit = 'g',
       updated_at = datetime('now')
 WHERE name IN ('Lemon Zest', 'Orange Zest')
   AND pack_size_status = 'unset';

INSERT INTO schema_version (version, name) VALUES (22, 'zest_pack_size_2026_09');
