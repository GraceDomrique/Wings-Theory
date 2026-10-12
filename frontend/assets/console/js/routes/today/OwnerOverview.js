import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { palette } from '../../components/charts/base.js';
import { money, count, stamp, ageFromNow } from '../../lib/format.js';
import { getOwnerOverview } from '../../lib/api/dashboard.js';
import { openOrderDetail } from '../reports/orderDetailModal.js';

/**
 * Owner Overview — the reference-design business-at-a-glance landing screen
 * (2026-10). One aggregation call (getOwnerOverview -> GET /console/
 * dashboard/owner-overview, wt/dashboard_overview.owner_overview()) built
 * on the existing business_overview() period math, GET /dashboard/summary's
 * own alert list, the existing FP-Growth product_combinations(), and the
 * forward-depletion forecast — see that module's docstring for the full
 * composition. Nothing here fits its own model or invents a figure.
 */
export async function render(ctx) {
  const d = await getOwnerOverview();
  const stale = d.dataStaleHours !== null && d.dataStaleHours > 24
    ? h('div', { class: 'callout callout-warn', style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', flexWrap: 'wrap' } },
      h('p', { style: { margin: 0 } }, h('strong', {}, 'Order data is stale — '),
        'the most recent order on file is ', ageFromNow(d.latestOrderAt),
        '. The figures below reflect that gap, not necessarily a real change in the business.'),
      Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('salesoverview'), style: { flexShrink: 0 } }, 'Look at a longer period →'))
    : null;

  return h('div', { class: 'owner-overview' },
    stale,
    h('div', { class: 'grid', style: { gridTemplateColumns: '1.6fr 1fr', gap: '14px' } },
      businessPulseCard(d, ctx),
      needsAttentionCard(d, ctx)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr 1fr', gap: '14px', marginTop: '14px' } },
      comingUpCard(d),
      salesToStockCard(d, ctx),
      customerPulseCard(d)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px', marginTop: '14px' } },
      topCategoriesCard(d),
      businessRhythmCard(d)),
    h('div', { style: { marginTop: '14px' } }, recentOrdersCard(d, ctx)),
    h('div', { style: { marginTop: '14px' } }, messagesCard(d, ctx)));
}

function deltaPill(value) {
  if (value === null || value === undefined) return Pill('flat', 'n/a');
  if (value > 0.05) return Pill('good', h('span', {}, '▲ ', value.toFixed(1), '%'));
  if (value < -0.05) return Pill('warn', h('span', {}, '▼ ', Math.abs(value).toFixed(1), '%'));
  return Pill('flat', '≈ flat');
}

function statusSeverity(status) {
  if (status === 'Doing Well') return 'good';
  if (status === 'Worth Watching') return 'warn';
  return 'flat';
}

// Different shot from the sidebar's (landing-hero.jpg) and Admin's own hero
// (the dark studio rack shot) so Owner/Admin and sidebar/hero don't all
// repeat the same single photo across the shell.
const BUSINESS_PULSE_PHOTO = "url('/img/POSimg/Unlimited/Wings-Platter-36pcs-6flavors.jpg')";

function businessPulseCard(d, ctx) {
  const p = d.businessPulse;
  const sev = statusSeverity(p.status);
  return Card({ className: 'photo-hero', style: { backgroundImage: BUSINESS_PULSE_PHOTO } },
    h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '2px' } },
      h('div', { class: 't-xs', style: { textTransform: 'uppercase', letterSpacing: '.06em', color: '#D8D1C2', fontWeight: '700' } },
        `Business Pulse — this week ${p.label || ''}`),
      Pill(sev, p.status)),
    h('div', { style: { fontSize: '32px', fontWeight: '700', margin: '6px 0 2px', fontFamily: 'var(--font-display)' } }, money(p.revenue, '₱0')),
    h('div', { style: { marginBottom: '8px' } }, deltaPill(p.deltas.revenue_pct)),
    h('p', { class: 't-xs t-faint', style: { marginBottom: '14px', maxWidth: '440px' } }, p.interpretation),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: '14px', maxWidth: '420px' } },
      Stat({ value: count(p.orders), label: 'Orders' }),
      Stat({ value: count(p.items), label: 'Items Sold' }),
      Stat({ value: p.avgOrder === null ? null : money(p.avgOrder), label: 'Average Order' })),
    Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('salesoverview'), style: { marginTop: '14px' } }, 'View Performance →'));
}

function needsAttentionCard(d, ctx) {
  const items = d.needsAttention || [];
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Needs Your Attention', right: items.length ? Pill('crit', items.length) : Pill('good', 'clear') }),
    items.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing urgent right now', tight: true })
      : h('div', { class: 'scrollpane' }, ...items.map((a) => h('div', {
        class: 'mini-row', style: { cursor: a.route ? 'pointer' : 'default' },
        onClick: a.route ? () => ctx.navigate(a.route) : undefined,
      },
      h('span', {}, h('div', {}, a.headline), h('div', { class: 'sub' }, a.detail)),
      Pill(a.severity, a.kind.replace(/_/g, ' '))))));
}

function comingUpCard(d) {
  const f = d.comingUp;
  if (!f.available) {
    return Card({ className: 'card-flex' }, CardHeader({ title: 'Coming Up' }), EmptyState({ icon: '▤', title: 'Not enough history yet', description: f.note, tight: true }));
  }
  const colors = f.days.map((x) => ({ Normal: palette.info, Moderate: palette.warn, High: palette.crit, Busiest: palette.crit }[x.level] || palette.info));
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Coming Up', desc: 'Expected business activity, next 7 days' }),
    BarChart({ labels: f.days.map((x) => x.weekday), data: f.days.map((x) => x.qty), colors, height: 100 }),
    f.note ? h('div', { class: 't-xs', style: { marginTop: '8px' } }, f.note) : null);
}

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
