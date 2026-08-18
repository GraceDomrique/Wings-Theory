-- =============================================================================
-- 0004  Track when an order was actually served.
--
-- The kitchen display's "recently served" list needs a timestamp for when
-- serving happened, not when cooking started (committed_at) or when the
-- ticket was punched (created_at) — neither says how long an order has sat
-- in the Served column, which is what decides whether the board still shows
-- it.
-- =============================================================================

ALTER TABLE customer_order ADD COLUMN served_at TEXT;

INSERT INTO schema_version (version, name) VALUES (4, 'served_at');
