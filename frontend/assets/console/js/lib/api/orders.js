import { api } from './client.js';

/**
 * Orders & Sales — the console's read-only order ledger (2026-09 IA
 * addition). Backs the same GET /pos/orders the till itself uses for its
 * Current Orders / Order History screens (wt/api.py's list_orders) — this is
 * a second READER of that one record, never a second calculation. Both
 * admin and owner hold order.read.
 *
 * status='open' returns tables currently in play (punched/in_progress/
 * ready). status='all' defaults to the till's own rolling 3-Manila-calendar-
 * month window (right for a till screen) — the console's Order History page
 * is BI, not a till screen, so it passes `allTime: true` or a `startDate`/
 * `endDate` range to see the full ledger; see list_orders' own docstring.
 * `opts` also accepts the backend's own param names (`start_date`,
 * `end_date`, `all_time`) passed straight through, same pattern as
 * getSales() in ./reports.js.
 */
/**
 * One order's full detail + line items — GET /pos/orders/{id} (wt/api.py's
 * order_detail()). The lifecycle timestamps on `order` (created_at/
 * committed_at/served_at/voided_at) ARE this schema's status history; there
 * is no separate status-change log table, so nothing here is reconstructed
 * or inferred beyond what the order record itself already carries.
 */
export async function getOrder(id) {
  const out = await api.get(`/pos/orders/${id}`);
  const o = out.order;
  return {
    id: o.id, orderNo: o.order_no, status: o.status, tableLabel: o.table_label,
    headCount: o.head_count, createdBy: o.created_by, createdAt: o.created_at,
    committedAt: o.committed_at, servedAt: o.served_at, voidedAt: o.voided_at,
    voidReason: o.void_reason, subtotal: o.subtotal, discountType: o.discount_type,
    discountAmount: o.discount_amount, serviceCharge: o.service_charge, total: o.total,
    cashReceived: o.cash_received, changeDue: o.change_due, source: o.source,
    sourceOrderId: o.source_order_id,
    lines: (out.lines || []).map((l) => ({ name: l.name, qty: l.qty, unitPrice: l.unit_price, note: l.note })),
  };
}

export async function listOrders({ status = 'all', limit = 200, allTime = false, startDate = null, endDate = null } = {}) {
  const params = { status, limit };
  if (allTime) params.all_time = true;
  if (startDate) params.start_date = startDate;
  if (endDate) params.end_date = endDate;
  const out = await api.get('/pos/orders', params);
  return {
    totalCount: out.total_count ?? null,
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
