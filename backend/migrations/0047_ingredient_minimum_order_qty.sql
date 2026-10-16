-- =============================================================================
-- 0047  minimum_order_qty: a supplier-imposed floor on how small a single
--       reorder may be, in the ingredient's own purchase unit (e.g. Chicken
--       Wings: pack/order unit 10 kg, minimum order 20 kg -> 2 packs). NULL
--       means no floor is known -- the replenishment engine (wt/depletion.py
--       ingredient_forecast()) then rounds a shortfall up to a whole pack
--       only, same as before this migration.
--
-- Additive, matches the existing pack_size/lead_time_days/reorder_point_base
-- pattern: free-text supplier already exists (migration 0001), lead time and
-- pack size already exist -- this is the one supplier constraint from the
-- replenishment spec ("Minimum order: 20 kg") that had no column yet.
-- =============================================================================

ALTER TABLE ingredient ADD COLUMN minimum_order_qty REAL;

INSERT INTO schema_version (version, name) VALUES (47, 'ingredient_minimum_order_qty');
