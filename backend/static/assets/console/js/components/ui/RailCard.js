import { h } from '../../lib/dom.js';

/**
 * Card with the colored left border — spec section 5. Used for every KPI card
 * that represents a status. A neutral or reference card uses plain <Card>
 * instead: the rail is what says "this has a state", so putting it on
 * something stateless dilutes it.
 *
 * @param {{severity:'crit'|'warn'|'good'|'info'|'flat', className?:string}} opts
 */
export function RailCard({ severity = 'flat', className } = {}, ...children) {
  const sev = ['crit', 'warn', 'good', 'info', 'flat'].includes(severity) ? severity : 'flat';
  return h('div', {
    class: ['card', 'rail', `rail-${sev}`, className].filter(Boolean).join(' '),
  }, ...children);
}
