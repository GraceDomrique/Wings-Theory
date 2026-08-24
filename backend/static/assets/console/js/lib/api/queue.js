import { api } from './client.js';

/**
 * The Ingredient (Unmapped Recipe Line) Queue — restored from the old
 * console, which had this as its own top-level page. It was dropped from
 * the rebuild entirely, which is the single biggest functional regression:
 * an unmapped line is what keeps a menu item's recipe from being sellable,
 * and there was no UI path left to resolve one.
 *
 * ROUTE MAP:
 *   GET  /console/queues/unmapped              — list
 *   POST /console/queues/unmapped/{pk}/resolve — map to a real ingredient,
 *        or dismiss
 *
 * Mapping writes an ingredient_alias so the next import of the same source
 * spreadsheet resolves automatically — there is no auto-guess path, a
 * person decides once. Resolving a line changes v_menu_item_availability's
 * unmapped_lines count for every menu item whose recipe contains it, which
 * is exactly what flips `sellable` on the POS menu — nothing here computes
 * sellability itself, it only asks the backend to resolve the block.
 */

const REASON_LABEL = {
  no_stock_record: 'No matching ingredient in the stock list',
  ambiguous: 'Matched more than one possible ingredient',
  not_stocked: 'Not something the kitchen holds stock of',
  utility: 'A utility line (e.g. "to taste") with nothing to deduct',
};

/** @returns {Promise<{items:any[], count:number, byReason:object}>} */
export async function listUnmapped(state = 'open') {
  const out = await api.get('/console/queues/unmapped', { state });
  return {
    items: (out.items || []).map((r) => ({
      id: r.id,
      recipeId: r.recipe_id,
      sourceRecipeName: r.source_recipe_name,
      sourceIngredientLabel: r.source_ingredient_label,
      sourceQtyText: r.source_qty_text || null,
      parsedQty: r.parsed_qty ?? null,
      parsedUnit: r.parsed_unit || null,
      reason: r.reason,
      reasonLabel: REASON_LABEL[r.reason] || r.reason,
      blocksMenuItems: r.blocks_menu_items || 0,
      createdAt: r.created_at,
    })),
    count: out.count || 0,
    byReason: out.by_reason || {},
  };
}

/** Map an unmapped line onto a real ingredient. */
export function resolveMap(pk, ingredientPk, note) {
  return api.post(`/console/queues/unmapped/${pk}/resolve`, {
    action: 'map', ingredient_pk: ingredientPk, note: note || null,
  });
}

/** Dismiss an unmapped line without mapping it (e.g. a genuine "to taste" line). */
export function resolveDismiss(pk, note) {
  return api.post(`/console/queues/unmapped/${pk}/resolve`, {
    action: 'dismiss', note: note || null,
  });
}
