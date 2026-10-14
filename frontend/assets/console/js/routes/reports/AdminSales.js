import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading } from '../../components/ui/Feedback.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { palette, destroyAllCharts, destroyChart } from '../../components/charts/base.js';
import { money, count, stamp } from '../../lib/format.js';
import { getAdminSales } from '../../lib/api/sales.js';
import { openOrderDetail } from './orderDetailModal.js';

/**
 * Admin Sales — operational summary + orders (2026-10, Phase 2). Narrower
 * than Owner Sales on purpose (see wt/sales_overview.admin_sales()'s own
 * docstring): no year-over-year, no business-rhythm/payday panel, no
 * extensive FP-Growth section. Recent Orders is the largest section, per
 * spec — this is an orders screen first, a summary second.
 */
const PERIODS = [
  { value: 'today', label: 'Today' },
  { value: 'this_week', label: 'This Week' },
  { value: 'this_month', label: 'This Month' },
];

export async function render(ctx) {
  const state = { period: 'today', trendMetric: 'revenue' };
  const host = h('div');

  async function draw() {
    destroyAllCharts();
    mount(host, Loading('Loading sales…'));
    let d;
    try {
      d = await getAdminSales({ period: state.period });
    } catch (e) {
      mount(host, EmptyState({ icon: '⨯', title: 'Could not load sales data', description: e.message || String(e) }));
      return;
    }
    mount(host, content(d, ctx));
  }

  function periodTabs() {
    return h('div', { style: { display: 'flex', gap: '6px' } },
      ...PERIODS.map((o) => Button({
        variant: o.value === state.period ? 'primary' : 'default', size: 'sm',
        onClick: () => { state.period = o.value; draw(); },
      }, o.label)));
  }

  function content(d, ctx2) {
    const host2 = h('div');
    mount(host2,
      summaryCard(d, periodTabs()),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1.4fr 1fr', gap: '14px', marginTop: '14px', alignItems: 'start' } },
        trendCard(d),
        salesMixCard(d)),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr 1fr', gap: '14px', marginTop: '14px', alignItems: 'start' } },
        whatsSellingCard(d, ctx2),
        orderActivityCard(d),
        busyTimesCard(d)),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px', marginTop: '14px', alignItems: 'start' } },
        salesToStockCard(d, ctx2),
        exceptionsCard(d)),
      h('div', { style: { marginTop: '14px' } }, recentOrdersCard(d, ctx2)));
    return host2;
  }

  function trendCard(d) {
    const metricBtns = h('div', { style: { display: 'flex', gap: '4px' } },
      Button({ variant: state.trendMetric === 'revenue' ? 'primary' : 'ghost', size: 'sm', onClick: () => { state.trendMetric = 'revenue'; paint(); } }, 'Sales'),
      Button({ variant: state.trendMetric === 'orders' ? 'primary' : 'ghost', size: 'sm', onClick: () => { state.trendMetric = 'orders'; paint(); } }, 'Orders'));
    const chartHost = h('div');
    function paint() {
      destroyChart(chartHost.firstChild);
      const series = d.compactSalesTrend.current;
      mount(chartHost, BarChart({
        labels: series.map((p) => String(p.period).slice(5)),
        data: series.map((p) => p[state.trendMetric]),
        color: state.trendMetric === 'revenue' ? palette.amber : palette.info,
        height: 180,
      }));
    }
    paint();
    return Card({},
      CardHeader({ title: 'Sales Trend', right: metricBtns }),
      chartHost);
  }

  await draw();
  return host;
}

function deltaPill(value) {
  if (value === null || value === undefined) return Pill('flat', 'n/a');
  if (value > 0.05) return Pill('good', h('span', {}, '▲ ', Math.abs(value).toFixed(1), '%'));
  if (value < -0.05) return Pill('warn', h('span', {}, '▼ ', Math.abs(value).toFixed(1), '%'));
  return Pill('flat', '≈ flat');
}

function summaryCard(d, periodTabs) {
  const s = d.salesSummary;
  return Card({},
    CardHeader({ title: 'Sales Summary', desc: `${d.period.label} — ${s.comparisonLabel}`, right: periodTabs }),
    h('div', { style: { fontSize: '28px', fontWeight: '700', margin: '4px 0 2px' } }, money(s.revenue, '₱0')),
    h('div', { style: { marginBottom: '8px' } }, deltaPill(s.deltas.revenue_pct)),
    h('p', { class: 't-xs t-faint', style: { marginBottom: '12px' } }, s.interpretation),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: '14px' } },
      Stat({ value: count(s.orders), label: 'Orders' }),
      Stat({ value: count(s.items), label: 'Items Sold' }),
      Stat({ value: s.avgOrder === null ? null : money(s.avgOrder), label: 'Avg. Order' })));
}

function salesMixCard(d) {
  const rows = d.salesMix || [];
  return Card({},
    CardHeader({ title: 'Sales Mix' }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'No sales recorded yet', tight: true })
      : rows.slice(0, 6).map((c) => h('div', { style: { marginBottom: '8px' } },
        h('div', { class: 'mini-row', style: { border: 'none', padding: '0 0 3px' } },
          h('span', {}, c.category), h('span', { class: 't-xs t-faint' }, `${c.sharePct}%`)),
        h('div', { style: { height: '5px', borderRadius: '3px', background: 'var(--line-soft)', overflow: 'hidden' } },
          h('div', { style: { height: '100%', width: `${c.sharePct}%`, background: 'var(--amber)' } })))));
}

function whatsSellingCard(d, ctx) {
  const rows = d.whatsSelling || [];
  return Card({},
    CardHeader({
      title: "What's Selling",
      right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('bestsellers') }, 'View all →'),
    }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing sold yet', tight: true })
      : h('div', { class: 'scrollpane' }, ...rows.map((r) => h('div', { class: 'mini-row' },
        h('span', {}, r.name), Pill('info', `${r.qty} sold`)))));
}

function orderActivityCard(d) {
  const a = d.orderActivity;
  return Card({},
    CardHeader({ title: 'Order Activity' }),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: '8px' } },
      Stat({ value: count(a.completed), label: 'Completed' }),
      Stat({ value: count(a.preparing), label: 'In Progress' }),
      Stat({ value: count(a.voided), label: 'Voided' })));
}

function busyTimesCard(d) {
  const b = d.busyTimes;
  return Card({},
    CardHeader({ title: 'Busy Times' }),
    b.totalOrders === 0
      ? EmptyState({ icon: '✓', title: 'No orders yet', tight: true })
      : h('div', {},
        BarChart({
          labels: b.hours.filter((x) => x.hour % 3 === 0).map((x) => `${x.hour}:00`),
          data: b.hours.filter((x) => x.hour % 3 === 0).map((x) => x.orders),
          color: palette.info, height: 100,
        }),
        b.busiestHour !== null ? h('div', { class: 't-xs t-faint', style: { marginTop: '6px' } }, `Busiest around ${b.busiestHour}:00`) : null));
}

function salesToStockCard(d, ctx) {
  const rows = d.salesToStock || [];
  return Card({},
    CardHeader({ title: 'Sales → Stock', desc: 'Top-seller ingredients at risk, plus any critical shortage regardless of sales mix' }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'No top-seller ingredient is currently at risk', tight: true })
      : rows.map((r) => h('div', { class: 'mini-row', style: { cursor: 'pointer' }, onClick: () => ctx.navigate('whattobuy') },
        h('span', {},
          h('div', {}, r.linkedToTopSeller ? r.itemName : h('span', {}, r.ingredientName, ' ', Pill('flat', 'not a top seller'))),
          h('div', { class: 'sub' }, r.linkedToTopSeller ? `needs ${r.ingredientName}` : 'critical regardless of current sales mix')),
        r.daysOfSupply !== null ? Pill(r.status === 'out' ? 'crit' : 'warn', `${r.daysOfSupply.toFixed(1)}d left`) : Pill('flat', r.status))));
}

function exceptionsCard(d) {
  const rows = d.exceptions || [];
  return Card({},
    CardHeader({ title: 'Operational Exceptions' }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing unusual', tight: true })
      : rows.map((e) => h('div', { class: 'mini-row' }, h('span', {}, e.note), Pill('warn', e.kind.replace(/_/g, ' ')))));
}

function recentOrdersCard(d, ctx) {
  const rows = d.recentOrders || [];
  return Card({},
    CardHeader({ title: 'Recent Orders', desc: 'Largest section on this page, by design', right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('orderhistory') }, 'View All →') }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'No orders recorded yet', tight: true })
      : h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, 'Order #'), h('th', {}, 'Time'), h('th', {}, 'Items'), h('th', {}, 'Total'), h('th', {}, 'Status'))),
        h('tbody', {}, ...rows.map((o) => h('tr', { style: { cursor: 'pointer' }, onClick: () => openOrderDetail(o.id) },
          h('td', {}, o.orderNo || `#${o.id}`),
          h('td', {}, stamp(o.createdAt)),
          h('td', {}, count(o.items)),
          h('td', {}, money(o.total)),
          h('td', {}, Pill(o.status === 'served' ? 'good' : o.status === 'void' ? 'crit' : 'info', o.status)))))));
}
