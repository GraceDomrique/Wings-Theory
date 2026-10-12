import { h } from '../../lib/dom.js';
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

  return h('div', { class: 'admin-today' },
    stale,
    storeStatusBanner(d, ctx),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1.3fr 1fr', gap: '14px', alignItems: 'start', marginTop: '14px' } },
      todaysSalesCard(d),
      inventoryPulseCard(d, ctx)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr 1fr', gap: '14px', alignItems: 'start', marginTop: '14px' } },
      doNowCard(d, ctx),
      next7DaysCard(d),
      quickActionsCard(d, ctx)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px', alignItems: 'start', marginTop: '14px' } },
      deliveriesCard(d, ctx),
      orderActivityCard(d)),
    h('div', { style: { marginTop: '14px' } }, recentOrdersCard(d)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px', alignItems: 'start', marginTop: '14px' } },
      unusualUsageCard(d, ctx),
      unusualStockDifferenceCard(d, ctx)),
    h('div', { style: { marginTop: '14px' } }, activityLogCard(d)));
}

function storeStatusBanner(d, ctx) {
  const s = d.storeStatus;
  const ok = s.operational;
  return Card({ className: 'store-status-banner', style: { borderLeft: `4px solid var(--${ok ? 'good' : 'crit'})` } },
    h('div', { class: 't-xs', style: { textTransform: 'uppercase', letterSpacing: '.04em', color: `var(--${ok ? 'good' : 'crit'})`, marginBottom: '4px' } },
      ok ? '● Store Status' : '● Needs attention'),
    h('div', { style: { fontSize: '20px', fontWeight: '700', marginBottom: '10px' } },
      ok ? 'All systems running' : `${s.needAttention} item${s.needAttention === 1 ? '' : 's'} need attention`),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: '10px' } },
      pillStat(s.needAttention, 'Need Attention', s.needAttention ? 'crit' : 'good', () => ctx.navigate('expiry')),
      pillStat(s.deliveriesToday, 'Deliveries Today', 'info', () => ctx.navigate('receiving')),
      pillStat(s.runningLow, 'Running Low', s.runningLow ? 'warn' : 'good', () => ctx.navigate('whattobuy')),
      pillStat(s.stockHealthy, 'Stock Healthy', 'good', () => ctx.navigate('onhand'))));
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
  return Card({},
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
  return Card({},
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

function doNowCard(d, ctx) {
  const items = d.doNow || [];
  return Card({ style: { gridColumn: 'span 2' } },
    CardHeader({ title: 'Do Now', desc: 'Ranked by what breaks first if ignored', right: items.length ? Pill('crit', `${items.length} open`) : Pill('good', 'all clear') }),
    items.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing needs you right now', tight: true })
      : h('div', { class: 'scrollpane' }, ...items.map((a, i) => h('div', { class: 'mini-row', style: { cursor: a.route ? 'pointer' : 'default' }, onClick: a.route ? () => ctx.navigate(a.route) : undefined },
        h('span', {}, h('span', { class: 't-xs t-faint' }, `${i + 1}. `), a.headline),
        Pill(a.severity, a.detail)))));
}

function next7DaysCard(d) {
  const f = d.next7Days;
  if (!f.available) {
    return Card({},
      CardHeader({ title: 'Next 7 Days' }),
      EmptyState({ icon: '▤', title: 'Not enough order history yet', description: f.note, tight: true }));
  }
  const colors = f.days.map((x) => ({ Normal: palette.info, Moderate: palette.warn, High: palette.crit, Busiest: palette.crit }[x.level] || palette.info));
  return Card({},
    CardHeader({ title: 'Next 7 Days', desc: 'Expected demand' }),
    BarChart({ labels: f.days.map((x) => x.weekday), data: f.days.map((x) => x.qty), colors, height: 110 }),
    f.note ? h('div', { class: 't-xs', style: { marginTop: '8px' } }, Pill('info', 'Forecast'), ' ', f.note) : null);
}

function quickActionsCard(d, ctx) {
  return Card({},
    CardHeader({ title: 'Quick Actions' }),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '8px' } },
      ...d.quickActions.map((a) => Button({ onClick: () => ctx.navigate(a.route), style: { width: '100%' } }, a.label))));
}

function deliveriesCard(d, ctx) {
  const rows = d.todaysDeliveries || [];
  return Card({},
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
  return Card({},
    CardHeader({ title: 'Order Activity', desc: "Today's orders by status" }),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: '10px' } },
      Stat({ value: count(a.completed), label: 'Completed' }),
      Stat({ value: count(a.preparing), label: 'In Progress' }),
      Stat({ value: count(a.voided), label: 'Voided' })));
}

function recentOrdersCard(d) {
  const rows = d.recentOrders || [];
  return Card({},
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
  return Card({},
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
  return Card({},
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
  return Card({},
    CardHeader({ title: 'Activity Log', desc: 'Recent actions' }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing recorded yet', tight: true })
      : h('div', { class: 'scrollpane' }, ...rows.map((e) => h('div', { class: 'mini-row' },
        h('span', {}, stamp(e.created_at), ' ', h('span', { class: 't-faint' }, e.user_id || 'system'), ' — ', e.action, ' ', e.entity_type, e.entity_id ? ` #${e.entity_id}` : ''),
        e.note ? h('span', { class: 't-xs t-faint' }, e.note) : null))));
}
