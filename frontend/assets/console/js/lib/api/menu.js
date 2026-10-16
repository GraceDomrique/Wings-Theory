import { api } from './client.js';

/**
 * Recipes & Menu domain.
 *
 * ROUTE MAP:
 *   GET /menu-items          -> GET /pos/menu?include_blocked=true
 *   GET /recipes/:menuItemId -> GET /console/recipes  +  /console/recipes/{pk}
 *
 * Availability is read straight off v_menu_item_availability. `blockReason` is
 * backend-authored and is never inferred from stock levels here — spec section
 * 4 is explicit that the reason must come from the server, because a menu item
 * can be blocked for reasons stock quantity cannot show (an unmapped recipe
 * line, an ingredient with no pack size).
 */

/** @returns {Promise<{items:import('../types.js').MenuItem[], sellable:number, blocked:number}>} */
export async function listMenuItems() {
  const out = await api.get('/pos/menu', { include_blocked: true });
  return {
    items: (out.items || []).map((r) => ({
      id: r.menu_item_id,
      pk: r.menu_item_pk,
      name: r.name,
      category: r.category,
      price: r.price ?? null,
      sellable: !!r.sellable,
      blockReason: r.block_reason || null,
      blockedLeaves: r.blocked_leaves || 0,
      unmappedLines: r.unmapped_lines || 0,
      isUnli: !!r.is_unli,
      recipeId: r.recipe_id ?? null,
    })),
    sellable: out.sellable || 0,
    blocked: out.blocked || 0,
  };
}

/** @returns {Promise<import('../types.js').Recipe[]>} */
export async function listRecipes() {
  const out = await api.get('/console/recipes');
  return (out.recipes || []).map((r) => ({
    id: r.recipe_id,
    pk: r.id,
    name: r.name,
    kind: r.kind,
    menuItemId: null,
    lines: [],
    costPerServing: null,
    marginPct: null,
    lineCount: r.line_count || 0,
    unmappedCount: r.unmapped_count || 0,
    yieldQty: r.yield_qty,
    yieldUnit: r.yield_unit,
  }));
}

/**
 * One recipe with its lines and costing.
 *
 * recipe_cost() may legitimately be unable to price a recipe when one of its
 * ingredients has no unit_cost. Whatever it reports as missing is passed
 * through untouched so Recipes can say "3 ingredients have no cost set"
 * instead of quoting a total that silently excludes them.
 */
export async function getRecipe(pk) {
  const d = await api.get(`/console/recipes/${pk}`);
  const cost = d.cost || {};
  return {
    recipe: {
      id: d.recipe.recipe_id,
      pk: d.recipe.id,
      name: d.recipe.name,
      kind: d.recipe.kind,
      yieldQty: d.recipe.yield_qty,
      yieldUnit: d.recipe.yield_unit,
    },
    lines: (d.lines || []).map((l) => ({
      ingredientId: l.ingredient_id,
      label: l.display_label,
      name: l.ingredient_name,
      amount: l.qty_base ?? null,
      unit: l.base_unit,
      qtyStatus: l.qty_status,
      isOptional: !!l.is_optional,
      cost: null, // filled from `cost.lines` below when the backend priced it
    })),
    expandedLeaves: d.expanded_leaves || [],
    unmapped: d.unmapped || [],
    problems: d.problems || [],
    cost,
  };
}

/**
 * POST /console/recipes — create a recipe (menu or sub-recipe) with its
 * lines in one call. ingredient_pk is a real FK; there is no create-and-map
 * shortcut here — an ingredient must already exist (add it under Inventory
 * first, or resolve it through the Ingredient Queue).
 */
export function createRecipe({ name, kind, producesIngredientPk, yieldQty, yieldUnit, lines }) {
  return api.post('/console/recipes', {
    name,
    kind: kind || 'menu',
    produces_ingredient_pk: producesIngredientPk || null,
    yield_qty: yieldQty || 1,
    yield_unit: yieldUnit || 'serving',
    lines: (lines || []).map((l) => ({
      ingredient_pk: l.ingredientPk,
      display_label: l.displayLabel,
      qty_base: l.qtyBase,
      is_optional: !!l.isOptional,
      note: l.note || null,
    })),
  });
}

/**
 * PATCH /console/recipes/{pk} — full edit of an existing recipe's name,
 * yield and ingredient lines (admin-only, recipe.write). `kind` and which
 * ingredient a sub-recipe produces are not editable here — see the
 * endpoint's own docstring for why. Safe against orders already in
 * flight: reserve_order() snapshots quantities at POS-punch time, so this
 * can only affect orders reserved after it commits.
 */
export function editRecipe(pk, { name, yieldQty, yieldUnit, lines }) {
  return api.patch(`/console/recipes/${pk}`, {
    name,
    yield_qty: yieldQty || 1,
    yield_unit: yieldUnit || 'serving',
    lines: (lines || []).map((l) => ({
      ingredient_pk: l.ingredientPk,
      display_label: l.displayLabel,
      qty_base: l.qtyBase,
      is_optional: !!l.isOptional,
      note: l.note || null,
    })),
  });
}

/**
 * PATCH /console/menu-items/{pk}/price — the only console-writable field on
 * a menu item. The POS reads this same `menu_item.price` column on its next
 * menu load, so a change here reaches the till without any separate sync
 * step — there is exactly one price, not a console copy and a POS copy.
 */
export function editMenuPrice(pk, price) {
  return api.patch(`/console/menu-items/${pk}/price`, { price });
}
