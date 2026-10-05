import { h, icon } from '../lib/dom.js';
import { treeForRole } from './nav.js';
import { session } from '../lib/api/client.js';

const EXPANDED_KEY = 'wt_console_nav_expanded';
const COLLAPSED_KEY = 'wt_console_nav_collapsed';

function loadExpanded() {
  try { return new Set(JSON.parse(localStorage.getItem(EXPANDED_KEY) || '[]')); } catch { return new Set(); }
}
function saveExpanded(set) {
  try { localStorage.setItem(EXPANDED_KEY, JSON.stringify([...set])); } catch { /* private window etc. */ }
}
function loadCollapsed() {
  try { return localStorage.getItem(COLLAPSED_KEY) === '1'; } catch { return false; }
}
function saveCollapsed(v) {
  try { localStorage.setItem(COLLAPSED_KEY, v ? '1' : '0'); } catch { /* private window etc. */ }
}

const CHEVRON = '<path d="M9 6l6 6-6 6"/>';
const COLLAPSE_ICON = '<path d="M15 18l-6-6 6-6"/>';

/**
 * A true hierarchical accordion sidebar (2026-10-01 redesign, spec section
 * 6): a group expands/collapses and never mounts a page itself; each child
 * is its own clickable route. This replaced the earlier flat, role-filtered
 * list (which grouped items under section labels like "Manage"/"Monitor"
 * but never actually nested a page's own sub-pages under it).
 *
 * Expanded state persists in localStorage across reloads (per-browser, not
 * per-account — acceptable for a single-tenant LAN console). The group
 * containing the active leaf is always force-expanded regardless of its
 * saved state, so a direct hash visit or reload never lands on a page whose
 * own entry is hidden inside a collapsed group.
 */
export function Sidebar({ activeId, onNavigate, onSignOut }) {
  const leafButtons = new Map();
  const badges = new Map();
  const groupBodies = new Map();
  const groupToggles = new Map();

  const role = (session.user || {}).role;
  const tree = treeForRole(role);
  const expanded = loadExpanded();
  let collapsed = loadCollapsed();

  function applyCollapsedClass() {
    document.body.classList.toggle('nav-collapsed', collapsed);
  }
  applyCollapsedClass();

  function setCollapsed(next) {
    collapsed = next;
    saveCollapsed(collapsed);
    applyCollapsedClass();
    collapseToggle.classList.toggle('open', !collapsed);
    collapseLabel.textContent = collapsed ? '' : 'Collapse';
  }

  function activeGroupId() {
    for (const node of tree) {
      if (node.children && node.children.some((c) => c.id === activeId)) return node.id;
    }
    return null;
  }

  function setGroupOpen(groupId, open) {
    if (open) expanded.add(groupId); else expanded.delete(groupId);
    saveExpanded(expanded);
    const body = groupBodies.get(groupId);
    const toggle = groupToggles.get(groupId);
    if (body) body.style.display = open ? '' : 'none';
    if (toggle) toggle.classList.toggle('open', open);
  }

  function leafButton(item) {
    const badge = item.badge ? h('span', { class: 'nav-badge', style: { display: 'none' } }) : null;
    if (badge) badges.set(item.badge, badge);
    const btn = h('button', {
      class: 'nav-item' + (item.id === activeId ? ' active' : ''),
      title: item.label,
      onClick: () => onNavigate(item.id),
    }, h('span', { class: 'nav-item-label' }, item.label), badge);
    leafButtons.set(item.id, btn);
    return btn;
  }

  const forceOpenGroup = activeGroupId();

  const nodes = tree.map((node) => {
    if (!node.children) {
      const badge = node.badge ? h('span', { class: 'nav-badge', style: { display: 'none' } }) : null;
      if (badge) badges.set(node.badge, badge);
      const btn = h('button', {
        class: 'nav-item nav-item-top' + (node.id === activeId ? ' active' : ''),
        title: node.label,
        onClick: () => onNavigate(node.id),
      }, icon(node.icon), h('span', { class: 'nav-item-label' }, node.label), badge);
      leafButtons.set(node.id, btn);
      return btn;
    }

    const isOpen = expanded.has(node.id) || node.id === forceOpenGroup;
    if (isOpen) expanded.add(node.id);

    const chevron = h('span', { class: 'nav-group-chevron' + (isOpen ? ' open' : '') }, icon(CHEVRON));
    groupToggles.set(node.id, chevron);

    const toggle = h('button', {
      class: 'nav-group-toggle',
      title: node.label,
      // Collapsed rail: a group can't show indented children in an
      // icon-only strip, so clicking one re-expands the whole sidebar and
      // opens that group, landing the user exactly where a click normally
      // would — rather than building a hover flyout submenu for this.
      onClick: () => {
        const wasCollapsed = collapsed;
        if (wasCollapsed) setCollapsed(false);
        setGroupOpen(node.id, wasCollapsed || !expanded.has(node.id));
      },
    }, icon(node.icon), h('span', { class: 'nav-group-label' }, node.label), chevron);

    const body = h('div', {
      class: 'nav-group-body',
      style: { display: isOpen ? '' : 'none' },
    }, ...node.children.map(leafButton));
    groupBodies.set(node.id, body);

    return h('div', { class: 'nav-group' }, toggle, body);
  });

  const user = session.user || {};
  const initials = (user.displayName || user.username || '?')
    .split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase();

  const collapseLabel = h('span', { class: 'nav-item-label' }, collapsed ? '' : 'Collapse');
  const collapseToggle = h('span', { class: 'nav-collapse-chevron' + (collapsed ? '' : ' open') }, icon(COLLAPSE_ICON));
  const collapseBtn = h('button', {
    class: 'nav-collapse-toggle',
    title: collapsed ? 'Expand sidebar' : 'Collapse sidebar',
    onClick: () => setCollapsed(!collapsed),
  }, collapseToggle, collapseLabel);

  const el = h('aside', { class: 'sidebar' },
    h('div', { class: 'brand' },
      h('div', { class: 'brand-mark' }, 'W'),
      h('div', { class: 'brand-text' },
        h('div', { class: 't1' }, 'Wings Theory'),
        h('div', { class: 't2' }, 'Operations Console'))),
    h('nav', { class: 'nav-tree' }, ...nodes),
    h('div', { class: 'sidebar-foot' },
      h('div', { class: 'avatar' }, initials),
      h('div', { class: 'nav-item-label' },
        h('div', { class: 'name' }, user.displayName || user.username || 'Signed in'),
        h('div', { class: 'role' }, user.role || '')),
      h('button', { class: 'signout', title: 'Sign out', onClick: onSignOut }, 'Sign out')),
    collapseBtn);

  /** Called on every route change. A leaf outside the currently-collapsed
   * group still needs its group force-opened so the active item is visible
   * — this is the reload/direct-link case, handled the same way as first
   * render. */
  el.setActive = (id) => {
    for (const [key, btn] of leafButtons) btn.classList.toggle('active', key === id);
    for (const node of tree) {
      if (node.children && node.children.some((c) => c.id === id) && !expanded.has(node.id)) {
        setGroupOpen(node.id, true);
      }
    }
  };

  /** A badge of 0 is hidden, not shown as "0" — an empty queue is the
   * absence of a call to action, not a call to action reading zero. */
  el.setBadge = (key, count) => {
    const badge = badges.get(key);
    if (!badge) return;
    if (!count) { badge.style.display = 'none'; return; }
    badge.style.display = '';
    badge.textContent = count > 999 ? '999+' : String(count);
  };

  return el;
}
