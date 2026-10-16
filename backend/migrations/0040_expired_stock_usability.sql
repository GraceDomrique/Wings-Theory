-- =============================================================================
-- 0040  Expired stock is physically real but not usable.
--
-- Audit finding (2026-10): deplete_fefo() ordered open batches by expiry but
-- never excluded one that had already passed it, so a sale could draw from a
-- batch sitting in the expiry monitor as "Expired". v_menu_item_availability
-- and v_recipe_capacity (0003) computed capacity from
-- `on_hand_base - reserved_base` -- the ingredient's TOTAL physical stock,
-- including whatever sits in an already-expired batch -- so the POS could
-- report an item sellable purely off stock nobody should be serving.
--
-- This does not delete, zero out, or auto-write-off expired stock: it still
-- physically exists, is still visible on /console/expiry and in the
-- ingredient drawer's batch list, and still requires an explicit wastage/
-- spoilage entry (reservation.record_wastage) to leave the ledger. This
-- migration only stops it from being counted as USABLE for a sale,
-- reservation, or purchase recommendation -- see reservation.py's _try_hold,
-- deplete_fefo and restore_fefo, and depletion.py's _ingredient_rows for the
-- matching Python-side changes that keep the reservation guard and the
-- replenishment engine computing the same number this view does.
--
-- expires_at < date('now','+8 hours') mirrors timeutil.py's Manila (+8,
-- no-DST) business-date shift exactly -- a batch expiring on today's Manila
-- business date is not yet counted as expired here, matching
-- api._annotate_batch_expiry's `days_to_expiry < 0` rule (now moved to
-- depletion.annotate_batch_expiry). A CREATE VIEW body is static SQL, so
-- this can't call timeutil.sql_business_date() the way a parameterised
-- query can -- the literal shift is the SQL-side equivalent, and the two
-- are expected to be changed together if Manila's offset ever does.
-- =============================================================================

CREATE VIEW v_ingredient_expired_stock AS
SELECT ingredient_id,
       SUM(qty_base) AS expired_base
  FROM fefo_batch
 WHERE state = 'open'
   AND expires_at IS NOT NULL
   AND expires_at < date('now', '+8 hours')
 GROUP BY ingredient_id;

DROP VIEW v_menu_item_availability;
DROP VIEW v_recipe_capacity;

-- Servings each recipe can still produce, limited by its tightest ingredient.
-- Usable stock now excludes whatever sits in an already-expired open batch.
CREATE VIEW v_recipe_capacity AS
SELECT l.recipe_id,
       CAST(MIN((i.on_hand_base - i.reserved_base - COALESCE(x.expired_base, 0)) / l.qty_base)
            AS INTEGER) AS max_qty,
       MIN(i.name) AS any_ingredient
  FROM v_recipe_leaf l
  JOIN ingredient i ON i.id = l.ingredient_id
  LEFT JOIN v_ingredient_expired_stock x ON x.ingredient_id = i.id
 WHERE l.qty_base > 0
 GROUP BY l.recipe_id;

CREATE VIEW v_menu_item_availability AS
SELECT m.id            AS menu_item_pk,
       m.menu_item_id,
       m.name,
       m.category,
       m.price,
       m.recipe_id,
       m.is_unli,

       -- structural readiness: does the data support deducting this at all?
       CASE
         WHEN m.is_unli = 1       THEN CASE WHEN COALESCE(x.blocked_streams,0)=0 THEN 1 ELSE 0 END
         WHEN m.recipe_id IS NULL THEN 0
         WHEN COALESCE(b.blocked,0)  > 0 THEN 0
         WHEN COALESCE(u.unmapped,0) > 0 THEN 0
         ELSE 1
       END AS structurally_ready,

       COALESCE(cap.max_qty, 0) AS max_qty,

       -- sellable = structurally ready AND at least one serving of USABLE
       -- (non-expired) stock on hand
       CASE
         WHEN m.is_unli = 1       THEN CASE WHEN COALESCE(x.blocked_streams,0)=0
                                             AND COALESCE(ucap.min_stream,0) > 0
                                            THEN 1 ELSE 0 END
         WHEN m.recipe_id IS NULL THEN 0
         WHEN COALESCE(b.blocked,0)  > 0 THEN 0
         WHEN COALESCE(u.unmapped,0) > 0 THEN 0
         WHEN COALESCE(cap.max_qty,0) < 1 THEN 0
         ELSE 1
       END AS sellable,

       CASE
         WHEN m.is_unli = 1 AND COALESCE(x.blocked_streams,0) > 0
              THEN CAST(x.blocked_streams AS TEXT) || ' unlimited stream(s) not deductable'
         WHEN m.is_unli = 1 AND COALESCE(ucap.min_stream,0) <= 0 THEN 'an unlimited stream is out of stock'
         WHEN m.is_unli = 1               THEN NULL
         WHEN m.recipe_id IS NULL         THEN 'no recipe linked'
         WHEN COALESCE(u.unmapped,0) > 0  THEN CAST(u.unmapped AS TEXT) || ' unmapped ingredient(s)'
         WHEN COALESCE(b.blocked,0)  > 0  THEN CAST(b.blocked AS TEXT) || ' ingredient(s) without a pack size'
         WHEN COALESCE(cap.max_qty,0) < 1 THEN 'out of stock: ' || COALESCE(lim.limiting, 'an ingredient')
         ELSE NULL
       END AS block_reason,

       lim.limiting AS limiting_ingredient,
       COALESCE(b.blocked,0)  AS blocked_leaves,
       COALESCE(u.unmapped,0) AS unmapped_lines
  FROM menu_item m
  LEFT JOIN (SELECT l.recipe_id, COUNT(*) AS blocked
               FROM v_recipe_leaf l
               JOIN v_ingredient_readiness r ON r.id = l.ingredient_id
              WHERE r.is_deductable = 0
              GROUP BY l.recipe_id) b ON b.recipe_id = m.recipe_id
  LEFT JOIN (SELECT recipe_id, COUNT(*) AS unmapped
               FROM unmapped_recipe_line
              WHERE state = 'open' AND recipe_id IS NOT NULL
              GROUP BY recipe_id) u ON u.recipe_id = m.recipe_id
  LEFT JOIN v_recipe_capacity cap ON cap.recipe_id = m.recipe_id
  -- name the ingredient that runs out first (usable stock), so the block
  -- reason is actionable
  LEFT JOIN (SELECT l.recipe_id, i.name AS limiting
               FROM v_recipe_leaf l
               JOIN ingredient i ON i.id = l.ingredient_id
               LEFT JOIN v_ingredient_expired_stock x ON x.ingredient_id = i.id
              WHERE l.qty_base > 0
              GROUP BY l.recipe_id
             HAVING MIN((i.on_hand_base - i.reserved_base - COALESCE(x.expired_base, 0)) / l.qty_base)
                  = ((i.on_hand_base - i.reserved_base - COALESCE(x.expired_base, 0)) / l.qty_base)) lim
         ON lim.recipe_id = m.recipe_id
  LEFT JOIN (SELECT COUNT(*) AS blocked_streams
               FROM unli_item ui
               JOIN v_ingredient_readiness r ON r.id = ui.ingredient_id
              WHERE ui.is_active = 1 AND r.is_deductable = 0) x ON 1 = 1
  LEFT JOIN (SELECT MIN(i.on_hand_base - i.reserved_base - COALESCE(xs.expired_base, 0)) AS min_stream
               FROM unli_item ui
               JOIN ingredient i ON i.id = ui.ingredient_id
               LEFT JOIN v_ingredient_expired_stock xs ON xs.ingredient_id = i.id
              WHERE ui.is_active = 1) ucap ON 1 = 1;

INSERT INTO schema_version (version, name) VALUES (40, 'expired_stock_usability');
