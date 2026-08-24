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
  if (ing.daysLeft === null) {
    return h('span', { class: 'nodata' }, 'needs usage history');
  }
  return h('span', { class: 'mono t-xs' }, fmtDays(ing.daysLeft), ' left');
}

export function statusCell(ing) {
  if (!ing.isDeductable) return Pill('crit', 'blocked');
  if (ing.confidence === 'unverified') {
    return Pill('warn', 'unverified', {
      title: `${ing.unverifiedCount} value(s) still need confirming`,
    });
  }
  return Pill('good', 'confirmed');
}
