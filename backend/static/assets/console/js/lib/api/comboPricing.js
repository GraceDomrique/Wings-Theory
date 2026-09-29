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
