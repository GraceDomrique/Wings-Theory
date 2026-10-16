import { h } from '../../lib/dom.js';

/**
 * One row in Today > Action Required.
 *
 * Rows are ranked by what breaks first if ignored — that ranking is the
 * backend's, and this component never re-sorts. Copy is factual: an anomaly
 * row states the multiple, it never implies cause or blame (spec section 6
 * point 5).
 *
 * @param {{severity:'crit'|'warn'|'good'|'info'|'flat', headline:string,
 *          detail:string, onClick?:Function}} opts
 */
export function AlertRow({ severity = 'flat', headline, detail, onClick }) {
  return h('div', {
    class: 'alert-row' + (onClick ? ' clickable' : ''),
    onClick,
  },
  h('span', { class: `dot dot-${severity}` }),
  h('div', { class: 'body' },
    h('div', { class: 'h' }, headline),
    detail ? h('div', { class: 's' }, detail) : null));
}
