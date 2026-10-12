import { h } from '../../lib/dom.js';

/**
 * The runway / time-to-expiry bar — spec section 5. One component, different
 * `max`: Inventory On Hand passes a days-left horizon, Expiry cards pass the
 * shelf-life window.
 *
 * An unknown value does not render an empty bar (which reads as "zero left").
 * It renders a flat, fully-muted track so the reader sees "not measured"
 * rather than "none remaining" — spec section 6 point 4.
 *
 * @param {{value:number|null, max:number, severity:'crit'|'warn'|'good'|'info'|'flat'}} opts
 */
export function Meter({ value, max, severity = 'flat' }) {
  const known = value !== null && value !== undefined && !Number.isNaN(value);
  const pct = known ? Math.max(2, Math.min(100, (value / (max || 1)) * 100)) : 100;
  const sev = known ? severity : 'flat';
  return h('div', {
    class: `meter meter-${sev}`,
    title: known ? undefined : 'No usage rate measured yet',
  }, h('span', {
    style: { width: pct + '%', opacity: known ? '1' : '0.25' },
  }));
}
