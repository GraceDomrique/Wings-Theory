import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading } from '../../components/ui/Feedback.js';
import { money, stamp } from '../../lib/format.js';
import { listOrders } from '../../lib/api/orders.js';
import { openOrderDetail } from './orderDetailModal.js';

/**
 * Sales & History > Order History (Owner's sidebar label: "Orders") — spec
 * section 14: a first-class, searchable order ledger, not buried inside a
 * generic reports screen the way it was before this page existed at all.
 * Read-only inspection: GET /pos/orders (wt/api.py's list_orders), the same
 * record the till itself reads — this is a second reader, never a second
 * calculation, and nothing here can change a completed order.
 *
 * Range picker lets this console page reach more than the till's own
 * 3-month/200-row default. Search and paging are both server-side
 * (2026-10-15) — on a dataset of 33k+ orders, loading up to 5000 rows and
 * filtering them in the browser put over a year of real history out of
 * reach under "All time" (the most recent 5000 only reached back a few
 * months) with no way to page further, and a search only ever matched
 * whatever page happened to already be loaded. Each page now holds at most
 * PAGE_SIZE rows; Previous/Next move the server-side offset, and typing in
 * Search re-queries the server instead of filtering what's in memory.
 */
const RANGE_OPTIONS = [
  { value: '90', label: 'Last 90 days' },
  { value: '365', label: 'Last 12 months' },
  { value: 'all', label: 'All time' },
];
const PAGE_SIZE = 100;

export async function render(ctx) {
  const shell = h('div');
  mount(shell, Loading('Loading…'));

  let orders = [];
  let totalCount = null;
  let range = '365';
  let query = '';
  let statusFilter = '';
  let offset = 0;
  let searchDebounce = null;

  async function load() {
    const opts = { status: 'all', limit: PAGE_SIZE, offset };
    if (range === 'all') opts.allTime = true;
    else {
      const d = new Date();
      d.setDate(d.getDate() - Number(range));
      opts.startDate = d.toISOString().slice(0, 10);
    }
    if (query.trim()) opts.q = query.trim();
    const out = await listOrders(opts);
    orders = out.orders;
    totalCount = out.totalCount;
  }

  function matches(o) {
    // Status is the one filter still applied client-side: it's cheap (one
    // page, max PAGE_SIZE rows) and keeping it local avoids a round-trip on
    // every click of the status dropdown. Search and date range are both
    // server-side now — see load() — since those need to reach rows this
    // page never fetched.
    if (statusFilter && o.status !== statusFilter) return false;
    return true;
  }

  function statusPill(o) {
    if (o.voidedAt) return Pill('crit', 'voided');
    switch (o.status) {
      case 'served': return Pill('good', 'served');
      case 'ready': return Pill('warn', 'ready');
      case 'in_progress': return Pill('warn', 'in progress');
      case 'punched': return Pill('info', 'punched');
      default: return Pill('flat', o.status);
    }
  }

  function drawCard() {
    const host = h('div');

    function draw() {
      const rows = orders.filter(matches);
      mount(host, Table({
        columns: [
          { key: 'orderNo', label: 'Order', render: (o) => h('span', { class: 'mono t-sm' }, o.orderNo || `#${o.id}`) },
          { key: 'when', label: 'When', render: (o) => h('span', { class: 'mono t-xs' }, stamp(o.createdAt)) },
          { key: 'table', label: 'Table', render: (o) => o.tableLabel || h('span', { class: 't-xs t-faint' }, '—') },
          { key: 'items', label: 'Items', render: (o) => h('span', { class: 't-xs t-soft' }, o.itemNames || `${o.lineCount} item${o.lineCount === 1 ? '' : 's'}`) },
          { key: 'total', label: 'Total', render: (o) => h('span', { class: 'mono t-sm' }, money(o.currentTotal ?? o.total, 'n/a')) },
          { key: 'status', label: 'Status', render: statusPill },
          {
            key: 'view',
            label: '',
            render: (o) => Button({ size: 'sm', variant: 'ghost', onClick: () => openOrderDetail(o.id) }, 'View'),
          },
        ],
        rows,
        empty: EmptyState({
          icon: '▤',
          title: orders.length ? 'No orders match this filter' : 'No orders in this range',
          description: orders.length ? 'Widen or clear the filters above.' : 'Try a wider date range, or Completed orders appear here once the till starts taking them.',
        }),
      }));
    }

    const searchInput = h('input', {
      type: 'text', placeholder: 'Order #, table, or item…', value: query,
      // Debounced server-side search (2026-10-15): every keystroke used to
      // just re-filter the page already in memory; now it re-queries the
      // backend, which is the only way to reach an order outside the
      // current page of 100. Debounced so typing doesn't fire a request per
      // keystroke, and reset to offset 0 — a search result page numbered
      // from wherever Previous/Next last left off would be confusing.
      onInput: (e) => {
        query = e.target.value;
        offset = 0;
        clearTimeout(searchDebounce);
        searchDebounce = setTimeout(async () => { await refresh(); }, 350);
      },
    });
    const statusPicker = h('select', { onChange: (e) => { statusFilter = e.target.value; draw(); } },
      h('option', { value: '' }, 'All statuses'),
      h('option', { value: 'served' }, 'Served'),
      h('option', { value: 'ready' }, 'Ready'),
      h('option', { value: 'in_progress' }, 'In progress'),
      h('option', { value: 'punched' }, 'Punched'));
    const rangePicker = h('select', {
      onChange: async (e) => { range = e.target.value; offset = 0; await refresh(); },
    }, ...RANGE_OPTIONS.map((o) => h('option', { value: o.value, selected: o.value === range ? 'selected' : undefined }, o.label)));

    const pageStart = totalCount === 0 ? 0 : offset + 1;
    const pageEnd = offset + orders.length;
    const prevBtn = Button({
      size: 'sm', variant: 'ghost', disabled: offset === 0,
      onClick: async () => { offset = Math.max(0, offset - PAGE_SIZE); await refresh(); },
    }, '← Previous');
    const nextBtn = Button({
      size: 'sm', variant: 'ghost', disabled: totalCount == null || pageEnd >= totalCount,
      onClick: async () => { offset += PAGE_SIZE; await refresh(); },
    }, 'Next →');
    const pager = h('div', { style: { display: 'flex', alignItems: 'center', gap: '10px', marginTop: '12px', justifyContent: 'flex-end' } },
      h('span', { class: 't-xs t-faint' },
        totalCount ? `Showing ${pageStart.toLocaleString()}–${pageEnd.toLocaleString()} of ${totalCount.toLocaleString()}` : ''),
      prevBtn, nextBtn);

    draw();

    mount(shell, Card({},
      CardHeader({
        title: `Order history${totalCount != null ? ` (${totalCount.toLocaleString()})` : ''}`,
        desc: query.trim()
          ? `Searching every order in this range for "${query.trim()}" — not just this page`
          : 'Voided orders included and clearly marked · search and paging both reach the full range on the server',
        right: h('label', { class: 'field' }, rangePicker),
      }),
      h('div', { style: { display: 'flex', gap: '10px', marginBottom: '14px', flexWrap: 'wrap' } },
        h('label', { class: 'field', style: { minWidth: '220px' } }, 'Search', searchInput),
        h('label', { class: 'field', style: { minWidth: '160px' } }, 'Status', statusPicker)),
      host,
      pager));
  }

  async function refresh() {
    mount(shell, Loading('Loading…'));
    await load();
    drawCard();
  }

  await refresh();
  return shell;
}
