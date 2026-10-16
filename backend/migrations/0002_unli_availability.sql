-- =============================================================================
-- 0002  Teach the menu gate about unlimited items.
--
-- An unli ticket has no bill of materials by design: consumption is unknown at
-- the point of sale and is estimated from the portion coefficient instead. The
-- 0001 gate treated that as "no recipe linked" and blocked the shop's single
-- highest-value product, which is the wrong answer for the right reason.
--
-- An unli item is sellable when every active unli stream it draws on is itself
-- deductable — that is, wings, rice, fries and drinks all have a pack size.
-- Blocking on any one of them is correct: you cannot honour an unlimited-rice
-- promise with no rice conversion on file.
-- =============================================================================

DROP VIEW v_menu_item_availability;

CREATE VIEW v_menu_item_availability AS
SELECT m.id            AS menu_item_pk,
       m.menu_item_id,
       m.name,
       m.category,
       m.price,
       m.recipe_id,
       m.is_unli,
       CASE
         WHEN m.is_unli = 1        THEN CASE WHEN COALESCE(x.blocked_streams, 0) = 0
                                             THEN 1 ELSE 0 END
         WHEN m.recipe_id IS NULL  THEN 0
         WHEN COALESCE(b.blocked, 0)  > 0 THEN 0
         WHEN COALESCE(u.unmapped, 0) > 0 THEN 0
         ELSE 1
       END AS sellable,
       CASE
         WHEN m.is_unli = 1 AND COALESCE(x.blocked_streams, 0) > 0
              THEN CAST(x.blocked_streams AS TEXT) || ' unlimited stream(s) without a pack size'
         WHEN m.is_unli = 1               THEN NULL
         WHEN m.recipe_id IS NULL         THEN 'no recipe linked'
         WHEN COALESCE(u.unmapped, 0) > 0 THEN CAST(u.unmapped AS TEXT) || ' unmapped ingredient(s)'
         WHEN COALESCE(b.blocked, 0)  > 0 THEN CAST(b.blocked  AS TEXT) || ' ingredient(s) without a pack size'
         ELSE NULL
       END AS block_reason,
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
              GROUP BY recipe_id) u ON u.recipe_id = m.recipe_id
  LEFT JOIN (SELECT COUNT(*) AS blocked_streams
               FROM unli_item ui
               JOIN v_ingredient_readiness r ON r.id = ui.ingredient_id
              WHERE ui.is_active = 1 AND r.is_deductable = 0) x ON 1 = 1;

INSERT INTO schema_version (version, name) VALUES (2, 'unli_availability');
