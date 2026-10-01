import { h } from '../../lib/dom.js';

/**
 * The one-sentence "what is happening" line at the top of a page.
 * Plain sentence, optional supporting sentence. No numbers grid, no badges.
 * @param {string|Node} text
 * @param {string|Node} [sub]
 */
export function StatusLine(text, sub) {
  return h('div', { class: 'status-line' },
    h('div', { class: 'status-line-main' }, text),
    sub ? h('div', { class: 'status-line-sub' }, sub) : null);
}
