import { h, icon } from '../../lib/dom.js';

/**
 * Status Card — spec section 5 (originally "card with the colored left
 * border"; redesigned 2026-10-14 to a soft tinted background instead, see
 * app.css's .rail comment for why). Used for every KPI card that represents
 * an operational status. A neutral or reference card uses plain <Card>
 * instead: the tint is what says "this has a state", so putting it on
 * something stateless dilutes it.
 *
 * `icon`, when given, renders as a small status-colored chip above the
 * children — the non-color signal the redesign asked for ("critical
 * statuses must remain distinguishable using text and icons, not color
 * alone"), not just a tinted rectangle.
 *
 * @param {{severity:'crit'|'warn'|'good'|'info'|'flat', className?:string, icon?:string}} opts
 */
export function RailCard({ severity = 'flat', className, icon: iconPaths } = {}, ...children) {
  const sev = ['crit', 'warn', 'good', 'info', 'flat'].includes(severity) ? severity : 'flat';
  return h('div', {
    class: ['card', 'rail', `rail-${sev}`, className].filter(Boolean).join(' '),
  },
  iconPaths ? h('div', { class: 'rail-icon' }, icon(iconPaths, 15)) : null,
  ...children);
}
