import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading } from '../../components/ui/Feedback.js';
import { LineChart } from '../../components/charts/LineChart.js';
import { palette, destroyAllCharts } from '../../components/charts/base.js';
import { money, count, stamp } from '../../lib/format.js';
import { getOwnerSales } from '../../lib/api/sales.js';
import { openOrderDetail } from './orderDetailModal.js';

/**
 * Owner Sales — strategic business intelligence (2026-10, Phase 2). One
 * aggregation call per period/comparison selection (getOwnerSales ->
 * GET /console/sales/owner, wt/sales_overview.owner_sales()). Re-fetches
 * on every control change rather than filtering client-side — the backend
 * owns every date boundary (Manila business-day alignment, partial-period
 * elapsed-matching), exactly like every other date-windowed screen in this
 * console (see sales_overview.py's own module docstring).
 */
const PERIODS = [
  { value: 'this_week', label: 'This Week' },
  { value: 'this_month', label: 'This Month' },
  { value: 'this_year', label: 'This Year' },
  { value: 'custom', label: 'Custom' },
];
const COMPARISONS = [
  { value: 'previous_period', label: 'Previous equivalent period' },
  { value: 'last_year', label: 'Same period last year' },
  { value: 'custom', label: 'Custom range' },
];

export async function render(ctx) {
  const state = { period: 'this_week', comparison: 'previous_period', periodStart: '', periodEnd: '', comparisonStart: '', comparisonEnd: '' };
  const host = h('div');

  async function draw() {
    destroyAllCharts();
    mount(host, Loading('Loading sales…'));
    let d;
    try {
      d = await getOwnerSales({
        period: state.period, comparison: state.comparison,
        periodStart: state.periodStart || undefined, periodEnd: state.periodEnd || undefined,
        comparisonStart: state.comparisonStart || undefined, comparisonEnd: state.comparisonEnd || undefined,
      });
    } catch (e) {
      mount(host, EmptyState({ icon: '⨯', title: 'Could not load sales data', description: e.message || String(e) }));
      return;
    }
    mount(host, controls(), content(d, ctx));
  }

  function controls() {
    const periodSel = h('select', { onChange: (e) => { state.period = e.target.value; draw(); } },
      ...PERIODS.map((o) => h('option', { value: o.value, selected: o.value === state.period ? 'selected' : undefined }, o.label)));
    const comparisonSel = h('select', { onChange: (e) => { state.comparison = e.target.value; draw(); } },
      ...COMPARISONS.map((o) => h('option', { value: o.value, selected: o.value === state.comparison ? 'selected' : undefined }, o.label)));

    const customPeriod = state.period === 'custom' ? h('div', { style: { display: 'flex', gap: '6px' } },
      h('input', { type: 'date', value: state.periodStart, onChange: (e) => { state.periodStart = e.target.value; } }),
      h('input', { type: 'date', value: state.periodEnd, onChange: (e) => { state.periodEnd = e.target.value; } }),
      Button({ size: 'sm', onClick: () => draw() }, 'Apply')) : null;
    const customComparison = state.comparison === 'custom' ? h('div', { style: { display: 'flex', gap: '6px' } },
      h('input', { type: 'date', value: state.comparisonStart, onChange: (e) => { state.comparisonStart = e.target.value; } }),
      h('input', { type: 'date', value: state.comparisonEnd, onChange: (e) => { state.comparisonEnd = e.target.value; } }),
      Button({ size: 'sm', onClick: () => draw() }, 'Apply')) : null;

    return Card({ style: { marginBottom: '14px' } },
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(2, minmax(0,1fr))', gap: '14px' } },
        h('label', { class: 'field' }, h('div', { class: 't-xs t-faint' }, 'Period'), periodSel, customPeriod),
        h('label', { class: 'field' }, h('div', { class: 't-xs t-faint' }, 'Compare against'), comparisonSel, customComparison)));
  }

  await draw();
  return host;
}

function deltaPill(value, opts = {}) {
  const { invert = false } = opts;
  if (value === null || value === undefined) return Pill('flat', 'n/a');
  const good = invert ? value < -0.05 : value > 0.05;
  const bad = invert ? value > 0.05 : value < -0.05;
  if (good) return Pill('good', h('span', {}, '▲ ', Math.abs(value).toFixed(1), '%'));
  if (bad) return Pill('warn', h('span', {}, '▼ ', Math.abs(value).toFixed(1), '%'));
  return Pill('flat', '≈ flat');
}

function pulseSeverity(label) {
  if (label === 'Doing Well' || label === 'Customer Favorite') return 'good';
  if (label === 'Worth Watching' || label === 'Losing Momentum') return 'warn';
  return 'flat';
}

function content(d, ctx) {
  return h('div', {},
    howAreSalesDoingCard(d),
    h('div', { style: { margin: '14px 0' } }, mainChartCard(d)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px' } },
      whatChangedCard(d),
      whatsDrivingCard(d)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px', marginTop: '14px' } },
      customerFavoritesCard(d, ctx),
      categoryPerformanceCard(d)),
    h('div', { style: { marginTop: '14px' } }, heatmapCard(d)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px', marginTop: '14px' } },
      thenVsNowCard(d),
      salesHighlightsCard(d)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px', marginTop: '14px' } },
      businessRhythmCard(d),
      oftenOrderedTogetherCard(d)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px', marginTop: '14px' } },
      salesToStockCard(d, ctx),
      watchThisItemCard(d)),
    h('div', { style: { marginTop: '14px' } }, recentOrdersCard(d, ctx)));
}

function howAreSalesDoingCard(d) {
  const s = d.howAreSalesDoing;
  return Card({},
    CardHeader({
      title: 'How Are Sales Doing?', desc: `${d.period.label} ${d.comparison.label}`,
      right: h('div', { style: { display: 'flex', gap: '6px' } }, ...d.salesPulse.map((p) => Pill(pulseSeverity(p), p))),
    }),
    h('div', { style: { fontSize: '30px', fontWeight: '700', margin: '4px 0 2px' } }, money(s.revenue, '₱0')),
    h('div', { style: { marginBottom: '10px' } }, deltaPill(s.deltas.revenue_pct)),
    h('p', { class: 't-xs t-faint', style: { marginBottom: '14px' } }, s.interpretation),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: '14px' } },
      Stat({ value: count(s.orders), label: 'Orders' }),
      Stat({ value: count(s.items), label: 'Items Sold' }),
      Stat({ value: s.avgOrder === null ? null : money(s.avgOrder), label: 'Average Order' })));
}

function mainChartCard(d) {
  const chart = d.mainSalesChart;
  const cur = chart.current;
  const cmp = chart.comparison;
  const len = Math.max(cur.length, cmp.length);
  const labels = Array.from({ length: len }, (_, i) => `Day ${i + 1}`);
  const curData = Array.from({ length: len }, (_, i) => (cur[i] ? cur[i].revenue : null));
  const cmpData = Array.from({ length: len }, (_, i) => (cmp[i] ? cmp[i].revenue : null));
  return Card({},
    CardHeader({ title: 'Sales Trend', desc: `${chart.granularity === 'month' ? 'Monthly' : 'Daily'} revenue — current vs. comparison period` }),
    LineChart({
      labels, data: curData, color: palette.amber, height: 220,
      extraSeries: [{ label: 'Comparison', data: cmpData, color: palette.info, dashed: true }],
    }));
}

function whatChangedCard(d) {
  const w = d.whatChanged;
  const row = (label, cur, prior, pct, fmt) => h('div', { class: 'mini-row' },
    h('span', {}, label, h('div', { class: 'sub' }, fmt(prior), ' → ', fmt(cur))),
    deltaPill(pct));
  return Card({},
    CardHeader({ title: 'What Changed' }),
    row('Revenue', w.revenue.current, w.revenue.prior, w.revenue.pct, (v) => `₱${(v ?? 0).toLocaleString()}`),
    row('Orders', w.orders.current, w.orders.prior, w.orders.pct, (v) => String(v ?? 0)),
    row('Items per Order', w.itemsPerOrder.current, w.itemsPerOrder.prior, w.itemsPerOrder.pct, (v) => (v === null ? 'n/a' : v)),
    row('Average Order', w.avgOrder.current, w.avgOrder.prior, w.avgOrder.pct, (v) => (v === null ? 'n/a' : `₱${v}`)),
    h('p', { class: 't-xs t-faint', style: { marginTop: '10px' } }, w.interpretation));
}

function whatsDrivingCard(d) {
  const g = d.whatsDrivingSales;
  return Card({},
    CardHeader({ title: "What's Driving Sales" }),
    h('div', { class: 't-xs t-faint', style: { marginBottom: '4px' } }, 'Growing'),
    g.growing.length === 0 ? h('div', { class: 't-xs t-faint' }, 'Nothing notable') : g.growing.map((i) => h('div', { class: 'mini-row' },
      h('span', {}, i.name), Pill('good', `+₱${i.revenueDelta.toLocaleString()}`))),
    h('div', { class: 't-xs t-faint', style: { margin: '10px 0 4px' } }, 'Losing Momentum'),
    g.declining.length === 0 ? h('div', { class: 't-xs t-faint' }, 'Nothing notable') : g.declining.map((i) => h('div', { class: 'mini-row' },
      h('span', {}, i.name), Pill('warn', `₱${i.revenueDelta.toLocaleString()}`))));
}

function favRow(label, items, pctField) {
  if (!items || items.length === 0) return null;
  return h('div', { class: 'mini-row' },
    h('span', {}, h('div', { class: 't-xs t-faint' }, label), h('div', {}, items[0].name)),
    pctField && items[0][pctField] !== undefined ? Pill(items[0][pctField] > 0 ? 'good' : 'warn', `${items[0][pctField].toFixed(0)}%`) : Pill('info', count(items[0].qty)));
}

function customerFavoritesCard(d) {
  const f = d.customerFavorites;
  const rows = [
    favRow('Most Ordered', f.mostOrdered),
    favRow('Highest Sales', f.highestSales),
    favRow('Rising Fastest', f.risingFastest, 'pct'),
    favRow('Losing Momentum', f.losingMomentum, 'pct'),
    favRow('Consistent Favorite', f.consistentFavorite),
    favRow('New Momentum', f.newMomentum),
  ].filter(Boolean);
  return Card({},
    CardHeader({ title: 'Customer Favorites' }),
    rows.length === 0 ? EmptyState({ icon: '✓', title: 'Not enough orders in this period yet', tight: true }) : rows);
}

function categoryPerformanceCard(d) {
  const rows = d.categoryPerformance || [];
  return Card({},
    CardHeader({ title: 'Category Performance' }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'No sales recorded this period yet', tight: true })
      : rows.map((c) => h('div', { style: { marginBottom: '10px' } },
        h('div', { class: 'mini-row', style: { border: 'none', padding: '0 0 4px' } },
          h('span', {}, c.category), h('span', {}, h('span', { class: 't-xs t-faint' }, `${c.sharePct}%`), ' ', deltaPill(c.deltaPct))),
        h('div', { style: { height: '6px', borderRadius: '3px', background: 'var(--line-soft)', overflow: 'hidden' } },
          h('div', { style: { height: '100%', width: `${c.sharePct}%`, background: 'var(--amber)' } })))));
}

function heatmapCard(d) {
  const hm = d.dayTimeHeatmap;
  if (!hm.available) {
    return Card({}, CardHeader({ title: 'Day × Time Heatmap' }), EmptyState({ icon: '▤', title: 'Not enough order history yet', tight: true }));
  }
  const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const byCell = new Map(hm.cells.map((c) => [`${c.weekday}-${c.hour}`, c]));
  const max = Math.max(1, ...hm.cells.map((c) => c.orders));
  const hours = [8, 10, 12, 14, 16, 18, 20, 22];
  return Card({},
    CardHeader({ title: 'Day × Time Heatmap', desc: `Order density, last ${hm.windowDays} days` }),
    h('div', { style: { overflowX: 'auto' } },
      h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, ''), ...hours.map((hh) => h('th', { class: 't-xs' }, `${hh}:00`)))),
        h('tbody', {}, ...WEEKDAYS.map((wd, wi) => h('tr', {},
          h('td', { class: 't-xs' }, wd),
          ...hours.map((hh) => {
            const cell = byCell.get(`${wi}-${hh}`);
            const intensity = cell ? cell.orders / max : 0;
            return h('td', {
              title: cell ? `${cell.orders} orders, ₱${cell.revenue}` : '0 orders',
              style: { background: `rgba(230,126,34,${intensity.toFixed(2)})`, width: '28px', height: '20px' },
            });
          })))))));
}

function thenVsNowCard(d) {
  const t = d.thenVsNow;
  const row = (label, cur, prior, fmt) => h('div', { class: 'mini-row' },
    h('span', {}, label), h('span', {}, fmt(prior), ' → ', h('strong', {}, fmt(cur))));
  return Card({},
    CardHeader({ title: 'Then vs. Now', desc: t.comparisonLabel }),
    row('Revenue', t.current.revenue, t.prior.revenue, (v) => `₱${v.toLocaleString()}`),
    row('Orders', t.current.orders, t.prior.orders, (v) => String(v)),
    row('Items Sold', t.current.items, t.prior.items, (v) => String(v)),
    row('Avg Order', t.current.avg_order, t.prior.avg_order, (v) => (v === null ? 'n/a' : `₱${v}`)),
    t.yearOverYear ? h('div', { class: 't-xs', style: { marginTop: '10px', paddingTop: '10px', borderTop: '1px solid var(--line-soft)' } },
      Pill('info', 'YoY'), ` ${t.yearOverYear.label}: `, deltaPill(t.yearOverYear.deltaPct)) : null);
}

function salesHighlightsCard(d) {
  const h2 = d.salesHighlights;
  const line = (label, v, metric) => h('div', { class: 'mini-row' },
    h('span', {}, label), v ? h('span', {}, v.period || stamp(v.createdAt), ' — ', money(metric === 'total' ? v.total : v.revenue)) : Pill('flat', 'no data'));
  return Card({},
    CardHeader({ title: 'Sales Highlights' }),
    line('Best Day', h2.bestDay),
    line('Quietest Day', h2.quietestDay),
    line('Busiest Day (orders)', h2.busiestDay),
    line('Largest Order', h2.largestOrder, 'total'));
}

function businessRhythmCard(d) {
  const r = d.businessRhythm;
  if (!r.available) return Card({}, CardHeader({ title: 'Business Rhythm' }), EmptyState({ icon: '▤', title: 'Not enough order history yet', tight: true }));
  const tiles = [];
  if (r.weekendRevenueSharePct !== null) tiles.push(['Weekends Matter', `Fri–Sun generate ${r.weekendRevenueSharePct}% of weekly sales.`]);
  if (r.dinnerShareOfOrdersPct !== null) tiles.push(['Dinner Leads', `${r.dinnerShareOfOrdersPct}% of orders occur from 5 PM onward.`]);
  if (r.peakHour !== null) tiles.push(['Peak Hour', `Most orders are placed around ${r.peakHour}:00.`]);
  if (r.paydayDeltaPct !== null) tiles.push(['Payday Pattern', `Sales around payday periods run about ${Math.abs(r.paydayDeltaPct)}% ${r.paydayDeltaPct > 0 ? 'higher' : 'lower'}.`]);
  return Card({},
    CardHeader({ title: 'Business Rhythm', desc: `Last ${r.windowDays} days` }),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '10px' } },
      ...tiles.map(([title, body]) => h('div', { class: 'card', style: { padding: '10px 12px' } },
        h('div', { style: { fontWeight: '600', fontSize: '12.5px', marginBottom: '4px' } }, title),
        h('div', { class: 't-xs t-faint' }, body)))));
}

function oftenOrderedTogetherCard(d) {
  const o = d.oftenOrderedTogether;
  return Card({},
    CardHeader({ title: 'Often Ordered Together', desc: 'Product associations (FP-Growth)' }),
    !o.available || o.pairs.length === 0
      ? EmptyState({ icon: '✓', title: 'Not enough transactions yet for a reliable pattern', description: o.dataQualityNote, tight: true })
      : o.pairs.map((p) => h('div', { class: 'mini-row' },
        h('span', {}, `${p.item_a} + ${p.item_b}`),
        Pill('info', `${(p.confidence * 100).toFixed(0)}% confidence`))));
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

function watchThisItemCard(d) {
  const w = d.watchThisItem;
  return Card({},
    CardHeader({ title: 'Watch This Item' }),
    !w
      ? EmptyState({ icon: '✓', title: 'No item shows a sustained swing worth flagging', tight: true })
      : h('div', {},
        h('div', { style: { fontWeight: '600', marginBottom: '6px' } }, w.name),
        Pill(w.direction === 'up' ? 'good' : 'warn', `${w.direction === 'up' ? '▲' : '▼'} ${Math.abs(w.pct).toFixed(0)}%`),
        h('p', { class: 't-xs t-faint', style: { marginTop: '8px' } }, w.note)));
}

function recentOrdersCard(d, ctx) {
  const rows = d.recentOrders || [];
  return Card({},
    CardHeader({ title: 'Recent Orders', right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('orderhistory') }, 'View All →') }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'No orders recorded in this period', tight: true })
      : h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, 'Order #'), h('th', {}, 'Time'), h('th', {}, 'Items'), h('th', {}, 'Total'), h('th', {}, 'Status'))),
        h('tbody', {}, ...rows.map((o) => h('tr', { style: { cursor: 'pointer' }, onClick: () => openOrderDetail(o.id) },
          h('td', {}, o.orderNo || `#${o.id}`),
          h('td', {}, stamp(o.createdAt)),
          h('td', {}, count(o.items)),
          h('td', {}, money(o.total)),
          h('td', {}, Pill(o.status === 'served' ? 'good' : o.status === 'void' ? 'crit' : 'info', o.status)))))));
}
