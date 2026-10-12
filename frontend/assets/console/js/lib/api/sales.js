import { api } from './client.js';

/**
 * Owner Sales / Admin Sales (2026-10, Phase 2) — GET /console/sales/owner
 * and GET /console/sales/admin (wt/sales_overview.py). Both payloads are
 * passed through close to verbatim: they are already shaped for direct
 * rendering (see that module's own docstring), unlike dashboard.js's older
 * snake_case->camelCase mapping layer.
 *
 * @param {{period?:string, comparison?:string, periodStart?:string,
 *          periodEnd?:string, comparisonStart?:string, comparisonEnd?:string}} [opts]
 */
export async function getOwnerSales(opts = {}) {
  return api.get('/console/sales/owner', {
    period: opts.period, comparison: opts.comparison,
    period_start: opts.periodStart, period_end: opts.periodEnd,
    comparison_start: opts.comparisonStart, comparison_end: opts.comparisonEnd,
  });
}

/** @param {{period?:string}} [opts] */
export async function getAdminSales(opts = {}) {
  return api.get('/console/sales/admin', { period: opts.period });
}
