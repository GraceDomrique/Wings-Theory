import { h, mount, icon } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { LineChart } from '../../components/charts/LineChart.js';
import { palette } from '../../components/charts/base.js';
import {
  money, count, stamp, ageFromNow, greeting,
} from '../../lib/format.js';
import { getOwnerOverview } from '../../lib/api/dashboard.js';
import { openOrderDetail } from '../reports/orderDetailModal.js';
import { session } from '../../lib/api/client.js';

/**
 * Owner Overview — business-command-center redesign (2026-10-16):
 *
 * A dedicated photo hero (36-pcs/6-flavor wing platter) opens the page with
 * the decision-maker's four numbers — Today's Sales, Orders Today, Average
 * Spend per Order, and Sales Performance vs. last week — plus a direct
 * purchasing call-to-action when stock needs it, so the Owner's very first
 * look answers "how is my restaurant doing right now." Business Rhythm (the
 * most distinctive, least-used-to-be-buried section) moves up to sit
 * directly under the attention strip instead of at the bottom of a long
 * scroll. Everything below follows the same priority order the brief asks
 * for: sales & best sellers, then stock & purchasing, then decisions/risks,
 * then recent activity — nothing removed from the prior layout, only
 * reordered and relabeled into plainer restaurant language.
 *
 * One aggregation call (getOwnerOverview -> GET /console/dashboard/
 * owner-overview, wt/dashboard_overview.owner_overview()). Every number on
 * this page is read straight from that payload or derived from two of its
 * own fields with ordinary arithmetic (e.g. revenue / orders for average
 * spend) — nothing here fits its own model or invents a figure.
 */

// 12pcs-Wings-Rack.jpg (round 3 feedback: "use a better yummy looking wings
// with sauce") — glossy sauce-glazed wings with visible steam and a ranch
// dip, a stronger appetite-appeal hero shot than the 36pcs platter's flatter
// overhead product photo it replaces.
const OWNER_HERO_PHOTO = "url('/img/POSimg/Ala%20Carte/12pcs-Wings-Rack.jpg')";

const ICON = {
  revenue: '<path d="M3 3v18h18"/><path d="M7 15l4-6 4 3 5-8"/>',
  orders: '<path d="M4 19V5a2 2 0 012-2h9l5 5v11a2 2 0 01-2 2H6a2 2 0 01-2-2z"/><path d="M9 13h6M9 17h6M9 9h2"/>',
  stock: '<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  spend: '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/>',
  trend: '<path d="M22 17l-9-9-4 4-6-6"/><path d="M22 7v10h-10"/>',
};

// One icon + pastel tint per "needs your attention" kind — a quick visual
// anchor so the list reads at a glance instead of as a column of identical
// bullet dots. Falls back to a plain alert triangle + its own severity's
// tint for any kind not listed here, same defensive-default pattern the
// rest of this page already uses for missing data.
const ATTENTION_ICON = {
  expired: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
  restock: '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
  expiring: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
  blocked: '<circle cx="12" cy="12" r="9"/><path d="M4.9 4.9l14.2 14.2"/>',
  declining_sales: '<path d="M22 17l-9-9-4 4-6-6"/><path d="M22 7v10h-10"/>',
};
const ATTENTION_ICON_DEFAULT = '<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>';
const SEV_TINT = { crit: 'var(--crit-tint)', warn: 'var(--warn-tint)', good: 'var(--good-tint)', info: 'var(--info-tint)', flat: 'var(--line-soft)' };
const SEV_INK = { crit: 'var(--crit)', warn: 'var(--warn)', good: 'var(--good)', info: 'var(--info)', flat: 'var(--ink-soft)' };

// One icon + pastel tint per Business Rhythm insight, same idea — see
// businessRhythmCard below. Titles are the plain-English labels the brief
// asks for (Best Sales Days / Busiest Hours / Payday Sales Effect); the
// underlying calculations (weekendRevenueSharePct, peakHour, etc.) are
// unchanged — only the on-screen wording moved.
const RHYTHM_ICON = {
  'Best Sales Days': '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
  'Dinner Leads': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
  'Busiest Hours': '<path d="M3 3v18h18"/><path d="M7 15l4-6 4 3 5-8"/>',
  'Payday Sales Effect': '<path d="M3 3v18h18"/><path d="M7 15l4-6 4 3 5-8"/>',
};
const RHYTHM_TINT = ['var(--amber-tint)', 'var(--info-tint)', 'var(--good-tint)', 'var(--cocoa-tint)'];
const RHYTHM_INK = ['var(--amber-deep)', 'var(--info)', 'var(--good)', 'var(--cocoa-deep)'];

export async function render(ctx) {
  const d = await getOwnerOverview();
  // Defensive against an older/cached backend build that predates the
  // needRestock/expiringSoon counts this tile reads — same honest-missing
  // treatment as a null field, not a crash.
  if (!d.stockAtRisk) d.stockAtRisk = { needRestock: null, expiringSoon: null };

  const bannerHost = h('div');
  const drawBanner = (expanded) => mount(bannerHost, attentionBanner(d, ctx, expanded, drawBanner));
  drawBanner(false);

  return h('div', { class: 'owner-overview' },
    greetingHeader(d),
    heroBanner(d, ctx),
    bannerHost,

    // Business Rhythm, promoted: the brief's core complaint was that this
    // section — one of the Owner's most distinctive features — used to sit
    // at the very bottom of a long scroll. It now runs right under the
    // attention strip, before any other secondary section.
    h('div', { style: { marginTop: '14px' } }, businessRhythmCard(d)),

    // Sales & best sellers.
    h('div', { class: 'grid', style: { gridTemplateColumns: '1.6fr 1fr', gap: '14px', marginTop: '14px' } },
      salesTrendCard(d, ctx),
      ownerDecisionsCard(d, ctx)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr 1fr', gap: '14px', marginTop: '14px' } },
      topCategoriesCard(d),
      customerPulseCard(d),
      comingUpCard(d)),

    // Stock & purchasing.
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px', marginTop: '14px' } },
      stockPurchasingCard(d, ctx),
      salesToStockCard(d, ctx)),

    // Recent decisions & activity.
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px', marginTop: '14px' } },
      recentActivityCard(d),
      messagesCard(d, ctx)),
    h('div', { style: { marginTop: '14px' } }, recentOrdersCard(d, ctx)));
}

// =============================================================================
// Hero — the Owner's first-screen read: today's headline numbers over a
// wing platter photo, in the same .photo-hero treatment AdminToday's Store
// Status banner uses, with its own photo and its own (business-performance,
// not operational-status) set of four numbers, plus a direct purchasing
// action when stock actually needs one.
// =============================================================================

// Sits above the photo-hero card itself, not inside it — a plain-language
// hello before the business-performance numbers, in the console's own
// (non-photo) ink colors rather than the hero's white-on-photo treatment.
function greetingHeader(d) {
  const p = d.businessPulse;
  const name = session.user && session.user.displayName;
  return h('div', { style: { margin: '0 0 12px' } },
    h('div', { style: { fontSize: '22px', fontWeight: '700', fontFamily: 'var(--font-display)' } },
      `${greeting()}${name ? `, ${name}` : ''}`),
    h('div', { class: 't-soft', style: { fontSize: '14px', fontWeight: '500', marginTop: '2px' } },
      p.interpretation));
}

function heroBanner(d, ctx) {
  const t = d.todayRevenue;
  const p = d.businessPulse;
  const sev = statusSeverity(p.status);
  const needRestock = d.stockAtRisk.needRestock || 0;
  const avgSpend = t && t.orders ? money(t.revenue / t.orders, '—') : null;
  return Card({ className: 'owner-hero photo-hero', style: { backgroundImage: OWNER_HERO_PHOTO } },
    Pill(sev, statusLabel(p.status)),
    h('div', { class: 'grid store-status-stats', style: { gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: '10px', marginTop: '16px' } },
      heroStat(t ? money(t.revenue, '₱0').textContent : '—', "Today's Sales", 'good', ICON.revenue),
      heroStat(t ? count(t.orders).textContent : '—', 'Orders Today', 'info', ICON.orders, () => ctx.navigate('orderhistory')),
      heroStat(avgSpend ? avgSpend.textContent : '—', 'Avg. Spend / Order', 'flat', ICON.spend),
      heroStat(p.deltas.revenue_pct === null ? 'n/a' : `${p.deltas.revenue_pct > 0 ? '+' : ''}${p.deltas.revenue_pct.toFixed(1)}%`, 'Sales vs Last Week', sev, ICON.trend, () => ctx.navigate('salesoverview'))),
    needRestock > 0
      ? Button({
        variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('stockreplenishment'), style: { marginTop: '16px' },
      }, `Review What to Buy — ${needRestock} ingredient${needRestock === 1 ? '' : 's'} →`)
      : null);
}

function heroStat(value, label, severity, iconPath, onClick) {
  return h('div', {
    class: `stat-tile stat-tile-${severity}`,
    style: { cursor: onClick ? 'pointer' : 'default' },
    onClick,
  },
  h('span', { class: `kpi-icon kpi-icon-${severity}` }, icon(iconPath, 15)),
  h('div', {},
    h('div', { class: 'stat-tile-value' }, value),
    h('div', { class: 'stat-tile-label' }, label)));
}

// =============================================================================
// Stock & Purchasing — combines the old Inventory Outlook counts with a
// direct purchasing action, so the Owner doesn't have to leave Overview to
// learn what needs buying before deciding whether to act on it.
// =============================================================================

function stockPurchasingCard(d, ctx) {
  const needRestock = d.stockAtRisk.needRestock || 0;
  const expiringSoon = d.stockAtRisk.expiringSoon || 0;
  return Card({ className: 'card-flex' },
    CardHeader({
      title: 'Stock & Purchasing', desc: 'What needs buying, and what is expiring',
      right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('stockreplenishment') }, 'Review →'),
    }),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr 1fr', gap: '10px' } },
      Stat({ value: count(needRestock), label: 'Need Purchasing' }),
      Stat({ value: count(expiringSoon), label: 'Expiring Soon' }),
      Stat({ value: count((d.needsAttention || []).length), label: 'Priority Risks' })),
    needRestock > 0 || expiringSoon > 0
      ? Button({
        variant: 'primary', size: 'sm', onClick: () => ctx.navigate('stockreplenishment'), style: { marginTop: '12px' },
      }, 'Draft Purchase Order →')
      : h('div', { class: 't-xs t-faint', style: { marginTop: '12px' } }, 'Nothing needs purchasing right now.'));
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

// businessPulse.status is "flat" (not a word, just the fallback severity
// name) whenever there isn't enough last-week data to compare against yet
// — display-only relabel so the pill reads as a sentence instead of a
// severity keyword leaking onto the page.
function statusLabel(status) {
  return status === 'flat' ? 'Not enough data yet' : status;
}

// =============================================================================
// Attention banner — mockup's "N matters need your attention" strip.
// =============================================================================

function attentionBanner(d, ctx, expanded = false, onToggle = null) {
  const items = d.needsAttention || [];
  if (items.length === 0) {
    return h('div', { class: 'attn-card attn-clear' },
      h('div', { class: 'attn-icon', style: { background: 'var(--good-tint)', color: 'var(--good)' } }, icon('<path d="M20 6L9 17l-5-5"/>', 18)),
      h('strong', {}, 'Nothing needs your attention right now.'));
  }
  const shown = expanded ? items : items.slice(0, 3);
  return h('div', { class: 'attn-card' },
    h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', marginBottom: '10px' } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
        h('div', { class: 'attn-icon', style: { background: 'var(--amber-tint)', color: 'var(--amber-deep)' } },
          icon('<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>', 16)),
        h('strong', {}, `${items.length} matter${items.length === 1 ? '' : 's'} need${items.length === 1 ? 's' : ''} your attention`)),
      items.length > 3
        ? Button({ variant: 'ghost', size: 'sm', onClick: () => onToggle && onToggle(!expanded) }, expanded ? 'Show less' : 'View All →')
        : null),
    h('div', { class: 'attn-grid' }, ...shown.map((a) => h('div', {
      class: 'attn-item', style: { cursor: a.route ? 'pointer' : 'default' },
      onClick: a.route ? () => ctx.navigate(a.route) : undefined,
    },
    h('div', {
      class: 'attn-icon', style: { background: SEV_TINT[a.severity] || SEV_TINT.flat, color: SEV_INK[a.severity] || SEV_INK.flat },
    }, icon(ATTENTION_ICON[a.kind] || ATTENTION_ICON_DEFAULT, 16)),
    h('div', { style: { minWidth: 0 } },
      h('div', { class: 't-sm', style: { fontWeight: '600' } }, a.headline),
      h('div', { class: 't-xs t-faint' }, a.kind.replace(/_/g, ' ')))))));
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
        Pill(sev, statusLabel(p.status)), deltaPill(p.deltas.revenue_pct)),
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
    tiles.push({ title: 'Best Sales Days', body: `Fri–Sun generate ${r.weekendRevenueSharePct}% of weekly sales.` });
  }
  if (r.dinnerShareOfOrdersPct !== null) {
    tiles.push({ title: 'Dinner Leads', body: `${r.dinnerShareOfOrdersPct}% of orders occur from 5 PM onward.` });
  }
  if (r.peakHour !== null) {
    tiles.push({ title: 'Busiest Hours', body: `Most orders are placed around ${r.peakHour}:00.` });
  }
  if (r.paydayDeltaPct !== null) {
    tiles.push({
      title: 'Payday Sales Effect',
      body: r.paydayDeltaPct > 0
        ? `Sales around payday periods run about ${r.paydayDeltaPct}% higher.`
        : `Sales around payday periods run about ${Math.abs(r.paydayDeltaPct)}% lower.`,
    });
  }
  return Card({},
    CardHeader({ title: 'Business Rhythm', desc: `Recurring patterns, last ${r.windowDays} days` }),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '12px' } },
      ...tiles.map((t, i) => h('div', {
        class: 'rhythm-tile', style: { background: RHYTHM_TINT[i % RHYTHM_TINT.length] },
      },
      h('div', { class: 'attn-icon', style: { background: 'rgba(255,255,255,0.6)', color: RHYTHM_INK[i % RHYTHM_INK.length] } },
        icon(RHYTHM_ICON[t.title] || ICON.revenue, 18)),
      h('div', { style: { minWidth: 0 } },
        h('div', { style: { fontWeight: '700', fontSize: '13px', color: RHYTHM_INK[i % RHYTHM_INK.length], marginBottom: '3px' } }, t.title),
        h('div', { class: 't-xs', style: { color: 'var(--ink-soft)', lineHeight: '1.4' } }, t.body))))));
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
