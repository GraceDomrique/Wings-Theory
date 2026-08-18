-- =============================================================================
-- 0009  Duplicate-order protection at the server, not just the button.
--
-- A concurrency test fired five identical checkout requests at /pos/orders
-- and got five real orders back. The POS disables its Confirm button while a
-- request is in flight, but that only covers one tab's fast double-tap — it
-- does nothing for a network retry, a slow response the cashier gives up on
-- and re-submits, or a tablet that reconnects and replays the request.
--
-- The fix is an idempotency key chosen by the client for one checkout
-- attempt. Replaying the same key returns the order that was already
-- created instead of ringing the customer up twice. NULL is allowed and is
-- not covered by the UNIQUE index (SQLite treats NULLs as distinct), so
-- older clients and internal callers that don't send a key keep working
-- exactly as before.
-- =============================================================================

ALTER TABLE customer_order ADD COLUMN client_request_id TEXT;

CREATE UNIQUE INDEX ux_order_client_request
    ON customer_order (client_request_id)
    WHERE client_request_id IS NOT NULL;

INSERT INTO schema_version (version, name) VALUES (9, 'order_idempotency');
