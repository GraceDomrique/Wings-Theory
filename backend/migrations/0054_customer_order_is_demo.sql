-- =============================================================================
-- 0054  Demo/showcase order marker.
--
-- `customer_order.source`'s CHECK constraint (migration 0030) deliberately
-- permits only NULL/'historical' -- that column means "this is a real
-- completed sale, just imported rather than punched live", and migration
-- 0030's own comment explicitly refuses 'demo'/'test'/'seed'/'synthetic'
-- there on purpose. This migration does not touch that invariant.
--
-- `is_demo` is a SEPARATE, orthogonal flag for orders generated purely to
-- populate dashboards/combo-mining for demonstration purposes (never a real
-- sale, never historical). Default 0 so every existing row is unaffected.
-- Revenue-reporting queries (business_overview.py/sales_overview.py/
-- dashboard_overview.py) filter this to 0 so demo orders never inflate an
-- "actual business" figure; FP-Growth (wt/associations.py's _transactions())
-- deliberately does NOT filter on this column, so demo data can still
-- surface combo recommendations through the real, unmodified mining code.
-- -----------------------------------------------------------------------------
ALTER TABLE customer_order ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0,1));

CREATE INDEX ix_customer_order_is_demo ON customer_order (is_demo);

INSERT INTO schema_version (version, name) VALUES (54, 'customer_order_is_demo');
