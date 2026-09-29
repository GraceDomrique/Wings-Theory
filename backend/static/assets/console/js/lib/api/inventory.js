import { api } from './client.js';

/**
 * Inventory domain — ingredients, batches, receiving, the ledger.
 *
 * ROUTE MAP (spec section 4 name -> what this backend actually exposes):
 *   GET /ingredients               -> GET /console/ingredients
 *   GET /ingredients/:id/batches   -> GET /console/ingredients/{pk}   (batches
 *                                     ride along in the detail payload; there
 *                                     is no standalone batches endpoint)
 *   POST /receiving                -> POST /console/ingredients/{pk}/receive
 *   GET /ledger?...                -> GET /console/ledger?ingredient_id=&reason=
 *
 * Every adapter below is the single place a backend field name becomes a UI
 * field name. Routes never see a raw row.
 */

/** Backend category enum -> the labels in spec section 3. */
const CATEGORY = {
  wet_food: 'Wet food',
  dry_food: 'Dry food',
  consumable: 'Consumable',
  packaging: 'Operational supply',
  resale: 'Resale item',
};

/** @returns {import('../types.js').Ingredient} */
function toIngredient(r, depletion) {
  const dep = depletion ? depletion.get(r.id) : null;
  return {
    id: r.ingredient_id,
    pk: r.id,
    name: r.name,
    category: CATEGORY[r.category] || r.category,
    onHandUnit: r.purchase_unit,
    onHandBaseQty: r.on_hand_base,
    availableBaseQty: r.available_base,
    baseUnit: r.base_unit,
    onHandDisplay: r.on_hand_display || null,
    // Runway and usage rate are NOT computed here — they come from the
    // depletion endpoint (wt/forecasting.py). An ingredient the model has no
    // usage rate for gets null, which renders as "no usage rate yet", never 0.
    usedPerDay: dep && dep.avg_daily_consumption > 0 ? dep.avg_daily_consumption : null,
    daysLeft: dep ? dep.days_remaining : null,
    risk: dep ? dep.risk : null,
    leadTimeDays: r.lead_time_days ?? null,
    expiryDate: null, // per-batch, not per-ingredient — see listBatches()
    confidence: r.unverified_count > 0 ? 'unverified' : 'confirmed',
    // unit_cost is nullable in the schema and 'unset' is a real state. Passing
    // the null through is the whole point: the UI must say "no cost set".
    costPerUnit: r.unit_cost ?? null,
    costStatus: r.unit_cost_status,
    isDeductable: !!r.is_deductable,
    blockReason: r.block_reason || null,
    unverifiedCount: r.unverified_count || 0,
    inventoryType: r.inventory_type,
    purchaseUnit: r.purchase_unit || null,
    packSize: r.pack_size ?? null,
    yieldFactor: r.yield_factor ?? null,
    reorderPointBase: r.reorder_point_base ?? null,
    // Supplier was previously dropped by this adapter, so the ingredient
    // drawer rendered "not recorded" whether or not the record had one. It
    // is carried through now. On the current catalog it is genuinely null
    // for every ingredient — which the UI must be able to say honestly
    // rather than by accident.
    supplier: r.supplier || null,
    supplierContact: r.supplier_contact || null,
    storageLocation: r.storage_location || null,
    expiryMode: r.expiry_mode || null,
    shelfLifeDays: r.shelf_life_days ?? null,
    priceStatus: r.price_status || null,
    verificationStatus: r.verification_status || null,
    packSizeStatus: r.pack_size_status || null,
    yieldFactorStatus: r.yield_factor_status || null,
    reservedBaseQty: r.reserved_base ?? null,
    // The untouched record, for the one surface that needs every field
    // (the ingredient drawer). Routes must keep reading the mapped names
    // above; this is an escape hatch for a detail panel, not a licence to
    // read raw column names throughout the console.
    raw: r,
  };
}

/**
 * On Hand needs both the ingredient record and its runway, which live in two
 * endpoints. The join happens here, once, rather than in the route — and only
 * because there is genuinely no combined endpoint. (Today is different: it
 * must not fan out like this. See dashboard.js.)
 *
 * @param {{q?:string, limit?:number, withRunway?:boolean}} [opts]
 * @returns {Promise<import('../types.js').Ingredient[]>}
 */
export async function listIngredients({ q = '', limit = 200, withRunway = true } = {}) {
  const [list, dep] = await Promise.all([
    api.get('/console/ingredients', { q, limit }),
    withRunway ? api.get('/console/forecasts/depletion', { lookback_days: 30 }).catch(() => null) : null,
  ]);
  const byId = new Map();
  if (dep && dep.ingredients) for (const d of dep.ingredients) byId.set(d.ingredient_id, d);
  return (list.ingredients || []).map((r) => toIngredient(r, byId));
}

/** @returns {Promise<import('../types.js').StockBatch[]>} */
export async function listBatches(ingredientPk) {
  const detail = await api.get(`/console/ingredients/${ingredientPk}`);
  return batchesFrom(detail);
}

/**
 * FEFO order is server-computed — the detail endpoint returns batches in the
 * exact order deplete_fefo() draws them down. drawOrder is the array index,
 * never a client-side re-sort by date: expiry *source* matters, and undated
 * batches deliberately sort last (spec section 4, Batches).
 */
export function batchesFrom(detail) {
  const name = detail.ingredient ? detail.ingredient.name : '';
  const unit = detail.ingredient ? detail.ingredient.base_unit : '';
  return (detail.batches || []).map((b, i) => ({
    batchId: b.lot_no || `#${b.id}`,
    pk: b.id,
    ingredientId: detail.ingredient ? detail.ingredient.ingredient_id : null,
    ingredientName: name,
    receivedDate: b.received_at || null,
    expiryDate: b.expires_at || null,
    expirySource: b.tracking === 'exact_date' ? 'exact'
      : b.tracking === 'estimated_shelf_life' ? 'estimated' : 'none',
    qtyRemaining: b.qty_base ?? null,
    baseUnit: unit,
    drawOrder: i + 1,
    daysToExpiry: b.days_to_expiry ?? null,
    expired: !!b.expired,
    storageStatus: b.storage_status || null,
  }));
}

/** Full ingredient drawer: record, recipes it feeds, batches, recent movements. */
export async function getIngredient(pk) {
  const d = await api.get(`/console/ingredients/${pk}`);
  return {
    ingredient: toIngredient(d.ingredient, new Map(
      d.depletion ? [[d.ingredient.id, d.depletion]] : []
    )),
    usedIn: d.used_in || [],
    batches: batchesFrom(d),
    movements: (d.movements || []).map((m) => toMovement(m, d.ingredient)),
    depletion: d.depletion || null,
  };
}

/** @returns {import('../types.js').StockMovement} */
function toMovement(r, ingredient) {
  return {
    id: String(r.id),
    timestamp: r.created_at,
    ingredientId: r.ingredient_id ?? (ingredient ? ingredient.id : null),
    ingredientName: r.ingredient_name || (ingredient ? ingredient.name : ''),
    reason: r.reason,
    wastageReason: r.wastage_reason || null,
    changeQty: r.delta_base ?? null,
    stockAfter: r.balance_after_base ?? null,
    baseUnit: r.base_unit || (ingredient ? ingredient.base_unit : ''),
    refType: r.ref_type || null,
    refId: r.ref_id ?? null,
    // What a movement is linked to. Real ref_types in this ledger are 'order',
    // 'unli_refill', 'purchase', 'manual_count', 'manual_writeoff' and
    // 'unli_reconciliation' — NOT just 'order'. Restricting the link to orders
    // would hide the refill reference, which is the one that actually joins
    // the two halves of a correction (see pairRows in Movements.js).
    linkedOrderId: r.ref_type === 'order' && r.ref_id != null ? String(r.ref_id) : null,
    // Stable pair key: two ledger lines belong to the same event when they
    // share an ingredient AND the same source reference.
    pairKey: r.ref_type && r.ref_id != null
      ? `${r.ingredient_id ?? (ingredient ? ingredient.id : '')}|${r.ref_type}|${r.ref_id}`
      : null,
    note: r.note || null,
    userId: r.user_id || null,
  };
}

/**
 * The append-only ledger. Read-only by contract: there is no update or delete
 * function in this module, and there must never be one (spec section 6 point 2).
 */
export async function listMovements({ ingredientPk, reason, limit = 200, offset = 0 } = {}) {
  const out = await api.get('/console/ledger', {
    ingredient_id: ingredientPk, reason, limit, offset,
  });
  return { entries: (out.entries || []).map((r) => toMovement(r, null)), total: out.total || 0 };
}

/**
 * POST /console/ingredients/{pk}/receive — logs a delivery, opens a FEFO batch.
 * The endpoint takes purchase_qty in the ingredient's own purchase unit, plus
 * an optional lot number and expiry. It does NOT accept a unit cost: cost per
 * unit lives on the ingredient and is changed through the verification queue,
 * so receiving cannot quietly restate the valuation of existing stock.
 */
export function receive(pk, { purchaseQty, lotNo, expiresAt }) {
  return api.post(`/console/ingredients/${pk}/receive`, {
    purchase_qty: purchaseQty,
    lot_no: lotNo || null,
    expires_at: expiresAt || null,
  });
}

/**
 * Ledger reason -> plain language and severity.
 *
 * This table is the fix for the bug that triggered the rebuild. `void_credit`
 * is stock coming BACK from a cancelled order — it was being presented as
 * "thrown away". It is not a loss, it is the second half of a pair, and it
 * reads as a gain.
 *
 * Severity here is about how the entry should read, not about blame:
 * wastage/spoilage are real losses (crit), a void return is a reversal (warn,
 * because it means an order was cancelled), routine sales are flat.
 */
export const LEDGER_REASON = {
  purchase_receipt: { label: 'Delivery received', severity: 'good' },
  sale_commit: { label: 'Order deduction', severity: 'flat' },
  void_credit: { label: 'Order voided — return', severity: 'warn' },
  wastage: { label: 'Wastage', severity: 'crit' },
  spoilage: { label: 'Spoilage', severity: 'crit' },
  count_adjustment: { label: 'Stock count correction', severity: 'info' },
  transfer: { label: 'Transfer', severity: 'info' },
  unli_provisional: { label: 'Unlimited — provisional', severity: 'flat' },
  unli_reconcile: { label: 'Unlimited — reconciled', severity: 'info' },
};

/** The filter list Movements offers, in the order it offers them. */
export const LEDGER_REASONS = Object.keys(LEDGER_REASON);

// ===========================================================================
// Write operations restored from the previous production console. Each of
// these calls the SAME endpoint the old console.html called — no new
// business logic, no client-side recomputation of stock. The server's
// record_count / record_wastage / record_usage / receive_stock functions
// (wt/reservation.py) do the actual ledger + FEFO + on-hand arithmetic; this
// file only shapes the request and hands back what the server decided.
// ===========================================================================

/**
 * POST /console/ingredients/{pk}/count — reconcile a physical stock count.
 * Goes through reservation.record_count, which posts a `count_adjustment`
 * ledger entry for the gap between what the system expected and what was
 * physically counted. actual_base is in the ingredient's BASE unit (g/ml/pc),
 * not its purchase unit — the caller must convert before calling this.
 */
export function countStock(pk, { actualBase, note }) {
  return api.post(`/console/ingredients/${pk}/count`, {
    actual_base: actualBase, note: note || null,
  });
}

/**
 * POST /console/ingredients/bulk-count — a full stock take in one submission.
 * Each line still goes through record_count individually server-side (its
 * own ledger entry, its own variance); one bad line does not abort the rest.
 */
export function bulkCount(items, note) {
  return api.post('/console/ingredients/bulk-count', {
    items: items.map((i) => ({ ingredient_pk: i.pk, actual_base: i.actualBase })),
    note: note || null,
  });
}

/**
 * POST /console/ingredients/{pk}/waste — write stock off as thrown away or
 * spoiled, through reservation.record_wastage. qty_base > 0 (the amount lost,
 * not a signed delta — the endpoint decides the sign).
 */
export function wasteStock(pk, { qtyBase, reason = 'wastage', note }) {
  return api.post(`/console/ingredients/${pk}/waste`, {
    qty_base: qtyBase, reason, note: note || null,
  });
}

/**
 * POST /console/ingredients/{pk}/use — record consumption of an operational
 * supply that never passes through the POS (tissue, gloves, cleaning
 * alcohol), through reservation.record_usage.
 */
export function useStock(pk, { qtyBase, note }) {
  return api.post(`/console/ingredients/${pk}/use`, {
    qty_base: qtyBase, note: note || null,
  });
}

/**
 * PATCH /console/ingredients/{pk} — general field editing (supplier, reorder
 * point, lead time, expiry tracking mode, …). Only send fields the user
 * actually changed; the endpoint applies exclude_unset so an omitted field is
 * left untouched, not cleared.
 */
export function updateIngredient(pk, fields) {
  return api.patch(`/console/ingredients/${pk}`, fields);
}

/** POST /console/ingredients — create a new ingredient. */
export function createIngredient(body) {
  return api.post('/console/ingredients', body);
}
