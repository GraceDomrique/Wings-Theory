import { h } from '../../lib/dom.js';

/**
 * Loading, error and toast states.
 *
 * A loading state is never rendered as zeros. Until data arrives the console
 * shows skeletons or "Loading…", because a KPI reading 0 while a request is
 * still in flight is exactly the class of lie spec section 6 point 4 forbids.
 */

export function Loading(label = 'Loading…') {
  return h('div', { class: 'load-note' }, label);
}

export function SkeletonLines(n = 3) {
  return h('div', { style: { display: 'flex', flexDirection: 'column', gap: '8px', padding: '6px 0' } },
    ...Array.from({ length: n }, (_, i) => h('div', {
      class: 'skeleton', style: { width: `${100 - i * 12}%` },
    })));
}

/**
 * A failed fetch is its own state — distinct from both "no data" and "zero".
 * @param {{message:string, onRetry?:Function}} opts
 */
export function ErrorState({ message, onRetry }) {
  return h('div', { class: 'callout callout-crit' },
    h('p', {}, h('strong', {}, 'Could not load this. '), message),
    onRetry ? h('div', { style: { marginTop: '10px' } },
      h('button', { class: 'btn btn-sm', onClick: onRetry }, 'Try again')) : null);
}

let host = null;
/** @param {'crit'|'warn'|'good'|'info'|'flat'} severity */
export function toast(message, severity = 'good') {
  if (!host) {
    host = h('div', { class: 'toast-host' });
    document.body.appendChild(host);
  }
  const el = h('div', {
    class: 'toast',
    style: { borderLeftColor: `var(--${severity === 'flat' ? 'line' : severity})` },
  }, message);
  host.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}
