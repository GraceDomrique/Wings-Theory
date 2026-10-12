import { h, mount } from '../lib/dom.js';
import { Sidebar } from './Sidebar.js';
import { Topbar } from './Topbar.js';
import {
  PAGE_SUB, ROUTE_IDS, routeRoles, breadcrumbFor,
} from './nav.js';
import { destroyAllCharts } from '../components/charts/base.js';
import { Loading, ErrorState } from '../components/ui/Feedback.js';
import { api, session, setUnauthorizedHandler } from '../lib/api/client.js';

/**
 * The grid: Sidebar + Main. Owns routing and is the only thing that mounts
 * a route.
 *
 * Routing is on the hash (#/losses) — one segment per route now (2026-10-01:
 * the old #/id/tab sub-tab convention is gone along with the in-page Tabbar
 * it served; every former sub-tab is its own top-level route id, reached
 * through the sidebar accordion instead). This keeps a direct link/reload
 * working exactly as before, just one level flatter.
 *
 * Every route module exports `render(ctx)` returning a Node (or a Promise of
 * one). Title, subtitle and breadcrumb are derived from nav.js against the
 * CURRENT role's tree, not stored per-route — the same id can carry a
 * different label/group depending on who's looking (Owner vs. Admin).
 */
export function AppShell({ routes, onSignOut }) {
  let current = null;
  let lastAuditedId = null;

  const content = h('div', { class: 'content' });

  const topbar = Topbar({
    title: 'Today',
    sub: PAGE_SUB.today,
    onSearch: (q) => { if (searchHandler) searchHandler(q); },
    onSignOut,
  });

  const sidebar = Sidebar({
    activeId: 'today',
    onNavigate: (id) => navigate(id),
  });

  let searchHandler = null;

  const el = h('div', { class: 'shell' }, sidebar, h('div', { class: 'main' }, topbar, content));

  function parseHash() {
    const raw = (location.hash || '').replace(/^#\/?/, '');
    // A bare id only now — the old "#/id/tab" sub-route is gone with the
    // in-page Tabbar it served (see this file's own docstring). A stray
    // "/tab" suffix on an old bookmark is simply ignored rather than
    // breaking the route lookup.
    const id = raw.split('/')[0];
    // Sidebar.js already filters what a role can CLICK; this covers a
    // typed URL, bookmark, or reload landing directly on a page that role
    // never sees (e.g. Owner on #/users) — the server would 403 the
    // underlying call anyway, but redirecting to the home page here avoids
    // an avoidable error page for something session.user.role already knows.
    const role = (session.user || {}).role;
    const valid = ROUTE_IDS.includes(id) && routeRoles(id).includes(role);
    return valid ? id : 'today';
  }

  async function draw() {
    const id = parseHash();
    current = id;

    // Chart.js holds global references; leaving old instances alive across a
    // route swap is what makes charts come back blank on a second visit.
    destroyAllCharts();
    searchHandler = null;
    const role = (session.user || {}).role;
    const { groupLabel, leafLabel } = breadcrumbFor(role, id);
    topbar.setCopy(leafLabel, PAGE_SUB[id] || '');
    topbar.setBreadcrumb(groupLabel);
    topbar.setSearchVisible(id === 'onhand');
    sidebar.setActive(id);
    mount(content, Loading());

    // Which tab/page a user opened, for the audit trail (2026-10
    // accountability pass) — fired once per actual NAVIGATION, not on a
    // same-route re-render (the topbar's refresh button also calls draw()
    // on an unchanged hash). Never blocks the page: this is attribution of
    // something that already happened, not a permission check.
    if (id !== lastAuditedId) {
      lastAuditedId = id;
      const area = groupLabel ? `${groupLabel} → ${leafLabel}` : leafLabel;
      api.post('/console/audit/page-view', { route_id: id, area })
        .catch(() => { /* navigation audit is best-effort, never blocking */ });
    }

    const ctx = {
      navigate,
      setSearchHandler: (fn) => { searchHandler = fn; },
      refreshBadges,
      // A handful of older Inventory subviews (Receiving/Movements) still
      // read ctx.selection.ingredient defensively from the days a click in
      // On Hand pre-filtered them via a shared in-page tab shell — nothing
      // sets it anymore (that cross-nav went through Inventory.js's own
      // Tabbar wrapper, now removed), but the shape must still exist so
      // that optional-read doesn't throw on an id that's never null.
      selection: { ingredient: null },
    };

    try {
      const view = await routes[id].render(ctx);
      // A slower route that resolves after the user has already moved on must
      // not paint over the page they are now looking at.
      if (current !== id) return;
      mount(content, view);
    } catch (err) {
      if (current !== id) return;
      mount(content, ErrorState({ message: err.message, onRetry: draw }));
    }
  }

  function navigate(id) {
    const next = '#/' + id;
    if (location.hash === next) draw();
    else location.hash = next;
  }

  /** Sidebar counts, refreshed after any action that could change them. */
  async function refreshBadges() {
    // Data Quality (the 'verify' page) isn't even in Owner's nav, and
    // Owner holds no queue.read capability at all (wt/auth.py) — fetching
    // its count for that role is a guaranteed, silently-swallowed 403 on
    // every single page load. Route id, not capability name, on purpose:
    // this mirrors Sidebar.js's own role filter rather than re-deriving
    // authorization here.
    if (routeRoles('verify').includes((session.user || {}).role)) {
      try {
        const { getQueue } = await import('../lib/api/verification.js');
        const q = await getQueue();
        sidebar.setBadge('verification', q.count);
      } catch { /* a badge is not worth surfacing an error for */ }
    }
    try {
      const { listInstructions } = await import('../lib/api/instructions.js');
      const open = await listInstructions('open');
      sidebar.setBadge('instructions', open.count);
    } catch { /* a badge is not worth surfacing an error for */ }
    // Owner's Stock & Replenishment badge — count of ingredients currently
    // recommended for purchase (same needsBuying() rule the page itself
    // uses: out/critical/at_risk status or below the configured reorder
    // point). Owner-only: admin doesn't have this page in its nav tree.
    if ((session.user || {}).role === 'owner') {
      try {
        const { getReplenishment } = await import('../lib/api/intelligence.js');
        const { rows } = await getReplenishment();
        const needsBuying = rows.filter((r) => r.status === 'crit' || r.status === 'warn' || r.belowReorderPoint);
        sidebar.setBadge('stockreplenishment', needsBuying.length);
      } catch { /* a badge is not worth surfacing an error for */ }
    }
  }

  window.addEventListener('hashchange', draw);
  topbar.onRefresh(draw);
  setUnauthorizedHandler(() => onSignOut(true));

  el.start = () => {
    if (!location.hash) location.hash = '#/today';
    else draw();
    if (session.token) refreshBadges();
  };

  return el;
}
