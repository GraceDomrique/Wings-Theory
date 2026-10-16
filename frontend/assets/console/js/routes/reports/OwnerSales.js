import { h, mount, icon } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { StatTile } from '../../components/ui/StatTile.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading } from '../../components/ui/Feedback.js';
import { LineChart } from '../../components/charts/LineChart.js';
import { DoughnutChart } from '../../components/charts/DoughnutChart.js';
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

const ICON = {
  revenue: '<path d="M3 3v18h18"/><path d="M7 15l4-6 4 3 5-8"/>',
  orders: '<path d="M4 19V5a2 2 0 012-2h9l5 5v11a2 2 0 01-2 2H6a2 2 0 01-2-2z"/><path d="M9 13h6M9 17h6M9 9h2"/>',
  items: '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
  avg: '<circle cx="12" cy="12" r="9"/><path d="M12 7v10M9.5 9.5a2.5 2.5 0 012.5-1h.5a2 2 0 010 4h-1a2 2 0 000 4h.5a2.5 2.5 0 002.5-1"/>',
};

// Same icon + pastel-tile treatment as the Overview page's Business Rhythm
// card (OwnerOverview.js) — restaurant-plain-English titles, not the raw
// field names (Weekends Matter -> Best Sales Days, Peak Hour -> Busiest
// Hours, Payday Pattern -> Payday Sales Effect), kept consistent across both
// pages that render the same businessRhythm payload.
const RHYTHM_ICON = {
  'Best Sales Days': '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
  'Dinner Leads': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
  'Busiest Hours': '<path d="M3 3v18h18"/><path d="M7 15l4-6 4 3 5-8"/>',
  'Payday Sales Effect': '<path d="M3 3v18h18"/><path d="M7 15l4-6 4 3 5-8"/>',
};
const RHYTHM_TINT = ['var(--amber-tint)', 'var(--info-tint)', 'var(--good-tint)', 'var(--cocoa-tint)'];
const RHYTHM_INK = ['var(--amber-deep)', 'var(--info)', 'var(--good)', 'var(--cocoa-deep)'];

// 2026-10 relayout: the page used to read as ~14 equally-weighted cards in
// one long column — every insight competing for the same attention whether
// it was "how much did we make" or "what hour do most orders land in".
// What matters most for a same-glance read (revenue, the trend, what
// changed and why) now leads; the deeper, more analytical cuts (heatmap,
// then-vs-now, rhythm, pairings) are grouped under their own "Deeper
// analysis" divider below the fold — still one click away, in whatever
// order a reader opens this page, never removed.
function sectionDivider(title, desc) {
  return h('div', { style: { margin: '26px 0 14px', paddingTop: '14px', borderTop: '1px solid var(--line)' } },
    h('div', { class: 'eyebrow' }, title),
    desc ? h('div', { class: 't-xs t-faint', style: { marginTop: '2px' } }, desc) : null);
}

function content(d, ctx) {
  const s = d.howAreSalesDoing;
  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', gap: '14px', marginBottom: '14px' } },
      StatTile({ iconPath: ICON.revenue, severity: 'good', value: money(s.revenue, '₱0'), label: 'Total Revenue', sub: deltaPill(s.deltas.revenue_pct) }),
      StatTile({ iconPath: ICON.orders, severity: 'info', value: count(s.orders), label: 'Orders', sub: deltaPill(s.deltas.orders_pct) }),
      StatTile({ iconPath: ICON.items, severity: 'flat', value: count(s.items), label: 'Items Sold' }),
      StatTile({ iconPath: ICON.avg, severity: 'flat', value: s.avgOrder === null ? null : money(s.avgOrder), label: 'Average Order Value' })),
    howAreSalesDoingCard(d),
    h('div', { style: { margin: '14px 0' } }, mainChartCard(d)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px' } },
      whatChangedCard(d),
      whatsDrivingCard(d)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px', marginTop: '14px' } },
      customerFavoritesCard(d, ctx),
      categoryPerformanceCard(d)),
    h('div', { style: { marginTop: '14px' } }, salesToStockCard(d, ctx)),

    sectionDivider('Deeper analysis', 'Patterns worth knowing, not needed for a daily glance'),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px' } },
      thenVsNowCard(d),
      salesHighlightsCard(d)),
    h('div', { style: { marginTop: '14px' } }, heatmapCard(d)),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px', marginTop: '14px' } },
      businessRhythmCard(d),
      oftenOrderedTogetherCard(d)),
    h('div', { style: { marginTop: '14px' } }, watchThisItemCard(d)),
    h('div', { style: { marginTop: '14px' } }, recentOrdersCard(d, ctx)));
}

function howAreSalesDoingCard(d) {
  const s = d.howAreSalesDoing;
  return Card({},
    CardHeader({
      title: 'How Are Sales Doing?', desc: `${d.period.label} ${d.comparison.label}`,
      right: h('div', { style: { display: 'flex', gap: '6px' } }, ...d.salesPulse.map((p) => Pill(pulseSeverity(p), p))),
    }),
    h('p', { class: 't-sm t-soft' }, s.interpretation));
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
    CardHeader({ title: 'Sales by Category' }),
    rows.length > 0
      ? DoughnutChart({ labels: rows.map((c) => c.category), data: rows.map((c) => c.sharePct), height: 160 })
      : null,
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
  if (r.weekendRevenueSharePct !== null) tiles.push({ title: 'Best Sales Days', body: `Fri–Sun generate ${r.weekendRevenueSharePct}% of weekly sales.` });
  if (r.dinnerShareOfOrdersPct !== null) tiles.push({ title: 'Dinner Leads', body: `${r.dinnerShareOfOrdersPct}% of orders occur from 5 PM onward.` });
  if (r.peakHour !== null) tiles.push({ title: 'Busiest Hours', body: `Most orders are placed around ${r.peakHour}:00.` });
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
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '12px' } },
      ...tiles.map((t, i) => h('div', {
        class: 'rhythm-tile', style: { background: RHYTHM_TINT[i % RHYTHM_TINT.length] },
      },
      h('div', { class: 'attn-icon', style: { background: 'rgba(255,255,255,0.6)', color: RHYTHM_INK[i % RHYTHM_INK.length] } },
        icon(RHYTHM_ICON[t.title] || ICON.revenue, 18)),
      h('div', { style: { minWidth: 0 } },
        h('div', { style: { fontWeight: '700', fontSize: '13px', color: RHYTHM_INK[i % RHYTHM_INK.length], marginBottom: '3px' } }, t.title),
        h('div', { class: 't-xs', style: { color: 'var(--ink-soft)', lineHeight: '1.4' } }, t.body))))));
}

function oftenOrderedTogetherCard(d) {
  const o = d.oftenOrderedTogether;
  return Card({},
    CardHeader({ title: 'Often Ordered Together', desc: 'What customers tend to order in the same visit' }),
    !o.available || o.pairs.length === 0
      ? EmptyState({ icon: '✓', title: 'Not enough transactions yet for a reliable pattern', description: o.dataQualityNote, tight: true })
      : h('div', {},
        o.pairs.map((p) => h('div', { class: 'mini-row' },
          h('span', {}, `${p.item_a} + ${p.item_b}`),
          Pill('info', `seen together ${(p.confidence * 100).toFixed(0)}% of the time`))),
        // Technical details collapsed, same pattern as Sales & History >
        // Product Combinations — the business-readable % is the headline,
        // FP-Growth/confidence wording is one click away, not front and
        // center (2026-10, "remove model accuracy from the owner's view").
        h('div', { class: 'accordion', style: { marginTop: '10px' } },
          h('details', {},
            h('summary', { class: 't-xs t-soft' }, 'Technical details'),
            h('div', { class: 't-xs t-faint', style: { marginTop: '8px' } },
              'FP-Growth product-association mining. ',
              o.pairs.map((p) => `${p.item_a} + ${p.item_b}: confidence ${(p.confidence * 100).toFixed(0)}%`).join(' · '))))));
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
