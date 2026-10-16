import { h, mount, icon } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { StatTile } from '../../components/ui/StatTile.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Table } from '../../components/ui/Table.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading, toast } from '../../components/ui/Feedback.js';
import { Tabbar } from '../../components/ui/Tabbar.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { DoughnutChart } from '../../components/charts/DoughnutChart.js';
import { ICONS } from '../../lib/icons.js';
import { destroyChart } from '../../components/charts/base.js';
import {
  money, count, qty, date, stamp, nodata, asText,
} from '../../lib/format.js';
import { getOwnerSales } from '../../lib/api/sales.js';
import { getSales, getValuation } from '../../lib/api/reports.js';
import { listOrders } from '../../lib/api/orders.js';
import { listPurchaseOrders } from '../../lib/api/purchaseOrders.js';
import { getAnomalies, getIngredientVariance, getReconciliationHistory } from '../../lib/api/intelligence.js';
import { openOrderDetail } from '../reports/orderDetailModal.js';
import { session } from '../../lib/api/client.js';

/**
 * Owner > Reports & History (2026-10-13 Owner Business Command Center
 * rebuild). Four internal tabs over data that already exists elsewhere in
 * the console — this page is a reader, never a second calculation:
 *
 *   Historical Sales     — getOwnerSales() (sales_overview.py) for the
 *     headline stats/YoY/category/best-seller figures, plus getSales()
 *     (reports.sales_summary) purely for its real per-day rows, aggregated
 *     client-side into the monthly bar chart. Drill-down (month -> day ->
 *     order) reuses listOrders() + the shared openOrderDetail() modal —
 *     exactly the same reader Order History already uses, so a figure here
 *     can never disagree with that screen.
 *   Inventory & Purchasing — listPurchaseOrders() (same table shape as
 *     Purchasing.js's own history table) + getValuation(). The supplier
 *     delivery summary is counted client-side from each PO's own item
 *     rows (quantity vs receivedQtyBase) — nothing estimated.
 *   Losses & Consumption — getAnomalies() for the stock-loss trend/category
 *     breakdown, getIngredientVariance() for the count-variance flag count,
 *     and getReconciliationHistory() (same reader UnliSummary.js uses) for
 *     the Unlimited Wings reconciliation record.
 *   Downloads — the two real server-generated reports (purchasing-list,
 *     expiry — wt/reports_export.py, same download-trigger pattern as
 *     Stock & Replenishment's own buttons) plus four client-side CSV
 *     exports following Downloads.js's exact Blob/escaping pattern.
 *
 * Nothing on this page is fabricated: a figure the backend hasn't supplied
 * renders via nodata()/EmptyState, never a guessed number. No field in any
 * of these responses distinguishes simulated/imported from live-tracked
 * history, so this page does not claim that distinction either.
 */

const CURRENT_YEAR = new Date().getFullYear();
const YEARS = [CURRENT_YEAR, CURRENT_YEAR - 1];
const LOSS_WINDOWS = [30, 90, 365];

const ICON = {
  revenue: '<path d="M3 3v18h18"/><path d="M7 15l4-6 4 3 5-8"/>',
  orders: '<path d="M4 19V5a2 2 0 012-2h9l5 5v11a2 2 0 01-2 2H6a2 2 0 01-2-2z"/><path d="M9 13h6M9 17h6M9 9h2"/>',
  items: '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
  avg: '<circle cx="12" cy="12" r="9"/><path d="M12 7v10M9.5 9.5a2.5 2.5 0 012.5-1h.5a2 2 0 010 4h-1a2 2 0 000 4h.5a2.5 2.5 0 002.5-1"/>',
};

const TABS = [
  { id: 'sales', icon: ICON.revenue, label: 'Historical Sales' },
  { id: 'purchasing', icon: ICONS.package, label: 'Inventory & Purchasing' },
  { id: 'losses', icon: ICONS.alertTriangle, label: 'Losses & Consumption' },
  { id: 'downloads', icon: ICONS.download, label: 'Downloads' },
];

export async function render(ctx) {
  let activeTab = TABS.some((t) => t.id === ctx.tab) ? ctx.tab : 'sales';
  const body = h('div');
  const tabbar = Tabbar({
    items: TABS, activeId: activeTab,
    onChange: (id) => { activeTab = id; draw(); },
  });

  // Each tab used to re-fetch and rebuild its content from scratch on every
  // switch, even flipping back to one already open — that network
  // round-trip (two calls for Historical Sales alone) is what made
  // switching feel slow, not a CSS transition. Built once per tab per
  // visit to this page, then reused — switching back to an already-loaded
  // tab is instant, no refetch, no rebuild. destroyAllCharts() is NOT
  // called here on a cache hit (or before building a not-yet-cached tab) —
  // it tears down every live Chart.js instance globally, which would blank
  // out a chart on an already-cached tab the moment any other tab's
  // content is built. The router (AppShell/app.js) still calls it when
  // leaving this page entirely, so nothing leaks across routes.
  const tabCache = new Map();

  async function draw() {
    if (tabCache.has(activeTab)) {
      mount(body, tabCache.get(activeTab));
      return;
    }
    mount(body, Loading('Loading…'));
    try {
      const content = activeTab === 'sales' ? await salesTab()
        : activeTab === 'purchasing' ? await purchasingTab()
          : activeTab === 'losses' ? await lossesTab()
            : await downloadsTab();
      tabCache.set(activeTab, content);
      mount(body, content);
    } catch (e) {
      mount(body, EmptyState({ icon: '⨯', title: 'Could not load this tab', description: e.message || String(e) }));
    }
  }

  await draw();
  return h('div', {}, h('div', { style: { marginBottom: '12px' } }, tabbar), body);
}

// ===========================================================================
// Small shared helpers
// ===========================================================================

function todayIso() { return new Date().toISOString().slice(0, 10); }

function monthLabel(key) {
  const [y, m] = key.split('-').map(Number);
  const names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return `${names[m - 1]} ${y}`;
}

function deltaPill(value) {
  if (value === null || value === undefined) return Pill('flat', 'n/a');
  if (value > 0.05) return Pill('good', `▲ ${Math.abs(value).toFixed(1)}%`);
  if (value < -0.05) return Pill('warn', `▼ ${Math.abs(value).toFixed(1)}%`);
  return Pill('flat', '≈ flat');
}

function csvEscape(v) {
  return `"${String(v ?? '').replace(/"/g, '""')}"`;
}

/** Same client-side CSV Blob pattern as Downloads.js's downloadOrdersCsv —
 * reused rather than reimplemented. An empty result set gets a real "no
 * records" state instead of a broken/blank file. */
function downloadCsv(filename, headers, rows) {
  if (!rows || rows.length === 0) {
    toast('No records for this filter — nothing to download.', 'warn');
    return;
  }
  const lines = [headers.map(csvEscape).join(',')];
  for (const row of rows) lines.push(row.map(csvEscape).join(','));
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = h('a', { href: URL.createObjectURL(blob), download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Same server-report download-trigger pattern as Stock & Replenishment's
 * own downloadReport() (copied verbatim) — authenticates with the same
 * bearer token the rest of the console uses, since a plain <a href> request
 * would not carry it. */
async function downloadServerReport(path, params) {
  const qs = '?' + new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''),
  ).toString();
  const headers = {};
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  const res = await fetch(path + qs, { headers });
  if (!res.ok) {
    let msg = `Request failed (${res.status}).`;
    try {
      const body = await res.json();
      if (typeof body.message === 'string') msg = body.message;
      else if (typeof body.detail === 'string') msg = body.detail;
    } catch { /* a binary/empty error body has no JSON to read */ }
    throw new Error(msg);
  }
  const blob = await res.blob();
  const disposition = res.headers.get('Content-Disposition') || '';
  const match = /filename="([^"]+)"/.exec(disposition);
  const filename = match ? match[1] : 'report';
  const a = h('a', { href: URL.createObjectURL(blob), download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ===========================================================================
// Historical Sales
// ===========================================================================

function monthlyFromByDay(byDay) {
  const map = new Map();
  for (const d of byDay || []) {
    const key = String(d.day).slice(0, 7);
    if (!map.has(key)) map.set(key, { month: key, revenue: 0, orders: 0 });
    const m = map.get(key);
    if (d.revenue !== null && d.revenue !== undefined) m.revenue += d.revenue;
    if (d.orders !== null && d.orders !== undefined) m.orders += d.orders;
  }
  return [...map.values()].sort((a, b) => a.month.localeCompare(b.month));
}

async function salesTab() {
  const state = { mode: 'year', year: CURRENT_YEAR, customStart: '', customEnd: '' };
  const host = h('div');
  // Only this tab's own chart instances, not destroyAllCharts() — this
  // tab's content gets torn down and cached tabs (Inventory & Purchasing,
  // Losses & Consumption) can still be holding live Chart.js instances on
  // detached-but-not-destroyed canvases; destroying those out from under
  // them is the exact "this._fn is not a function" crash base.js's
  // destroyChart() doc comment describes.
  let chartNodes = [];

  function range() {
    if (state.mode === 'custom' && state.customStart && state.customEnd) {
      return { start: state.customStart, end: state.customEnd };
    }
    const y = state.year;
    const start = `${y}-01-01`;
    const end = y === CURRENT_YEAR ? todayIso() : `${y}-12-31`;
    return { start, end };
  }

  function controls() {
    const yearSelect = h('select', {
      onChange: (e) => { state.mode = 'year'; state.year = Number(e.target.value); drawMain(); },
    }, ...YEARS.map((y) => h('option', {
      value: y, selected: state.mode === 'year' && state.year === y ? 'selected' : undefined,
    }, String(y))));
    const customBtn = Button({
      size: 'sm', variant: state.mode === 'custom' ? 'primary' : 'ghost',
      onClick: () => { state.mode = 'custom'; drawMain(); },
    }, 'Custom range');
    const customInputs = state.mode === 'custom' ? h('div', { style: { display: 'flex', gap: '6px', alignItems: 'center' } },
      h('input', { type: 'date', value: state.customStart, onChange: (e) => { state.customStart = e.target.value; } }),
      h('input', { type: 'date', value: state.customEnd, onChange: (e) => { state.customEnd = e.target.value; } }),
      Button({ size: 'sm', onClick: () => drawMain() }, 'Apply')) : null;
    // Same compact pill-strip treatment as Owner Sales' Period/Compare bar
    // (app.css .period-filter-bar) — a full-width Card around one small
    // select used to leave a long stretch of empty white space beside it.
    return h('div', { class: 'period-filter-bar' },
      h('div', { class: 'period-filter-field' },
        h('span', { class: 'period-filter-label' }, icon(ICONS.calendarClock, 13), 'Year'),
        yearSelect),
      h('div', { class: 'period-filter-divider' }),
      h('div', { class: 'period-filter-field' }, customBtn, customInputs));
  }

  function statsCard(d) {
    const s = d.howAreSalesDoing;
    return h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: '14px', marginBottom: '14px' } },
      StatTile({ iconPath: ICON.revenue, severity: 'good', value: money(s.revenue), label: `Total Revenue ${d.period.label}` }),
      StatTile({ iconPath: ICON.orders, severity: 'info', value: count(s.orders), label: 'Total Orders' }),
      StatTile({ iconPath: ICON.items, severity: 'flat', value: count(s.items), label: 'Items Sold' }),
      StatTile({ iconPath: ICON.avg, severity: 'flat', value: s.avgOrder === null ? null : money(s.avgOrder), label: 'Average Order Value' }));
  }

  function monthlyCard(months, monthRow) {
    return Card({},
      CardHeader({ title: 'Monthly Revenue', desc: 'Click a month to see its days and orders' }),
      months.length === 0
        ? EmptyState({ icon: '✓', title: 'No sales recorded in this range yet', tight: true })
        : h('div', {},
          BarChart({ labels: months.map((m) => monthLabel(m.month)), data: months.map((m) => m.revenue) }),
          h('div', { style: { marginTop: '10px' } }, ...months.map(monthRow))));
  }

  function yoyCard(d) {
    const t = d.thenVsNow;
    if (!t) return Card({}, CardHeader({ title: 'Year-over-Year' }), EmptyState({ icon: '—', title: 'Not available for this range', tight: true }));
    const row = (label, cur, prior, fmt) => h('div', { class: 'mini-row' },
      h('span', {}, label), h('span', {}, fmt(prior), ' → ', h('strong', {}, fmt(cur))));
    return Card({},
      CardHeader({ title: 'Year-over-Year', desc: t.comparisonLabel }),
      row('Revenue', t.current.revenue, t.prior.revenue, (v) => asText.money(v)),
      row('Orders', t.current.orders, t.prior.orders, (v) => String(v)),
      row('Items Sold', t.current.items, t.prior.items, (v) => String(v)),
      t.yearOverYear ? h('div', { class: 't-xs', style: { marginTop: '10px', paddingTop: '10px', borderTop: '1px solid var(--line-soft)' } },
        Pill('info', 'YoY'), ` ${t.yearOverYear.label}: `, deltaPill(t.yearOverYear.deltaPct)) : null);
  }

  function categoryCard(d) {
    const rows = d.categoryPerformance || [];
    return Card({},
      CardHeader({ title: 'Category Revenue' }),
      rows.length === 0
        ? EmptyState({ icon: '✓', title: 'No sales recorded in this range yet', tight: true })
        : rows.map((c) => h('div', { style: { marginBottom: '10px' } },
          h('div', { class: 'mini-row', style: { border: 'none', padding: '0 0 4px' } },
            h('span', {}, c.category), h('span', {}, h('span', { class: 't-xs t-faint' }, `${c.sharePct}%`), ' ', deltaPill(c.deltaPct))),
          h('div', { style: { height: '6px', borderRadius: '3px', background: 'var(--line-soft)', overflow: 'hidden' } },
            h('div', { style: { height: '100%', width: `${c.sharePct}%`, background: 'var(--amber)' } })))));
  }

  function bestSellersCard(d) {
    const f = d.customerFavorites || {};
    const rows = [];
    if (f.mostOrdered && f.mostOrdered.length) rows.push(['Most Ordered', f.mostOrdered[0].name, count(f.mostOrdered[0].qty)]);
    if (f.highestSales && f.highestSales.length) rows.push(['Highest Sales', f.highestSales[0].name, money(f.highestSales[0].revenue)]);
    if (f.consistentFavorite && f.consistentFavorite.length) rows.push(['Consistent Favorite', f.consistentFavorite[0].name, count(f.consistentFavorite[0].qty)]);
    return Card({},
      CardHeader({ title: 'Best Sellers' }),
      rows.length === 0
        ? EmptyState({ icon: '✓', title: 'Not enough orders in this range yet', tight: true })
        : rows.map(([label, name, val]) => h('div', { class: 'mini-row' },
          h('span', {}, h('div', { class: 't-xs t-faint' }, label), h('div', {}, name)), h('span', {}, val))));
  }

  function content(d, s) {
    const months = monthlyFromByDay(s.byDay);
    const drillArea = h('div', { style: { marginTop: '14px' } });

    function monthRow(m) {
      return h('div', { class: 'mini-row', style: { cursor: 'pointer' }, onClick: () => showMonth(m.month) },
        h('span', {}, monthLabel(m.month)),
        h('span', {}, money(m.revenue), ' · ', count(m.orders), ' orders'));
    }

    async function showMonth(monthKey) {
      mount(drillArea, Loading('Loading month…'));
      const [y, mo] = monthKey.split('-').map(Number);
      const monthStart = `${monthKey}-01`;
      const lastDay = new Date(y, mo, 0).getDate();
      const today = todayIso();
      let monthEnd = `${monthKey}-${String(lastDay).padStart(2, '0')}`;
      if (monthEnd > today) monthEnd = today;

      let out;
      try {
        out = await listOrders({ status: 'all', limit: 5000, startDate: monthStart, endDate: monthEnd });
      } catch (e) {
        mount(drillArea, EmptyState({ icon: '⨯', title: 'Could not load orders for this month', description: e.message || String(e) }));
        return;
      }
      const orders = out.orders;
      const truncated = out.totalCount != null && out.totalCount > orders.length;
      const byDay = new Map();
      for (const o of orders) {
        const day = String(o.createdAt || '').slice(0, 10);
        if (!byDay.has(day)) byDay.set(day, { day, orders: [], revenue: 0 });
        const b = byDay.get(day);
        b.orders.push(o);
        b.revenue += (o.currentTotal ?? o.total ?? 0);
      }
      const days = [...byDay.values()].sort((a, b) => a.day.localeCompare(b.day));

      function dayList() {
        return Card({},
          CardHeader({
            title: `${monthLabel(monthKey)} — days`,
            desc: truncated
              ? `Showing the most recent ${orders.length.toLocaleString()} of ${out.totalCount.toLocaleString()} orders this month — narrow the range to see the rest.`
              : `${orders.length} order${orders.length === 1 ? '' : 's'}`,
          }),
          days.length === 0
            ? EmptyState({ icon: '✓', title: 'No orders recorded this month', tight: true })
            : h('div', {}, ...days.map((dd) => h('div', { class: 'mini-row', style: { cursor: 'pointer' }, onClick: () => showDay(dd) },
              h('span', {}, date(dd.day)),
              h('span', {}, money(dd.revenue), ' · ', count(dd.orders.length), ' orders')))));
      }

      function showDay(dd) {
        mount(drillArea, dayList(), h('div', { style: { marginTop: '14px' } }, Card({},
          CardHeader({ title: `Orders on ${dd.day}` }),
          Table({
            columns: [
              { key: 'orderNo', label: 'Order', render: (o) => h('span', { class: 'mono t-sm' }, o.orderNo || `#${o.id}`) },
              { key: 'when', label: 'Time', render: (o) => h('span', { class: 'mono t-xs' }, stamp(o.createdAt)) },
              { key: 'items', label: 'Items', render: (o) => h('span', { class: 't-xs t-soft' }, o.itemNames || `${o.lineCount} item${o.lineCount === 1 ? '' : 's'}`) },
              { key: 'total', label: 'Total', render: (o) => money(o.currentTotal ?? o.total) },
              { key: 'status', label: 'Status', render: (o) => (o.voidedAt ? Pill('crit', 'voided') : Pill('flat', o.status)) },
              { key: 'view', label: '', render: (o) => Button({ size: 'sm', variant: 'ghost', onClick: () => openOrderDetail(o.id) }, 'View') },
            ],
            rows: dd.orders,
          }))));
      }

      mount(drillArea, dayList());
    }

    return h('div', {},
      statsCard(d),
      h('div', { style: { margin: '14px 0' } }, monthlyCard(months, monthRow)),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px' } },
        yoyCard(d), categoryCard(d)),
      h('div', { style: { marginTop: '14px' } }, bestSellersCard(d)),
      drillArea);
  }

  async function drawMain() {
    for (const n of chartNodes) destroyChart(n);
    chartNodes = [];
    mount(host, Loading('Loading sales…'));
    const { start, end } = range();
    let ownerData;
    let salesData;
    try {
      [ownerData, salesData] = await Promise.all([
        getOwnerSales({
          period: 'custom', periodStart: start, periodEnd: end,
          comparison: state.mode === 'custom' ? 'previous_period' : 'last_year',
        }),
        getSales({ start_date: start, end_date: end }),
      ]);
    } catch (e) {
      mount(host, controls(), EmptyState({ icon: '⨯', title: 'Could not load sales data', description: e.message || String(e) }));
      return;
    }
    mount(host, controls(), content(ownerData, salesData));
    chartNodes = [...host.querySelectorAll('.chart-box')];
  }

  await drawMain();
  return host;
}

// ===========================================================================
// Inventory & Purchasing
// ===========================================================================

function statusLabel(status) {
  switch (status) {
    case 'draft': return ['flat', 'Draft'];
    case 'ordered': return ['warn', 'Ordered'];
    case 'partially_received': return ['warn', 'Partially received'];
    case 'received': return ['good', 'Received'];
    case 'cancelled': return ['flat', 'Cancelled'];
    default: return ['flat', status];
  }
}

/** Received vs. outstanding item-lines per supplier, counted straight from
 * each PO's own item rows (quantity vs receivedQtyBase) — no estimation.
 * Draft/cancelled POs are excluded: they were never actually placed with
 * a supplier, so "outstanding" would be misleading for them. */
function deriveSupplierSummary(all) {
  const bySupplier = new Map();
  for (const po of all) {
    if (po.status === 'draft' || po.status === 'cancelled') continue;
    const key = po.supplier || 'No supplier recorded';
    if (!bySupplier.has(key)) bySupplier.set(key, { supplier: key, received: 0, outstanding: 0 });
    const s = bySupplier.get(key);
    for (const it of po.items) {
      if ((it.receivedQtyBase || 0) >= it.quantity) s.received += 1;
      else s.outstanding += 1;
    }
  }
  return [...bySupplier.values()].sort((a, b) => a.supplier.localeCompare(b.supplier));
}

async function purchasingTab() {
  let pos;
  let valuation;
  try {
    [pos, valuation] = await Promise.all([listPurchaseOrders(), getValuation()]);
  } catch (e) {
    return EmptyState({ icon: '⨯', title: 'Could not load purchasing history', description: e.message || String(e) });
  }

  const all = pos.items;
  const closed = all.filter((po) => po.status === 'received' || po.status === 'cancelled');
  const supplierSummary = deriveSupplierSummary(all);

  let supplierQuery = '';
  let statusFilter = '';
  let fromDate = '';
  let toDate = '';
  const historyHost = h('div');

  function matches(po) {
    if (statusFilter && po.status !== statusFilter) return false;
    if (supplierQuery && !(po.supplier || '').toLowerCase().includes(supplierQuery.toLowerCase())) return false;
    const day = (po.updatedAt || po.createdAt || '').slice(0, 10);
    if (fromDate && day < fromDate) return false;
    if (toDate && day > toDate) return false;
    return true;
  }

  function drawHistory() {
    const rows = closed.filter(matches);
    mount(historyHost, Table({
      // Same column pattern as Purchasing.js's own history table.
      columns: [
        { key: 'id', label: 'PO', render: (po) => `PO-${po.id}` },
        { key: 'status', label: 'Status', render: (po) => { const [sev, label] = statusLabel(po.status); return Pill(sev, label); } },
        { key: 'supplier', label: 'Supplier', render: (po) => po.supplier || h('span', { class: 't-xs t-faint' }, '—') },
        {
          key: 'items', label: 'Items',
          render: (po) => h('span', { class: 't-xs t-soft' }, po.items.map((it) => `${it.ingredientName} × ${asText.num(it.quantity, '', 2)}`).join(', ')),
        },
        { key: 'created', label: 'Created', render: (po) => h('span', { class: 'mono t-xs' }, stamp(po.createdAt)) },
        { key: 'closed', label: 'Closed', render: (po) => h('span', { class: 'mono t-xs' }, stamp(po.updatedAt || po.createdAt)) },
      ],
      rows,
      empty: EmptyState({
        icon: '≡',
        title: closed.length ? 'No purchase orders match these filters' : 'No purchase history yet',
        description: closed.length ? 'Widen or clear the filters above.' : 'Purchase orders appear here once they are received or cancelled.',
      }),
    }));
  }

  const supplierInput = h('input', { type: 'text', placeholder: 'Filter by supplier…', onInput: (e) => { supplierQuery = e.target.value; drawHistory(); } });
  const statusPicker = h('select', { onChange: (e) => { statusFilter = e.target.value; drawHistory(); } },
    h('option', { value: '' }, 'All'),
    h('option', { value: 'received' }, 'Completed'),
    h('option', { value: 'cancelled' }, 'Cancelled'));
  const fromInput = h('input', { type: 'date', onChange: (e) => { fromDate = e.target.value; drawHistory(); } });
  const toInput = h('input', { type: 'date', onChange: (e) => { toDate = e.target.value; drawHistory(); } });
  const clearBtn = Button({
    size: 'sm', variant: 'ghost',
    onClick: () => {
      supplierQuery = ''; statusFilter = ''; fromDate = ''; toDate = '';
      supplierInput.value = ''; statusPicker.value = ''; fromInput.value = ''; toInput.value = '';
      drawHistory();
    },
  }, 'Clear filters');

  drawHistory();

  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: '14px', marginBottom: '14px' } },
      Card({}, Stat({ value: valuation.available ? money(valuation.totalValue) : null, label: 'Inventory value', missing: valuation.available ? undefined : valuation.reason })),
      Card({}, Stat({ value: valuation.available ? count(valuation.pricedItems) : null, label: 'Priced ingredients' })),
      Card({}, Stat({ value: count(closed.length), label: 'Completed purchase orders' }))),
    supplierSummary.length ? Card({ style: { marginBottom: '14px' } },
      CardHeader({ title: 'Supplier delivery summary', desc: "Received vs. outstanding item-lines per supplier, across every purchase order that was actually placed" }),
      ...supplierSummary.map((s) => h('div', { class: 'mini-row' },
        h('span', {}, s.supplier),
        h('span', {}, count(s.received), ' received · ', count(s.outstanding), ' outstanding')))) : null,
    Card({},
      CardHeader({ title: 'Purchase order history', desc: 'Received and cancelled purchase orders' }),
      h('div', { style: { display: 'flex', gap: '10px', marginBottom: '14px', flexWrap: 'wrap', alignItems: 'flex-end' } },
        h('label', { class: 'field', style: { minWidth: '160px' } }, 'Supplier', supplierInput),
        h('label', { class: 'field', style: { minWidth: '140px' } }, 'Status', statusPicker),
        h('label', { class: 'field' }, 'From', fromInput),
        h('label', { class: 'field' }, 'To', toInput),
        clearBtn),
      historyHost));
}

// ===========================================================================
// Losses & Consumption
// ===========================================================================

function bucketLossesByDate(stockLoss) {
  const byDay = new Map();
  for (const a of stockLoss) {
    const day = String(a.timestamp || '').slice(0, 10);
    byDay.set(day, (byDay.get(day) || 0) + 1);
  }
  return [...byDay.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([day, cnt]) => ({ label: day.slice(5), count: cnt }));
}

function humaniseCat(cat) {
  return cat.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

// Doughnut, not a plain count list — every entry here shares the same unit
// (a flagged-event count), unlike Top-5-by-quantity elsewhere on this page
// which deliberately stays a list because its rows mix base units (kg vs.
// pc vs. L) that a shared chart axis would misrepresent as comparable.
function categoryBreakdown(stockLoss) {
  const byCat = new Map();
  for (const a of stockLoss) {
    const key = a.category || 'uncategorized';
    byCat.set(key, (byCat.get(key) || 0) + 1);
  }
  const entries = [...byCat.entries()].sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return EmptyState({ icon: '✓', title: 'No flagged losses in this window', tight: true });
  return h('div', {},
    DoughnutChart({ labels: entries.map(([cat]) => humaniseCat(cat)), data: entries.map(([, cnt]) => cnt), height: 150 }),
    h('div', { style: { marginTop: '10px' } }, ...entries.map(([cat, cnt]) => h('div', { class: 'mini-row' }, h('span', {}, humaniseCat(cat)), Pill('flat', count(cnt))))));
}

async function lossesTab() {
  let windowDays = 90;
  const host = h('div');
  let chartNodes = [];

  function windowPicker() {
    const sel = h('select', { onChange: (e) => { windowDays = Number(e.target.value); draw(); } },
      ...LOSS_WINDOWS.map((d) => h('option', { value: d, selected: d === windowDays ? 'selected' : undefined }, `Last ${d} days`)));
    return h('label', { class: 'field' }, h('div', { class: 't-xs t-faint' }, 'Window'), sel);
  }

  function content(anomalies, variance, unliRows) {
    const stockLoss = anomalies.stockLoss;
    const byDate = bucketLossesByDate(stockLoss);
    const rows = Array.isArray(unliRows) ? unliRows : (unliRows.items || []);
    const withVariance = rows.filter((r) => r.variancePct !== undefined && r.variancePct !== null);
    const avgVariancePct = withVariance.length
      ? withVariance.reduce((sum, r) => sum + (r.variancePct || 0), 0) / withVariance.length
      : null;
    const flaggedVarianceCount = variance ? variance.items.filter((i) => i.flagged).length : null;

    return h('div', {},
      h('div', { style: { marginBottom: '14px' } }, windowPicker()),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: '14px', marginBottom: '14px' } },
        Card({}, Stat({ value: count(stockLoss.length), label: `Flagged loss events (last ${windowDays}d)` })),
        Card({}, Stat({ value: count(rows.length), label: 'Reconciled unli sessions' })),
        Card({}, Stat({ value: avgVariancePct === null ? null : `${avgVariancePct.toFixed(1)}%`, label: 'Avg. unli variance vs. estimate' }))),
      Card({ style: { marginBottom: '14px' } },
        CardHeader({ title: 'Loss trend', desc: `Flagged stock-loss events per day, last ${windowDays} days` }),
        BarChart({
          labels: byDate.map((b) => b.label), data: byDate.map((b) => b.count),
          empty: { icon: '✓', title: 'No flagged losses in this window', tight: true },
        })),
      Card({ style: { marginBottom: '14px' } },
        CardHeader({ title: 'Flagged losses by category' }),
        categoryBreakdown(stockLoss)),
      Card({ style: { marginBottom: '14px' } },
        CardHeader({ title: 'Ingredient count-variance', desc: `Recipe/wastage-expected consumption vs. physical counts, last ${windowDays} days` }),
        variance
          ? Stat({ value: count(flaggedVarianceCount), label: 'Ingredients flagged for count variance' })
          : EmptyState({ icon: '—', title: 'Not available for this window', tight: true })),
      Card({},
        CardHeader({ title: 'Unlimited Wings reconciliation history', desc: 'Estimated consumption vs. what the kitchen actually weighed out' }),
        Table({
          columns: [
            { key: 'when', label: 'When', render: (r) => h('span', { class: 'mono t-xs' }, stamp(r.serviceDate)) },
            { key: 'stream', label: 'Stream', render: (r) => r.stream || '—' },
            { key: 'estimated', label: 'Estimated', render: (r) => qty(r.theoreticalBase, r.baseUnit, '—') },
            { key: 'actual', label: 'Actual', render: (r) => qty(r.actualBase, r.baseUnit, '—') },
            { key: 'variance', label: 'Variance', render: (r) => (r.variancePct === null || r.variancePct === undefined ? nodata('—') : `${r.variancePct.toFixed(1)}%`) },
            { key: 'status', label: 'Status', render: (r) => r.status || '—' },
          ],
          rows,
          empty: EmptyState({ icon: '—', title: 'No reconciled sessions yet', description: 'Sessions appear here once Admin signs one off.', tight: true }),
        })));
  }

  async function draw() {
    for (const n of chartNodes) destroyChart(n);
    chartNodes = [];
    mount(host, Loading('Loading losses…'));
    let anomalies;
    let variance;
    let unli;
    try {
      [anomalies, variance, unli] = await Promise.all([
        getAnomalies(windowDays),
        getIngredientVariance(Math.min(windowDays, 90)).catch(() => null),
        getReconciliationHistory({ limit: 120, days: windowDays }).catch(() => []),
      ]);
    } catch (e) {
      mount(host, EmptyState({ icon: '⨯', title: 'Could not load losses data', description: e.message || String(e) }));
      return;
    }
    mount(host, content(anomalies, variance, unli));
    chartNodes = [...host.querySelectorAll('.chart-box')];
  }

  await draw();
  return host;
}

// ===========================================================================
// Downloads
// ===========================================================================

function formatDownloadRow(onDownload) {
  const formatSelect = h('select', {},
    h('option', { value: 'pdf' }, 'PDF'),
    h('option', { value: 'csv' }, 'CSV'),
    h('option', { value: 'xlsx' }, 'Excel (.xlsx)'));
  const btn = Button({
    size: 'sm',
    onClick: async () => {
      btn.disabled = true;
      try {
        await onDownload(formatSelect.value);
        toast('Report downloaded.', 'good');
      } catch (e) {
        toast(e.message || 'Could not download this report.', 'warn');
      } finally {
        btn.disabled = false;
      }
    },
  }, '↓ Download');
  return h('div', { style: { display: 'flex', gap: '10px', alignItems: 'center' } },
    h('div', { class: 'field', style: { minWidth: '120px' } }, formatSelect), btn);
}

function salesCsvControls() {
  const fromInput = h('input', { type: 'date' });
  const toInput = h('input', { type: 'date' });
  const btn = Button({
    size: 'sm',
    onClick: async () => {
      btn.disabled = true;
      try {
        const params = {};
        if (fromInput.value) params.start_date = fromInput.value;
        if (toInput.value) params.end_date = toInput.value;
        if (!fromInput.value && !toInput.value) params.all_time = true;
        const s = await getSales(params);
        const rows = (s.byDay || []).map((d) => [d.day, d.orders ?? '', d.revenue ?? '']);
        downloadCsv(`wings-theory-sales-summary-${todayIso()}.csv`, ['date', 'orders', 'revenue'], rows);
      } catch (e) {
        toast(e.message || 'Could not build this report.', 'warn');
      } finally {
        btn.disabled = false;
      }
    },
  }, '↓ Download CSV');
  return h('div', { style: { display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' } },
    h('label', { class: 'field' }, 'From (optional)', fromInput),
    h('label', { class: 'field' }, 'To (optional)', toInput),
    btn);
}

async function downloadPurchaseOrdersCsv() {
  const { items } = await listPurchaseOrders();
  const rows = [];
  for (const po of items) {
    if (po.items.length === 0) {
      rows.push([po.id, po.status, po.supplier || '', '', '', '', '', po.createdAt || '', po.updatedAt || po.createdAt || '']);
      continue;
    }
    for (const it of po.items) {
      rows.push([
        po.id, po.status, po.supplier || '', it.ingredientName,
        asText.num(it.quantity, '', 2), it.unit || '', asText.num(it.receivedQtyBase, '', 2),
        po.createdAt || '', po.updatedAt || po.createdAt || '',
      ]);
    }
  }
  downloadCsv(`wings-theory-purchase-orders-${todayIso()}.csv`,
    ['po_id', 'status', 'supplier', 'ingredient', 'quantity', 'unit', 'received_qty_base', 'created', 'closed'], rows);
}

async function downloadValuationCsv() {
  const v = await getValuation();
  if (!v.available) {
    toast(v.reason || 'Inventory value is not available right now.', 'warn');
    return;
  }
  downloadCsv(`wings-theory-inventory-valuation-${todayIso()}.csv`,
    ['total_value', 'priced_items', 'total_items'],
    [[asText.num(v.totalValue, '', 2), v.pricedItems ?? '', v.totalItems ?? '']]);
}

async function downloadLossesCsv() {
  const a = await getAnomalies(365);
  const rows = a.stockLoss.map((r) => [
    r.ingredientName, r.timestamp || '', asText.delta(r.amount, r.baseUnit),
    r.category || '', r.reasonCode || '', r.severity || '', r.reviewStatus || '',
  ]);
  downloadCsv(`wings-theory-losses-${todayIso()}.csv`,
    ['ingredient', 'when', 'amount', 'category', 'reason', 'severity', 'review_status'], rows);
}

async function downloadsTab() {
  return h('div', {},
    Card({ style: { marginBottom: '14px' } },
      CardHeader({ title: 'Purchasing recommendations', desc: 'Full ingredient list, same figures as Stock & Replenishment' }),
      formatDownloadRow((fmt) => downloadServerReport('/console/reports/purchasing-list', { scope: 'all', group_by_supplier: true, include_costs: true, format: fmt }))),
    Card({ style: { marginBottom: '14px' } },
      CardHeader({ title: 'Expiry monitoring', desc: 'All tracked batches, including those with no recorded expiry' }),
      formatDownloadRow((fmt) => downloadServerReport('/console/reports/expiry', { filter: 'within_30', include_missing_expiry: true, format: fmt }))),
    Card({ style: { marginBottom: '14px' } },
      CardHeader({ title: 'Sales summary (CSV)', desc: 'Daily revenue and orders for the selected range — leave both dates blank for all time' }),
      salesCsvControls()),
    Card({ style: { marginBottom: '14px' } },
      CardHeader({ title: 'Purchase order history (CSV)', desc: 'Every purchase order on file, one row per ingredient line' }),
      Button({ size: 'sm', onClick: downloadPurchaseOrdersCsv }, '↓ Download CSV')),
    Card({ style: { marginBottom: '14px' } },
      CardHeader({ title: 'Inventory valuation (CSV)', desc: 'Current total valued stock' }),
      Button({ size: 'sm', onClick: downloadValuationCsv }, '↓ Download CSV')),
    Card({},
      CardHeader({ title: 'Recorded wastage and losses (CSV)', desc: 'Flagged stock-loss events, last 365 days' }),
      Button({ size: 'sm', onClick: downloadLossesCsv }, '↓ Download CSV')));
}
