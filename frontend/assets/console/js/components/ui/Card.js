import { h } from '../../lib/dom.js';

/**
 * Card, CardHeader, CardTitle, CardDesc — spec section 2.
 *
 * @param {{className?:string, sunk?:boolean, flush?:boolean, style?:object}} [opts]
 */
export function Card(opts = {}, ...children) {
  const cls = ['card',
    opts.sunk && 'card-sunk',
    opts.flush && 'card-flush',
    opts.className].filter(Boolean).join(' ');
  return h('div', { class: cls, style: opts.style }, ...children);
}

export function CardTitle(text, extra) {
  return h('div', { class: 'card-title' }, text, extra);
}

export function CardDesc(text) {
  return text ? h('div', { class: 'card-desc' }, text) : null;
}

/**
 * The title/description block on the left, anything actionable on the right.
 * @param {{title?:string|Node, desc?:string|Node, right?:Node|Node[]}} opts
 */
export function CardHeader({ title, desc, right } = {}) {
  return h('div', { class: 'card-head' },
    h('div', {},
      title ? (typeof title === 'string' ? CardTitle(title) : title) : null,
      desc ? (typeof desc === 'string' ? CardDesc(desc) : desc) : null),
    right ? h('div', { class: 'card-actions' }, right) : null);
}
