-- =============================================================================
-- 0030  Historical-sale provenance: source + source_order_id.
--
-- The problem this solves
-- ------------------------
-- ~8,918 of this database's orders were bulk-imported from the owner's real
-- sales-history workbook (data/Wings_Theory_Order_History.xlsx), replayed
-- through the same reserve_order/commit_order pipeline a live sale uses (see
-- wt/xlsx_history_import.py). They are genuine historical Wings Theory
-- sales, not demo/test data -- but the only thing distinguishing them from a
-- live order today is created_by IS NULL, which is not a safe permanent
-- marker: NULL there could mean other things in the future, and it carries
-- no positive information ("this IS historical") beyond an absence.
--
-- The workbook's own Order ID (e.g. "WT-20260601-006") was also never
-- persisted anywhere -- the importer used it only as an in-memory grouping
-- key before replaying, then discarded it, which is why the existing 8,918
-- rows cannot be traced back to a specific source transaction today.
--
-- What this migration adds, and does not add
-- ---------------------------------------------
-- Two new nullable columns on customer_order:
--   source            -- NULL for every live-punched order (unchanged
--                         meaning), 'historical' for an imported one. The
--                         CHECK constraint deliberately allows only that one
--                         value -- 'demo'/'test'/'seed'/'synthetic' are
--                         refused at the schema level, because those words
--                         describe how a record was produced, not what it
--                         is: every 'historical' row is a real completed
--                         sale, and the column must never be able to imply
--                         otherwise.
--   source_order_id   -- the original workbook Order ID, when it can be
--                         confidently identified. Never the live order_no --
--                         order_no keeps its own sequential scheme
--                         unconditionally; this is purely a cross-reference.
--
-- Both are additive and nullable: every existing row keeps its current
-- meaning unchanged (NULL, exactly as before) until a separate, reviewable
-- backfill (wt/xlsx_history_reconcile.py) explicitly sets them.
--
-- Not covered by trg_order_financials_immutable (0011/0019): that trigger
-- only guards the money columns, and only once a row's `total` is already
-- non-NULL. Neither new column is a financial figure, and the backfill this
-- migration enables specifically targets rows where `total IS NULL` --
-- outside that trigger's guard either way.
-- =============================================================================

ALTER TABLE customer_order ADD COLUMN source TEXT
  CHECK (source IS NULL OR source = 'historical');

ALTER TABLE customer_order ADD COLUMN source_order_id TEXT;

-- Deliberately NOT a UNIQUE index: a mixed workbook order (one Order ID
-- containing both a-la-carte and unlimited-wings lines) is represented as
-- TWO customer_order rows (its regular half and its unlimited half -- see
-- xlsx_history_reconcile.py's docstring for why they cannot be safely
-- merged into one after the fact), and both legitimately carry the same
-- source_order_id so the UI can show them as one historical sale split
-- across two receipts rather than two unrelated purchases. A UNIQUE
-- constraint would reject that legitimate case; duplicate-import
-- prevention (Problem 5) is instead enforced by the reconciliation script
-- itself querying this column before writing, which this index makes cheap.
CREATE INDEX ix_customer_order_source_order_id ON customer_order (source_order_id)
  WHERE source_order_id IS NOT NULL;

CREATE INDEX ix_customer_order_source ON customer_order (source)
  WHERE source IS NOT NULL;

INSERT INTO schema_version (version, name) VALUES (30, 'historical_source_tracking');
