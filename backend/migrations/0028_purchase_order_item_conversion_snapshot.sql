-- =============================================================================
-- 0028  Freeze pack_size/yield_factor onto purchase_order_item at PO creation.
--
-- purchase_order_item already snapshots purchase_unit (migration 0026) so a
-- later unit rename can never reinterpret history, but it did not snapshot
-- the pack_size/yield_factor used to convert the ordered purchase-unit
-- quantity into base units. wt/purchasing.py re-read those two columns live
-- from `ingredient` every time it needed "how much was ordered, in base
-- units" (receive_line's over-receipt guard and _line_fully_received's
-- completion check) -- so a pack_size correction made between PO creation
-- and receiving (e.g. a Needs Verification fix) silently changed what
-- "fully received" means for an order already placed on the old terms.
--
-- This does not affect what receive_line() actually credits to
-- ingredient.on_hand_base: the admin's entered qty_base is already in base
-- units, and reservation._receive_stock_locked (the one place stock is
-- physically credited, untouched by this migration) is intentionally left
-- reading the ingredient's current, true conversion for that -- receiving
-- stock should reflect today's real-world pack size, not a stale one. Only
-- the paperwork question of "how much did this PO order, and is it done" is
-- frozen here.
--
-- Nullable, backward-safe: existing rows (today, zero in production) are
-- backfilled from the ingredient's current values, the closest available
-- truth for orders that predate this column existing.
-- -----------------------------------------------------------------------------
ALTER TABLE purchase_order_item ADD COLUMN pack_size REAL;
ALTER TABLE purchase_order_item ADD COLUMN yield_factor REAL;

UPDATE purchase_order_item
   SET pack_size    = (SELECT pack_size    FROM ingredient WHERE ingredient.id = purchase_order_item.ingredient_id),
       yield_factor = (SELECT yield_factor FROM ingredient WHERE ingredient.id = purchase_order_item.ingredient_id)
 WHERE pack_size IS NULL;

INSERT INTO schema_version (version, name) VALUES (28, 'purchase_order_item_conversion_snapshot');
