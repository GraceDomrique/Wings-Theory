-- =============================================================================
-- 0005  Three real gaps the ingredient model didn't distinguish:
--
--   1. Not every stocked thing is sold. Tissue, alcohol, gloves and garbage
--      bags never pass through the POS, so "no sales yet" was a lie every
--      time it was shown for one of them. inventory_type says which kind of
--      thing this row is, so the console can ask the right question:
--      "how many sold" for food/products, "how many used" for supplies.
--
--   2. Not every ingredient has a real expiry date. A sack of rice, a fresh
--      mango and a carton of eggs all get treated identically today — every
--      batch either has a verified date or is nagging the owner to supply
--      one. expiry_mode + shelf_life_days let a batch be tracked by an
--      *estimated* shelf life (fresh produce) or not tracked for expiry at
--      all (eggs, by default) without that being a data-entry failure.
--
--   3. There was no ceiling on how much of something to hold, so "how much
--      should I buy" had no upper bound to check against. max_stock_base is
--      optional and enforced nowhere yet but by the console's own UI warning
--      — it is guidance, not a hard limit on receiving.
--
-- supplier_contact is additive: 'supplier' already existed as a free-text
-- name; this is a second free-text field for how to actually reach them
-- (phone/email/whatever the owner writes down), not a new structured entity.
-- =============================================================================

ALTER TABLE ingredient ADD COLUMN inventory_type TEXT NOT NULL DEFAULT 'food_ingredient'
  CHECK (inventory_type IN ('food_ingredient', 'sellable_product', 'operational_supply'));

ALTER TABLE ingredient ADD COLUMN expiry_mode TEXT NOT NULL DEFAULT 'fixed_date'
  CHECK (expiry_mode IN ('fixed_date', 'shelf_life_estimate', 'none'));

ALTER TABLE ingredient ADD COLUMN shelf_life_days INTEGER
  CHECK (shelf_life_days IS NULL OR shelf_life_days > 0);

ALTER TABLE ingredient ADD COLUMN max_stock_base REAL
  CHECK (max_stock_base IS NULL OR max_stock_base > 0);

ALTER TABLE ingredient ADD COLUMN supplier_contact TEXT;

-- stock_ledger needs a new reason ('operational_use', for a supply that was
-- consumed rather than sold or wasted) but SQLite cannot ALTER a CHECK
-- constraint in place, so the table is rebuilt under the same name. This is
-- the one place append-only really matters, so the two triggers that
-- enforce it are recreated identically afterward, not loosened.
CREATE TABLE stock_ledger_new (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  ingredient_id      INTEGER NOT NULL REFERENCES ingredient(id) ON DELETE RESTRICT,
  delta_base         REAL    NOT NULL,
  balance_after_base REAL    NOT NULL,
  reason             TEXT    NOT NULL CHECK (reason IN (
                       'purchase_receipt', 'sale_commit', 'void_credit', 'wastage',
                       'spoilage', 'count_adjustment', 'transfer',
                       'unli_provisional', 'unli_reconcile', 'operational_use')),
  wastage_reason     TEXT,
  ref_type           TEXT,
  ref_id             TEXT,
  user_id            TEXT,
  note               TEXT,
  created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO stock_ledger_new
  (id, ingredient_id, delta_base, balance_after_base, reason, wastage_reason,
   ref_type, ref_id, user_id, note, created_at)
  SELECT id, ingredient_id, delta_base, balance_after_base, reason, wastage_reason,
         ref_type, ref_id, user_id, note, created_at
    FROM stock_ledger;
DROP TABLE stock_ledger;
ALTER TABLE stock_ledger_new RENAME TO stock_ledger;

CREATE INDEX ix_ledger_ingredient ON stock_ledger (ingredient_id, created_at);
CREATE INDEX ix_ledger_ref        ON stock_ledger (ref_type, ref_id);

CREATE TRIGGER trg_ledger_no_update BEFORE UPDATE ON stock_ledger
BEGIN SELECT RAISE(ABORT, 'stock_ledger is append-only'); END;
CREATE TRIGGER trg_ledger_no_delete BEFORE DELETE ON stock_ledger
BEGIN SELECT RAISE(ABORT, 'stock_ledger is append-only'); END;

-- The owner named these explicitly (fresh produce -> an estimated shelf
-- life instead of a fixed date; eggs -> not tracked for expiry unless a
-- real date shows up). Every other ingredient keeps today's behaviour
-- (fixed_date, the column default above) — reclassifying the rest is a
-- judgement call for whoever owns that ingredient, not something to guess
-- at in a migration.
UPDATE ingredient SET expiry_mode = 'shelf_life_estimate', shelf_life_days = 7
 WHERE name IN ('Mango', 'Banana', 'Spring Onion', 'Cucumber') AND is_active = 1;
UPDATE ingredient SET expiry_mode = 'shelf_life_estimate', shelf_life_days = 5
 WHERE name = 'Parsley' AND is_active = 1;
UPDATE ingredient SET expiry_mode = 'none'
 WHERE name IN ('White Eggs') AND is_active = 1;

INSERT INTO schema_version (version, name) VALUES (5, 'inventory_type_and_expiry_mode');
