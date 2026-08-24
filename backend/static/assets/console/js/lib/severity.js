/**
 * Display-only severity mapping — spec section 3, "Rule for the frontend team".
 *
 * This file NEVER computes business meaning. `daysLeft`, `status`,
 * `suggestedOrderQty` and every risk decision come from the backend
 * (wt/forecasting.py, wt/anomaly.py, the readiness views). All this does is
 * turn a value the backend already decided on into one of the five display
 * severities, so a palette or threshold change is one edit, not forty.
 *
 * The five, and nothing else (spec section 1):
 *   crit  expired / blocking / out of tolerance
 *   warn  expiring soon / needs review
 *   good  on track / confirmed
 *   info  neutral / reference
 *   flat  no data yet  (gray pill, no dot)
 */

/** @typedef {'crit'|'warn'|'good'|'info'|'flat'} Severity */

/**
 * Days-to-expiry or days-of-runway to a severity.
 * null (unknown) is `flat`, NOT `crit` and NOT `good` — we do not know.
 * @param {number|null|undefined} days
 * @returns {Severity}
 */
export function severityFromDays(days) {
  if (days === null || days === undefined || Number.isNaN(days)) return 'flat';
  if (days < 0) return 'crit';
  if (days <= 2) return 'crit';
  if (days <= 7) return 'warn';
  if (days <= 30) return 'info';
  return 'good';
}

/**
 * The backend's `risk` field on a depletion row (wt/forecasting.py) mapped to
 * a display severity. Unknown strings fall to `flat` rather than being
 * guessed at.
 * @param {string|null} risk
 * @returns {Severity}
 */
export function severityFromRisk(risk) {
  switch (risk) {
    case 'critical': case 'out_of_stock': return 'crit';
    case 'low': case 'warning': return 'warn';
    case 'ok': case 'healthy': return 'good';
    case 'unknown': case 'no_usage': return 'flat';
    default: return risk ? 'info' : 'flat';
  }
}

/**
 * Anomaly severity as reported by wt/anomaly.py ("high" | "medium").
 * @param {string|null} s
 * @returns {Severity}
 */
export function severityFromAnomaly(s) {
  if (s === 'high') return 'crit';
  if (s === 'medium') return 'warn';
  return 'flat';
}

/**
 * Verification priority (1|2|3) to severity — 1 affects stock deductions,
 * 2 affects cost, 3 affects expiry display only.
 * @param {number} priority
 * @returns {Severity}
 */
export function severityFromPriority(priority) {
  return priority === 1 ? 'crit' : priority === 2 ? 'warn' : 'info';
}

/** A signed stock delta: gains read good, losses read crit. Zero is flat. */
export function severityFromDelta(delta) {
  if (delta === null || delta === undefined) return 'flat';
  if (delta > 0) return 'good';
  if (delta < 0) return 'crit';
  return 'flat';
}

/** CSS var for a severity, for the few places that need a raw color (charts). */
export function severityColor(sev) {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(sev === 'flat' ? '--ink-faint' : `--${sev}`).trim();
}
