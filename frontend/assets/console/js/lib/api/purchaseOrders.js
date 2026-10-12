import { api } from './client.js';

/**
 * Purchase orders — Draft -> Ordered -> Partially Received -> Received.
 *
 * ROUTE MAP:
 *   GET  /console/purchase-orders                         list
 *   GET  /console/purchase-orders/{pk}                     detail
 *   POST /console/purchase-orders                          create (owner) — draft, no inventory effect
 *   POST /console/purchase-orders/{pk}/order                admin
 *   POST /console/purchase-orders/{pk}/cancel                owner (own draft) or admin (draft/ordered)
 *   POST /console/purchase-orders/items/{itemPk}/receive      admin — the ONLY endpoint that ever
 *                                                              touches inventory in this module
 *
 * client_request_id (a fresh random string per submit) makes receiving safe
 * to retry — the same idempotency pattern POS order creation already uses.
 */

function itemFromApi(r) {
  return {
    id: r.id,
    ingredientId: r.ingredient_id,
    ingredientName: r.ingredient_name,
    baseUnit: r.base_unit,
    recommendedQtyBase: r.recommended_qty_base,
    quantity: r.quantity,
    unit: r.unit,
    receivedQtyBase: r.received_qty_base,
    note: r.note,
  };
}

function poFromApi(r) {
  return {
    id: r.id,
    status: r.status,
    supplier: r.supplier,
    notes: r.notes,
    expectedDeliveryDate: r.expected_delivery_date,
    createdBy: r.created_by,
    createdAt: r.created_at,
    updatedAt: r.updated_at,
    orderedAt: r.ordered_at,
    cancelledAt: r.cancelled_at,
    cancelledBy: r.cancelled_by,
    cancelReason: r.cancel_reason,
    items: (r.items || []).map(itemFromApi),
  };
}

export async function listPurchaseOrders(status) {
  const out = await api.get('/console/purchase-orders', status ? { status } : undefined);
  return { items: (out.items || []).map(poFromApi), count: out.count || 0 };
}

export async function getPurchaseOrder(pk) {
  return poFromApi(await api.get(`/console/purchase-orders/${pk}`));
}

/** items: [{ ingredientId, quantity, recommendedQtyBase?, note? }, ...] */
export async function createPurchaseOrder({ supplier, notes, expectedDeliveryDate, items }) {
  const out = await api.post('/console/purchase-orders', {
    supplier: supplier || null,
    notes: notes || null,
    expected_delivery_date: expectedDeliveryDate || null,
    items: items.map((i) => ({
      ingredient_id: i.ingredientId,
      quantity: i.quantity,
      recommended_qty_base: i.recommendedQtyBase ?? null,
      note: i.note || null,
    })),
  });
  return poFromApi(out);
}

export async function markOrdered(pk) {
  return poFromApi(await api.post(`/console/purchase-orders/${pk}/order`));
}

export async function cancelPurchaseOrder(pk, reason) {
  return poFromApi(await api.post(`/console/purchase-orders/${pk}/cancel`, { reason: reason || null }));
}

function newRequestId() {
  return `po-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export async function receiveLine(itemPk, { qtyBase, lotNo, expiresAt }) {
  const out = await api.post(`/console/purchase-orders/items/${itemPk}/receive`, {
    qty_base: qtyBase,
    lot_no: lotNo || null,
    expires_at: expiresAt || null,
    client_request_id: newRequestId(),
  });
  return poFromApi(out);
}
