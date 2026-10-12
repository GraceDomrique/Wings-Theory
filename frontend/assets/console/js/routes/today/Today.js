import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { RailCard } from '../../components/ui/RailCard.js';
import { Stat } from '../../components/ui/Stat.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { AlertRow } from '../../components/domain/AlertRow.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { palette } from '../../components/charts/base.js';
import { count, money, pct } from '../../lib/format.js';
import { getSummary, getBusinessOverview } from '../../lib/api/dashboard.js';
import { session } from '../../lib/api/client.js';

/**
 * Today — spec section 6 point 1: renders without its own scroll on a
 * 1366x768 viewport.
 *
 * How that is held: `.today-grid` is a fixed-height CSS grid sized to
 * calc(100vh - chrome), with five KPI tiles across the top and three panels
 * below. The panels scroll INTERNALLY (.scrollpane); the page does not. A
 * sixth KPI cannot be appended — the grid has five columns and adding one
 * would shrink all five below legibility, which is the mechanical version of
 * the rule "a new KPI replaces one of the five, it does not get appended".
 *
 * Data comes from ONE aggregation call (lib/api/dashboard.js). This route
 * deliberately does not fan out to six endpoints and merge — see spec
 * section 4, Today.
 */
export async function render(ctx) {
  const s = await getSummary();

  const grid = h('div', { class: 'today-grid' + (s.isSample ? ' has-banner' : '') },
    kpiStrip(s, ctx),
    alertsPanel(s, ctx),
    instructionsPanel(s, ctx),
    expiryPanel(s, ctx),
    verifyPanel(s, ctx));

  if (s.isSample) {
    // The stub is in use. Say so loudly and permanently — sample figures read
    // as real numbers is the exact failure this rebuild exists to remove.
    return h('div', {},
      h('div', { class: 'callout callout-warn', style: { marginBottom: '14px' } },
        h('p', {},
          h('strong', {}, 'Sample figures — not your real numbers. '),
          'Today needs one aggregation endpoint, ',
          h('span', { class: 'mono' }, 'GET /dashboard/summary'),
          ', which this backend does not serve yet. The layout below is wired and will show real data the moment that endpoint exists; nothing here has been read from your database.')),
      grid);
  }

  // 2026-10: the business-companion section below (spec: "how is my
  // business doing, is it better or worse than before, what's coming next")
  // is a SECOND, independent call (getBusinessOverview) laid above the
  // original fixed-viewport grid — see that grid's own docstring for why
  // it was built to fit one screen without scrolling. This content doesn't
  // fit that constraint without shrinking every number below legibility,
  // so this page now scrolls; the grid below keeps its own internal
  // scrollpanes exactly as before.
  //
  // 2026-10 (second pass): the first version of this section stacked
  // Business Today, a two-row Performance table, Recent Trend AND Business
  // Outlook as four separate cards — accurate, but it buried the original,
  // already-tuned operational grid under a wall of numbers before an admin
  // ever reached it. Collapsed into ONE tabbed snapshot card (Today / This
  // Week / This Month share the same four tiles instead of each getting
  // its own block) plus Recent Trend, with the forecast folded into the
  // trend card as one caption line — present only when the model actually
  // produced something, never an empty placeholder card for "no forecast
  // yet". Same data, roughly half the vertical space.
  // Failure here is non-fatal and never blocks the original grid: a host
  // under heavy load or an edge case in the new aggregation should not take
  // down the page every other section on Today already depends on.
  const overviewHost = h('div', { style: { marginBottom: '18px' } });
  getBusinessOverview().then((bo) => {
    mount(overviewHost,
      h('div', { class: 'grid', style: { gridTemplateColumns: '1.5fr 1fr', gap: '14px', alignItems: 'start' } },
        businessSnapshotSection(bo),
        trendSection(bo)));
  }).catch(() => { /* the five-KPI grid below still stands on its own */ });

  return h('div', {}, overviewHost, grid);
}

function deltaPill(value) {
  if (value === null || value === undefined) return Pill('flat', 'n/a');
  if (value > 0.05) return Pill('good', h('span', {}, '▲ ', pct(value)));
  if (value < -0.05) return Pill('warn', h('span', {}, '▼ ', pct(Math.abs(value))));
  return Pill('flat', '≈ flat');
}

function miniStat(label, valueNode, deltaValue) {
  return h('div', {},
    h('div', { class: 't-xs t-faint' }, label),
    h('div', { style: { fontWeight: '600', fontSize: '17px', margin: '3px 0 6px' } }, valueNode),
    deltaPill(deltaValue));
}

function staleNote(bo) {
  if (bo.dataStaleHours === null || bo.dataStaleHours <= 24) return null;
  const days = Math.floor(bo.dataStaleHours / 24);
  return h('div', { class: 't-xs', style: { color: 'var(--warn)', marginBottom: '10px' } },
    `⚠ Order data is ${days} day${days === 1 ? '' : 's'} old — these figures reflect that gap, not necessarily a real change.`);
}

/**
 * One tabbed card instead of three stacked ones: Today / This Week / This
 * Month all share the same four tiles (Sales, Orders, Avg order, Covers)
 * and just swap which comparison backs them, rather than each getting its
 * own full card. The stale-data note only applies to the Today tab — a
 * 1-2 day lag barely dents a week- or month-to-date total the way it
 * swamps a single partial day.
 */
function businessSnapshotSection(bo) {
  const TABS = [
    { key: 'today', label: 'Today', block: bo.today, compare: bo.today.label ? `vs ${bo.today.label}` : '' },
    { key: 'week', label: 'This Week', block: bo.thisWeek, compare: 'vs last week' },
    { key: 'month', label: 'This Month', block: bo.thisMonth, compare: 'vs last month' },
  ];
  let active = 'today';
  const body = h('div');
  const tabEls = new Map();

  function paintTabs() {
    for (const [key, el] of tabEls) {
      el.style.background = key === active ? 'var(--amber)' : 'transparent';
      el.style.color = key === active ? '#221805' : 'var(--ink-soft)';
    }
  }

  function draw() {
    const tab = TABS.find((t) => t.key === active);
    const b = tab.block;
    mount(body,
      tab.key === 'today' ? staleNote(bo) : null,
      h('div', { class: 't-xs t-faint', style: { marginBottom: '10px' } }, tab.compare),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: '14px' } },
        miniStat('Sales', money(b.current.revenue, '₱0'), b.deltas.revenuePct),
        miniStat('Orders', count(b.current.orders), b.deltas.ordersPct),
        miniStat('Avg order', b.current.avgOrder === null ? count(null, 'no orders yet') : money(b.current.avgOrder), b.deltas.avgOrderPct),
        miniStat('Covers', count(b.current.covers), b.deltas.coversPct)));
  }

  const tabRow = h('div', { style: { display: 'flex', gap: '4px' } },
    ...TABS.map((t) => {
      const btn = h('button', {
        class: 'btn btn-sm',
        style: { border: 'none' },
        onClick: () => { active = t.key; paintTabs(); draw(); },
      }, t.label);
      tabEls.set(t.key, btn);
      return btn;
    }));

  draw();
  paintTabs();

  return Card({},
    CardHeader({ title: 'Business Snapshot', right: tabRow }),
    body);
}

function trendSection(bo) {
  const weeks = bo.trend.weeks || [];
  const avg = bo.trend.averageRevenue;
  const colors = weeks.map((w) => (w.revenue < avg ? palette.warn : palette.good));
  const o = bo.outlook;
  return Card({},
    CardHeader({ title: 'Recent Trend', desc: `${weeks.length} completed weeks, revenue` }),
    BarChart({
      labels: weeks.map((w) => w.weekStart.slice(5)),
      data: weeks.map((w) => w.revenue),
      colors,
      height: 120,
      empty: { icon: '▤', title: 'Not enough completed weeks yet', description: 'This fills in as more weeks close.', tight: true },
    }),
    bo.trend.belowAverageStreak >= 2
      ? h('div', { class: 't-xs', style: { color: 'var(--warn)', marginTop: '8px' } },
        `⚠ Below the ${weeks.length}-week average for ${bo.trend.belowAverageStreak} straight weeks.`)
      : null,
    // Only a one-line caption when the existing forecast model actually
    // produced something real — no card, no placeholder, when it didn't.
    o.available
      ? h('div', { class: 't-xs t-faint', style: { marginTop: '10px', paddingTop: '10px', borderTop: '1px solid var(--line-soft)' } },
        Pill('info', 'Forecast'), ' Next 7 days: ', money(o.forecastTotal),
        o.deltaPct !== null ? ` (${Math.abs(o.deltaPct).toFixed(1)}% ${o.deltaPct > 0 ? 'higher' : 'lower'} than the recent pace)` : '',
        ' — based on recent order patterns, not tied to any calendar event.')
      : null);
}

/** The five KPI tiles. Order is fixed and meaningful: soonest-to-hurt first. */
function kpiStrip(s, ctx) {
  return h('div', { class: 'g-kpis' }, ...s.kpis.map((k) => RailCard(
    { severity: k.severity, className: 'kpi-card' },
    h('div', { class: 'top' },
      Stat({
        // A KPI the backend could not compute shows the "no data" state, never
        // a zero — "0 expired batches" and "we could not check" are different
        // statements and must look different.
        value: k.display ? k.display : count(k.value, 'not checked'),
        label: k.label,
      }),
      h('div', { class: `kpi-icon kpi-icon-${k.severity}` }, k.icon)),
    h('div', { class: 'foot' }, s.isSample ? 'sample' : k.foot))));
}

/**
 * GET /dashboard/summary (wt/api.py) is backend, unmodified by the
 * 2026-10-01 nav rebuild — it still emits the OLD, pre-split route
 * vocabulary ('inventory', 'purchasing', 'menu') an alert's `a.route`
 * carries. This maps those onto the current flat leaf ids, per role, since
 * several of the old composite ids no longer exist as routes at all (e.g.
 * 'inventory' split into onhand/receiving/stockcounts/movements — an
 * alert about low stock means "go look at what to buy", not any one of
 * those).
 */
function roleRoute(route) {
  const role = (session.user || {}).role;
  const BACKEND_ALIAS = {
    inventory: role === 'owner' ? 'inventoryoverview' : 'onhand',
    purchasing: 'whattobuy',
    // Owner's nav has no Menu & Recipes group (editing is admin-only) — the
    // read-only "what's unavailable and why" view Owner needs instead lives
    // on Inventory Overview.
    menu: role === 'owner' ? 'inventoryoverview' : 'menu',
  };
  return BACKEND_ALIAS[route] || route;
}

function alertsPanel(s, ctx) {
  const open = s.alerts.length;
  const body = open === 0
    ? EmptyState({
      icon: '✓',
      title: 'Nothing needs you right now',
      description: 'No expired stock, no blocked menu items, and no flagged losses in the current window.',
      tight: true,
    })
    : h('div', { class: 'scrollpane' }, ...s.alerts.map((a) => AlertRow({
      severity: a.severity,
      headline: a.headline,
      detail: a.detail,
      onClick: a.route ? () => ctx.navigate(roleRoute(a.route)) : undefined,
    })));

  return Card({ className: 'g-alerts' },
    CardHeader({
      title: 'Action Required',
      desc: 'Ranked by what breaks first if ignored',
      right: open ? Pill(worstOf(s.alerts), `${open} open`) : Pill('good', 'all clear'),
    }),
    body);
}

/** The most severe severity present, for the header pill. */
function worstOf(alerts) {
  const order = ['crit', 'warn', 'info', 'good', 'flat'];
  return order.find((sev) => alerts.some((a) => a.severity === sev)) || 'flat';
}

/**
 * Instructions — replaced the old Demand Forecast chart in this exact grid
 * slot (2026-09-30 UX/IA redesign, part G): a Prophet preview is a model
 * output nobody acts on from Today, while an open instruction IS something
 * to act on today. The forecast chart itself still exists, just relocated
 * to its own admin-only "Demand Forecast" page rather than the primary
 * screen. (2026-10: the free-text "Messages" half of this panel — ad hoc
 * updates with no structure — was removed; instructions are the only
 * Owner/Admin operational-communication channel now.)
 */
function instructionsPanel(s, ctx) {
  const m = s.instructionsPreview || { openInstructions: 0, items: [] };
  return Card({ className: 'g-instructions' },
    CardHeader({
      title: 'Instructions',
      desc: 'From Owner to Admin',
      right: h('div', { style: { display: 'flex', gap: '6px', alignItems: 'center' } },
        m.openInstructions ? Pill('flat', `${m.openInstructions} open`) : Pill('good', 'all clear'),
        Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('instructions') }, 'View all →')),
    }),
    m.items.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing open', description: 'No instruction is waiting on either side right now.', tight: true })
      : h('div', { class: 'scrollpane' }, ...m.items.map((i) => h('div', { class: 'mini-row' },
        h('span', {}, i.title),
        Pill(i.status === 'open' ? 'flat' : 'warn', i.status.replace('_', ' '))))));
}

function expiryPanel(s, ctx) {
  // Field is `useFirst` on the real payload and on SAMPLE alike (see
  // dashboard.js's own typedef comment: "renamed from the old
  // expiryPreview") -- this reader was never updated when that rename
  // happened, so this panel has been silently empty regardless of real
  // data until this fix (2026-09-30, found while adding messagesPanel).
  const rows = s.useFirst || [];
  return Card({ className: 'g-expiry' },
    CardHeader({
      title: 'Use First',
      desc: 'Oldest batches, soonest expiry',
      right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('expiry') }, 'View all →'),
    }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing turning soon', description: 'No open batch is inside the expiry window.', tight: true })
      : h('div', { class: 'scrollpane' }, ...rows.map((r) => h('div', { class: 'mini-row' },
        // r.qty is the structured {qty,unit,display} pair every quantity on
        // this payload uses (2026-09 rewrite) -- .display is the pre-
        // formatted string; this used to hand the object itself to `h()`
        // and print "[object Object]" until this fix (found alongside the
        // expiryPreview/useFirst field-name bug just above).
        h('span', {}, r.name, r.qty ? h('div', { class: 'sub' }, r.qty.display) : null),
        Pill(r.severity, r.label)))));
}

function verifyPanel(s, ctx) {
  const rows = s.verifyPreview || [];
  const isAdmin = (session.user || {}).role === 'admin';
  return Card({ className: 'g-verify' },
    CardHeader({
      title: 'Needs Confirming',
      desc: 'Affects stock deductions',
      // Owner can see this preview (it rides along on the one dashboard
      // call both roles can read) but has no capability to resolve a
      // queue item -- Data Quality is admin-only (wt/auth.py: queue.read
      // is manager+admin, not owner) -- so there is nowhere to send the
      // click for that role.
      right: isAdmin ? Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('verify') }, 'Review →') : null,
    }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'Every value is confirmed', description: 'No placeholder numbers are left in setup.', tight: true })
      // Flat pills here, matching the prototype: on Today these are a queue to
      // work through, not four items each shouting a different severity. The
      // priority-coloured treatment lives on the Verification page itself.
      : h('div', { class: 'scrollpane' }, ...rows.map((r) => h('div', { class: 'mini-row' },
        h('span', {}, r.label),
        Pill('flat', `Priority ${r.priority}`)))));
}
