import { h } from '../../lib/dom.js';

/**
 * "How long will this last" - shown ONLY when the backend produced a
 * runway. The backend (wt/forecasting.py) already decides this: risk
 * 'no_data' means no usage history at all, 'idle' means history exists but
 * nothing is being used, and days_remaining is null in both. We never
 * estimate one here; absence of evidence is shown as such.
 *
 * Pure text function exported for tests.
 * @param {{daysLeft:number|null|undefined, risk?:string|null}} p
 * @returns {{text:string, known:boolean}}
 */
export function daysOfStockText({ daysLeft, risk } = {}) {
  if (risk === 'out') return { text: 'Out of stock', known: true };
  if (risk === 'no_data') return { text: 'Not enough history', known: false };
  if (risk === 'idle') return { text: 'Not being used lately', known: false };
  if (daysLeft === null || daysLeft === undefined || Number.isNaN(daysLeft)) {
    return { text: 'Not enough history', known: false };
  }
  if (daysLeft <= 0) return { text: 'Out of stock', known: true };
  const d = Math.round(daysLeft);
  return { text: d <= 1 ? 'About 1 day' : `About ${d} days`, known: true };
}

export function DaysOfStock(props) {
  const { text, known } = daysOfStockText(props);
  return h('span', { class: known ? 'days-of-stock' : 'days-of-stock is-unknown' }, text);
}
