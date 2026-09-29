-- =============================================================================
-- 0026  Purchase orders: Draft -> Ordered -> Partially Received -> Received.
--
-- The owner reviews an existing replenishment recommendation
-- (depletion.ingredient_forecast, unchanged) and creates a Draft PO against
-- it; admin marks it Ordered once a supplier has actually been contacted,
-- then receives it line by line as stock physically arrives. Creating or
-- ordering a PO never touches ingredient/stock_ledger/fefo_batch -- only
-- receive_line (wt/purchasing.py) does, and it does so by calling the
-- existing reservation._receive_stock_locked() (the same logic
-- reservation.receive_stock() has always used, split so it can run inside
-- this module's own transaction -- see that module for why), never a second
-- inventory-receiving implementation.
--
-- supplier stays free text, matching ingredient.supplier -- no supplier
-- master table; inventing one wasn't asked for.
--
-- purchase_order_receipt is both the idempotency record and the audit trail
-- for physical receiving: one row per actual receive_line() call, with a
-- UNIQUE client_request_id so a retried/duplicated request (the same
-- concern customer_order.client_request_id already solves for order
-- creation, migration 0009) replays the original result instead of
-- crediting stock twice.
-- -----------------------------------------------------------------------------
CREATE TABLE purchase_order (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  status                 TEXT    NOT NULL DEFAULT 'draft'
                            CHECK (status IN ('draft','ordered','partially_received','received','cancelled')),
  supplier               TEXT,
  notes                  TEXT,
  expected_delivery_date TEXT,
  created_by             TEXT,
  created_at             TEXT    NOT NULL DEFAULT (datetime('now','subsec')),
  updated_at             TEXT    NOT NULL DEFAULT (datetime('now','subsec')),
  ordered_at             TEXT,
  cancelled_at           TEXT,
  cancelled_by           TEXT,
  cancel_reason          TEXT
);

CREATE TABLE purchase_order_item (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  purchase_order_id    INTEGER NOT NULL REFERENCES purchase_order(id) ON DELETE CASCADE,
  ingredient_id        INTEGER NOT NULL REFERENCES ingredient(id) ON DELETE RESTRICT,
  recommended_qty_base REAL,                    -- snapshot of the forecast recommendation at creation time
  quantity             REAL    NOT NULL CHECK (quantity > 0),   -- owner's final order qty, in the ingredient's purchase_unit
  unit                 TEXT    NOT NULL,         -- purchase_unit snapshot, so a later unit change never reinterprets history
  received_qty_base    REAL    NOT NULL DEFAULT 0 CHECK (received_qty_base >= 0),
  note                 TEXT
);

CREATE INDEX ix_po_item_po ON purchase_order_item (purchase_order_id);

-- client_request_id is nullable, same convention migration 0009 established
-- for customer_order: NULL is not covered by the UNIQUE index (SQLite
-- treats NULLs as distinct), so a caller that doesn't send a key still
-- receives normally -- it just isn't idempotent-safe against a retry. Every
-- real admin UI submission generates one; this only matters for direct/
-- internal callers.
CREATE TABLE purchase_order_receipt (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  purchase_order_item_id INTEGER NOT NULL REFERENCES purchase_order_item(id) ON DELETE RESTRICT,
  qty_base               REAL    NOT NULL CHECK (qty_base > 0),
  client_request_id      TEXT,
  created_by             TEXT,
  created_at             TEXT    NOT NULL DEFAULT (datetime('now','subsec'))
);

CREATE UNIQUE INDEX ux_po_receipt_client_request
    ON purchase_order_receipt (client_request_id)
    WHERE client_request_id IS NOT NULL;

CREATE INDEX ix_po_receipt_item ON purchase_order_receipt (purchase_order_item_id);

INSERT INTO schema_version (version, name) VALUES (26, 'purchase_orders');
