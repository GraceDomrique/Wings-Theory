import { h } from '../../lib/dom.js';

/**
 * Every "nothing here yet" state — spec section 5. Not a one-off <p>, so the
 * copy keeps one tone across the console.
 *
 * Copy rule, carried over from the prototype: state what is true, then what to
 * do next. Never "Oops", never apologetic, never blame the reader. An empty
 * list is usually a fact about the business ("nothing needs restocking"), not
 * a failure.
 *
 * @param {{icon?:string, title:string, description?:string, action?:Node, tight?:boolean}} opts
 */
export function EmptyState({ icon, title, description, action, tight = false }) {
  return h('div', { class: 'empty' + (tight ? ' empty-tight' : '') },
    icon ? h('div', { class: 'ic' }, icon) : null,
    h('div', { class: 't' }, title),
    description ? h('div', { class: 'd' }, description) : null,
    action ? h('div', { class: 'act' }, action) : null);
}
