-- =============================================================================
-- 0038  Order numbers move from WT-<n> to WTRORD-<year><6-digit sequence>
--       (e.g. WTRORD-2026000123), matching the owner's requested receipt
--       format. legacy_order_no preserves every order's original WT-<n>
--       value so admin_audit_log rows written before this migration (which
--       carry the OLD order_no as entity_id, per wt/audit.py's append-only
--       contract -- those rows are never rewritten) can still be traced back
--       to the order they describe.
--
-- The actual renumbering of existing rows is done by a one-off data script
-- (not embedded here as SQL) because assigning a chronological per-year
-- sequence requires sorting all rows first -- a single UPDATE statement
-- would need an O(n^2) correlated subquery for that ordering. This migration
-- only adds the column; wt/scripts/rename_order_numbers.py performs the
-- actual backfill once, immediately after this migration is applied.
-- =============================================================================

ALTER TABLE customer_order ADD COLUMN legacy_order_no TEXT;

INSERT INTO schema_version (version, name) VALUES (38, 'order_number_rename');
