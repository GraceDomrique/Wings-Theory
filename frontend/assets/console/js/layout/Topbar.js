import { h, icon } from '../lib/dom.js';

/**
 * Page title/sub, search, and the refresh control.
 *
 * Search is a real control, not decoration: it filters the Inventory table via
 * the callback the shell installs. Cmd/Ctrl-K focuses it from anywhere.
 */
export function Topbar({ title, sub, onSearch }) {
  const crumbEl = h('div', { class: 'breadcrumb', style: { display: 'none' } });
  const h1 = h('h1', {}, title);
  const subEl = h('div', { class: 'sub' }, sub);

  const input = h('input', {
    type: 'search',
    placeholder: 'Search ingredients…',
    onInput: (e) => onSearch && onSearch(e.target.value),
  });

  const refresh = h('button', {
    class: 'icon-btn', title: 'Reload this page’s data',
  }, icon('<path d="M21 12a9 9 0 11-3-6.7"/><path d="M21 3v6h-6"/>', 15));

  const el = h('div', { class: 'topbar' },
    h('div', {}, crumbEl, h1, subEl),
    h('div', { class: 'topbar-right' },
      h('div', { class: 'search' },
        icon('<circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>', 14),
        input,
        h('span', { class: 'kbd' }, '⌘K')),
      refresh));

  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      input.focus();
    }
  });

  el.setCopy = (t, s) => { h1.textContent = t; subEl.textContent = s; };
  /** Location context (spec section 49) — "Inventory / Losses & Variance".
   * `group` is null for a standalone top-level page (Today, Messages), which
   * hides the line entirely rather than showing a crumb to nowhere. */
  el.setBreadcrumb = (group) => {
    if (!group) { crumbEl.style.display = 'none'; return; }
    crumbEl.style.display = '';
    crumbEl.textContent = group;
  };
  el.setSearchVisible = (visible) => {
    el.querySelector('.search').style.visibility = visible ? '' : 'hidden';
    if (!visible) input.value = '';
  };
  el.onRefresh = (fn) => refresh.addEventListener('click', fn);
  return el;
}
