-- =============================================================================
-- 0037  customer_order.leftover_charge_total -- the printed-on-the-menu
--        "Leftover Charge - Wings/Rice/Fries" penalty fee for unfinished
--        Unlimited Wings food.
--
-- The problem this solves
-- ------------------------
-- Three menu rows exist for this fee (see wt/xlsx_menu_audit_import.py's
-- IGNORED_MENU_ITEMS: "Leftover Charge - Wings/Rice/Fries") but were
-- deliberately never turned into menu_item rows, since they are a penalty
-- fee, not a sellable item, and have no ingredient lines in the menu/recipe
-- workbook. No other column or table has ever recorded this charge either:
-- order_charge.kind is CHECK-constrained to ('unli_flavor_addon',
-- 'addon_reversal') only, and no app_setting key for it exists. The result
-- was a real, printed-on-the-menu charge with no home anywhere in the
-- schema -- 680 instances of it in the owner's real order-history workbook
-- (wt/xlsx_history_import.py) were silently dropped on import as a side
-- effect of being correctly excluded from menu_item.
--
-- Why a new column on customer_order, not a new order_charge.kind
-- -------------------------------------------------------------------
-- order_charge.refill_id is a (nullable) FK to unli_refill: every existing
-- charge in that table is conceptually scoped to one refill round. A
-- leftover fee is not tied to any refill -- it is assessed once, at
-- checkout, against the whole table's unfinished food. Forcing it into
-- order_charge would misrepresent that scope. It belongs with the order's
-- other order-level money figures (subtotal/service_charge/total), as an
-- explicit column, the same shape as those.
--
-- What this migration adds
-- ----------------------------
-- One nullable, additive column: every existing row keeps its current
-- meaning (NULL, i.e. "no leftover fee on this order") until a separate,
-- reviewable backfill explicitly sets it for the 680 historical orders that
-- actually carried one, and the live checkout path is updated to write it
-- going forward.
--
-- Not covered by trg_order_financials_immutable (0011/0019): that trigger
-- guards exactly five columns (subtotal/service_charge/total/cash_received/
-- change_due) once total is non-NULL. leftover_charge_total is deliberately
-- NOT one of them -- same reasoning migration 0030 gave for source/
-- source_order_id: this is provenance/detail on an already-settled order,
-- not a figure that trigger's immutability guarantee is about.
-- =============================================================================

ALTER TABLE customer_order ADD COLUMN leftover_charge_total NUMERIC
  DEFAULT 0
  CHECK (leftover_charge_total IS NULL OR leftover_charge_total >= 0);

INSERT INTO schema_version (version, name) VALUES (37, 'leftover_charge_total');
