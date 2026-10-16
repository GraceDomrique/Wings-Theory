import { api } from './client.js';

/**
 * Cost-based combo pricing — the "Create combo idea" flow.
 *
 * ROUTE MAP:
 *   POST /console/combo-pricing/preview          — read-only cost + price range
 *   GET  /console/combo-pricing-config            — pricing settings (System page)
 *   PATCH /console/combo-pricing-config
 *   POST /console/combo-pricing/drafts             — save a draft combo idea
 *   GET  /console/combo-pricing/drafts
 *   POST /console/combo-pricing/drafts/{id}/review — approve/reject a draft
 *
 * All ₱ figures here are estimates built from the real recipe/BOM cost data
 * (see wt/combo_pricing.py) — never a guess. A missing cost input (no
 * recipe, an unpriced ingredient, labor/overhead not yet configured) comes
 * back as a null with its own `*Status` field, never as a fabricated 0.
 */

function mapCostLine(item) {
  return {
    menuItemId: item.menu_item_id,
    name: item.name,
    price: item.price ?? null,
    ingredientCost: item.ingredient_cost ?? null,
    packagingCost: item.packaging_cost ?? null,
    costConfidence: item.cost_confidence,
    unpricedIngredients: item.unpriced_ingredients || [],
  };
}

function mapCostEstimate(est) {
  return {
    itemA: mapCostLine(est.item_a),
    itemB: mapCostLine(est.item_b),
    portionMultiplierA: est.portion_multiplier_a,
    portionMultiplierB: est.portion_multiplier_b,
    ingredientCost: est.ingredient_cost ?? null,
    ingredientStatus: est.ingredient_status,
    packagingCost: est.packaging_cost ?? null,
    laborCost: est.labor_cost ?? null,
    laborStatus: est.labor_status,
    overheadCost: est.overhead_cost ?? null,
    overheadStatus: est.overhead_status,
    totalCost: est.total_cost ?? null,
    totalStatus: est.total_status,
    unpricedIngredients: est.unpriced_ingredients || [],
    currentMenuTotal: est.current_menu_total ?? null,
  };
}

function mapPriceSuggestion(p) {
  return {
    range: p.range || null,
    costBasedPrice: p.cost_based_price ?? null,
    issue: p.issue ?? null,
  };
}

export async function getComboPricingPreview({
  itemAId, itemBId, portionMultiplierA = 1.0, portionMultiplierB = 1.0,
}) {
  const out = await api.post('/console/combo-pricing/preview', {
    item_a_menu_item_id: itemAId,
    item_b_menu_item_id: itemBId,
    portion_multiplier_a: portionMultiplierA,
    portion_multiplier_b: portionMultiplierB,
  });
  return {
    cost: mapCostEstimate(out.cost_estimate),
    price: mapPriceSuggestion(out.price_suggestion),
  };
}

export async function getComboPricingConfig() {
  const out = await api.get('/console/combo-pricing-config');
  return {
    targetFoodCostPct: out.target_food_cost_pct ?? null,
    laborMethod: out.labor_method ?? null,
    laborFixedAmount: out.labor_fixed_amount ?? null,
    laborPct: out.labor_pct ?? null,
    overheadPct: out.overhead_pct ?? null,
    preferredMinPrice: out.preferred_min_price ?? null,
    preferredMaxPrice: out.preferred_max_price ?? null,
    maxDiscountPct: out.max_discount_pct ?? null,
  };
}

export function setComboPricingConfig(body) {
  return api.patch('/console/combo-pricing-config', body);
}

export async function createDraftCombo({
  itemAId, itemBId, name, portionMultiplierA = 1.0, portionMultiplierB = 1.0,
  chosenPrice, note, pairStats, clientRequestId,
}) {
  const out = await api.post('/console/combo-pricing/drafts', {
    item_a_menu_item_id: itemAId,
    item_b_menu_item_id: itemBId,
    name,
    portion_multiplier_a: portionMultiplierA,
    portion_multiplier_b: portionMultiplierB,
    chosen_price: chosenPrice ?? null,
    note: note ?? null,
    pair_stats: pairStats || null,
    client_request_id: clientRequestId,
  });
  return { id: out.id, status: 'draft', createdAt: out.created_at };
}

export async function listDraftCombos() {
  const out = await api.get('/console/combo-pricing/drafts');
  return (out.drafts || []).map((d) => ({
    id: d.id,
    itemAMenuItemId: d.item_a_menu_item_id,
    itemBMenuItemId: d.item_b_menu_item_id,
    name: d.name,
    status: d.status,
    chosenPrice: d.chosen_price ?? null,
    note: d.note,
    createdBy: d.created_by,
    createdAt: d.created_at,
    pricingSnapshot: d.pricing_snapshot,
  }));
}

export function reviewDraftCombo(draftId, status, note) {
  return api.post(`/console/combo-pricing/drafts/${draftId}/review`, { status, note: note || null });
}

/**
 * Publishing a combo to the live menu/POS — wt/combos.py. Everything below
 * this line is the "becomes a real sellable item" half of the workflow;
 * everything above is the "cost-based idea" half.
 *
 * ROUTE MAP:
 *   POST /console/combos/preview          — read-only cost preview, N items
 *   POST /console/combos/image-upload     — base64 image -> image_path
 *   GET  /console/combos                  — list published combos
 *   GET  /console/combos/{id}
 *   POST /console/combos                  — publish
 *   PATCH /console/combos/{id}             — edit items/price/name/image
 *   PATCH /console/combos/{id}/visibility  — show/hide on POS
 */
function mapComboCostEstimate(est) {
  return {
    items: (est.items || []).map((i) => ({ ...mapCostLine(i), quantity: i.quantity })),
    ingredientCost: est.ingredient_cost ?? null,
    ingredientStatus: est.ingredient_status,
    packagingCost: est.packaging_cost ?? null,
    laborCost: est.labor_cost ?? null,
    laborStatus: est.labor_status,
    overheadCost: est.overhead_cost ?? null,
    overheadStatus: est.overhead_status,
    totalCost: est.total_cost ?? null,
    totalStatus: est.total_status,
    unpricedIngredients: est.unpriced_ingredients || [],
    currentMenuTotal: est.current_menu_total ?? null,
  };
}

export async function getComboPreview(components) {
  const out = await api.post('/console/combos/preview', {
    components: components.map((c) => ({ menu_item_id: c.menuItemId, quantity: c.quantity })),
  });
  return { cost: mapComboCostEstimate(out.cost_estimate), price: mapPriceSuggestion(out.price_suggestion) };
}

export async function uploadComboImage(contentBase64) {
  const out = await api.post('/console/combos/image-upload', { content_base64: contentBase64 });
  return out.image_path;
}

function mapCombo(d) {
  return {
    menuItemId: d.menu_item_id,
    code: d.code,
    name: d.name,
    category: d.category,
    price: d.price,
    imagePath: d.image_path,
    posVisible: !!d.pos_visible,
    sourceDraftComboId: d.source_draft_combo_id,
    createdAt: d.created_at,
    updatedAt: d.updated_at,
    components: (d.components || []).map((c) => ({
      menuItemId: c.menu_item_id, quantity: c.quantity, name: c.name, price: c.price, category: c.category,
    })),
  };
}

export async function listCombos() {
  const out = await api.get('/console/combos');
  return (out.combos || []).map(mapCombo);
}

export async function getCombo(menuItemId) {
  return mapCombo(await api.get(`/console/combos/${menuItemId}`));
}

export async function publishCombo({ name, category, price, components, imagePath, draftComboId }) {
  return mapCombo(await api.post('/console/combos', {
    name, category, price,
    components: components.map((c) => ({ menu_item_id: c.menuItemId, quantity: c.quantity })),
    image_path: imagePath || null,
    draft_combo_id: draftComboId || null,
  }));
}

export async function updateCombo(menuItemId, { name, category, price, components, imagePath, removeImage }) {
  const body = {};
  if (name !== undefined) body.name = name;
  if (category !== undefined) body.category = category;
  if (price !== undefined) body.price = price;
  if (components !== undefined) body.components = components.map((c) => ({ menu_item_id: c.menuItemId, quantity: c.quantity }));
  if (imagePath !== undefined) body.image_path = imagePath;
  if (removeImage !== undefined) body.remove_image = removeImage;
  return mapCombo(await api.patch(`/console/combos/${menuItemId}`, body));
}

export async function setComboVisibility(menuItemId, posVisible) {
  return mapCombo(await api.patch(`/console/combos/${menuItemId}/visibility`, { pos_visible: posVisible }));
}
