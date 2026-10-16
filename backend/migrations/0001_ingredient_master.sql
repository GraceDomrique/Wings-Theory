-- =============================================================================
-- 0001  Ingredient master, recipes, two-phase reservation, FEFO batches.
--
-- Design notes that matter downstream:
--   * All stock is held in BASE UNITS (g / ml / pc). Purchase units exist only
--     for ordering and display. pack_size is the only bridge between them.
--   * recipe_line.ingredient_id is NOT NULL + FK. A recipe line therefore
--     cannot exist without a resolved ingredient. Lines that could not be
--     mapped during import live in unmapped_recipe_line, never here.
--   * Verification status is tracked per constant, not per row, so a seeded
--     placeholder can never be mistaken for a confirmed measurement.
--   * FEFO (expiry-date order), never FIFO (receipt order). The two diverge
--     whenever a later delivery expires sooner, which is routine here.
-- =============================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE schema_version (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- -----------------------------------------------------------------------------
-- Ingredient master
-- -----------------------------------------------------------------------------
-- verification vocabulary, used by every physical constant in the schema:
--   'verified'           confirmed against the store, a scale, or a supplier invoice
--   'needs_verification' seeded placeholder; arithmetic works but is NOT production-ready
--   'unset'              no value at all; blocks deduction outright
CREATE TABLE ingredient (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  ingredient_id        TEXT    NOT NULL UNIQUE,          -- stable, immutable, e.g. ING-0007
  name                 TEXT    NOT NULL,
  category             TEXT    NOT NULL
                         CHECK (category IN ('wet_food','dry_food','packaging','consumable','resale')),

  -- 'raw'        : bought as-is
  -- 'sub_recipe' : produced in-house before it is consumed (sauces, dips, syrup)
  kind                 TEXT    NOT NULL DEFAULT 'raw'
                         CHECK (kind IN ('raw','sub_recipe')),
  -- how a sub-recipe is consumed:
  --   'exploded' : resolved recursively to raw ingredients at deduction time (no stock of its own)
  --   'stocked'  : batch-prepped and held as stock in its own right
  sub_recipe_mode      TEXT
                         CHECK (sub_recipe_mode IN ('exploded','stocked')),

  base_unit            TEXT    NOT NULL CHECK (base_unit IN ('g','ml','pc')),
  purchase_unit        TEXT    NOT NULL,
  pack_size            REAL    CHECK (pack_size IS NULL OR pack_size > 0),
  pack_size_status     TEXT    NOT NULL DEFAULT 'unset'
                         CHECK (pack_size_status IN ('verified','needs_verification','unset')),

  -- raw -> usable conversion: trim, drain, peel, or raw -> cooked weight gain.
  -- Defaulted to 1.0 so arithmetic never breaks, but status defaults to
  -- needs_verification so an unconfirmed 1.0 is always visible as unconfirmed.
  yield_factor         REAL    NOT NULL DEFAULT 1.0 CHECK (yield_factor > 0),
  yield_factor_status  TEXT    NOT NULL DEFAULT 'needs_verification'
                         CHECK (yield_factor_status IN ('verified','needs_verification','unset')),

  on_hand_base         REAL    NOT NULL DEFAULT 0 CHECK (on_hand_base >= 0),
  reserved_base        REAL    NOT NULL DEFAULT 0 CHECK (reserved_base >= 0),
  reorder_point_base   REAL,
  lead_time_days       INTEGER NOT NULL DEFAULT 3,
  unit_cost            REAL,                              -- per purchase_unit
  unit_cost_status     TEXT    NOT NULL DEFAULT 'needs_verification'
                         CHECK (unit_cost_status IN ('verified','needs_verification','unset')),
  supplier             TEXT,
  storage_location     TEXT,
  is_active            INTEGER NOT NULL DEFAULT 1,
  created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at           TEXT    NOT NULL DEFAULT (datetime('now')),

  -- a sub-recipe must declare how it is consumed; a raw good must not
  CHECK ((kind = 'sub_recipe' AND sub_recipe_mode IS NOT NULL)
      OR (kind = 'raw'        AND sub_recipe_mode IS NULL)),
  -- Reserved stock can never exceed what is physically on hand. The 1e-6
  -- allowance matches the rounding guard in reservation.py: base quantities
  -- accumulate through float multiplication, and a sub-milligram residue must
  -- fail a reservation cleanly (rowcount 0) rather than trip a constraint.
  CHECK (reserved_base <= on_hand_base + 0.000001)
);

CREATE INDEX ix_ingredient_name     ON ingredient (name);
CREATE INDEX ix_ingredient_category ON ingredient (category, is_active);

-- Import-time aliases. Lets "Bacon (cooked, chopped)" and "Bacon (strips)"
-- resolve to one ingredient, and makes re-imports idempotent. Aliases are a
-- record of human mapping decisions, never a fuzzy-matching shortcut.
CREATE TABLE ingredient_alias (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ingredient_id INTEGER NOT NULL REFERENCES ingredient(id) ON DELETE CASCADE,
  alias_norm    TEXT    NOT NULL UNIQUE,     -- normalised source label
  alias_raw     TEXT    NOT NULL,
  source        TEXT,
  created_by    TEXT,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- -----------------------------------------------------------------------------
-- Recipes
-- -----------------------------------------------------------------------------
CREATE TABLE recipe (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  recipe_id              TEXT    NOT NULL UNIQUE,
  name                   TEXT    NOT NULL,
  kind                   TEXT    NOT NULL CHECK (kind IN ('menu','sub_recipe')),
  -- a sub-recipe produces exactly one ingredient, which other recipes consume
  produces_ingredient_id INTEGER REFERENCES ingredient(id) ON DELETE RESTRICT,
  yield_qty              REAL    NOT NULL DEFAULT 1 CHECK (yield_qty > 0),
  yield_unit             TEXT    NOT NULL DEFAULT 'serving',
  is_active              INTEGER NOT NULL DEFAULT 1,
  created_at             TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at             TEXT    NOT NULL DEFAULT (datetime('now')),
  CHECK ((kind = 'sub_recipe' AND produces_ingredient_id IS NOT NULL)
      OR (kind = 'menu'       AND produces_ingredient_id IS NULL))
);
CREATE UNIQUE INDEX ux_recipe_produces ON recipe (produces_ingredient_id)
  WHERE produces_ingredient_id IS NOT NULL;

-- The core constraint of this phase: ingredient_id is NOT NULL and a foreign
-- key. There is no code path that writes a recipe line without a resolved
-- ingredient, because the database will not accept one.
CREATE TABLE recipe_line (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  recipe_id     INTEGER NOT NULL REFERENCES recipe(id)     ON DELETE CASCADE,
  ingredient_id INTEGER NOT NULL REFERENCES ingredient(id) ON DELETE RESTRICT,
  display_label TEXT    NOT NULL,          -- what the kitchen calls it on this recipe
  qty_base      REAL    NOT NULL CHECK (qty_base > 0),
  qty_status    TEXT    NOT NULL DEFAULT 'needs_verification'
                  CHECK (qty_status IN ('verified','needs_verification','unset')),
  is_optional   INTEGER NOT NULL DEFAULT 0,
  sort_order    INTEGER NOT NULL DEFAULT 0,
  note          TEXT,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (recipe_id, ingredient_id, display_label)
);
CREATE INDEX ix_recipe_line_recipe     ON recipe_line (recipe_id);
CREATE INDEX ix_recipe_line_ingredient ON recipe_line (ingredient_id);

-- -----------------------------------------------------------------------------
-- Menu
-- -----------------------------------------------------------------------------
CREATE TABLE menu_item (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  menu_item_id TEXT    NOT NULL UNIQUE,
  name         TEXT    NOT NULL,
  category     TEXT    NOT NULL,
  price        REAL    NOT NULL CHECK (price >= 0),
  recipe_id    INTEGER REFERENCES recipe(id) ON DELETE RESTRICT,
  is_unli      INTEGER NOT NULL DEFAULT 0,
  pos_visible  INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- -----------------------------------------------------------------------------
-- Unlimited-item portion coefficients — vectorised, one row per unli stream
-- -----------------------------------------------------------------------------
CREATE TABLE unli_item (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  item_key      TEXT    NOT NULL UNIQUE
                  CHECK (item_key IN ('wings','rice','fries','drinks')),
  label         TEXT    NOT NULL,
  ingredient_id INTEGER NOT NULL REFERENCES ingredient(id) ON DELETE RESTRICT,
  window_days   INTEGER NOT NULL DEFAULT 28,
  is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE unli_coefficient (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  unli_item_id      INTEGER NOT NULL REFERENCES unli_item(id) ON DELETE CASCADE,
  lambda_value      REAL    NOT NULL CHECK (lambda_value > 0),   -- base units per head
  role              TEXT    NOT NULL CHECK (role IN ('baseline','rolling')),
  window_start      TEXT,
  window_end        TEXT,
  total_actual_base REAL,
  total_heads       INTEGER,
  status            TEXT    NOT NULL DEFAULT 'needs_verification'
                      CHECK (status IN ('verified','needs_verification','unset')),
  effective_from    TEXT    NOT NULL DEFAULT (datetime('now')),
  created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_unli_coeff_pick ON unli_coefficient (unli_item_id, role, effective_from DESC);

CREATE TABLE unli_reconciliation (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  service_date     TEXT    NOT NULL,
  unli_item_id     INTEGER NOT NULL REFERENCES unli_item(id) ON DELETE CASCADE,
  heads            INTEGER NOT NULL CHECK (heads >= 0),
  theoretical_base REAL    NOT NULL,
  actual_base      REAL    NOT NULL,
  variance_base    REAL    NOT NULL,
  variance_ratio   REAL,
  coefficient_id   INTEGER REFERENCES unli_coefficient(id),
  created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (service_date, unli_item_id)
);

-- -----------------------------------------------------------------------------
-- Orders and the two-phase reservation
-- -----------------------------------------------------------------------------
CREATE TABLE customer_order (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no     TEXT    NOT NULL UNIQUE,
  status       TEXT    NOT NULL DEFAULT 'punched'
                 CHECK (status IN ('punched','in_progress','ready','served','void')),
  table_label  TEXT,
  head_count   INTEGER NOT NULL DEFAULT 0,
  created_by   TEXT,
  created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
  committed_at TEXT,
  voided_at    TEXT,
  void_reason  TEXT
);
CREATE INDEX ix_order_status ON customer_order (status, created_at);

CREATE TABLE order_line (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id     INTEGER NOT NULL REFERENCES customer_order(id) ON DELETE CASCADE,
  menu_item_id INTEGER NOT NULL REFERENCES menu_item(id)      ON DELETE RESTRICT,
  qty          INTEGER NOT NULL CHECK (qty > 0),
  unit_price   REAL    NOT NULL,
  note         TEXT
);
CREATE INDEX ix_order_line_order ON order_line (order_id);

-- One row per (order, ingredient). 'held' is a soft allocation that has not
-- touched the ledger; 'committed' has. Availability reads on_hand - reserved.
CREATE TABLE reservation (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id      INTEGER NOT NULL REFERENCES customer_order(id) ON DELETE CASCADE,
  ingredient_id INTEGER NOT NULL REFERENCES ingredient(id)     ON DELETE RESTRICT,
  qty_base      REAL    NOT NULL CHECK (qty_base > 0),
  state         TEXT    NOT NULL DEFAULT 'held'
                  CHECK (state IN ('held','committed','released')),
  created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
  resolved_at   TEXT,
  UNIQUE (order_id, ingredient_id)
);
CREATE INDEX ix_reservation_open ON reservation (state, ingredient_id);

-- -----------------------------------------------------------------------------
-- Append-only stock ledger
-- -----------------------------------------------------------------------------
CREATE TABLE stock_ledger (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  ingredient_id      INTEGER NOT NULL REFERENCES ingredient(id) ON DELETE RESTRICT,
  delta_base         REAL    NOT NULL,
  balance_after_base REAL    NOT NULL,
  reason             TEXT    NOT NULL CHECK (reason IN (
                       'purchase_receipt','sale_commit','void_credit','wastage',
                       'spoilage','count_adjustment','transfer',
                       'unli_provisional','unli_reconcile')),
  wastage_reason     TEXT,
  ref_type           TEXT,
  ref_id             TEXT,
  user_id            TEXT,
  note               TEXT,
  created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_ledger_ingredient ON stock_ledger (ingredient_id, created_at);
CREATE INDEX ix_ledger_ref        ON stock_ledger (ref_type, ref_id);

-- The ledger is the audit trail for every prediction downstream. Make it
-- physically append-only rather than trusting callers to behave.
CREATE TRIGGER trg_ledger_no_update BEFORE UPDATE ON stock_ledger
BEGIN SELECT RAISE(ABORT, 'stock_ledger is append-only'); END;
CREATE TRIGGER trg_ledger_no_delete BEFORE DELETE ON stock_ledger
BEGIN SELECT RAISE(ABORT, 'stock_ledger is append-only'); END;

-- -----------------------------------------------------------------------------
-- FEFO batches — issue order is by expiry date, never by receipt date
-- -----------------------------------------------------------------------------
CREATE TABLE fefo_batch (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ingredient_id  INTEGER NOT NULL REFERENCES ingredient(id) ON DELETE RESTRICT,
  lot_no         TEXT,
  qty_base       REAL    NOT NULL CHECK (qty_base >= 0),
  received_at    TEXT    NOT NULL,
  expires_at     TEXT,                       -- NULL = non-perishable
  expiry_status  TEXT    NOT NULL DEFAULT 'needs_verification'
                   CHECK (expiry_status IN ('verified','needs_verification','unset')),
  state          TEXT    NOT NULL DEFAULT 'open'
                   CHECK (state IN ('open','depleted','discarded')),
  purchase_ref   TEXT,
  created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
-- The FEFO pick index: expiry ascending. Renaming this to receipt order would
-- silently turn the system back into FIFO, so the ordering lives in the index.
CREATE INDEX ix_fefo_batch_pick ON fefo_batch (ingredient_id, state, expires_at);

-- -----------------------------------------------------------------------------
-- Admin queue: recipe lines that could not be mapped to an ingredient.
-- These deliberately live outside recipe_line so production data stays clean
-- while the work item survives.
-- -----------------------------------------------------------------------------
CREATE TABLE unmapped_recipe_line (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  recipe_id               INTEGER REFERENCES recipe(id) ON DELETE CASCADE,
  source_recipe_name      TEXT    NOT NULL,
  source_ingredient_label TEXT    NOT NULL,
  source_qty_text         TEXT,
  parsed_qty              REAL,
  parsed_unit             TEXT,
  reason                  TEXT    NOT NULL
                            CHECK (reason IN ('no_stock_record','ambiguous','not_stocked','utility')),
  state                   TEXT    NOT NULL DEFAULT 'open'
                            CHECK (state IN ('open','resolved','dismissed')),
  resolved_ingredient_id  INTEGER REFERENCES ingredient(id) ON DELETE SET NULL,
  resolved_by             TEXT,
  resolved_at             TEXT,
  note                    TEXT,
  created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (source_recipe_name, source_ingredient_label)
);
CREATE INDEX ix_unmapped_state ON unmapped_recipe_line (state, source_recipe_name);

-- -----------------------------------------------------------------------------
-- Users
-- -----------------------------------------------------------------------------
CREATE TABLE app_user (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT    NOT NULL UNIQUE,
  display_name  TEXT    NOT NULL,
  role          TEXT    NOT NULL CHECK (role IN ('admin','manager','cashier','kitchen')),
  password_hash TEXT    NOT NULL,
  password_salt TEXT    NOT NULL,
  is_active     INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- =============================================================================
-- Derived views. Readiness is computed, never stored, so it cannot drift.
-- =============================================================================

-- Recursive expansion of any recipe down to deductable leaves.
-- Exploded sub-recipes are walked through; stocked sub-recipes are leaves.
-- The path guard makes a cyclic definition terminate instead of hanging.
CREATE VIEW v_recipe_expansion AS
WITH RECURSIVE exp(root_recipe_id, ingredient_id, qty_base, depth, path) AS (
    SELECT rl.recipe_id,
           rl.ingredient_id,
           rl.qty_base,
           0,
           '/' || rl.ingredient_id || '/'
      FROM recipe_line rl
  UNION ALL
    SELECT e.root_recipe_id,
           child.ingredient_id,
           e.qty_base * child.qty_base / sub.yield_qty,
           e.depth + 1,
           e.path || child.ingredient_id || '/'
      FROM exp e
      JOIN ingredient  i     ON i.id = e.ingredient_id
                            AND i.kind = 'sub_recipe'
                            AND i.sub_recipe_mode = 'exploded'
      JOIN recipe      sub   ON sub.produces_ingredient_id = i.id AND sub.is_active = 1
      JOIN recipe_line child ON child.recipe_id = sub.id
     WHERE e.depth < 8
       AND e.path NOT LIKE '%/' || child.ingredient_id || '/%'
)
SELECT root_recipe_id, ingredient_id, qty_base, depth, path FROM exp;

-- Leaves only: what actually gets deducted.
CREATE VIEW v_recipe_leaf AS
SELECT e.root_recipe_id  AS recipe_id,
       e.ingredient_id,
       SUM(e.qty_base)   AS qty_base
  FROM v_recipe_expansion e
  JOIN ingredient i ON i.id = e.ingredient_id
 WHERE NOT (i.kind = 'sub_recipe' AND i.sub_recipe_mode = 'exploded')
 GROUP BY e.root_recipe_id, e.ingredient_id;

-- Can this ingredient be deducted at all?
CREATE VIEW v_ingredient_readiness AS
SELECT i.id,
       i.ingredient_id,
       i.name,
       CASE WHEN i.kind = 'sub_recipe' AND i.sub_recipe_mode = 'exploded' THEN 1
            WHEN i.pack_size IS NOT NULL AND i.pack_size_status <> 'unset'  THEN 1
            ELSE 0 END AS is_deductable,
       CASE WHEN i.kind = 'sub_recipe' AND i.sub_recipe_mode = 'exploded' THEN NULL
            WHEN i.pack_size IS NULL THEN 'pack size not set'
            WHEN i.pack_size_status = 'unset' THEN 'pack size not set'
            ELSE NULL END AS block_reason,
       (i.pack_size_status    = 'needs_verification')
     + (i.yield_factor_status = 'needs_verification')
     + (i.unit_cost_status    = 'needs_verification') AS unverified_count
  FROM ingredient i;

-- The POS gate. A menu item is sellable only when it has a recipe, every leaf
-- is deductable, and no line of that recipe is sitting in the unmapped queue.
CREATE VIEW v_menu_item_availability AS
SELECT m.id            AS menu_item_pk,
       m.menu_item_id,
       m.name,
       m.category,
       m.price,
       m.recipe_id,
       CASE WHEN m.recipe_id IS NULL              THEN 0
            WHEN COALESCE(b.blocked, 0)    > 0    THEN 0
            WHEN COALESCE(u.unmapped, 0)   > 0    THEN 0
            ELSE 1 END                            AS sellable,
       CASE WHEN m.recipe_id IS NULL           THEN 'no recipe linked'
            WHEN COALESCE(u.unmapped, 0) > 0  THEN CAST(u.unmapped AS TEXT) || ' unmapped ingredient(s)'
            WHEN COALESCE(b.blocked, 0)  > 0  THEN CAST(b.blocked  AS TEXT) || ' ingredient(s) without a pack size'
            ELSE NULL END                         AS block_reason,
       COALESCE(b.blocked, 0)  AS blocked_leaves,
       COALESCE(u.unmapped, 0) AS unmapped_lines
  FROM menu_item m
  LEFT JOIN (SELECT l.recipe_id, COUNT(*) AS blocked
               FROM v_recipe_leaf l
               JOIN v_ingredient_readiness r ON r.id = l.ingredient_id
              WHERE r.is_deductable = 0
              GROUP BY l.recipe_id) b ON b.recipe_id = m.recipe_id
  LEFT JOIN (SELECT recipe_id, COUNT(*) AS unmapped
               FROM unmapped_recipe_line
              WHERE state = 'open' AND recipe_id IS NOT NULL
              GROUP BY recipe_id) u ON u.recipe_id = m.recipe_id;

-- Unified "needs verification" queue across every unconfirmed physical constant.
CREATE VIEW v_needs_verification AS
  SELECT 'ingredient.pack_size'    AS field, i.ingredient_id AS entity_id, i.name AS entity_name,
         CAST(i.pack_size AS TEXT) AS current_value,
         i.purchase_unit || ' -> ' || i.base_unit AS context, i.id AS row_id
    FROM ingredient i WHERE i.pack_size_status = 'needs_verification'
UNION ALL
  SELECT 'ingredient.yield_factor', i.ingredient_id, i.name,
         CAST(i.yield_factor AS TEXT), 'raw -> usable', i.id
    FROM ingredient i WHERE i.yield_factor_status = 'needs_verification'
UNION ALL
  SELECT 'ingredient.unit_cost', i.ingredient_id, i.name,
         CAST(i.unit_cost AS TEXT), 'per ' || i.purchase_unit, i.id
    FROM ingredient i WHERE i.unit_cost_status = 'needs_verification'
UNION ALL
  SELECT 'recipe_line.qty_base', r.recipe_id, r.name || ' / ' || rl.display_label,
         CAST(rl.qty_base AS TEXT), 'per ' || r.yield_unit, rl.id
    FROM recipe_line rl JOIN recipe r ON r.id = rl.recipe_id
   WHERE rl.qty_status = 'needs_verification'
UNION ALL
  SELECT 'fefo_batch.expires_at', COALESCE(b.lot_no,'batch#'||b.id), i.name,
         COALESCE(b.expires_at,'(none)'), 'received ' || b.received_at, b.id
    FROM fefo_batch b JOIN ingredient i ON i.id = b.ingredient_id
   WHERE b.expiry_status = 'needs_verification' AND b.state = 'open'
UNION ALL
  SELECT 'unli_coefficient.lambda', u.item_key, u.label,
         CAST(c.lambda_value AS TEXT), c.role || ' coefficient', c.id
    FROM unli_coefficient c JOIN unli_item u ON u.id = c.unli_item_id
   WHERE c.status = 'needs_verification';

-- Live availability for the POS, in base units.
CREATE VIEW v_ingredient_available AS
SELECT i.id, i.ingredient_id, i.name, i.base_unit,
       i.on_hand_base, i.reserved_base,
       (i.on_hand_base - i.reserved_base) AS available_base,
       r.is_deductable
  FROM ingredient i JOIN v_ingredient_readiness r ON r.id = i.id;

INSERT INTO schema_version (version, name) VALUES (1, 'ingredient_master');
