import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading } from '../../components/ui/Feedback.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { palette, destroyAllCharts, destroyChart } from '../../components/charts/base.js';
import { money, count, stamp, asText } from '../../lib/format.js';
import { getAdminSales } from '../../lib/api/sales.js';
import { getSales, getValuation } from '../../lib/api/reports.js';
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

const REPORT_PERIODS = [
  { value: 'today', label: 'Today' },
  { value: 'this_week', label: 'This Week' },
  { value: 'this_month', label: 'This Month' },
  { value: 'all_time', label: 'All Time' },
];

export async function render(ctx) {
  // 'today' is its own fixed state, never driven by the Historical
  // Performance period tabs below (spec: "Do not make the user choose
  // between them merely to understand the business" / "Maintain separate
  // state and date-window semantics for Today's Operations and Historical
  // Performance") — switching the historical section to This Week or This
  // Month must never make today's own figures disappear or change.
  const state = { period: 'today', trendMetric: 'revenue', reportPeriod: 'this_month' };
  const host = h('div');
  const todayHost = h('div');
  const reportHost = h('div');

  async function drawToday() {
    let t;
    try {
      t = await getAdminSales({ period: 'today' });
    } catch (e) {
      mount(todayHost, EmptyState({ icon: '⨯', title: "Could not load today's figures", description: e.message || String(e), tight: true }));
      return;
    }
    mount(todayHost, todaySnapshotCard(t));
  }

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
      h('div', { class: 'eyebrow', style: { margin: '18px 0 10px' } }, 'Historical Performance'),
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
      h('div', { style: { marginTop: '14px' } }, recentOrdersCard(d, ctx2)),
      h('div', { style: { marginTop: '14px' } }, reportHost));
    return host2;
  }

  // -------------------------------------------------------------------
  // Generate Report — 2026-10-15 integration (spec: "Connect Reports...
  // Provide a prominent Generate Report action"). Same two endpoints
  // Reports.js reads (getSales/getValuation) with the same date-range
  // semantics and CSV export, folded directly into this page instead of
  // requiring a separate navigation to see totals or download them. The
  // full Reports.js page stays reachable ('reports' route) for anyone who
  // wants it as a dedicated, uncluttered printable view — not removed,
  // just no longer the only way to generate a report.
  // -------------------------------------------------------------------
  async function drawReport() {
    mount(reportHost, Card({}, Loading('Loading report…')));
    const opts = state.reportPeriod === 'all_time' ? { all_time: true } : { preset: state.reportPeriod };
    const [sales, valuation] = await Promise.all([getSales(opts), getValuation()]);
    const range = sales.allTime ? 'all recorded history' : `${sales.startDate || '—'} to ${sales.endDate || '—'}`;

    const reportPicker = h('div', { class: 'action-strip' },
      ...REPORT_PERIODS.map((o) => h('button', {
        class: `action-chip${state.reportPeriod === o.value ? ' is-active' : ''}`,
        onClick: () => { state.reportPeriod = o.value; drawReport(); },
      }, o.label)));

    mount(reportHost, Card({},
      CardHeader({
        title: 'Generate Report',
        desc: `Covers ${range} · generated ${new Date().toLocaleString()} · voided orders excluded from every total`,
        right: Button({ size: 'sm', onClick: () => ctx.navigate('reports') }, 'Full report center →'),
      }),
      reportPicker,
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', gap: '14px', margin: '14px 0' } },
        Stat({ value: money(sales.revenue, '₱0'), label: 'Revenue' }),
        Stat({ value: count(sales.orders), label: 'Orders' }),
        Stat({ value: count(sales.covers), label: 'Covers' }),
        Stat({ value: sales.averageOrder === null ? null : money(sales.averageOrder), label: 'Avg. Order' })),
      h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' } },
        h('div', { class: 'eyebrow' }, 'Best sellers this period'),
        h('div', { style: { display: 'flex', gap: '8px' } },
          Button({ size: 'sm', variant: 'ghost', onClick: () => downloadSalesSummaryCsv(sales) }, '↓ Sales summary CSV'),
          Button({ size: 'sm', variant: 'ghost', onClick: () => downloadBestSellersCsv(sales) }, '↓ Best sellers CSV'))),
      bestSellersRanked(sales.byItem || []),
      valuation.available
        ? h('div', { class: 't-xs t-faint', style: { marginTop: '12px', paddingTop: '12px', borderTop: '1px solid var(--line-soft)' } },
          'Current inventory valuation: ', h('strong', {}, money(valuation.totalValue).textContent), ' · confirmed unit costs only, not tied to the period above')
        : null));
  }

  // Compact ranked rows with a proportional bar (same device as
  // salesMixCard's category bars below), not a generic full-width table —
  // spec: "compact ranked product rows rather than another generic
  // full-width table". Top 5 by default; "Show more" expands to 10.
  function bestSellersRanked(items) {
    if (items.length === 0) return EmptyState({ icon: '✓', title: 'No sales recorded for this period', tight: true });
    const maxQty = Math.max(...items.map((r) => r.qtySold || 0), 1);
    const visible = items.slice(0, state.bestSellersExpanded ? 10 : 5);
    const rows = visible.map((r, i) => h('div', { style: { padding: '7px 0' } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '3px' } },
        h('span', { class: 't-xs t-faint', style: { width: '16px', flexShrink: '0', fontWeight: '700' } }, `#${i + 1}`),
        h('span', { class: 't-sm t-strong', style: { flex: '1', minWidth: '0' } }, r.name),
        h('span', { class: 'mono t-xs t-faint' }, count(r.qtySold), ' sold'),
        h('span', { class: 'mono t-sm t-strong', style: { minWidth: '72px', textAlign: 'right' } }, money(r.revenue))),
      h('div', { style: { height: '5px', borderRadius: '3px', background: 'var(--line-soft)', overflow: 'hidden', marginLeft: '26px' } },
        h('div', { style: { height: '100%', width: `${((r.qtySold || 0) / maxQty) * 100}%`, background: 'var(--amber)' } }))));
    return h('div', {},
      ...rows,
      items.length > 5 ? h('div', { style: { marginTop: '8px', textAlign: 'center' } },
        Button({
          size: 'sm', variant: 'ghost',
          onClick: () => { state.bestSellersExpanded = !state.bestSellersExpanded; drawReport(); },
        }, state.bestSellersExpanded ? 'Show fewer' : `Show more (${Math.min(items.length, 10) - 5} more)`)) : null);
  }

  function downloadSalesSummaryCsv(sales) {
    const lines = [['metric', 'value'].join(','),
      `revenue,${asText.num(sales.revenue, '', 2)}`,
      `orders,${sales.orders ?? ''}`,
      `items_sold,${sales.items ?? ''}`,
      `avg_order,${asText.num(sales.averageOrder, '', 2)}`].join('\n');
    downloadCsv(lines, 'sales-summary');
  }

  function downloadBestSellersCsv(sales) {
    const head = ['rank', 'item', 'qty_sold', 'revenue'];
    const lines = [head.join(',')];
    (sales.byItem || []).forEach((r, i) => {
      lines.push([i + 1, r.name, asText.num(r.qtySold, '', 0), asText.num(r.revenue, '', 2)]
        .map((v) => `"${String(v).replace(/"/g, '""')}"`).join(','));
    });
    downloadCsv(lines.join('\n'), 'best-sellers');
  }

  function downloadCsv(csv, name) {
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const a = h('a', { href: URL.createObjectURL(blob), download: `wings-theory-${name}-${new Date().toISOString().slice(0, 10)}.csv` });
    document.body.appendChild(a);
    a.click();
    a.remove();
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
  drawReport();
  drawToday();
  return h('div', {},
    h('div', { class: 'eyebrow' }, "Today's Operations"),
    todayHost,
    host);
}

/** Today's Operations — a fixed snapshot, always 'today', regardless of
 * whatever window Historical Performance below is currently showing. Reuses
 * the same order-activity/recent-orders/exceptions data the historical
 * section already computes for its own 'today' case — no second
 * calculation, just a dedicated always-on fetch instead of one tied to
 * state.period. */
function todaySnapshotCard(t) {
  const s = t.salesSummary;
  const a = t.orderActivity;
  const freshness = t.asOf
    ? h('div', { class: 't-xs t-faint', style: { marginTop: '8px' } }, 'As of ', stamp(t.asOf))
    : null;
  return Card({ className: 'kpi-primary' },
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: '14px' } },
      Stat({ value: money(s.revenue, '₱0'), label: "Today's Revenue" }),
      Stat({ value: count(s.orders), label: 'Orders Today' }),
      Stat({ value: s.avgOrder === null ? null : money(s.avgOrder), label: 'Avg. Order' }),
      Stat({ value: count(a.preparing), label: 'Active Right Now' })),
    (t.exceptions || []).length > 0
      ? h('div', { class: 'callout callout-warn', style: { marginTop: '12px', marginBottom: '0' } },
        h('p', {}, h('strong', {}, `${t.exceptions.length} exception${t.exceptions.length === 1 ? '' : 's'} today: `),
          t.exceptions.map((e) => e.note).join(' · ')))
      : null,
    freshness);
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
