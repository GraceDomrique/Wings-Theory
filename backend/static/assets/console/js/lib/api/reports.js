import { api, ApiError } from './client.js';
import { getValuation } from './dashboard.js';

/**
 * Reports.
 *
 * ROUTE MAP:
 *   GET /reports/sales?range=  -> GET /console/reports/sales?days=
 *                                 (also accepts start_date/end_date,
 *                                 all_time, or preset=today|yesterday|
 *                                 this_week|this_month — see getSales())
 *   GET /reports/combos        -> GET /console/combinations?lookback_days=
 *   GET /reports/valuation     -> see getValuation() in ./dashboard.js
 *   (order receipt)            -> GET /pos/orders/{id}/receipt — see
 *                                 getOrderReceipt()
 */

// Re-exported so both existing callers (Reports.js, OnHand.js) keep working
// unchanged — the one implementation lives in ./dashboard.js, next to the
// dashboard summary that reads the same backend figure.
export { getValuation };

/**
 * Sales. Voided orders are excluded from every total by the backend; they stay
 * visible in `recentOrders` flagged as void, which is audit visibility, not
 * revenue. The UI must not add them back in.
 *
 * `opts` is either a plain number (legacy `days` lookback, unchanged) or an
 * object of the backend's own query param names for a real date window —
 * `{ start_date, end_date }`, `{ all_time: true }`, or `{ preset: 'this_week'
 * | 'this_month' }`. All four win over `days` server-side; see
 * wt/api.py's sales_summary for the exact precedence. Passed through
 * verbatim rather than translated, since these already ARE the wire names.
 */
export async function getSales(opts = 30) {
  const params = typeof opts === 'number' ? { days: opts } : opts;
  const out = await api.get('/console/reports/sales', params);
  const revenue = out.revenue ?? null;
  const orders = out.orders ?? null;
  return {
    windowDays: out.window_days ?? null,
    startDate: out.start_date ?? null,
    endDate: out.end_date ?? null,
    allTime: !!out.all_time,
    revenue,
    orders,
    covers: out.covers ?? null,
    // An average of nothing is not zero — with no orders there is no average
    // order value to report.
    averageOrder: orders ? revenue / orders : null,
    byItem: (out.by_item || []).map((r) => ({
      name: r.name,
      qtySold: r.qty_sold ?? null,
      revenue: r.revenue ?? null,
      // Share of a zero total is undefined, not 0%.
      sharePct: revenue ? (r.revenue / revenue) * 100 : null,
    })),
    byDay: (out.by_day || []).map((r) => ({
      day: r.day, orders: r.orders ?? null, revenue: r.revenue ?? null,
    })),
    recentOrders: (out.recent_orders || []).map((o) => ({
      id: o.id,
      orderNo: o.order_no,
      status: o.status,
      tableLabel: o.table_label,
      createdAt: o.created_at,
      total: o.total ?? null,
    })),
  };
}

/**
 * One order's receipt — GET /pos/orders/{id}/receipt (wt/refills.order_receipt,
 * the exact record a cashier's own receipt view reads). Owner/admin/manager
 * all hold order.read; this is a read-only reuse of the existing receipt
 * calculation, not a second one — see that endpoint's own docstring for why
 * `total` (frozen) and `currentTotal` (plus any later add-on charges) are
 * kept as two separate fields.
 */
export async function getOrderReceipt(orderId) {
  const out = await api.get(`/pos/orders/${orderId}/receipt`);
  const o = out.order;
  return {
    id: o.id,
    orderNo: o.order_no,
    status: o.status,
    tableLabel: o.table_label,
    headCount: o.head_count,
    createdAt: o.created_at,
    servedAt: o.served_at,
    voidedAt: o.voided_at,
    voidReason: o.void_reason,
    source: o.source,
    subtotal: o.subtotal ?? null,
    discountType: o.discount_type,
    discountAmount: o.discount_amount ?? null,
    serviceCharge: o.service_charge ?? null,
    total: o.total ?? null,
    cashReceived: o.cash_received ?? null,
    changeDue: o.change_due ?? null,
    currentTotal: out.current_total ?? null,
    addonTotal: out.addon_total ?? 0,
    paymentReconstructed: !!out.payment_reconstructed,
    reconstructedCashReceived: out.reconstructed_cash_received ?? null,
    reconstructedChangeDue: out.reconstructed_change_due ?? null,
    lines: (out.lines || []).map((l) => ({
      name: l.name, qty: l.qty, unitPrice: l.unit_price, note: l.note, isUnli: !!l.is_unli,
    })),
    charges: (out.charges || []).map((c) => ({
      kind: c.kind, amount: c.amount, description: c.description, createdAt: c.created_at,
    })),
  };
}

/**
 * FP-Growth combinations. When there is not enough order history the backend
 * says so explicitly (`insufficient_data`, with how many transactions it has
 * versus how many it needs) — that gap is shown as a gap, never as "no
 * combinations found", which would read as a finding rather than a shortfall.
 *
 * `transactionCount` (same as `validTransactions`) is the count of real
 * completed TRANSACTIONS FP-growth actually mined — not the raw
 * customer_order row count (`totalOrdersAnalyzed`). The two differ on this
 * database because some historical mixed orders were recorded as two rows
 * sharing one source_order_id and are merged back into one real transaction
 * before mining; `dataQualityNote`, when present, explains the gap in plain
 * language (see wt/associations.py's module docstring for the full story).
 */
export async function getCombinations(lookbackDays = 365) {
  const out = await api.get('/console/combinations', { lookback_days: lookbackDays });
  return {
    insufficientData: !!out.insufficient_data,
    transactionCount: out.transaction_count ?? null,
    totalOrdersAnalyzed: out.total_orders_analyzed ?? null,
    validTransactions: out.valid_transactions ?? null,
    excludedTransactions: out.excluded_transactions ?? null,
    uniqueProducts: out.unique_products ?? null,
    dataQualityNote: out.data_quality_note ?? null,
    required: out.required ?? out.min_transactions ?? null,
    comboPriceBasis: out.combo_price_basis ?? null,
    emptyReason: out.empty_reason ?? null,
    thresholdTier: out.threshold_tier ?? null,
    numberOfFrequentItemsets: out.number_of_frequent_itemsets ?? null,
    numberOfAssociationRules: out.number_of_association_rules ?? null,
    numberOfFinalRecommendations: out.number_of_final_recommendations ?? null,
    combos: (out.combos || []).map((c) => ({
      itemA: c.item_a,
      itemB: c.item_b,
      itemAMenuItemId: c.item_a_menu_item_id ?? null,
      itemBMenuItemId: c.item_b_menu_item_id ?? null,
      categoryA: c.category_a ?? null,
      categoryB: c.category_b ?? null,
      pairCount: c.pair_count ?? null,
      support: c.support ?? null,
      confidence: c.confidence ?? null,
      lift: c.lift ?? null,
      recommendationType: c.recommendation_type ?? null,
      // A plain discount-off-list-price reference (Part 59) — never a
      // recommendation. The actual pricing tool is Create combo idea ->
      // getComboPricingPreview() in ./comboPricing.js.
      discountReferencePrice: c.discount_reference_price ?? null,
      individualTotal: c.individual_total ?? null,
    })),
    params: {
      minSupport: out.min_support ?? null,
      minLift: out.min_lift ?? null,
      minConfidence: out.min_confidence ?? null,
      minPairCount: out.min_pair_count ?? null,
    },
  };
}
