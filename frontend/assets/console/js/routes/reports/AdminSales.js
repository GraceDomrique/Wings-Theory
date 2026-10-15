import { h, mount, icon } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Drawer } from '../../components/ui/Drawer.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading } from '../../components/ui/Feedback.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { palette, destroyAllCharts, destroyChart } from '../../components/charts/base.js';
import { money, count, stamp, asText } from '../../lib/format.js';
import { ICONS } from '../../lib/icons.js';
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
  const state = {
    period: 'today', trendMetric: 'revenue', reportPeriod: 'this_month', sellersSort: 'qty',
  };
  const host = h('div');
  const todayHost = h('div');

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

  // 2026-10-15 rebuild (spec C: "Reduce scrolling dramatically" — the exact
  // 5-row composition asked for, not a lighter pass over the old one):
  //   Row 1 Today's Operations: a compact strip (todayHost, above) instead
  //     of a big card — handled outside content() since it's independent of
  //     the historical period.
  //   Row 2 Historical Performance: summaryCard, unchanged content.
  //   Row 3 Analytics: Sales Trend (65%) beside Best Sellers (35%) — Best
  //     Sellers moved here from a buried "Generate Report" section, ranked
  //     and sortable by qty or revenue, using the SAME d.whatsSelling the
  //     old 3-up row's small "What's Selling" list read (no new fetch).
  //   Row 4 Secondary Analytics: Sales Mix beside Busy Times.
  //   Row 5 Operations: Recent Orders capped at 5 rows (was up to 30) beside
  //     Sales -> Stock + Operational Exceptions stacked.
  // Order Activity's own card is gone — "Active Orders" already lives in
  // the Today's Operations strip, and Completed/Voided duplicated what
  // Recent Orders' own Status column already shows per row.
  function content(d, ctx2) {
    const host2 = h('div');
    mount(host2,
      h('div', { class: 'eyebrow', style: { margin: '18px 0 10px' } }, 'Historical Performance'),
      summaryCard(d, periodTabs()),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1.65fr 1fr', gap: '14px', marginTop: '14px', alignItems: 'start' } },
        trendCard(d),
        bestSellersAnalyticsCard(d)),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px', marginTop: '14px', alignItems: 'start' } },
        salesMixCard(d),
        busyTimesCard(d)),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1.4fr 1fr', gap: '14px', marginTop: '14px', alignItems: 'start' } },
        recentOrdersCard(d, ctx2),
        h('div', {},
          salesToStockCard(d, ctx2),
          h('div', { style: { marginTop: '14px' } }, exceptionsCard(d)))));
    return host2;
  }

  // Row 3's ranked Best Sellers — same compact-bar device as the report
  // drawer's own list (bestSellersRanked below), reused on real data this
  // row already had (d.whatsSelling), with a Qty/Revenue sort toggle (spec:
  // "Support ranking by Quantity Sold or Revenue, with the correct ranking
  // and bar calculations").
  function bestSellersAnalyticsCard(d) {
    const sortBtns = h('div', { style: { display: 'flex', gap: '4px' } },
      Button({ variant: state.sellersSort !== 'revenue' ? 'primary' : 'ghost', size: 'sm', onClick: () => { state.sellersSort = 'qty'; draw(); } }, 'Qty'),
      Button({ variant: state.sellersSort === 'revenue' ? 'primary' : 'ghost', size: 'sm', onClick: () => { state.sellersSort = 'revenue'; draw(); } }, 'Revenue'));
    const items = (d.whatsSelling || []).map((r) => ({ name: r.name, qtySold: r.qty, revenue: r.revenue }))
      .sort((a, b) => (state.sellersSort === 'revenue' ? b.revenue - a.revenue : b.qtySold - a.qtySold));
    return Card({},
      CardHeader({ title: 'Best Sellers', right: sortBtns }),
      bestSellersRanked(items, 'analytics'));
  }

  // -------------------------------------------------------------------
  // Generate Report — 2026-10-15 rebuild: now a drawer opened from the top
  // toolbar (spec D) instead of an always-expanded section at the bottom of
  // the page, which duplicated the Revenue/Orders/Avg-Order figures Rows 1
  // and 2 already show. Same two endpoints Reports.js reads (getSales/
  // getValuation), same CSV export — composition changed, not the data
  // source. The full Reports.js page stays reachable ("Full report
  // center") for anyone who wants the dedicated, uncluttered printable
  // view. Only CSV is a real, backend-generated export today; this drawer
  // says so rather than offering a PDF/Excel button that doesn't exist.
  // -------------------------------------------------------------------
  function openReportDrawer() {
    const bodyHost = h('div');
    const drawer = Drawer({
      title: 'Generate Report',
      desc: 'Sales summary and best sellers for a chosen period, exported as CSV',
      onClose: () => {},
    }, bodyHost);

    async function drawBody() {
      mount(bodyHost, Loading('Loading report…'));
      const opts = state.reportPeriod === 'all_time' ? { all_time: true } : { preset: state.reportPeriod };
      const [sales, valuation] = await Promise.all([getSales(opts), getValuation()]);
      const range = sales.allTime ? 'all recorded history' : `${sales.startDate || '—'} to ${sales.endDate || '—'}`;

      const reportPicker = h('div', { class: 'action-strip' },
        ...REPORT_PERIODS.map((o) => h('button', {
          class: `action-chip${state.reportPeriod === o.value ? ' is-active' : ''}`,
          onClick: () => { state.reportPeriod = o.value; drawBody(); },
        }, o.label)));

      mount(bodyHost,
        h('div', { class: 't-xs t-faint', style: { marginBottom: '12px' } },
          `Covers ${range} · generated ${new Date().toLocaleString()} · voided orders excluded from every total · CSV export only — no other format is currently generated by this system`),
        h('div', { class: 'eyebrow', style: { marginBottom: '6px' } }, 'Date range'),
        reportPicker,
        h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(2,1fr)', gap: '14px', margin: '16px 0' } },
          Stat({ value: money(sales.revenue, '₱0'), label: 'Revenue' }),
          Stat({ value: count(sales.orders), label: 'Orders' }),
          Stat({ value: count(sales.covers), label: 'Covers' }),
          Stat({ value: sales.averageOrder === null ? null : money(sales.averageOrder), label: 'Avg. Order' })),
        h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' } },
          h('div', { class: 'eyebrow' }, 'Best sellers this period'),
          h('div', { style: { display: 'flex', gap: '6px' } },
            Button({ size: 'sm', variant: 'ghost', onClick: () => downloadSalesSummaryCsv(sales) }, '↓ Summary'),
            Button({ size: 'sm', variant: 'ghost', onClick: () => downloadBestSellersCsv(sales) }, '↓ Sellers'))),
        bestSellersRanked(sales.byItem || [], 'report', drawBody),
        valuation.available
          ? h('div', { class: 't-xs t-faint', style: { marginTop: '14px', paddingTop: '12px', borderTop: '1px solid var(--line-soft)' } },
            'Current inventory valuation (now, not historical for the period above): ',
            h('strong', {}, money(valuation.totalValue).textContent), ' · confirmed unit costs only')
          : null,
        h('div', { style: { marginTop: '16px' } },
          Button({ variant: 'ghost', style: { width: '100%' }, onClick: () => { drawer.close(); ctx.navigate('reports'); } }, 'Open full report center →')));
    }
    drawBody();
  }

  // Compact ranked rows with a proportional bar (same device as
  // salesMixCard's category bars below), not a generic full-width table —
  // spec: "compact ranked product rows rather than another generic
  // full-width table". Top 5 by default; "Show more" expands to 10.
  function bestSellersRanked(items, scope, onToggle) {
    if (items.length === 0) return EmptyState({ icon: '✓', title: 'No sales recorded for this period', tight: true });
    const expandKey = `bestSellersExpanded_${scope}`;
    const maxQty = Math.max(...items.map((r) => r.qtySold || 0), 1);
    const visible = items.slice(0, state[expandKey] ? 10 : 5);
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
          onClick: () => { state[expandKey] = !state[expandKey]; (onToggle || draw)(); },
        }, state[expandKey] ? 'Show fewer' : `Show more (${Math.min(items.length, 10) - 5} more)`)) : null);
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
  drawToday();

  // Top toolbar (spec C/D): Generate Report and View All Orders both
  // prominent, before any of the page's own content — not a tiny link
  // buried after Recent Orders the way the old bottom section was.
  const toolbar = h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '10px', marginBottom: '14px', flexWrap: 'wrap' } },
    h('div', { class: 't-xs t-faint' }, 'Figures refresh on demand — use the reload button above to pull the latest.'),
    h('div', { style: { display: 'flex', gap: '8px' } },
      Button({ variant: 'ghost', onClick: () => ctx.navigate('orderhistory') }, icon(ICONS.history, 14), ' View All Orders'),
      Button({ variant: 'primary', onClick: openReportDrawer }, icon(ICONS.fileChart, 14), ' Generate Report')));

  return h('div', {},
    toolbar,
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
// 2026-10-15: a compact horizontal strip (same device as Menu Studio's
// .menu-health-strip) instead of a big kpi-primary card — spec explicitly:
// "Do not apply large pale beige backgrounds behind a few tiny numbers."
function todaySnapshotCard(t) {
  const s = t.salesSummary;
  const a = t.orderActivity;
  const hasExceptions = (t.exceptions || []).length > 0;
  return h('div', {},
    h('div', { class: 'menu-health-strip' },
      h('span', { class: 'mhs-item' }, h('b', {}, money(s.revenue, '₱0').textContent), ' today’s revenue'),
      h('span', { class: 'mhs-dot' }),
      h('span', { class: 'mhs-item' }, h('b', {}, count(s.orders).textContent), ' orders today'),
      h('span', { class: 'mhs-dot' }),
      h('span', { class: 'mhs-item' }, h('b', {}, s.avgOrder === null ? '—' : money(s.avgOrder).textContent), ' avg. order'),
      h('span', { class: 'mhs-dot' }),
      h('span', { class: `mhs-item ${a.preparing ? '' : 'mhs-good'}` }, h('b', {}, count(a.preparing).textContent), ' active orders'),
      t.asOf ? h('span', { class: 'mhs-dot' }) : null,
      t.asOf ? h('span', { class: 'mhs-item' }, 'as of ', stamp(t.asOf)) : null),
    hasExceptions
      ? h('div', { class: 'callout callout-warn', style: { marginTop: '8px', marginBottom: '0' } },
        h('p', {}, h('strong', {}, `${t.exceptions.length} exception${t.exceptions.length === 1 ? '' : 's'} today: `),
          t.exceptions.map((e) => e.note).join(' · ')))
      : null);
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

// 2026-10-15: capped at 5 rows (was up to 30) — spec: "Do not place 15-20
// recent orders on the main dashboard." Full history is one click away via
// the page's own top-toolbar "View All Orders" button and this card's own
// link, both pointing at the dedicated, server-paginated Order History page.
function recentOrdersCard(d, ctx) {
  const rows = (d.recentOrders || []).slice(0, 5);
  return Card({},
    CardHeader({ title: 'Recent Orders', desc: 'Most recent 5 — see Order History for the full record', right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('orderhistory') }, 'View All →') }),
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
