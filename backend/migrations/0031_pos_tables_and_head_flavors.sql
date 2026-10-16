-- =============================================================================
-- 0031  POS table selection (admin-configurable count) + per-head initial
--       flavour assignment for an unlimited session.
--
-- Table count
-- -----------
-- No new table: the restaurant's configured table count is one more row in
-- app_setting (migration 0010's generic operator-configurable-numbers
-- table, already used for unli_pieces_per_head/unli_variance_tolerance/
-- depletion_demand_model). 'pos_table_count' defaults to 9 by simply not
-- having a row yet -- wt.unli.get_setting(conn, 'pos_table_count', 9)
-- returns 9 until an admin explicitly sets it, so installing this migration
-- changes nothing about an existing deployment's behaviour.
--
-- Per-head flavour assignment
-- ----------------------------
-- Genuinely new, additive, informational data: which flavour each head at
-- an unlimited table initially chose (repeats allowed -- three diners can
-- all pick Buffalo). This is NOT a new pricing or consumption mechanism --
-- refill rounds keep working exactly as before (wt/refills.py's own
-- record_refill still sizes every round off the session's head_count alone,
-- "a till cannot talk the kitchen into a bigger round" -- unchanged). This
-- column exists purely so the POS can show and cap "which flavour is head
-- N having" against the session's own head_count, before any refill round
-- is ever served.
--
-- Stored as a JSON array of flavour display names (e.g.
-- '["Buffalo","Buffalo","Garlic Parmesan"]'), length-capped at head_count
-- in application code (wt/refills.set_head_flavors) -- a JSON array's
-- length isn't something a CHECK constraint can enforce cleanly, and the
-- validation already has to look up each name against unli_flavor anyway.
-- =============================================================================

ALTER TABLE unli_session ADD COLUMN head_flavors TEXT;

DROP VIEW v_unli_session_summary;

CREATE VIEW v_unli_session_summary AS
SELECT s.id AS session_id, s.order_id, o.order_no, o.status AS order_status,
       o.table_label, o.created_at AS order_created_at,
       o.subtotal, o.service_charge, o.total AS order_total,
       o.cash_received, o.change_due,
       s.head_count, s.status, s.started_at, s.ended_at,
       s.created_by, s.completed_by, s.note, s.head_flavors,
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

INSERT INTO schema_version (version, name) VALUES (31, 'pos_tables_and_head_flavors');
