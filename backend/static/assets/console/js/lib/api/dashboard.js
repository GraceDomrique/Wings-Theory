import { api, ApiError } from './client.js';

/**
 * Today's single aggregation payload — spec section 4:
 *
 *   "Aggregation endpoint — do NOT have Today independently query 6 other
 *    endpoints and merge client-side. Backend should return one shaped
 *    payload: { kpis, alerts, forecastPreview, expiryPreview, verifyPreview }."
 *
 * STATUS: `GET /dashboard/summary` DOES NOT EXIST in this backend yet. Nothing
 * under wt/api.py serves it, and there is no near-equivalent that returns the
 * whole shape.
 *
 * TODO(backend): implement GET /dashboard/summary returning DashboardSummary
 * below. It should compose, server-side, from the same sources the individual
 * pages use: v_ingredient_readiness + fefo_batch expiry (kpis.expired /
 * expiringSoon), ingredient_depletion() (belowReorderPoint),
 * v_menu_item_availability (unsellable), ingredient.unit_cost * on_hand_base
 * (stockValue, plus how many items are actually priced), and the verification
 * queue.
 *
 * Until it exists this module returns the typed mock below, and Today renders
 * a banner saying so. The mock is NOT quietly presented as real numbers — the
 * failure mode this whole rebuild exists to prevent is exactly "sample data
 * read as real". When the endpoint ships, delete `SAMPLE` and the fallback;
 * the route needs no change.
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
 *              labels:string[], values:(number|null)[], note:string }} forecastPreview
 * @property {{ name:string, qty:string|null, label:string,
 *              severity:import('../types.js').Severity }[]} expiryPreview
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
    { key: 'belowReorderPoint', value: 0, display: null, label: 'Below reorder point', foot: 'every item has a buffer', severity: 'good', icon: '✓', route: 'intel' },
    { key: 'unsellable', value: 1, display: null, label: 'Menu item unsellable', foot: 'missing ingredient stock', severity: 'warn', icon: '⨯', route: 'menu' },
    { key: 'stockValue', value: 64510, display: '₱64.5k', label: 'Stock on hand', foot: '94 of 111 items priced', severity: 'info', icon: '₱', route: 'inventory' },
  ],
  alerts: [
    { severity: 'crit', headline: 'Fresh Milk — expired 1 day ago', detail: '5,000 ml on hand', route: 'expiry' },
    { severity: 'crit', headline: 'Shredded Cabbage — expired', detail: '3,000 g on hand', route: 'expiry' },
    { severity: 'crit', headline: 'Fresh Garlic Peeled — use today', detail: '950 g · exp 2026-08-20', route: 'expiry' },
    { severity: 'warn', headline: 'White Truffle Pasta unsellable', detail: 'Missing: Cheese Sauce (house)', route: 'menu' },
    { severity: 'warn', headline: 'Buffalo Hot Sauce waste — review soon', detail: '2.4× this ingredient’s typical wastage', route: 'intel' },
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
  expiryPreview: [
    { name: 'Fresh Garlic Peeled', qty: '950 g', label: 'today', severity: 'crit' },
    { name: 'Shredded Cabbage', qty: '3,000 g', label: 'expired', severity: 'crit' },
    { name: 'Fresh Milk', qty: '5,000 ml', label: 'expired', severity: 'crit' },
    { name: 'Plain Yogurt', qty: '3,000 g', label: '4d', severity: 'warn' },
    { name: 'Lemon', qty: '75 g', label: '11d', severity: 'warn' },
  ],
  verifyPreview: [
    { label: 'Alcohol — pack size', priority: 1 },
    { label: 'All-Purpose Cream — yield', priority: 1 },
    { label: 'Buffalo Hot Sauce — cost', priority: 2 },
    { label: 'Banana — expiry date', priority: 3 },
  ],
};

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
