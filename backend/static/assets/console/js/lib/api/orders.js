import { api } from './client.js';

/**
 * Orders & Sales — the console's read-only order ledger (2026-09 IA
 * addition). Backs the same GET /pos/orders the till itself uses for its
 * Current Orders / Order History screens (wt/api.py's list_orders) — this is
 * a second READER of that one record, never a second calculation. Both
 * admin and owner hold order.read.
 *
 * status='open' returns tables currently in play (punched/in_progress/
 * ready); status='all' additionally returns everything from the last
 * rolling 3 Manila-calendar months (a query-level cap only — see
 * list_orders' own docstring — not a data deletion).
 */
export async function listOrders({ status = 'all', limit = 200 } = {}) {
  const out = await api.get('/pos/orders', { status, limit });
  return {
    orders: (out.orders || []).map((o) => ({
      id: o.id,
      orderNo: o.order_no,
      status: o.status,
      tableLabel: o.table_label,
      headCount: o.head_count,
      createdAt: o.created_at,
      committedAt: o.committed_at,
      servedAt: o.served_at,
      voidedAt: o.voided_at,
      subtotal: o.subtotal ?? null,
      discountType: o.discount_type,
      discountAmount: o.discount_amount ?? null,
      serviceCharge: o.service_charge ?? null,
      total: o.total ?? null,
      currentTotal: o.current_total ?? null,
      source: o.source,
      itemNames: o.item_names || null,
      lineCount: o.line_count ?? 0,
    })),
  };
}
