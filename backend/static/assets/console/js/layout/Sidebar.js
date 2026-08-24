import { h, icon } from '../lib/dom.js';
import { NAV_GROUPS } from './nav.js';
import { session } from '../lib/api/client.js';

/**
 * The eight grouped sections. Badge counts are pushed in after load
 * (setBadge) rather than fetched here — the sidebar renders immediately and
 * never blocks the first paint on a network call.
 */
export function Sidebar({ activeId, onNavigate, onSignOut }) {
  const buttons = new Map();
  const badges = new Map();

  const groups = NAV_GROUPS.map((group) => h('div', { class: 'nav-group' },
    group.label ? h('div', { class: 'nav-label' }, group.label) : null,
    ...group.items.map((item) => {
      const badge = item.badge ? h('span', { class: 'nav-badge', style: { display: 'none' } }) : null;
      if (badge) badges.set(item.badge, badge);
      const btn = h('button', {
        class: 'nav-item' + (item.id === activeId ? ' active' : ''),
        onClick: () => onNavigate(item.id),
      }, icon(item.icon), item.label, badge);
      buttons.set(item.id, btn);
      return btn;
    })));

  const user = session.user || {};
  const initials = (user.displayName || user.username || '?')
    .split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase();

  const el = h('aside', { class: 'sidebar' },
    h('div', { class: 'brand' },
      h('div', { class: 'brand-mark' }, 'W'),
      h('div', { class: 'brand-text' },
        h('div', { class: 't1' }, 'Wings Theory'),
        h('div', { class: 't2' }, 'Operations Console'))),
    ...groups,
    h('div', { class: 'sidebar-foot' },
      h('div', { class: 'avatar' }, initials),
      h('div', {},
        h('div', { class: 'name' }, user.displayName || user.username || 'Signed in'),
        h('div', { class: 'role' }, user.role || '')),
      h('button', { class: 'signout', onClick: onSignOut, title: 'Sign out' }, 'Sign out')));

  el.setActive = (id) => {
    for (const [key, btn] of buttons) btn.classList.toggle('active', key === id);
  };

  /**
   * A badge of 0 is hidden, not shown as "0" — an empty queue is the absence
   * of a call to action, not a call to action reading zero.
   */
  el.setBadge = (key, count) => {
    const badge = badges.get(key);
    if (!badge) return;
    if (!count) { badge.style.display = 'none'; return; }
    badge.style.display = '';
    badge.textContent = count > 999 ? '999+' : String(count);
  };

  return el;
}
