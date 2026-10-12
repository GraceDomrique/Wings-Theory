import { api, ApiError } from './client.js';

/**
 * Today's single aggregation payload — spec section 4:
 *
 *   "Aggregation endpoint — do NOT have Today independently query 6 other
 *    endpoints and merge client-side. Backend should return one shaped
 *    payload: { kpis, alerts, forecastPreview, useFirst, verifyPreview }."
 *
 * `GET /dashboard/summary` (wt/api.py's dashboard_summary()) composes,
 * server-side, from the same sources the individual pages use:
 * v_ingredient_readiness + fefo_batch expiry (kpis.expired / expiringSoon),
 * wt/depletion.ingredient_forecast() (kpis.needRestock — the forward
 * depletion engine's own out/critical/at_risk count; NOT the same number as
 * wt/depletion's own belowReorderPoint field on the Replenishment screen,
 * which is a literal reorder-point comparison — the two were briefly given
 * the same key despite meaning different things, which is exactly the
 * contract bug the 2026-09 rename fixed), v_menu_item_availability
 * (unsellable), reservation.inventory_valuation() (stockValue, plus how
 * many items are actually priced), and the verification queue.
 *
 * getSummary() below calls it directly. SAMPLE only remains as the fallback
 * for a 404 — "this backend build predates the endpoint" — never presented
 * as real data (isSample stays true), and not the normal path: it should not
 * fire against this codebase's own backend any more.
 */

/**
 * @typedef {Object} TodayKpi
 * @property {string} key
 * @property {number|null} value       null renders as "no data", never 0
 * @property {string|null} display     pre-formatted (money); null -> format value
 * @property {string} label
 * @property {string} foot             the small line under the number
 * @property {import('../types.js').Severity} severity
 * @property {string} icon
 * @property {string} [route]          where clicking it goes
 */

/**
 * @typedef {Object} TodayAlert
 * @property {import('../types.js').Severity} severity
 * @property {string} headline
 * @property {string} detail
 * @property {string} [route]
 */

/**
 * @typedef {Object} DashboardSummary
 * @property {boolean} isSample        true while the stub is in use
 * @property {TodayKpi[]} kpis
 * @property {TodayAlert[]} alerts
 * @property {{ insufficientData:boolean, daysOfHistory:number, requiredDays:number,
 *              labels:string[], values:(number|null)[], note:string|null }} forecastPreview
 * @property {{ openInstructions:number, items:{id:number,title:string,status:string,priority:string}[]
 *              }} instructionsPreview
 * @property {{ ingredientId:number, name:string, qty:{qty:number,unit:string,display:string},
 *              label:string, daysToExpiry:number, severity:import('../types.js').Severity }[]} useFirst
 *              renamed from the old expiryPreview — same "oldest batches
 *              first" list, qty is now the structured {qty,unit,display}
 *              pair every quantity on this payload uses
 * @property {{ label:string, priority:1|2|3 }[]} verifyPreview
 */

/**
 * Typed mock matching DashboardSummary exactly. Values mirror the approved
 * prototype so the layout can be reviewed against it, and `isSample: true`
 * makes Today render a standing banner plus a "sample" tag on every tile —
 * these numbers can never be read as this restaurant's real position.
 * @type {DashboardSummary}
 */
const SAMPLE = {
  isSample: true,
  kpis: [
    { key: 'expired', value: 8, display: null, label: 'Expired batches', foot: 'discard or credit today', severity: 'crit', icon: '⏱', route: 'expiry' },
    { key: 'expiringSoon', value: 6, display: null, label: 'Expiring ≤7 days', foot: 'use first, FEFO order', severity: 'warn', icon: '◔', route: 'expiry' },
    { key: 'needRestock', value: 0, display: null, label: 'Need restock', foot: 'every item has a buffer', severity: 'good', icon: '✓', route: 'purchasing' },
    { key: 'unsellable', value: 1, display: null, label: 'Menu item unsellable', foot: 'missing ingredient stock', severity: 'warn', icon: '⨯', route: 'menu' },
    { key: 'stockValue', value: 64510, display: '₱64.5k', label: 'Stock on hand', foot: '94 of 111 items priced', severity: 'info', icon: '₱', route: 'inventory' },
  ],
  alerts: [
    { severity: 'crit', headline: 'Fresh Milk — expired 1 day ago', detail: '5,000 ml on hand', route: 'expiry' },
    { severity: 'crit', headline: 'Shredded Cabbage — expired', detail: '3,000 g on hand', route: 'expiry' },
    { severity: 'crit', headline: 'Fresh Garlic Peeled — use today', detail: '950 g · exp 2026-08-20', route: 'expiry' },
    { severity: 'warn', headline: 'White Truffle Pasta unsellable', detail: 'Missing: Cheese Sauce (house)', route: 'menu' },
    { severity: 'warn', headline: 'Buffalo Hot Sauce waste — review soon', detail: '2.4× this ingredient’s typical wastage', route: 'purchasing' },
    { severity: 'flat', headline: '94 ingredients need cost confirmed', detail: 'Affects inventory valuation', route: 'verify' },
    { severity: 'flat', headline: '465 stock-amount values unconfirmed', detail: 'Affects how much stock a sale deducts', route: 'verify' },
  ],
  forecastPreview: {
    insufficientData: true,
    daysOfHistory: 2,
    requiredDays: 7,
    labels: [],
    values: [],
    note: 'Prophet needs 7+ days of real sales per menu item. The closest item has 2 days on file.',
  },
  instructionsPreview: {
    openInstructions: 1,
    items: [{ id: 1, title: 'Verify Chicken Wings count before ordering', status: 'open', priority: 'normal' }],
  },
  useFirst: [
    { name: 'Fresh Garlic Peeled', qty: { qty: 950, unit: 'g', display: '950 g' }, label: 'today', severity: 'crit' },
    { name: 'Shredded Cabbage', qty: { qty: 3000, unit: 'g', display: '3000 g' }, label: 'expired', severity: 'crit' },
    { name: 'Fresh Milk', qty: { qty: 5000, unit: 'ml', display: '5000 ml' }, label: 'expired', severity: 'crit' },
    { name: 'Plain Yogurt', qty: { qty: 3000, unit: 'g', display: '3000 g' }, label: '4d', severity: 'warn' },
    { name: 'Lemon', qty: { qty: 75, unit: 'g', display: '75 g' }, label: '11d', severity: 'warn' },
  ],
  verifyPreview: [
    { label: 'Alcohol — pack size', priority: 1 },
    { label: 'All-Purpose Cream — yield', priority: 1 },
    { label: 'Buffalo Hot Sauce — cost', priority: 2 },
    { label: 'Banana — expiry date', priority: 3 },
  ],
};

/**
 * Inventory valuation — GET /reports/valuation (wt/reservation.
 * inventory_valuation(), the exact figure the Today dashboard's own
 * "Stock value" KPI is built from — see getSummary() above). Reports and
 * Inventory > On Hand both need this number; this is the one
 * implementation, re-exported from lib/api/reports.js so neither caller's
 * import line has to change.
 *
 * `available: false` covers both "the backend call failed" and (once it
 * succeeds) "nothing is priced yet" is still `available: true` with
 * `pricedItems: 0` — a real answer, not a failure — so callers must not
 * conflate the two.
 *
 * @returns {Promise<{available:boolean, totalValue:number|null,
 *   pricedItems:number|null, totalItems:number|null, reason:string}>}
 */
export async function getValuation() {
  try {
    const out = await api.get('/reports/valuation');
    return {
      available: true,
      totalValue: out.total_value,
      pricedItems: out.priced_items,
      totalItems: out.total_items,
      reason: '',
    };
  } catch (e) {
    return {
      available: false,
      totalValue: null,
      pricedItems: null,
      totalItems: null,
      reason: e instanceof ApiError ? e.message : 'Inventory value is not available right now.',
    };
  }
}

/**
 * @returns {Promise<DashboardSummary>}
 */
export async function getSummary() {
  try {
    const raw = await api.get('/dashboard/summary');
    return { ...raw, isSample: false };
  } catch (e) {
    // 404 = not built yet, which is the expected state today. Anything else is
    // a real failure and must surface as one rather than as sample data.
    if (e instanceof ApiError && e.status === 404) return SAMPLE;
    throw e;
  }
}

/**
 * Owner Overview "business companion" — GET /console/dashboard/business-
 * overview (wt/business_overview.py). A second, independent read over the
 * same order tables getSummary() already reads, never a duplicate
 * calculation of anything in DashboardSummary above — see that module's own
 * docstring for exactly what each block is computed against.
 *
 * `dataStaleHours` is the gap between "now" and the most recent real order
 * — the frontend must check it before presenting `today`/`thisWeek`/
 * `thisMonth` as a real performance read: a large gap means those blocks
 * are comparing a trading day against a day nothing was entered on, not
 * reporting an actual decline.
 */
function totalsBlock(t) {
  return { revenue: t.revenue, orders: t.orders, covers: t.covers, avgOrder: t.avg_order };
}

function periodBlock(raw) {
  if (!raw) return null;
  return {
    label: raw.label,
    current: totalsBlock(raw.current),
    prior: totalsBlock(raw.prior),
    deltas: {
      revenuePct: raw.deltas.revenue_pct,
      ordersPct: raw.deltas.orders_pct,
      avgOrderPct: raw.deltas.avg_order_pct,
      coversPct: raw.deltas.covers_pct,
    },
  };
}

export async function getBusinessOverview() {
  const raw = await api.get('/console/dashboard/business-overview');
  return {
    asOf: raw.as_of,
    latestOrderAt: raw.latest_order_at,
    dataStaleHours: raw.data_stale_hours,
    today: periodBlock(raw.today),
    thisWeek: periodBlock(raw.this_week),
    thisMonth: periodBlock(raw.this_month),
    trend: {
      weeks: (raw.trend.weeks || []).map((w) => ({ weekStart: w.week_start, revenue: w.revenue })),
      averageRevenue: raw.trend.average_revenue,
      belowAverageStreak: raw.trend.below_average_streak,
    },
    outlook: raw.outlook.available
      ? {
        available: true,
        horizonDays: raw.outlook.horizon_days,
        forecastTotal: raw.outlook.forecast_total,
        trailingAvgTotal: raw.outlook.trailing_avg_total,
        deltaPct: raw.outlook.delta_pct,
      }
      : { available: false, reason: raw.outlook.reason },
  };
}

/**
 * Admin Today's reference-design landing payload — GET /console/dashboard/
 * admin-today (wt/dashboard_overview.admin_today()). A richer, separate
 * aggregation from getSummary() above: that one backs the original fixed-
 * viewport five-KPI grid (still used for the alerts/useFirst/verify panels
 * further down this same page — see Today.js), this one backs the new
 * reference-image sections (store status, today's sales vs yesterday,
 * inventory pulse, deliveries, order activity, recent orders, the two
 * anomaly previews, activity log).
 */
export async function getAdminToday() {
  const raw = await api.get('/console/dashboard/admin-today');
  return {
    asOf: raw.asOf,
    businessDate: raw.businessDate,
    freshness: raw.freshness,
    storeStatus: raw.storeStatus,
    todaySales: raw.todaySales,
    doNow: raw.doNow,
    next7Days: raw.next7Days,
    inventoryPulse: raw.inventoryPulse,
    todaysDeliveries: raw.todaysDeliveries,
    quickActions: raw.quickActions,
    orderActivity: raw.orderActivity,
    recentOrders: raw.recentOrders,
    unusualUsage: raw.unusualUsage,
    unusualStockDifference: raw.unusualStockDifference,
    activityLog: raw.activityLog,
    instructionsPreview: raw.instructionsPreview,
  };
}

/**
 * Owner Overview's reference-design landing payload — GET /console/
 * dashboard/owner-overview (wt/dashboard_overview.owner_overview()). Builds
 * on business_overview()'s own this-week period comparison (same shape
 * getBusinessOverview() already maps above) plus the sections that payload
 * doesn't carry: needs-attention, coming-up forecast, sales->stock,
 * customer pulse, category mix, and business rhythm.
 */
export async function getOwnerOverview() {
  const raw = await api.get('/console/dashboard/owner-overview');
  return raw;
}
