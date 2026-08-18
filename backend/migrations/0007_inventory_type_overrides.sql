-- =============================================================================
-- 0007  inventory_type (added in 0005) was never actually set away from its
-- 'food_ingredient' default for anything — this fills in the two named
-- carve-outs the owner asked for:
--
--   operational_supply: items with zero recipe_line references anywhere.
--   Nothing has ever deducted them through a sale, so "No Sales Yet" was a
--   lie for every one of them, same as White Eggs' expiry story in 0005 —
--   manual usage (record_usage / POST .../use) is the only real signal.
--   Parchment Paper, Shake Straw and Tissue Interfold are deliberately NOT
--   included here even though they're the same physical kind of item —
--   they DO have real recipe_line rows and therefore real sale-driven
--   consumption today, so calling them operational_supply would make
--   ingredient_depletion() read the wrong ledger reason and report them as
--   never consumed.
--
--   sellable_product: canned drinks sold directly rather than consumed
--   inside a recipe. Doesn't change how they're deducted (only
--   operational_supply changes the ledger reason forecasting looks for) —
--   this is a console label correction, not a behaviour change.
-- =============================================================================

UPDATE ingredient SET inventory_type = 'operational_supply'
 WHERE name IN ('Takeout Box', 'Takeout Cups', 'Takeout Plastic Bags', 'Takeout Paper Bags',
                'Thermal Paper', 'Tissue Rolls', 'Toothpicks', 'Plastic Gloves',
                'Trash Bags', 'Alcohol')
   AND is_active = 1;

UPDATE ingredient SET inventory_type = 'sellable_product'
 WHERE name IN ('Coke in Can', 'Coke Zero in Can', 'Sprite in Can', 'Royal in Can')
   AND is_active = 1;

INSERT INTO schema_version (version, name) VALUES (7, 'inventory_type_overrides');
