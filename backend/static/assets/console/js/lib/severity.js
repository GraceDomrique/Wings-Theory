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
 * The backend's `risk` field on a depletion row (wt/forecasting.py's
 * RISK_ORDER = {"out":0, "critical":1, "warning":2, "idle":3, "ok":4,
 * "no_data":5}) mapped to a display severity. Unknown strings fall to
 * `flat` rather than being guessed at.
 *
 * 2026-08 audit finding: this switch was written against a risk
 * vocabulary ('out_of_stock', 'low', 'healthy', 'unknown', 'no_usage')
 * the backend has never actually emitted — RISK_ORDER above is the real,
 * only vocabulary forecasting.py produces. Because 'out' (the single most
 * urgent tier — an ingredient with zero runway left) had no matching
 * case, it fell through to the same 'info'/neutral-blue default as an
 * unrecognized string, which also meant it failed the crit/warn filter
 * Reports.js's Attention Needed card uses to decide what counts as "low
 * stock." Confirmed against the live dev data: Rice was at risk='out'
 * (completely depleted) and rendered as a calm gray "no usage rate" pill,
 * invisible to the one report specifically built to surface it. Fixed by
 * matching RISK_ORDER's real six values exactly, not by re-guessing a
 * vocabulary that never matched.
 * @param {string|null} risk
 * @returns {Severity}
 */
export function severityFromRisk(risk) {
  switch (risk) {
    case 'out': case 'critical': return 'crit';
    case 'warning': return 'warn';
    case 'ok': return 'good';
    case 'idle': case 'no_data': return 'flat';
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
