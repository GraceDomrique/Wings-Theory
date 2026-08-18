-- =============================================================================
-- 0011  The operational half of unlimited service: sessions, refills, flavours,
--       and an append-only way to charge for a premium flavour.
--
-- The problem this solves
-- -----------------------
-- A paid order is a financial record: what the customer bought and what they
-- handed over. An unlimited ticket is bought once and then *served* many
-- times, and until now nothing recorded the serving at all. The order said
-- "Unlimited Wings, 2 heads, P498" and that was the entire story: which
-- flavours went out, how many pieces, how many rounds, and what sauce that
-- consumed were all invisible.
--
-- The wrong fix is to keep editing the order as the night goes on. A paid
-- order is settled; re-opening it to append a round is how receipts stop
-- matching the till. So the order stays exactly as it was at payment, and
-- everything that happens afterwards hangs off it:
--
--     customer_order (immutable, financial)
--       └── unli_session          one per unlimited order, the living context
--             └── unli_refill     one append-only row per round served
--                   └── unli_refill_line   what that round actually deducted
--             └── order_charge    a premium-flavour add-on, never folded back
--                                 into customer_order.total
--
-- Immutability is enforced, not requested
-- ---------------------------------------
-- trg_order_financials_immutable below makes the paid figures physically
-- unwritable, the same way stock_ledger is physically append-only. It fires
-- only on the five money columns and only once `total` has been set, so
-- create_order's own INSERT-then-fill-in still works (OLD.total IS NULL at
-- that moment) and every later status/timestamp update is untouched.
--
-- What a refill deducts, and what it deliberately does not
-- -------------------------------------------------------
-- A refill consumes the SELECTED FLAVOUR'S sauce, resolved through the
-- existing BOM resolver against that flavour's own sub-recipe. It does NOT
-- deduct wings. The wings for the whole session already left the ledger at
-- commit, sized H x lambda by resolver.order_requirements — that is the
-- provisional deduction the entire reconciliation pipeline in 0010 exists to
-- true up. Deducting wings again per refill would be exactly the double
-- deduction wt/unli.py was written to make impossible.
--
-- Sauce, on the other hand, was never deducted for an unlimited order at all:
-- an unli line has no recipe, and the four lambda streams are wings, rice,
-- fries and drinks. Fourteen flavour sub-recipes sat in the database with
-- nothing consuming them outside the a-la-carte platters. This closes that.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- The paid order becomes physically unwritable
-- -----------------------------------------------------------------------------
CREATE TRIGGER trg_order_financials_immutable
BEFORE UPDATE OF subtotal, service_charge, total, cash_received, change_due
ON customer_order
FOR EACH ROW WHEN OLD.total IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'a settled order''s financial figures are immutable — record an additional charge against it instead');
END;

-- -----------------------------------------------------------------------------
-- Flavours
--
-- The fourteen sauces already exist as kind='sub_recipe' ingredients, each
-- with a producing recipe. This table does not restate any of that; it adds
-- only what was genuinely missing — whether a flavour is included in the
-- unlimited price or carries an add-on, and where that add-on's price comes
-- from.
--
-- Price resolution is COALESCE(menu_item.price, addon_price, 0), preferring a
-- linked menu_item so a premium flavour that is also sold a la carte is
-- priced in exactly one place. No price is ever sent from, or decided by, the
-- client.
-- -----------------------------------------------------------------------------
CREATE TABLE unli_flavor (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  ingredient_id      INTEGER NOT NULL UNIQUE REFERENCES ingredient(id) ON DELETE RESTRICT,
  recipe_id          INTEGER REFERENCES recipe(id) ON DELETE RESTRICT,
  display_name       TEXT    NOT NULL,
  flavor_type        TEXT    NOT NULL DEFAULT 'included'
                       CHECK (flavor_type IN ('included','special')),
  -- preferred price source: an existing priced menu item
  addon_menu_item_id INTEGER REFERENCES menu_item(id) ON DELETE SET NULL,
  -- fallback when the flavour has no a-la-carte equivalent
  addon_price        REAL    NOT NULL DEFAULT 0 CHECK (addon_price >= 0),
  is_active          INTEGER NOT NULL DEFAULT 1,
  sort_order         INTEGER NOT NULL DEFAULT 0,
  updated_by         TEXT,
  updated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
  -- an 'included' flavour that carries a price is a contradiction, not a
  -- configuration; the console cannot save one and neither can the API
  CHECK (flavor_type = 'special' OR (addon_price = 0 AND addon_menu_item_id IS NULL))
);
CREATE INDEX ix_unli_flavor_active ON unli_flavor (is_active, sort_order, display_name);

-- Seeded from the sauces that already exist, using the same rule /pos/flavors
-- has always used to tell a customer-selectable flavour from a house prep:
-- the '(house)' suffix. Every one starts 'included' at zero, because no
-- premium pricing exists anywhere in this database and inventing one would
-- put a number nobody agreed to in front of a customer. The owner marks the
-- specials in the console.
INSERT INTO unli_flavor (ingredient_id, recipe_id, display_name, sort_order)
SELECT i.id,
       (SELECT r.id FROM recipe r WHERE r.produces_ingredient_id = i.id AND r.is_active = 1),
       i.name,
       ROW_NUMBER() OVER (ORDER BY i.name)
  FROM ingredient i
 WHERE i.kind = 'sub_recipe'
   AND i.is_active = 1
   AND i.name NOT LIKE '%(house)%';

-- -----------------------------------------------------------------------------
-- Sessions
-- -----------------------------------------------------------------------------
CREATE TABLE unli_session (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id      INTEGER NOT NULL UNIQUE REFERENCES customer_order(id) ON DELETE CASCADE,
  head_count    INTEGER NOT NULL CHECK (head_count > 0),
  status        TEXT    NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','completed','cancelled')),
  started_at    TEXT    NOT NULL DEFAULT (datetime('now')),
  ended_at      TEXT,
  created_by    TEXT,
  completed_by  TEXT,
  reopened_by   TEXT,
  reopened_at   TEXT,
  note          TEXT
);
CREATE INDEX ix_unli_session_status ON unli_session (status, started_at);

-- -----------------------------------------------------------------------------
-- Refills — append-only in behaviour, enforced by the code path and by the
-- uniqueness constraints here. Nothing deletes a refill; a mistake produces a
-- reversal that points back at what it reverses.
--
-- flavor_name / flavor_type / addon_amount / sauce_base are SNAPSHOTS taken at
-- the moment the round was served. Re-pricing a flavour next week must not
-- silently restate what a customer was charged last night, and re-classifying
-- one must not rewrite what the kitchen actually sent out.
-- -----------------------------------------------------------------------------
CREATE TABLE unli_refill (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id        INTEGER NOT NULL REFERENCES unli_session(id) ON DELETE CASCADE,
  refill_no         INTEGER NOT NULL CHECK (refill_no > 0),
  head_count        INTEGER NOT NULL CHECK (head_count > 0),
  pieces            INTEGER NOT NULL CHECK (pieces > 0),
  flavor_id         INTEGER NOT NULL REFERENCES unli_flavor(id) ON DELETE RESTRICT,
  flavor_name       TEXT    NOT NULL,
  flavor_type       TEXT    NOT NULL CHECK (flavor_type IN ('included','special')),
  addon_amount      REAL    NOT NULL DEFAULT 0 CHECK (addon_amount >= 0),
  sauce_base        REAL    NOT NULL DEFAULT 0 CHECK (sauce_base >= 0),
  status            TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','served','cancelled','voided')),
  -- one key per submit attempt from the till. A retry replays the refill that
  -- was already created instead of serving the table a second round.
  client_request_id TEXT,
  -- a reversal points at what it reverses; the original is never edited
  corrects_refill_id INTEGER REFERENCES unli_refill(id) ON DELETE RESTRICT,
  note              TEXT,
  created_by        TEXT,
  created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
  served_by         TEXT,
  served_at         TEXT,
  voided_by         TEXT,
  voided_at         TEXT,
  void_reason       TEXT,
  -- sequential per session, and the database is what guarantees it: two
  -- simultaneous submits cannot both take round 3
  UNIQUE (session_id, refill_no)
);
CREATE UNIQUE INDEX ux_refill_client_request
    ON unli_refill (client_request_id) WHERE client_request_id IS NOT NULL;
CREATE INDEX ix_refill_session ON unli_refill (session_id, refill_no);
CREATE INDEX ix_refill_status  ON unli_refill (status, created_at);

-- What a refill actually held and then deducted, one row per raw ingredient.
-- Mirrors `reservation` — held -> committed -> released — but keyed on the
-- refill rather than the order, because `reservation` is UNIQUE per
-- (order, ingredient) and one order now has many rounds drawing on the same
-- ingredients.
CREATE TABLE unli_refill_line (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  refill_id     INTEGER NOT NULL REFERENCES unli_refill(id) ON DELETE CASCADE,
  ingredient_id INTEGER NOT NULL REFERENCES ingredient(id)  ON DELETE RESTRICT,
  qty_base      REAL    NOT NULL CHECK (qty_base > 0),
  state         TEXT    NOT NULL DEFAULT 'held'
                  CHECK (state IN ('held','committed','released')),
  created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
  resolved_at   TEXT,
  UNIQUE (refill_id, ingredient_id)
);
CREATE INDEX ix_refill_line_open ON unli_refill_line (state, ingredient_id);

-- -----------------------------------------------------------------------------
-- Additional charges against a settled order.
--
-- The order total is never touched. A premium flavour served at 9pm is a new
-- row here, and "what the table owes now" is order.total + SUM(amount) —
-- computed when it is read, never stored back. A reversal is another row with
-- a negative amount pointing at the one it cancels, so the arithmetic stays
-- additive and the history stays complete.
--
-- Append-only for the same reason stock_ledger is: it is money.
-- -----------------------------------------------------------------------------
CREATE TABLE order_charge (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id           INTEGER NOT NULL REFERENCES customer_order(id) ON DELETE RESTRICT,
  refill_id          INTEGER REFERENCES unli_refill(id) ON DELETE RESTRICT,
  kind               TEXT    NOT NULL
                       CHECK (kind IN ('unli_flavor_addon','addon_reversal')),
  amount             REAL    NOT NULL,          -- signed: a reversal is negative
  description        TEXT    NOT NULL,
  reverses_charge_id INTEGER REFERENCES order_charge(id) ON DELETE RESTRICT,
  created_by         TEXT,
  created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_order_charge_order ON order_charge (order_id, created_at);
CREATE UNIQUE INDEX ux_order_charge_reversal
    ON order_charge (reverses_charge_id) WHERE reverses_charge_id IS NOT NULL;

CREATE TRIGGER trg_order_charge_no_update BEFORE UPDATE ON order_charge
BEGIN SELECT RAISE(ABORT, 'order_charge is append-only'); END;
CREATE TRIGGER trg_order_charge_no_delete BEFORE DELETE ON order_charge
BEGIN SELECT RAISE(ABORT, 'order_charge is append-only'); END;

-- -----------------------------------------------------------------------------
-- Operator-configurable numbers, in the table 0010 created for exactly this.
-- -----------------------------------------------------------------------------
-- The shop's stated rule: every head gets 3 pieces per round.
INSERT INTO app_setting (key, value) VALUES ('unli_pieces_per_head', '3');

-- Sauce per piece, derived rather than invented. The Wing Rack BOM carries
-- 1,000 g of wings as 12 pieces with two flavour lines of 60 g each
-- (mapping.FLAVOUR_PLACEHOLDER_DEFAULT_QTY documents that 60 g as half of
-- Classic Buffalo's reconciled 120 g-per-kg ratio applied to the 500 g half
-- rack a flavour covers). 60 g over the 6 pieces in a half rack is 10 g a
-- piece. That is an inherited estimate, not a measured constant — it should
-- be confirmed against the kitchen, which is why it lives here and not in
-- Python.
INSERT INTO app_setting (key, value) VALUES ('unli_sauce_g_per_piece', '10');

-- -----------------------------------------------------------------------------
-- Backfill: unlimited orders that predate this migration.
--
-- Given a session so their history is not a hole, with a status matching what
-- already happened to the order. No refills are invented for them — nobody
-- recorded which flavours went out, and a fabricated round would be worse
-- than an honest gap in a system whose entire point is that the numbers are
-- real.
-- -----------------------------------------------------------------------------
INSERT INTO unli_session (order_id, head_count, status, started_at, ended_at, created_by, note)
SELECT o.id, o.head_count,
       CASE WHEN o.status IN ('served','void') THEN 'completed' ELSE 'active' END,
       o.created_at,
       CASE WHEN o.status IN ('served','void') THEN COALESCE(o.served_at, o.voided_at, o.created_at) END,
       o.created_by,
       'backfilled by migration 0011 — served before refills were recorded'
  FROM customer_order o
 WHERE o.head_count > 0
   AND EXISTS (SELECT 1 FROM order_line ol JOIN menu_item m ON m.id = ol.menu_item_id
                WHERE ol.order_id = o.id AND m.is_unli = 1);

-- -----------------------------------------------------------------------------
-- Read models. Views rather than stored aggregates so a refill total can never
-- drift from the refills it counts.
-- -----------------------------------------------------------------------------
CREATE VIEW v_unli_flavor AS
SELECT f.id, f.ingredient_id, f.recipe_id, f.display_name, f.flavor_type,
       f.is_active, f.sort_order,
       COALESCE(m.price, f.addon_price, 0) AS addon_price,
       m.name        AS addon_menu_item_name,
       f.addon_menu_item_id,
       i.base_unit,
       -- a flavour whose sub-recipe has no active producing recipe cannot be
       -- costed or deducted, so the POS must not offer it
       CASE WHEN f.recipe_id IS NULL THEN 0 ELSE 1 END AS is_deductable
  FROM unli_flavor f
  JOIN ingredient i ON i.id = f.ingredient_id
  LEFT JOIN menu_item m ON m.id = f.addon_menu_item_id;

CREATE VIEW v_unli_session_summary AS
SELECT s.id AS session_id, s.order_id, o.order_no, o.status AS order_status,
       o.table_label, o.created_at AS order_created_at,
       o.subtotal, o.service_charge, o.total AS order_total,
       o.cash_received, o.change_due,
       s.head_count, s.status, s.started_at, s.ended_at,
       s.created_by, s.completed_by, s.note,
       COALESCE(r.served_refills, 0)  AS served_refills,
       COALESCE(r.pieces_served, 0)   AS pieces_served,
       COALESCE(r.sauce_base, 0)      AS sauce_base,
       COALESCE(c.addon_total, 0)     AS addon_total,
       o.total + COALESCE(c.addon_total, 0) AS current_total
  FROM unli_session s
  JOIN customer_order o ON o.id = s.order_id
  LEFT JOIN (SELECT session_id,
                    COUNT(*)      AS served_refills,
                    SUM(pieces)   AS pieces_served,
                    SUM(sauce_base) AS sauce_base
               FROM unli_refill WHERE status = 'served' GROUP BY session_id) r
         ON r.session_id = s.id
  LEFT JOIN (SELECT order_id, SUM(amount) AS addon_total
               FROM order_charge GROUP BY order_id) c
         ON c.order_id = s.order_id;

INSERT INTO schema_version (version, name) VALUES (11, 'unli_session_refills');
