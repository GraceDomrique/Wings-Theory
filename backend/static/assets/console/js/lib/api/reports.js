import { api, ApiError } from './client.js';

/**
 * Reports.
 *
 * ROUTE MAP:
 *   GET /reports/sales?range=  -> GET /console/reports/sales?days=
 *   GET /reports/combos        -> GET /console/combinations?lookback_days=
 *   GET /reports/valuation     -> DOES NOT EXIST (see getValuation below)
 */

/**
 * Sales. Voided orders are excluded from every total by the backend; they stay
 * visible in `recentOrders` flagged as void, which is audit visibility, not
 * revenue. The UI must not add them back in.
 */
export async function getSales(days = 30) {
  const out = await api.get('/console/reports/sales', { days });
  const revenue = out.revenue ?? null;
  const orders = out.orders ?? null;
  return {
    windowDays: out.window_days,
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
 * FP-Growth combinations. When there is not enough order history the backend
 * says so explicitly (`insufficient_data`, with how many transactions it has
 * versus how many it needs) — that gap is shown as a gap, never as "no
 * combinations found", which would read as a finding rather than a shortfall.
 */
export async function getCombinations(lookbackDays = 90) {
  const out = await api.get('/console/combinations', { lookback_days: lookbackDays });
  return {
    insufficientData: !!out.insufficient_data,
    transactionCount: out.transaction_count ?? null,
    required: out.required ?? out.min_transactions ?? null,
    comboPriceBasis: out.combo_price_basis ?? null,
    combos: (out.combos || []).map((c) => ({
      itemA: c.item_a,
      itemB: c.item_b,
      support: c.support ?? null,
      confidence: c.confidence ?? null,
      lift: c.lift ?? null,
      suggestedComboPrice: c.suggested_combo_price ?? null,
      individualTotal: c.individual_total ?? null,
    })),
    params: {
      minSupport: out.min_support ?? null,
      minLift: out.min_lift ?? null,
      minConfidence: out.min_confidence ?? null,
    },
  };
}

/**
 * Inventory valuation.
 *
 * STATUS: `GET /reports/valuation` DOES NOT EXIST in this backend. There is no
 * endpoint that returns stock value, and there is no safe way to derive it
 * client-side: unit_cost is nullable and unverified for most ingredients, so
 * summing what we happen to have would quietly understate the total while
 * looking authoritative. That is precisely the failure this rebuild exists to
 * remove.
 *
 * TODO(backend): implement GET /reports/valuation returning
 *   { totalValue, pricedItems, totalItems, unpricedItems: [{id, name}] }
 * computed as SUM(on_hand_base / (pack_size * yield_factor) * unit_cost) over
 * ingredients WHERE unit_cost IS NOT NULL, and reporting the excluded ones so
 * the figure can be shown with its own caveat.
 *
 * Until then this resolves to nulls and Reports renders the "not available"
 * state with the reason — it does not fabricate a number.
 *
 * @returns {Promise<{available:boolean, totalValue:number|null, pricedItems:number|null,
 *                    totalItems:number|null, reason:string|null}>}
 */
export async function getValuation() {
  try {
    const out = await api.get('/reports/valuation');
    return {
      available: true,
      totalValue: out.total_value ?? null,
      pricedItems: out.priced_items ?? null,
      totalItems: out.total_items ?? null,
      reason: null,
    };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      return {
        available: false,
        totalValue: null,
        pricedItems: null,
        totalItems: null,
        reason: 'No valuation endpoint yet — most ingredients still have no confirmed cost, so a total here would be misleading.',
      };
    }
    throw e;
  }
}
