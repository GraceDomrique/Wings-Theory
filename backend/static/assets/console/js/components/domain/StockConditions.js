import { h } from '../../lib/dom.js';
import { Pill } from '../ui/Pill.js';

/**
 * An ingredient can be low AND expiring at once, so conditions are a LIST of
 * independent indicators, never one exclusive status. Warning severity is
 * the existing display severity (crit/warn, mapped from backend values by
 * lib/severity.js); nothing is shown for a normal condition.
 *
 * Pure builder exported for tests. Inputs are values the backend already
 * decided on - this only turns them into labels.
 *
 * @param {{outOfStock?:boolean, low?:boolean, lowSeverity?:'crit'|'warn',
 *          expiresOn?:string|null, expirySeverity?:'crit'|'warn'|null,
 *          expiredQty?:boolean}} s
 * @returns {{key:string, label:string, severity:'crit'|'warn'}[]}
 */
export function stockConditions(s = {}) {
  const out = [];
  if (s.outOfStock) out.push({ key: 'out', label: 'Out of stock', severity: 'crit' });
  else if (s.low) out.push({ key: 'low', label: 'Low', severity: s.lowSeverity || 'warn' });
  if (s.expiredQty) out.push({ key: 'expired', label: 'Has expired stock', severity: 'crit' });
  else if (s.expiresOn && s.expirySeverity) {
    out.push({ key: 'expiring', label: `Expires ${s.expiresOn}`, severity: s.expirySeverity });
  }
  return out;
}

/** Renders the conditions as separate indicators; empty (no badge) when normal. */
export function StockConditions(s) {
  const list = stockConditions(s);
  return h('span', { class: 'stock-conditions' },
    list.map((c) => Pill(c.severity, c.label)));
}
