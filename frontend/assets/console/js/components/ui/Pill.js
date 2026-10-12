import { h } from '../../lib/dom.js';

/**
 * Status badge — spec section 5. EVERY status badge in the console goes
 * through this component, so a palette change is one file. No route may
 * hardcode a pill color class at its usage site.
 *
 * `flat` renders without the leading dot: per spec section 1 it is explicitly
 * not a status color, it is "doesn't fit the four / no data yet".
 *
 * @param {'crit'|'warn'|'good'|'info'|'flat'} severity
 * @param {string|Node} label
 * @param {{title?:string}} [opts]
 */
export function Pill(severity, label, opts = {}) {
  const sev = ['crit', 'warn', 'good', 'info', 'flat'].includes(severity) ? severity : 'flat';
  return h('span', { class: `pill pill-${sev}`, title: opts.title }, label);
}
