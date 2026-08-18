-- =============================================================================
-- 0003  Split "sellable" into two questions the POS has to ask separately.
--
-- 0001/0002 conflated them. An item whose recipe is structurally complete was
-- reported sellable even with zero stock, so the POS showed it available and
-- then reserve_order rejected it — the customer finds out after ordering.
--
--   structurally_ready : the recipe resolves and every leaf can be deducted.
--                        A data-readiness property. Fixed by the owner in the
--                        console, not by buying anything.
--   max_qty            : how many the current on-hand actually covers.
--                        An operational property. Fixed by restocking.
--
-- sellable now requires both. block_reason distinguishes them so the greyed-out
-- card can say "out of stock" rather than something the cashier cannot act on.
-- =============================================================================

DROP VIEW v_menu_item_availability;

-- Servings each recipe can still produce, limited by its tightest ingredient.
-- Uses available (on_hand - reserved) so a held order removes stock from view.
CREATE VIEW v_recipe_capacity AS
SELECT l.recipe_id,
       CAST(MIN((i.on_hand_base - i.reserved_base) / l.qty_base) AS INTEGER) AS max_qty,
       MIN(i.name) AS any_ingredient
  FROM v_recipe_leaf l
  JOIN ingredient i ON i.id = l.ingredient_id
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

       -- sellable = structurally ready AND at least one serving on hand
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
  -- name the ingredient that runs out first, so the block reason is actionable
  LEFT JOIN (SELECT l.recipe_id, i.name AS limiting
               FROM v_recipe_leaf l
               JOIN ingredient i ON i.id = l.ingredient_id
              WHERE l.qty_base > 0
              GROUP BY l.recipe_id
             HAVING MIN((i.on_hand_base - i.reserved_base) / l.qty_base)
                  = ((i.on_hand_base - i.reserved_base) / l.qty_base)) lim
         ON lim.recipe_id = m.recipe_id
  LEFT JOIN (SELECT COUNT(*) AS blocked_streams
               FROM unli_item ui
               JOIN v_ingredient_readiness r ON r.id = ui.ingredient_id
              WHERE ui.is_active = 1 AND r.is_deductable = 0) x ON 1 = 1
  LEFT JOIN (SELECT MIN(i.on_hand_base - i.reserved_base) AS min_stream
               FROM unli_item ui JOIN ingredient i ON i.id = ui.ingredient_id
              WHERE ui.is_active = 1) ucap ON 1 = 1;

INSERT INTO schema_version (version, name) VALUES (3, 'stock_availability');
