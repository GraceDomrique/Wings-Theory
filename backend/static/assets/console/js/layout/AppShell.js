import { h, mount } from '../lib/dom.js';
import { Sidebar } from './Sidebar.js';
import { Topbar } from './Topbar.js';
import { PAGE_COPY, ROUTE_IDS } from './nav.js';
import { destroyAllCharts } from '../components/charts/base.js';
import { Loading, ErrorState } from '../components/ui/Feedback.js';
import { session, setUnauthorizedHandler } from '../lib/api/client.js';

/**
 * The grid: Sidebar + Main. Owns routing and is the only thing that mounts
 * a route.
 *
 * Routing is on the hash (#/inventory/batches) so a sub-tab is linkable and
 * survives a reload — the prototype toggled `.active` classes on pre-rendered
 * divs, which cannot do that. Visually it is the same shell; only the address
 * bar knows the difference.
 *
 * Every route module exports `render(ctx)` returning a Node (or a Promise of
 * one). `ctx` carries the sub-tab and a `setSearch` hook.
 */
export function AppShell({ routes, onSignOut }) {
  let current = null;

  const content = h('div', { class: 'content' });

  const topbar = Topbar({
    title: 'Today',
    sub: PAGE_COPY.today[1],
    onSearch: (q) => { if (searchHandler) searchHandler(q); },
  });

  const sidebar = Sidebar({
    activeId: 'today',
    onNavigate: (id) => navigate(id),
    onSignOut,
  });

  let searchHandler = null;

  const el = h('div', { class: 'shell' }, sidebar, h('div', { class: 'main' }, topbar, content));

  function parseHash() {
    const raw = (location.hash || '').replace(/^#\/?/, '');
    const [id, tab] = raw.split('/');
    return { id: ROUTE_IDS.includes(id) ? id : 'today', tab: tab || null };
  }

  async function draw() {
    const { id, tab } = parseHash();
    current = id;

    // Chart.js holds global references; leaving old instances alive across a
    // route swap is what makes charts come back blank on a second visit.
    destroyAllCharts();
    searchHandler = null;
    topbar.setCopy(PAGE_COPY[id][0], PAGE_COPY[id][1]);
    topbar.setSearchVisible(id === 'inventory');
    sidebar.setActive(id);
    mount(content, Loading());

    const ctx = {
      tab,
      navigate,
      setSearchHandler: (fn) => { searchHandler = fn; },
      refreshBadges,
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

  function navigate(id, tab) {
    const next = '#/' + id + (tab ? '/' + tab : '');
    if (location.hash === next) draw();
    else location.hash = next;
  }

  /** Sidebar counts, refreshed after any action that could change them. */
  async function refreshBadges() {
    try {
      const { getQueue } = await import('../lib/api/verification.js');
      const q = await getQueue();
      sidebar.setBadge('verification', q.count);
    } catch { /* a badge is not worth surfacing an error for */ }
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
