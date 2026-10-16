-- =============================================================================
-- 0048  Combo publish — turns an approved combo idea into a real sellable
--       POS item, closing the gap migration 0036 explicitly left open
--       ("publishing a combo to the live menu is a distinct action outside
--       this migration's and this feature's scope").
--
-- A published combo is an ordinary menu_item row: same table, same
-- v_menu_item_availability gate, same order_line/reservation/depletion/void
-- path every other menu item already goes through. is_combo and
-- source_draft_combo_id are provenance only (so the admin UI and reporting
-- can tell a combo apart from a workbook-sourced item) — nothing in the
-- order/kitchen/inventory pipeline branches on them.
--
-- combo_component is the CURRENT composition of a published combo (which
-- menu items, what quantity of each) — the data the admin "edit this
-- combo's items" UI reads and writes. It is deliberately mutable, unlike
-- draft_combo's append-only snapshot: a published combo is a live sellable
-- entity whose price/items/image can change, same as any other menu_item's
-- price or category can. Editing it regenerates the combo's own recipe_line
-- rows (wt/combos.py); it never touches the component items' own recipes,
-- and it never touches a past order's already-recorded reservation/
-- depletion, because those were computed from the recipe as it stood at
-- order time, not stored as a forward-looking pointer to today's recipe.
--
-- image_path is general to every menu_item, not combo-only (no menu item of
-- any kind has ever had an image before this) — a nullable pointer to a
-- file under the uploads directory wt/combos.py manages; the image bytes
-- themselves are never stored in SQLite.
-- =============================================================================

ALTER TABLE menu_item ADD COLUMN image_path TEXT;
ALTER TABLE menu_item ADD COLUMN is_combo INTEGER NOT NULL DEFAULT 0;
ALTER TABLE menu_item ADD COLUMN source_draft_combo_id INTEGER REFERENCES draft_combo(id) ON DELETE SET NULL;

CREATE TABLE combo_component (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  combo_menu_item_id     INTEGER NOT NULL REFERENCES menu_item(id) ON DELETE CASCADE,
  component_menu_item_id INTEGER NOT NULL REFERENCES menu_item(id) ON DELETE RESTRICT,
  quantity               REAL    NOT NULL CHECK (quantity > 0),
  sort_order             INTEGER NOT NULL DEFAULT 0,
  created_at             TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_combo_component_combo ON combo_component (combo_menu_item_id, sort_order);
CREATE INDEX ix_combo_component_component ON combo_component (component_menu_item_id);
