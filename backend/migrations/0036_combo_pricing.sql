-- =============================================================================
-- 0036  Combo pricing — cost-based "Create combo idea" drafting for the
--       Frequently Bought Together (FP-growth) report.
--
-- 2026-09 redesign: the FP-growth report's only price figure used to be a
-- flat 8% discount off the sum of current menu prices (wt/associations.py's
-- COMBO_DISCOUNT) — a discount calculation, not a cost or margin one. This
-- migration adds the settings a real cost-based price range needs
-- (wt/combo_pricing.py) and the append-only record of a draft combo an
-- owner/admin actually chose to save.
--
-- app_setting only stores floats (see wt/unli.get_setting), so every new key
-- here is seeded at -1 ("not configured") rather than a guessed default —
-- the same enum-as-float convention 'depletion_demand_model' already uses.
-- Nothing is fabricated: until an admin sets these via
-- PATCH /console/combo-pricing-config, no cost-based price range is shown
-- at all (see combo_pricing.suggest_price_range's "not configured" path).
--
-- draft_combo follows the exact append-only convention as anomaly_review
-- (migration 0035) / ingredient_variance_review (0033): a full pricing
-- snapshot at creation time (Part 50/51 — auditable even after ingredient
-- costs later change), and its review/approval status is a SEPARATE
-- append-only log (draft_combo_review) rather than a mutated column, so the
-- original snapshot a decision was made from is never rewritten.
--
-- Approving a draft_combo_review row does NOT create a real menu_item or
-- POS product — publishing a combo to the live menu is a distinct action
-- outside this migration's and this feature's scope (spec Part 62: do not
-- touch POS/menu unless a dependency requires it). This table is the
-- owner's own review record, not a POS integration.
-- =============================================================================

INSERT INTO app_setting (key, value) VALUES ('combo_target_food_cost_pct', '-1');
INSERT INTO app_setting (key, value) VALUES ('combo_labor_method', '-1');
INSERT INTO app_setting (key, value) VALUES ('combo_labor_fixed_amount', '-1');
INSERT INTO app_setting (key, value) VALUES ('combo_labor_pct', '-1');
INSERT INTO app_setting (key, value) VALUES ('combo_overhead_pct', '-1');
INSERT INTO app_setting (key, value) VALUES ('combo_preferred_min_price', '-1');
INSERT INTO app_setting (key, value) VALUES ('combo_preferred_max_price', '-1');
INSERT INTO app_setting (key, value) VALUES ('combo_max_discount_pct', '-1');

CREATE TABLE draft_combo (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  item_a_menu_item_id   INTEGER NOT NULL REFERENCES menu_item(id) ON DELETE RESTRICT,
  item_b_menu_item_id   INTEGER NOT NULL REFERENCES menu_item(id) ON DELETE RESTRICT,
  name                  TEXT,
  portion_multiplier_a  REAL    NOT NULL DEFAULT 1.0,
  portion_multiplier_b  REAL    NOT NULL DEFAULT 1.0,
  -- Full JSON snapshot of estimate_combo_cost() + suggest_price_range() +
  -- the FP-growth pair stats (pair_count/support/confidence/lift) at the
  -- moment this draft was saved — the audit trail Part 50 requires, since
  -- ingredient costs (and therefore the price) can drift after today.
  pricing_snapshot      TEXT    NOT NULL,
  chosen_price          REAL,
  note                  TEXT,
  created_by            TEXT,
  client_request_id     TEXT UNIQUE,
  created_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_draft_combo_pair ON draft_combo (item_a_menu_item_id, item_b_menu_item_id, created_at);

CREATE TRIGGER trg_draft_combo_no_update
BEFORE UPDATE ON draft_combo
BEGIN SELECT RAISE(ABORT, 'draft_combo is append-only — record a draft_combo_review instead'); END;
CREATE TRIGGER trg_draft_combo_no_delete
BEFORE DELETE ON draft_combo
BEGIN SELECT RAISE(ABORT, 'draft_combo is append-only'); END;

CREATE TABLE draft_combo_review (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  draft_combo_id     INTEGER NOT NULL REFERENCES draft_combo(id) ON DELETE RESTRICT,
  status             TEXT    NOT NULL CHECK (status IN ('draft', 'approved', 'rejected')),
  note               TEXT,
  user_id            TEXT,
  client_request_id  TEXT UNIQUE,
  created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_draft_combo_review_combo ON draft_combo_review (draft_combo_id, created_at);

CREATE TRIGGER trg_draft_combo_review_no_update
BEFORE UPDATE ON draft_combo_review
BEGIN SELECT RAISE(ABORT, 'draft_combo_review is append-only'); END;
CREATE TRIGGER trg_draft_combo_review_no_delete
BEFORE DELETE ON draft_combo_review
BEGIN SELECT RAISE(ABORT, 'draft_combo_review is append-only'); END;
