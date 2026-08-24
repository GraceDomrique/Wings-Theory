import { h } from '../../lib/dom.js';
import { nodata } from '../../lib/format.js';

/**
 * Big number + label — spec section 2.
 *
 * `value` is expected to be a Node from lib/format.js, which is what keeps
 * spec section 6 point 4 honest: a missing number arrives here already
 * rendered as the italic "no data" state and is displayed smaller and fainter,
 * so it can never be mistaken for a real figure.
 *
 * @param {{value:Node|string|number|null, label:string, missing?:string}} opts
 */
export function Stat({ value, label, missing = 'no data yet' }) {
  let node = value;
  let isMissing = false;
  if (value === null || value === undefined) { node = nodata(missing); isMissing = true; }
  else if (value instanceof Element && value.classList.contains('nodata')) isMissing = true;

  return h('div', { class: 'stat' },
    h('div', { class: 'n' + (isMissing ? ' is-nodata' : '') }, node),
    h('div', { class: 'l' }, label));
}
