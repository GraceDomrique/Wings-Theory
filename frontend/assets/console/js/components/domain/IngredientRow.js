import { h } from '../../lib/dom.js';
import { Meter } from '../ui/Meter.js';
import { Pill } from '../ui/Pill.js';
import { daysShort, days as fmtDays, str } from '../../lib/format.js';
import { severityFromRisk, severityFromDays } from '../../lib/severity.js';

/**
 * The cells that make up one Inventory > On Hand row, including the runway
 * Meter. Exposed as column renderers rather than a whole <tr> so <Table> keeps
 * ownership of sorting and row behaviour.
 *
 * The runway meter is scaled against a fixed 90-day horizon rather than the
 * longest runway on the page: a bar that rescales as stock changes makes two
 * screenshots incomparable, and "full bar" should mean "months of cover", not
 * "most of anything currently listed".
 */
const RUNWAY_HORIZON_DAYS = 90;

// Beyond this, "days of cover" stops being a meaningful restaurant-planning
// number and starts being an artifact of dividing a tiny-but-nonzero usage
// rate into a large on-hand quantity (the backend genuinely computed
// 2,497,590 days for one ingredient — not a bug, just not a number a
// restaurant should ever be shown as a forecast). The raw daysLeft value
// itself is untouched (still used for sorting, filtering, severity) — this
// only changes how it's WORDED past the cap, distinct from "no usage data
// at all" (daysLeft === null, handled separately below).
const RUNWAY_DISPLAY_CAP_DAYS = 365;

export function nameCell(ing) {
  return h('div', {},
    h('strong', {}, ing.name),
    ing.blockReason ? h('div', { class: 't-xs t-crit' }, ing.blockReason) : null);
}

export function onHandCell(ing) {
  // The backend renders "2.00 bottles (1.00 L)" itself via format_base(), so
  // the two-unit display can never disagree with what the deduction engine
  // thinks a pack contains.
  return h('span', { class: 'mono t-sm' }, str(ing.onHandDisplay, 'not counted'));
}

export function runwayCell(ing) {
  const severity = ing.risk ? severityFromRisk(ing.risk) : severityFromDays(ing.daysLeft);
  return h('div', { class: 'runway-cell' },
    Meter({ value: ing.daysLeft, max: RUNWAY_HORIZON_DAYS, severity }),
    h('span', { class: 'days mono' }, daysShort(ing.daysLeft, 'n/a')));
}

export function usageCell(ing) {
  // No measured usage rate is not "uses none per day". It means the ledger has
  // no consumption for this item in the lookback window.
  return h('span', { class: 'mono t-sm' },
    ing.usedPerDay === null
      ? h('span', { class: 'nodata' }, 'no usage yet')
      : `${ing.usedPerDay.toFixed(2)} ${ing.baseUnit}/day`);
}

export function outlookCell(ing) {
  // Missing demand data (never measured) is a different fact from a
  // measured-but-vanishingly-small usage rate (below) — the two must never
  // collapse into the same wording.
  if (ing.daysLeft === null) {
    return h('span', { class: 'nodata' }, 'needs usage history');
  }
  // Presented as an estimate, not a fact — "0d left" read like a precise
  // countdown for what is really a rough days-of-cover projection.
  if (ing.daysLeft <= 0) {
    const usable = ing.usableBaseQty ?? ing.onHandBaseQty;
    return h('span', { class: 'mono t-xs' }, usable != null && usable <= 0.0001 ? 'out of usable stock' : '~0 days of cover');
  }
  if (ing.daysLeft > RUNWAY_DISPLAY_CAP_DAYS) {
    return h('span', { class: 'nodata', title: `Backend estimate: ${Math.round(ing.daysLeft)} days` }, 'Very low usage — estimate unreliable');
  }
  return h('span', { class: 'mono t-xs' }, '~', fmtDays(ing.daysLeft), ' of cover');
}

/**
 * Status must answer "can this be sold right now", not just "is this
 * ingredient correctly configured" — those are different questions, and
 * conflating them is what let a 0-usable-stock ingredient show a calm green
 * "confirmed" pill (2026-10 audit finding). Usable stock is checked BEFORE
 * confidence: a correctly-configured, cost-verified ingredient with nothing
 * left to use is not "good", it is out of stock.
 */
export function statusCell(ing) {
  if (!ing.isDeductable) return Pill('crit', 'blocked', { title: ing.blockReason || undefined });
  const usable = ing.usableBaseQty ?? ing.onHandBaseQty;
  if (usable != null && usable <= 0.0001) return Pill('crit', 'out of stock');
  if (ing.confidence === 'unverified') {
    return Pill('warn', 'needs confirming', {
      title: `${ing.unverifiedCount} value(s) still need confirming`,
    });
  }
  return Pill('good', 'details verified');
}
