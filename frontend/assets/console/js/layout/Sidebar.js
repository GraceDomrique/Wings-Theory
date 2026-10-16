import { h, icon } from '../lib/dom.js';
import { treeForRole } from './nav.js';
import { session } from '../lib/api/client.js';
import { ICONS } from '../lib/icons.js';

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
export function Sidebar({ activeId, onNavigate }) {
  const leafButtons = new Map();
  const badges = new Map();
  const groupBodies = new Map();
  const groupToggles = new Map();

  const role = (session.user || {}).role;
  const tree = treeForRole(role);
  const expanded = loadExpanded();
  let collapsed = loadCollapsed();

  // body (not just the sidebar) carries the role class, so --role-accent*
  // (app.css) is readable by the topbar too -- a sibling of .sidebar in the
  // shell, not a descendant of it.
  document.body.classList.toggle('role-admin', role !== 'owner');
  document.body.classList.toggle('role-owner', role === 'owner');

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

  // 860px, not 600px: below 860 the sidebar is ALREADY an icon-only rail
  // (app.css's 601-860px tier), where an inline-expanded group body has no
  // room to lay out and used to be hidden outright (display:none !important)
  // with no way to reach it at all -- "Management" was simply unopenable at
  // tablet/narrow-desktop widths. Reusing the same floating-sheet affordance
  // the <=600px bottom-tab-bar tier already has (just anchored beside the
  // icon instead of above the bar — see app.css) makes every width below
  // 860px able to open a group, not just phone width.
  const isMobile = () => window.matchMedia('(max-width:860px)').matches;
  // Mobile's floating sheet is a transient, one-at-a-time affordance, not
  // the desktop accordion's persisted expand/collapse state -- routing its
  // open/close through `expanded` (and therefore through saveExpanded, i.e.
  // localStorage) would let a phone session silently overwrite whatever
  // groups the user had deliberately left expanded on desktop, since both
  // share the same EXPANDED_KEY. Tracked separately instead.
  let mobileOpenGroup = null;

  function setGroupOpen(groupId, open) {
    if (isMobile()) {
      const body = groupBodies.get(groupId);
      const toggle = groupToggles.get(groupId);
      if (body) {
        body.style.display = open ? '' : 'none';
        // 601-860px (icon rail): the floating sheet is position:fixed,
        // anchored next to whichever group's icon was actually tapped (see
        // app.css's media query) -- unlike the <=600px bottom-bar tier,
        // which pins the sheet above the bar via its own bottom/left/right
        // and must NOT get a conflicting inline `top` here.
        if (open && window.matchMedia('(min-width:601px)').matches) {
          const anchorTop = body.parentElement.getBoundingClientRect().top;
          const maxTop = window.innerHeight - body.offsetHeight - 10;
          body.style.top = `${Math.max(10, Math.min(anchorTop, maxTop))}px`;
        } else {
          body.style.top = '';
        }
      }
      if (toggle) toggle.classList.toggle('open', open);
      return;
    }
    if (open) expanded.add(groupId); else expanded.delete(groupId);
    saveExpanded(expanded);
    const body = groupBodies.get(groupId);
    const toggle = groupToggles.get(groupId);
    if (body) body.style.display = open ? '' : 'none';
    if (toggle) toggle.classList.toggle('open', open);
  }

  function leafButton(item, groupId) {
    const badge = item.badge ? h('span', { class: 'nav-badge', style: { display: 'none' } }) : null;
    if (badge) badges.set(item.badge, badge);
    const btn = h('button', {
      class: 'nav-item' + (item.id === activeId ? ' active' : ''),
      title: item.label,
      onClick: () => {
        onNavigate(item.id);
        // Mobile only (<=600px, the bottom-tab-bar tier): a selected leaf's
        // group sheet floats fixed above the bar and would otherwise stay
        // open over the page the user just navigated to -- the spec's own
        // "closes after selection" requirement for a drawer. Desktop's
        // inline-expanded accordion is untouched (collapsing it on every
        // click there would undo the point of leaving groups expanded).
        if (groupId && isMobile()) { setGroupOpen(groupId, false); mobileOpenGroup = null; }
      },
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

    // At the mobile bottom-tab-bar tier (<=600px) a group's body is a
    // floating sheet, not an inline list -- it must start closed (the user
    // taps the group icon to open it) even when it's the active page's own
    // parent group or was left expanded from a desktop session, otherwise
    // the sheet would cover freshly-loaded content on first paint/reload
    // without the user asking for it.
    const isOpen = !isMobile() && (expanded.has(node.id) || node.id === forceOpenGroup);
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
        if (isMobile()) {
          // One sheet at a time: opening a second group while the first is
          // still showing would stack two floating sheets over the bar.
          const willOpen = mobileOpenGroup !== node.id;
          if (mobileOpenGroup && mobileOpenGroup !== node.id) setGroupOpen(mobileOpenGroup, false);
          setGroupOpen(node.id, willOpen);
          mobileOpenGroup = willOpen ? node.id : null;
          return;
        }
        const wasCollapsed = collapsed;
        if (wasCollapsed) setCollapsed(false);
        setGroupOpen(node.id, wasCollapsed || !expanded.has(node.id));
      },
    }, icon(node.icon), h('span', { class: 'nav-group-label' }, node.label), chevron);

    const body = h('div', {
      class: 'nav-group-body',
      style: { display: isOpen ? '' : 'none' },
    }, ...node.children.map((child) => leafButton(child, node.id)));
    groupBodies.set(node.id, body);

    return h('div', { class: 'nav-group' }, toggle, body);
  });

  const collapseLabel = h('span', { class: 'nav-item-label' }, collapsed ? '' : 'Collapse');
  const collapseToggle = h('span', { class: 'nav-collapse-chevron' + (collapsed ? '' : ' open') }, icon(COLLAPSE_ICON));
  const collapseBtn = h('button', {
    class: 'nav-collapse-toggle',
    title: collapsed ? 'Expand sidebar' : 'Collapse sidebar',
    onClick: () => setCollapsed(!collapsed),
  }, collapseToggle, collapseLabel);

  // Account Settings — pinned below the main tree for every role (not a
  // leaf of either ADMIN_TREE/OWNER_TREE; see nav.js's routeRoles('settings')
  // special case), same place "Collapse" already lives, so it reads as
  // account-level rather than one more operational destination.
  const settingsBtn = h('button', {
    class: 'nav-item nav-item-top' + ('settings' === activeId ? ' active' : ''),
    title: 'Account Settings',
    onClick: () => onNavigate('settings'),
  }, icon(ICONS.userRound), h('span', { class: 'nav-item-label' }, 'Account Settings'));
  leafButtons.set('settings', settingsBtn);

  // 2026-10-16: the hand-drawn role glyph (a shield/crown swapped in via
  // this same brand-mark slot) read as a generic admin-dashboard icon next
  // to the actual Wings Theory brand mark. Using the real wing artwork
  // (/img/wing.png — the same mark already used on the sign-in screen)
  // here instead, for every role, so the sidebar's identity matches the
  // brand rather than a role abstraction.
  const subtitle = role === 'owner' ? 'Owner Intelligence' : 'Operations Console';
  const el = h('aside', { class: 'sidebar' },
    h('div', { class: 'brand' },
      h('div', { class: 'brand-mark' }, h('img', { src: '/img/wing.png', alt: '', class: 'brand-mark-img' })),
      h('div', { class: 'brand-text' },
        h('div', { class: 't1' }, 'Wings Theory'),
        h('div', { class: 't2' }, subtitle))),
    h('nav', { class: 'nav-tree' }, ...nodes),
    h('div', { class: 'nav-foot' }, settingsBtn),
    collapseBtn);

  /** Called on every route change. A leaf outside the currently-collapsed
   * group still needs its group force-opened so the active item is visible
   * — this is the reload/direct-link case, handled the same way as first
   * render.
   *
   * Skipped at <=860px (the icon-rail and bottom-tab-bar tiers): there, a
   * group's children render as a floating sheet rather than an
   * inline-expanded list, and leafButton's own onClick already closes that
   * sheet right after navigating (spec: "closes after selection"). Without
   * this guard, THIS force-open — running moments later, once the
   * navigation's hashchange reaches AppShell.draw() — immediately reopened
   * the very sheet the user had just dismissed by picking an item from it,
   * because the newly-active leaf still belongs to a now-collapsed group. */
  el.setActive = (id) => {
    for (const [key, btn] of leafButtons) btn.classList.toggle('active', key === id);
    if (window.matchMedia('(max-width:860px)').matches) return;
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
