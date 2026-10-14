import { h, icon } from '../lib/dom.js';
import { session } from '../lib/api/client.js';

/**
 * Page title/sub, search, the refresh control, and a profile chip with an
 * icon-only sign-out button -- reuses session.user, already loaded for the
 * whole app. Sign-out lives here (not in the sidebar foot, which no longer
 * repeats name/role/sign-out).
 *
 * Search is a real control, not decoration: it filters the Inventory table via
 * the callback the shell installs. Cmd/Ctrl-K focuses it from anywhere.
 */
export function Topbar({ title, sub, onSearch, onSignOut }) {
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

  const user = session.user || {};
  const initials = (user.displayName || user.username || '?')
    .split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase();
  const signOutBtn = h('button', {
    class: 'icon-btn', title: 'Sign out', onClick: () => onSignOut && onSignOut(),
  }, icon('<path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/>', 15));

  const profile = h('div', { class: 'topbar-profile' },
    h('div', { class: 'avatar' }, initials),
    h('div', { class: 'id' },
      h('div', { class: 'name' }, user.displayName || user.username || 'Signed in'),
      user.role ? h('div', { class: 'role' }, user.role) : null),
    signOutBtn);

  const el = h('div', { class: 'topbar' },
    h('div', {}, crumbEl, h1, subEl),
    h('div', { class: 'topbar-right' },
      h('div', { class: 'search' },
        icon('<circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>', 14),
        input,
        h('span', { class: 'kbd' }, '⌘K')),
      refresh,
      profile));

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
  el.setSearchPlaceholder = (text) => { input.placeholder = text || 'Search…'; };
  el.onRefresh = (fn) => refresh.addEventListener('click', fn);
  return el;
}
