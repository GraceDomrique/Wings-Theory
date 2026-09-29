-- =============================================================================
-- 0039  Correct historical Unlimited Wings subtotal/total to per-head pricing.
--
-- Background: the owner's original xlsx history import (wt/xlsx_history_import.py)
-- recorded 21,013 orders' subtotal as a flat per-ORDER charge (order_line.qty
-- pinned to 1 for unli lines regardless of head_count), not per-head. The
-- owner has confirmed the shop's actual, always-true pricing rule is ₱399 per
-- head -- see README.md's "Is ₱399 per head or per table?" section for the
-- full ambiguity this resolves (a documented ₱588,126 gap between the two
-- readings). This migration restates those 21,013 orders' subtotal/total to
-- SUM(order_line.unit_price * head_count) for unli lines, matching exactly
-- what wt/api.py's live create_order() has always computed for new orders.
--
-- Scope guard: only rows with discount_amount=0 AND service_charge=0 are
-- touched (verified: every affected historical row has both at 0 -- the
-- xlsx import never populated discounts or service charge). This is a
-- defensive boundary, not a formula choice -- if a row outside that shape
-- ever needs correcting, it should be reviewed by hand, not swept in here
-- unexamined. leftover_charge_total (migration 0037) is untouched -- it is
-- deliberately a separate, additive column, never folded into total.
-- cash_received/change_due are NULL on every affected row (the xlsx import
-- never recorded cash tendered), so there is nothing to reconcile there.
--
-- subtotal/total are guarded by trg_order_financials_immutable (0011/0019)
-- once an order is settled. That guard exists to stop a settled order's
-- figures being edited after the fact through the app; a reviewed,
-- versioned, one-time historical correction — recreating the same trigger
-- immediately after — is a different thing from what it defends against.
-- =============================================================================

DROP TRIGGER trg_order_financials_immutable;

UPDATE customer_order
SET subtotal = (
      SELECT COALESCE(SUM(
        ol.unit_price * (CASE WHEN mi.is_unli = 1 THEN customer_order.head_count ELSE ol.qty END)
      ), 0)
      FROM order_line ol JOIN menu_item mi ON mi.id = ol.menu_item_id
      WHERE ol.order_id = customer_order.id
    ),
    total = (
      SELECT COALESCE(SUM(
        ol.unit_price * (CASE WHEN mi.is_unli = 1 THEN customer_order.head_count ELSE ol.qty END)
      ), 0)
      FROM order_line ol JOIN menu_item mi ON mi.id = ol.menu_item_id
      WHERE ol.order_id = customer_order.id
    )
WHERE discount_amount = 0
  AND service_charge = 0
  AND ROUND(subtotal, 2) != ROUND((
      SELECT COALESCE(SUM(
        ol.unit_price * (CASE WHEN mi.is_unli = 1 THEN customer_order.head_count ELSE ol.qty END)
      ), 0)
      FROM order_line ol JOIN menu_item mi ON mi.id = ol.menu_item_id
      WHERE ol.order_id = customer_order.id
  ), 2);

CREATE TRIGGER trg_order_financials_immutable
BEFORE UPDATE OF subtotal, discount_type, discount_amount, service_charge, total,
                  cash_received, change_due
ON customer_order
FOR EACH ROW WHEN OLD.total IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'a settled order''s financial figures are immutable — record an additional charge against it instead');
END;

INSERT INTO schema_version (version, name) VALUES (39, 'historical_unli_subtotal_correction');
