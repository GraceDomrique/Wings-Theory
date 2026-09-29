-- =============================================================================
-- 0019  Order-level discounts (Senior Citizen 20%, PWD 20%, Staff 10%).
--
-- The POS cart has always had a "Charge" total but no way to apply the three
-- discounts every till in the Philippines needs — RA 9994 (senior citizens)
-- and RA 10754 (PWD) both mandate 20% off the sale price before service
-- charge, and staff gets a house 10% courtesy discount. None of that existed
-- as a column, so there was nowhere for wt.api.create_order to record which
-- discount an order was rung up under, or how much it took off.
--
-- discount_type names WHICH discount, never a rate or a peso figure — those
-- are decided server-side from DISCOUNT_RATES so a client can never charge a
-- rate it made up. discount_amount is the peso figure that rate produced
-- against that specific order's subtotal, stored the same way subtotal and
-- service_charge already are: computed once at punch time, then frozen.
-- =============================================================================

ALTER TABLE customer_order ADD COLUMN discount_type TEXT
  CHECK (discount_type IS NULL OR discount_type IN ('senior', 'pwd', 'staff'));
ALTER TABLE customer_order ADD COLUMN discount_amount REAL NOT NULL DEFAULT 0
  CHECK (discount_amount >= 0);

-- Re-cut trg_order_financials_immutable (0011) to also guard the two new
-- columns. Without this a settled order's discount could be edited after the
-- fact through the same door subtotal/total are already barred from.
DROP TRIGGER trg_order_financials_immutable;

CREATE TRIGGER trg_order_financials_immutable
BEFORE UPDATE OF subtotal, discount_type, discount_amount, service_charge, total,
                  cash_received, change_due
ON customer_order
FOR EACH ROW WHEN OLD.total IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'a settled order''s financial figures are immutable — record an additional charge against it instead');
END;

INSERT INTO schema_version (version, name) VALUES (19, 'order_discounts');
