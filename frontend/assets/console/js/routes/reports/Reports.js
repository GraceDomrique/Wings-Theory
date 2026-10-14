import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { Button } from '../../components/ui/Button.js';
import { Loading } from '../../components/ui/Feedback.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { money, count, asText } from '../../lib/format.js';
import { getValuation, getSales } from '../../lib/api/reports.js';

/**
 * Business Reports — 2026-10-15 rebuild. This used to be a single inventory-
 * valuation card and nothing else ("the nearly empty Business Reports
 * page"). It's now a real report center: Sales Summary and Best Sellers
 * (both backed by the same GET /console/reports/sales the Sales & Orders
 * Overview page already reads — no new backend endpoint, no figure
 * recalculated here that the server didn't already compute) plus the
 * existing Inventory Valuation card, each downloadable as CSV.
 *
 * Every report states its own date range and generation time, and that
 * voided orders are excluded (the backend already excludes them from every
 * revenue total it returns — see getSales()'s own comment) — so a report
 * is never read as "the whole truth" without saying what it actually covers.
 */
const PERIODS = [
  { value: 'today', label: 'Today' },
  { value: 'this_week', label: 'This Week' },
  { value: 'this_month', label: 'This Month' },
  { value: 'all_time', label: 'All Time' },
];

export async function render(ctx) {
  const state = { period: 'this_month' };
  const host = h('div');

  async function draw() {
    mount(host, Loading('Loading reports…'));
    const opts = state.period === 'all_time' ? { all_time: true } : { preset: state.period };
    const [sales, valuation] = await Promise.all([getSales(opts), getValuation()]);
    mount(host, content(sales, valuation));
  }

  function periodTabs() {
    return h('div', { style: { display: 'flex', gap: '6px' } },
      ...PERIODS.map((o) => Button({
        variant: o.value === state.period ? 'primary' : 'default', size: 'sm',
        onClick: () => { state.period = o.value; draw(); },
      }, o.label)));
  }

  function reportMeta(sales) {
    const range = sales.allTime
      ? 'all recorded history'
      : `${sales.startDate || '—'} to ${sales.endDate || '—'}`;
    return h('div', { class: 't-xs t-faint', style: { marginBottom: '14px' } },
      `Generated ${new Date().toLocaleString()} · covers ${range} · voided orders excluded from every total`);
  }

  function content(sales, valuation) {
    return h('div', {},
      h('div', { style: { marginBottom: '12px' } }, periodTabs()),
      reportMeta(sales),
      salesSummaryCard(sales),
      h('div', { style: { marginTop: '14px' } }, bestSellersCard(sales)),
      h('div', { style: { marginTop: '14px' } }, valuationCard(valuation)));
  }

  function salesSummaryCard(sales) {
    return Card({},
      CardHeader({
        title: 'Sales Summary',
        desc: sales.windowDays ? `Last ${sales.windowDays} day(s)` : 'Revenue, orders and items sold for the selected period',
        right: Button({ size: 'sm', onClick: () => downloadSalesSummaryCsv(sales) }, '↓ Download CSV'),
      }),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', gap: '14px' } },
        Stat({ value: money(sales.revenue, '₱0'), label: 'Revenue' }),
        Stat({ value: count(sales.orders), label: 'Orders' }),
        Stat({ value: count(sales.covers), label: 'Covers' }),
        Stat({ value: sales.averageOrder === null ? null : money(sales.averageOrder), label: 'Avg. Order' })));
  }

  function bestSellersCard(sales) {
    const rows = sales.byItem || [];
    return Card({},
      CardHeader({
        title: 'Best Sellers',
        desc: 'Ranked by quantity sold this period',
        right: Button({ size: 'sm', onClick: () => downloadBestSellersCsv(sales) }, '↓ Download CSV'),
      }),
      rows.length === 0
        ? EmptyState({ icon: '✓', title: 'No sales recorded for this period', tight: true })
        : Table({
          columns: [
            { key: 'rank', label: '#', render: (r, i) => String(rows.indexOf(r) + 1) },
            { key: 'name', label: 'Item', render: (r) => r.name },
            { key: 'qty', label: 'Qty sold', render: (r) => h('span', { class: 'mono t-sm' }, count(r.qtySold)) },
            { key: 'revenue', label: 'Revenue', render: (r) => h('span', { class: 'mono t-sm' }, money(r.revenue)) },
            { key: 'share', label: 'Share of revenue', render: (r) => (r.sharePct === null ? '—' : `${r.sharePct.toFixed(1)}%`) },
          ],
          rows: rows.slice(0, 20),
        }));
  }

  function valuationCard(valuation) {
    if (valuation.available) {
      return Card({},
        CardHeader({ title: 'Inventory Valuation', desc: 'On-hand stock at confirmed cost — not tied to the period selector above' }),
        h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)' } },
          Card({ sunk: true }, Stat({ value: money(valuation.totalValue), label: 'Valued on hand' })),
          Card({ sunk: true }, Stat({ value: count(valuation.pricedItems), label: 'Items priced' })),
          Card({ sunk: true }, Stat({ value: count(valuation.totalItems), label: 'Items tracked' }))));
    }
    return Card({},
      CardHeader({ title: 'Inventory Valuation', desc: 'On-hand stock at confirmed cost' }),
      EmptyState({ icon: '₱', title: 'Inventory value is not available yet', description: valuation.reason }));
  }

  await draw();
  return host;
}

function downloadSalesSummaryCsv(sales) {
  const head = ['metric', 'value'];
  const lines = [head.join(','), `revenue,${asText.num(sales.revenue, '', 2)}`,
    `orders,${sales.orders ?? ''}`, `items_sold,${sales.items ?? ''}`,
    `avg_order,${asText.num(sales.averageOrder, '', 2)}`];
  downloadCsv(lines.join('\n'), 'sales-summary');
}

function downloadBestSellersCsv(sales) {
  const head = ['rank', 'item', 'qty_sold', 'revenue', 'share_pct'];
  const lines = [head.join(',')];
  (sales.byItem || []).forEach((r, i) => {
    lines.push([i + 1, r.name, asText.num(r.qtySold, '', 0), asText.num(r.revenue, '', 2), r.sharePct === null ? '' : r.sharePct.toFixed(1)]
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
