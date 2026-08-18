-- =============================================================================
-- 0008  The POS cart has always shown a 10% service charge and a grand total,
-- but neither was ever sent to the backend — /pos/orders only ever received
-- menu_item_pk/qty. There was no payment step at all: punching an order sent
-- it straight to the kitchen. This adds real columns for what a sale actually
-- involved (subtotal, the service charge, the total charged, what the
-- customer handed over, and the change given back) so a receipt can state
-- real numbers instead of the cart's client-only estimate.
-- =============================================================================

ALTER TABLE customer_order ADD COLUMN subtotal REAL;
ALTER TABLE customer_order ADD COLUMN service_charge REAL NOT NULL DEFAULT 0;
ALTER TABLE customer_order ADD COLUMN total REAL;
ALTER TABLE customer_order ADD COLUMN cash_received REAL CHECK (cash_received IS NULL OR cash_received >= 0);
ALTER TABLE customer_order ADD COLUMN change_due REAL CHECK (change_due IS NULL OR change_due >= 0);

INSERT INTO schema_version (version, name) VALUES (8, 'order_payment');
