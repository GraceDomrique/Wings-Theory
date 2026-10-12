import { h, icon } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { RailCard } from '../../components/ui/RailCard.js';
import { Stat } from '../../components/ui/Stat.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { palette } from '../../components/charts/base.js';
import { money, count, pct, stamp, ageFromNow } from '../../lib/format.js';
import { severityFromAnomaly } from '../../lib/severity.js';
import { getAdminToday } from '../../lib/api/dashboard.js';
import { openOrderDetail } from '../reports/orderDetailModal.js';

/**
 * Admin Today — the reference-design operational landing screen (2026-10).
 *
 * One aggregation call (getAdminToday -> GET /console/dashboard/admin-today),
 * same discipline as the original five-KPI grid this route still renders
 * below (see Today.js) -- no independent per-card fetch, no client-side
 * recomputation of anything the backend already decided.
 *
 * Every section here reads real rows from the live database. Where this
 * restaurant's schema genuinely has no "Cancelled" order status distinct
 * from "Voided" (see wt/dashboard_overview.py's _order_activity docstring),
 * Order Activity shows the three statuses that actually exist rather than
 * inventing a fourth bucket to match a generic reference layout.
 */
export async function render(ctx) {
  const d = await getAdminToday();
  const stale = d.freshness && d.freshness.latestOrderAt
    ? h('div', { class: 'callout', style: { marginBottom: '14px' } },
      h('p', { class: 't-xs t-faint' },
        'Figures below reflect orders up to ', stamp(d.freshness.latestOrderAt),
        ' (', ageFromNow(d.freshness.latestOrderAt), ').'))
    : null;

  // Layout (2026-10, matched to the reference design): Store Status beside
  // Today's Sales up top; Do Now, Next 7 Days and Inventory Pulse in one
  // three-up row; Today's Deliveries, Quick Actions, Order Activity and
  // Recent Orders in one four-up row (Recent Orders widest, since it has
  // the most to show); Unusual Usage, Unusual Stock Difference and
  // Activity Log in the final three-up row.
  return h('div', { class: 'admin-today' },
    stale,
    h('div', { class: 'grid', style: { gridTemplateColumns: '1.6fr 1fr', gap: '14px' } },
      storeStatusBanner(d, ctx),
      todaysSalesCard(d)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr 1fr', gap: '14px', marginTop: '14px' } },
      doNowCard(d, ctx),
      next7DaysCard(d),
      inventoryPulseCard(d, ctx)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '0.85fr 0.85fr 0.85fr 1.5fr', gap: '14px', marginTop: '14px' } },
      deliveriesCard(d, ctx),
      quickActionsCard(d, ctx),
      orderActivityCard(d),
      recentOrdersCard(d)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr 1fr', gap: '14px', marginTop: '14px' } },
      unusualUsageCard(d, ctx),
      unusualStockDifferenceCard(d, ctx),
      activityLogCard(d)));
}

// Dark, moody studio shot (2026-10) -- deliberately NOT the brighter,
// busier platter shots used elsewhere, since .photo-hero's gradient has to
// do less work to keep the overlaid white stats readable against it.
const STORE_STATUS_PHOTO = "url('/img/POSimg/Ala Carte/12pcs-Wings-Rack.jpg')";

// Simple line-icon paths for the stat tiles below (24x24 viewbox, matches
// icon()'s stroke treatment) -- distinct per stat so the dark tiles read at
// a glance without relying on color alone.
const STAT_ICON = {
  attention: '<path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/>',
  truck: '<path d="M1 3h15v13H1z"/><path d="M16 8h4l3 3v5h-7V8z"/><circle cx="5.5" cy="18.5" r="2.5"/><circle cx="18.5" cy="18.5" r="2.5"/>',
  box: '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
  check: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>',
};

function storeStatusBanner(d, ctx) {
  const s = d.storeStatus;
  const ok = s.operational;
  const sev = ok ? 'good' : 'crit';
  return Card({ className: 'store-status-banner photo-hero', style: { backgroundImage: STORE_STATUS_PHOTO } },
    Pill(sev, ok ? 'Store Status' : 'Needs attention'),
    h('div', { style: { fontSize: '26px', fontWeight: '700', margin: '8px 0 16px', fontFamily: 'var(--font-display)' } },
      ok ? 'All systems running' : `${s.needAttention} item${s.needAttention === 1 ? '' : 's'} need attention`),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: '10px' } },
      heroStat(s.needAttention, 'Need Attention', s.needAttention ? 'crit' : 'good', STAT_ICON.attention, () => ctx.navigate('expiry')),
      heroStat(s.deliveriesToday, 'Deliveries Today', 'info', STAT_ICON.truck, () => ctx.navigate('receiving')),
      heroStat(s.runningLow, 'Running Low', s.runningLow ? 'warn' : 'good', STAT_ICON.box, () => ctx.navigate('whattobuy')),
      heroStat(s.stockHealthy, 'Stock Healthy', 'good', STAT_ICON.check, () => ctx.navigate('onhand'))));
}

// Dark icon-badge tile for storeStatusBanner's row, sitting on the hero
// photo -- distinct from pillStat() below (an opaque white card used
// elsewhere on this page, off the photo) since the two need opposite
// contrast treatments.
function heroStat(value, label, severity, iconPath, onClick) {
  return h('div', {
    class: `stat-tile stat-tile-${severity}`,
    style: { cursor: onClick ? 'pointer' : 'default' },
    onClick,
  },
    h('span', { class: `kpi-icon kpi-icon-${severity}` }, icon(iconPath, 15)),
    h('div', {},
      h('div', { class: 'stat-tile-value' }, count(value)),
      h('div', { class: 'stat-tile-label' }, label)));
}

function pillStat(value, label, severity, onClick) {
  return h('div', {
    class: `card rail rail-${severity}`,
    style: { padding: '10px 12px', cursor: onClick ? 'pointer' : 'default' },
    onClick,
  },
    h('div', { style: { fontSize: '20px', fontWeight: '700' } }, count(value)),
    h('div', { class: 't-xs t-faint' }, label));
}

function deltaPill(value) {
  if (value === null || value === undefined) return Pill('flat', 'n/a');
  if (value > 0.05) return Pill('good', h('span', {}, '▲ ', value.toFixed(1), '%'));
  if (value < -0.05) return Pill('warn', h('span', {}, '▼ ', Math.abs(value).toFixed(1), '%'));
  return Pill('flat', '≈ flat');
}

function todaysSalesCard(d) {
  const t = d.todaySales;
  return Card({ className: 'card-flex' },
    CardHeader({ title: "Today's Sales", desc: t.comparisonLabel }),
    h('div', { style: { fontSize: '30px', fontWeight: '700', margin: '4px 0 2px' } }, money(t.revenue, '₱0')),
    h('div', { style: { marginBottom: '14px' } }, deltaPill(t.deltas.revenue_pct)),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: '14px' } },
      Stat({ value: count(t.orders), label: 'Orders' }),
      Stat({ value: count(t.items), label: 'Items Sold' }),
      Stat({ value: t.avgOrder === null ? null : money(t.avgOrder), label: 'Avg. Order' })));
}

function inventoryPulseCard(d, ctx) {
  const p = d.inventoryPulse;
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Inventory Pulse', right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('onhand') }, 'View Inventory →') }),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: '10px', marginBottom: '12px' } },
      pillStat(p.purchaseNeeded, 'Purchase Needed', p.purchaseNeeded ? 'crit' : 'good', () => ctx.navigate('whattobuy')),
      pillStat(p.runningLow, 'Running Low', p.runningLow ? 'warn' : 'good', () => ctx.navigate('whattobuy')),
      pillStat(p.expiringSoon, 'Expiring Soon', p.expiringSoon ? 'warn' : 'good', () => ctx.navigate('expiry')),
      pillStat(p.countsDue, 'Counts Due', p.countsDue ? 'info' : 'good', () => ctx.navigate('stockcounts'))),
    h('div', { class: 't-xs t-faint', style: { marginBottom: '6px' } }, 'Top Stock Risks'),
    p.topRisks.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing at risk right now', tight: true })
      : h('div', {}, ...p.topRisks.map((r) => h('div', { class: 'mini-row' },
        h('span', {}, r.name),
        r.daysOfSupply === null ? Pill('flat', 'no usage rate') : Pill(r.status === 'out' ? 'crit' : r.status === 'critical' ? 'crit' : 'warn', `${r.daysOfSupply.toFixed(1)}d left`)))));
}

// `a.detail` here is a full sentence ("still counted as on hand until it is
// written off"), not a status word -- rendering it as a filled Pill/chip
// (meant for short badges like "warn"/"3 open") produced an oversized,
// awkward red blob next to every row. A plain severity-colored line of text
// carries the same meaning without the chip treatment it was never sized for.
const SEV_TEXT_CLASS = { crit: 't-crit', warn: 't-warn', good: 't-good', info: 't-info' };

function doNowCard(d, ctx) {
  const items = d.doNow || [];
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Do Now', desc: 'Ranked by what breaks first if ignored', right: items.length ? Pill('crit', `${items.length} open`) : Pill('good', 'all clear') }),
    items.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing needs you right now', tight: true })
      : h('div', { class: 'scrollpane' }, ...items.map((a, i) => h('div', { class: 'mini-row', style: { cursor: a.route ? 'pointer' : 'default' }, onClick: a.route ? () => ctx.navigate(a.route) : undefined },
        h('span', {}, h('span', { class: 't-xs t-faint' }, `${i + 1}. `), a.headline),
        h('span', { class: `t-xs t-strong ${SEV_TEXT_CLASS[a.severity] || 't-soft'}`, style: { textAlign: 'right', flexShrink: 0, maxWidth: '48%' } }, a.detail)))));
}

function next7DaysCard(d) {
  const f = d.next7Days;
  if (!f.available) {
    return Card({ className: 'card-flex' },
      CardHeader({ title: 'Next 7 Days' }),
      EmptyState({ icon: '▤', title: 'Not enough order history yet', description: f.note, tight: true }));
  }
  const colors = f.days.map((x) => ({ Normal: palette.info, Moderate: palette.warn, High: palette.crit, Busiest: palette.crit }[x.level] || palette.info));
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Next 7 Days', desc: 'Expected demand' }),
    BarChart({ labels: f.days.map((x) => x.weekday), data: f.days.map((x) => x.qty), colors, height: 110 }),
    f.note ? h('div', { class: 't-xs', style: { marginTop: '8px' } }, Pill('info', 'Forecast'), ' ', f.note) : null);
}

// Display-only: pairs each quick action's stable backend `key` (see
// wt/dashboard_overview.py) with an icon + a tile color for visual variety —
// same "backend decides the data, this file only decides how it looks"
// discipline as lib/severity.js, just not generalized into that shared file
// since nothing else in the console needs an action-to-icon mapping.
const QUICK_ACTION_ICON = {
  receive: { sev: 'info', path: '<path d="M3 3h2l.4 2M7 13h10l3.6-8H5.4"/><circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/>' },
  waste: { sev: 'crit', path: '<path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/>' },
  stockcount: { sev: 'good', path: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>' },
  createpo: { sev: 'warn', path: '<path d="M6 2l1.5 5h9L18 2"/><path d="M3 7h18l-1.7 10.2A2 2 0 0117.3 19H6.7a2 2 0 01-2-1.8L3 7z"/><path d="M12 10v4M10 12h4"/>' },
};

function quickActionsCard(d, ctx) {
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Quick Actions' }),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(2, 1fr)', gap: '8px' } },
      ...d.quickActions.map((a) => {
        const spec = QUICK_ACTION_ICON[a.key] || { sev: 'flat', path: '<circle cx="12" cy="12" r="9"/>' };
        return h('button', {
          class: 'quick-action',
          onClick: () => ctx.navigate(a.route),
        },
        h('span', { class: `kpi-icon kpi-icon-${spec.sev}` }, icon(spec.path, 15)),
        h('span', {}, a.label));
      })));
}

function deliveriesCard(d, ctx) {
  const rows = d.todaysDeliveries || [];
  return Card({ className: 'card-flex' },
    CardHeader({ title: "Today's Deliveries", right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('receiving') }, 'View all →') }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing due in today', tight: true })
      : h('div', { class: 'scrollpane' }, ...rows.map((po) => h('div', { class: 'mini-row' },
        h('span', {},
          h('div', {}, po.poCode, ' — ', po.supplier || 'no supplier set'),
          h('div', { class: 'sub' }, po.itemsSummary || `${po.itemCount} item(s)`)),
        h('div', {},
          po.overdue ? Pill('crit', 'overdue') : Pill('info', po.status === 'partially_received' ? 'partial' : 'expected'),
          Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('receiving') }, 'Receive'))))));
}

function orderActivityCard(d) {
  const a = d.orderActivity;
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Order Activity', desc: "Today's orders by status" }),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: '10px' } },
      Stat({ value: count(a.completed), label: 'Completed' }),
      Stat({ value: count(a.preparing), label: 'In Progress' }),
      Stat({ value: count(a.voided), label: 'Voided' })));
}

function recentOrdersCard(d) {
  const rows = d.recentOrders || [];
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Recent Orders' }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'No orders yet today', tight: true })
      : h('table', { class: 'table' },
        h('thead', {}, h('tr', {}, h('th', {}, 'Order'), h('th', {}, 'Time'), h('th', {}, 'Items'), h('th', {}, 'Total'), h('th', {}, 'Status'))),
        h('tbody', {}, ...rows.map((o) => h('tr', { style: { cursor: 'pointer' }, onClick: () => openOrderDetail(o.id) },
          h('td', {}, o.orderNo || `#${o.id}`),
          h('td', {}, stamp(o.createdAt)),
          h('td', {}, count(o.items)),
          h('td', {}, money(o.total)),
          h('td', {}, Pill(o.status === 'served' ? 'good' : o.status === 'void' ? 'crit' : 'info', o.status)))))));
}

function unusualUsageCard(d, ctx) {
  const rows = d.unusualUsage || [];
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Unusual Usage', desc: 'Consumption anomaly detection', right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('losses') }, 'View all →') }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing unusual detected', tight: true })
      : h('div', { class: 'scrollpane' }, ...rows.map((r) => h('div', { class: 'mini-row' },
        h('span', {},
          h('div', {}, r.label || r.ingredient_name, ' — ', r.service_date || ''),
          h('div', { class: 'sub' }, r.why)),
        Pill(severityFromAnomaly(r.severity), `${r.variance_pct > 0 ? '+' : ''}${(r.variance_pct ?? 0).toFixed(0)}%`)))));
}

function unusualStockDifferenceCard(d, ctx) {
  const rows = d.unusualStockDifference || [];
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Unusual Stock Difference', desc: 'Stock-loss anomaly detection', right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('losses') }, 'View all →') }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing unusual detected', tight: true })
      : h('div', { class: 'scrollpane' }, ...rows.map((r) => h('div', { class: 'mini-row' },
        h('span', {},
          h('div', {}, r.ingredient_name),
          h('div', { class: 'sub' }, r.why)),
        Pill(severityFromAnomaly(r.severity), `${r.delta_base > 0 ? '+' : ''}${r.delta_base} ${r.base_unit}`)))));
}

function activityLogCard(d) {
  const rows = d.activityLog || [];
  return Card({ className: 'card-flex' },
    CardHeader({ title: 'Activity Log', desc: 'Recent actions' }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing recorded yet', tight: true })
      : h('div', { class: 'scrollpane' }, ...rows.map((e) => h('div', { class: 'mini-row' },
        h('span', {}, stamp(e.created_at), ' ', h('span', { class: 't-faint' }, e.user_id || 'system'), ' — ', e.action, ' ', e.entity_type, e.entity_id ? ` #${e.entity_id}` : ''),
        e.note ? h('span', { class: 't-xs t-faint' }, e.note) : null))));
}
