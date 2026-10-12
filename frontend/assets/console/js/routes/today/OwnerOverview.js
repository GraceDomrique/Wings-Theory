import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { StatTile } from '../../components/ui/StatTile.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { LineChart } from '../../components/charts/LineChart.js';
import { palette } from '../../components/charts/base.js';
import {
  money, count, stamp, ageFromNow,
} from '../../lib/format.js';
import { getOwnerOverview } from '../../lib/api/dashboard.js';
import { openOrderDetail } from '../reports/orderDetailModal.js';

/**
 * Owner Overview — reference-mockup landing screen (2026-10 relayout):
 * attention banner -> 4 headline stat tiles -> 2-col (sales trend / Owner
 * Decisions) -> 3-col (Inventory Outlook / Next 7 Days / Recent Activity)
 * -> full Recent Orders table. Everything the earlier version of this page
 * showed (Customer Pulse, Top Categories, Business Rhythm, Sales->Stock,
 * Messages) still renders, just folded into a second section below the
 * mockup's own sections rather than removed, per "preserve existing
 * functionality" — the mockup doesn't show them, it doesn't say to delete
 * them either.
 *
 * One aggregation call (getOwnerOverview -> GET /console/dashboard/
 * owner-overview, wt/dashboard_overview.owner_overview()). Nothing here
 * fits its own model or invents a figure — a mockup widget needing data
 * the backend doesn't expose (e.g. a unified order+PO activity log) renders
 * from the closest real data this payload actually carries, never a
 * fabricated stand-in.
 */

const ICON = {
  revenue: '<path d="M3 3v18h18"/><path d="M7 15l4-6 4 3 5-8"/>',
  orders: '<path d="M4 19V5a2 2 0 012-2h9l5 5v11a2 2 0 01-2 2H6a2 2 0 01-2-2z"/><path d="M9 13h6M9 17h6M9 9h2"/>',
  stock: '<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
};

export async function render(ctx) {
  const d = await getOwnerOverview();
  // Defensive against an older/cached backend build that predates the
  // needRestock/expiringSoon counts this tile reads — same honest-missing
  // treatment as a null field, not a crash.
  if (!d.stockAtRisk) d.stockAtRisk = { needRestock: null, expiringSoon: null };
  const stale = d.dataStaleHours !== null && d.dataStaleHours > 24
    ? h('div', { class: 'callout callout-warn', style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', flexWrap: 'wrap' } },
      h('p', { style: { margin: 0 } }, h('strong', {}, 'Order data is stale — '),
        'the most recent order on file is ', ageFromNow(d.latestOrderAt),
        '. The figures below reflect that gap, not necessarily a real change in the business.'),
      Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('salesoverview'), style: { flexShrink: 0 } }, 'Look at a longer period →'))
    : null;

  const bannerHost = h('div');
  const drawBanner = (expanded) => mount(bannerHost, attentionBanner(d, ctx, expanded, drawBanner));
  drawBanner(false);

  return h('div', { class: 'owner-overview' },
    stale,
    bannerHost,
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', gap: '14px', margin: '14px 0' } },
      StatTile({
        iconPath: ICON.revenue, severity: 'good', label: "Today's Revenue",
        value: d.todayRevenue ? money(d.todayRevenue.revenue, '₱0') : null,
        sub: d.todayRevenue ? deltaLine(d.todayRevenue.deltaPct, 'vs yesterday') : null,
      }),
      StatTile({
        iconPath: ICON.revenue, severity: 'info', label: "This Month's Revenue",
        value: d.monthRevenue ? money(d.monthRevenue.revenue, '₱0') : null,
        sub: d.monthRevenue ? deltaLine(d.monthRevenue.deltaPct, 'vs last month') : null,
      }),
      StatTile({
        iconPath: ICON.orders, severity: 'flat', label: 'Orders Today',
        value: d.todayRevenue ? count(d.todayRevenue.orders) : null,
        sub: d.todayRevenue ? deltaLine(d.todayRevenue.ordersDeltaPct, 'vs yesterday') : null,
      }),
      StatTile({
        iconPath: ICON.stock, severity: (d.stockAtRisk.needRestock || 0) > 0 ? 'crit' : 'good', label: 'Stock at Risk',
        value: count((d.stockAtRisk.needRestock || 0) + (d.stockAtRisk.expiringSoon || 0)),
        sub: h('span', { class: 't-xs t-faint' }, `${d.stockAtRisk.needRestock || 0} urgent · ${d.stockAtRisk.expiringSoon || 0} low`),
      })),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1.6fr 1fr', gap: '14px' } },
      salesTrendCard(d, ctx),
      ownerDecisionsCard(d, ctx)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr 1fr', gap: '14px', marginTop: '14px' } },
      inventoryOutlookCard(d, ctx),
      comingUpCard(d),
      recentActivityCard(d)),
    h('div', { style: { marginTop: '14px' } }, recentOrdersCard(d, ctx)),

    // Everything the prior layout showed that the mockup doesn't call out
    // by name — preserved, not deleted.
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr 1fr', gap: '14px', marginTop: '14px' } },
      salesToStockCard(d, ctx),
      customerPulseCard(d),
      topCategoriesCard(d)),
    h('div', { style: { marginTop: '14px' } }, businessRhythmCard(d)),
    h('div', { style: { marginTop: '14px' } }, messagesCard(d, ctx)));
}

function deltaPill(value) {
  if (value === null || value === undefined) return Pill('flat', 'n/a');
  if (value > 0.05) return Pill('good', h('span', {}, '▲ ', value.toFixed(1), '%'));
  if (value < -0.05) return Pill('warn', h('span', {}, '▼ ', Math.abs(value).toFixed(1), '%'));
  return Pill('flat', '≈ flat');
}

function deltaLine(value, against) {
  return h('span', {}, deltaPill(value), h('span', { class: 't-xs t-faint' }, ` ${against}`));
}

function statusSeverity(status) {
  if (status === 'Doing Well') return 'good';
  if (status === 'Worth Watching') return 'warn';
  return 'flat';
}

// =============================================================================
// Attention banner — mockup's "N matters need your attention" strip.
// =============================================================================

function attentionBanner(d, ctx, expanded = false, onToggle = null) {
  const items = d.needsAttention || [];
  if (items.length === 0) {
    return h('div', { class: 'callout callout-info' },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: '10px' } },
        Pill('good', '✓'), h('strong', {}, 'Nothing needs your attention right now.')));
  }
  const shown = expanded ? items : items.slice(0, 2);
  return h('div', { class: 'callout callout-warn' },
    h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', marginBottom: '8px' } },
      h('strong', {}, `${items.length} matter${items.length === 1 ? '' : 's'} need${items.length === 1 ? 's' : ''} your attention`),
      items.length > 2
        ? Button({ variant: 'ghost', size: 'sm', onClick: () => onToggle && onToggle(!expanded) }, expanded ? 'Show less' : 'View All →')
        : null),
    h('div', {}, ...shown.map((a) => h('div', {
      class: 'mini-row', style: { cursor: a.route ? 'pointer' : 'default', border: 'none', padding: '4px 0' },
      onClick: a.route ? () => ctx.navigate(a.route) : undefined,
    },
    h('span', { class: 't-sm' }, '• ', a.headline),
    Pill(a.severity, a.kind.replace(/_/g, ' '))))));
}

// =============================================================================
// Sales trend — 12-week revenue trend (the only pre-aggregated series this
// payload carries; a true daily "last 7 days" series isn't computed here,
// so this is labeled for what it actually is rather than mislabeled).
// =============================================================================

function salesTrendCard(d, ctx) {
  const p = d.businessPulse;
  const sev = statusSeverity(p.status);
  const t = d.trend;
  return Card({},
    CardHeader({
      title: 'Sales Performance', desc: `This week ${p.label || ''}`,
      right: h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
        Pill(sev, p.status), deltaPill(p.deltas.revenue_pct)),
    }),
    t && t.weeks && t.weeks.length > 0
      ? LineChart({
        labels: t.weeks.map((w) => w.weekStart.slice(5)),
        data: t.weeks.map((w) => w.revenue),
        color: palette.amber, height: 170,
      })
      : EmptyState({ icon: '▤', title: 'Not enough history yet', tight: true }),
    h('p', { class: 't-xs t-faint', style: { margin: '10px 0 0' } }, p.interpretation),
    Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('salesoverview'), style: { marginTop: '10px' } }, 'View Performance →'));
}

// =============================================================================
// Owner Decisions — what's waiting on an Owner action, from the same
// attention feed plus the counts this payload already carries.
// =============================================================================

function ownerDecisionsCard(d, ctx) {
  const rows = [];
  if ((d.stockAtRisk.needRestock || 0) > 0) {
    rows.push({ text: `${d.stockAtRisk.needRestock} ingredient${d.stockAtRisk.needRestock === 1 ? '' : 's'} need${d.stockAtRisk.needRestock === 1 ? 's' : ''} purchasing decisions`, route: 'stockreplenishment', sev: 'crit' });
  }
  if ((d.stockAtRisk.expiringSoon || 0) > 0) {
    rows.push({ text: `${d.stockAtRisk.expiringSoon} ingredient${d.stockAtRisk.expiringSoon === 1 ? '' : 's'} expiring soon`, route: 'stockreplenishment', sev: 'warn' });
  }
  const openIns = (d.instructionsPreview || {}).openInstructions || 0;
  if (openIns > 0) {
    rows.push({ text: `${openIns} instruction${openIns === 1 ? '' : 's'} awaiting acknowledgment`, route: 'instructions', sev: 'warn' });
  }
  for (const a of d.needsAttention || []) {
    if (!rows.some((r) => r.text.includes(a.headline))) rows.push({ text: a.headline, route: a.route, sev: a.severity });
  }
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Owner Decisions' }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing waiting on you', tight: true })
      : h('div', {}, ...rows.slice(0, 5).map((r) => h('div', {
        class: 'mini-row', style: { cursor: r.route ? 'pointer' : 'default' },
        onClick: r.route ? () => ctx.navigate(r.route) : undefined,
      }, h('span', { class: 't-sm' }, r.text), Pill(r.sev, '→')))));
}

// =============================================================================
// Inventory Outlook / Next 7 Days / Recent Activity — 3-col row.
// =============================================================================

function inventoryOutlookCard(d) {
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Inventory Outlook' }),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '10px' } },
      Stat({ value: count(d.stockAtRisk.needRestock), label: 'Need Restock' }),
      Stat({ value: count(d.stockAtRisk.expiringSoon), label: 'Expiring Soon' }),
      Stat({ value: count((d.needsAttention || []).length), label: 'Priority Risks' })));
}

function comingUpCard(d) {
  const f = d.comingUp;
  if (!f.available) {
    return Card({ className: 'card-flex' }, CardHeader({ title: 'Next 7 Days' }), EmptyState({ icon: '▤', title: 'Not enough history yet', description: f.note, tight: true }));
  }
  const colors = f.days.map((x) => ({ Normal: palette.info, Moderate: palette.warn, High: palette.crit, Busiest: palette.crit }[x.level] || palette.info));
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Next 7 Days', desc: 'Expected business activity' }),
    BarChart({ labels: f.days.map((x) => x.weekday), data: f.days.map((x) => x.qty), colors, height: 100 }),
    f.note ? h('div', { class: 't-xs', style: { marginTop: '8px' } }, f.note) : null);
}

function recentActivityCard(d) {
  const rows = (d.recentOrders || []).slice(0, 5);
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Recent Activity' }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'No activity recorded this week yet', tight: true })
      : h('div', { class: 'scrollpane' }, ...rows.map((o) => h('div', {
        class: 'mini-row', style: { cursor: 'pointer' }, onClick: () => openOrderDetail(o.id),
      },
      h('span', {},
        h('div', {}, `New order ${o.orderNo || `#${o.id}`} ${o.status === 'served' ? 'completed' : o.status}`),
        h('div', { class: 'sub' }, ageFromNow(o.createdAt))),
      Pill(o.status === 'served' ? 'good' : o.status === 'void' ? 'crit' : 'info', o.status)))));
}

function recentOrdersCard(d, ctx) {
  const rows = d.recentOrders || [];
  return Card({},
    CardHeader({ title: 'Recent Orders', right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('orderhistory') }, 'View All →') }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'No orders recorded this week yet', tight: true })
      : h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, 'Order #'), h('th', {}, 'Time'), h('th', {}, 'Items'), h('th', {}, 'Total'), h('th', {}, 'Status'))),
        h('tbody', {}, ...rows.map((o) => h('tr', { style: { cursor: 'pointer' }, onClick: () => openOrderDetail(o.id) },
          h('td', {}, o.orderNo || `#${o.id}`),
          h('td', {}, stamp(o.createdAt)),
          h('td', {}, count(o.items)),
          h('td', {}, money(o.total)),
          h('td', {}, Pill(o.status === 'served' ? 'good' : o.status === 'void' ? 'crit' : 'info', o.status)))))));
}

// =============================================================================
// Preserved from the prior layout — not shown in the mockup, not deleted.
// =============================================================================

function salesToStockCard(d, ctx) {
  const s = d.salesToStock;
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Sales → Stock' }),
    !s.available
      ? EmptyState({ icon: '✓', title: 'Nothing at risk right now', tight: true })
      : h('div', {},
        h('div', { style: { fontWeight: '600', marginBottom: '4px' } }, s.ingredientName),
        h('p', { class: 't-xs t-faint', style: { marginBottom: '8px' } }, s.note),
        s.daysOfSupply !== null ? Pill(s.status === 'out' ? 'crit' : 'warn', `~${s.daysOfSupply.toFixed(1)} days remaining`) : null,
        Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('whattobuy'), style: { marginTop: '10px', display: 'block' } }, 'View Inventory →')));
}

function customerPulseCard(d) {
  const p = d.customerPulse;
  const row = (label, name, extra) => name
    ? h('div', { class: 'mini-row' }, h('span', {}, h('div', { class: 't-xs t-faint' }, label), h('div', {}, name)), extra)
    : null;
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Customer Pulse' }),
    p.mostOrdered.length === 0 && p.rising.length === 0 && p.slowing.length === 0 && !p.frequentPair
      ? EmptyState({ icon: '✓', title: 'Not enough orders this week yet', tight: true })
      : h('div', {},
        row('Most Ordered', p.mostOrdered[0] && p.mostOrdered[0].name, p.mostOrdered[0] && Pill('info', `${p.mostOrdered[0].orders} orders`)),
        row('Rising', p.rising[0] && p.rising[0].name, p.rising[0] && Pill('good', `+${p.rising[0].pct.toFixed(0)}%`)),
        row('Slowing', p.slowing[0] && p.slowing[0].name, p.slowing[0] && Pill('warn', `${p.slowing[0].pct.toFixed(0)}%`)),
        p.frequentPair
          ? h('div', { class: 'mini-row' }, h('span', {}, h('div', { class: 't-xs t-faint' }, 'Frequent Pair'), h('div', {}, `${p.frequentPair.itemA} + ${p.frequentPair.itemB}`)), null)
          : null));
}

function topCategoriesCard(d) {
  const rows = d.topCategories || [];
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Top Categories', desc: "This week's sales mix" }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'No sales recorded this week yet', tight: true })
      : h('div', {}, ...rows.map((c) => h('div', { style: { marginBottom: '10px' } },
        h('div', { class: 'mini-row', style: { border: 'none', padding: '0 0 4px' } },
          h('span', {}, c.category), h('span', { class: 't-xs t-faint' }, `${c.sharePct}%`)),
        h('div', { style: { height: '6px', borderRadius: '3px', background: 'var(--line-soft)', overflow: 'hidden' } },
          h('div', { style: { height: '100%', width: `${c.sharePct}%`, background: 'var(--amber)' } }))))));
}

function businessRhythmCard(d) {
  const r = d.businessRhythm;
  if (!r.available) {
    return Card({ className: 'card-flex' }, CardHeader({ title: 'Business Rhythm' }), EmptyState({ icon: '▤', title: 'Not enough order history yet', tight: true }));
  }
  const tiles = [];
  if (r.weekendRevenueSharePct !== null) {
    tiles.push({ title: 'Weekends Matter', body: `Fri–Sun generate ${r.weekendRevenueSharePct}% of weekly sales.` });
  }
  if (r.dinnerShareOfOrdersPct !== null) {
    tiles.push({ title: 'Dinner Leads', body: `${r.dinnerShareOfOrdersPct}% of orders occur from 5 PM onward.` });
  }
  if (r.peakHour !== null) {
    tiles.push({ title: 'Peak Hour', body: `Most orders are placed around ${r.peakHour}:00.` });
  }
  if (r.paydayDeltaPct !== null) {
    tiles.push({
      title: 'Payday Pattern',
      body: r.paydayDeltaPct > 0
        ? `Sales around payday periods run about ${r.paydayDeltaPct}% higher.`
        : `Sales around payday periods run about ${Math.abs(r.paydayDeltaPct)}% lower.`,
    });
  }
  return Card({},
    CardHeader({ title: 'Business Rhythm', desc: `Recurring patterns, last ${r.windowDays} days` }),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '10px' } },
      ...tiles.map((t) => h('div', { class: 'card', style: { padding: '10px 12px' } },
        h('div', { style: { fontWeight: '600', fontSize: '12.5px', marginBottom: '4px' } }, t.title),
        h('div', { class: 't-xs t-faint' }, t.body)))));
}

function messagesCard(d, ctx) {
  const m = d.instructionsPreview || { openInstructions: 0, items: [] };
  return Card({},
    CardHeader({
      title: 'Messages from Your Team', desc: 'From Admin to Owner, via Instructions',
      right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('instructions') }, 'View Messages →'),
    }),
    m.items.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing open', tight: true })
      : h('div', {}, ...m.items.map((i) => h('div', { class: 'mini-row' },
        h('span', {}, i.title), Pill(i.status === 'open' ? 'flat' : 'warn', i.status.replace('_', ' '))))));
}
