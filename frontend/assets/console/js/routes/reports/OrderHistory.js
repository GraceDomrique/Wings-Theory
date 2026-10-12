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
 * Range picker added so this console page can actually reach more than the
 * till's own 3-month/200-row default (list_orders' docstring always claimed
 * the console route was exempt from that cap — it wasn't wired up until
 * now). Backend limit tops out at 5000 rows per request; when the range
 * holds more than that, the header says so rather than silently truncating.
 */
const RANGE_OPTIONS = [
  { value: '90', label: 'Last 90 days' },
  { value: '365', label: 'Last 12 months' },
  { value: 'all', label: 'All time' },
];

export async function render(ctx) {
  const shell = h('div');
  mount(shell, Loading('Loading…'));

  let orders = [];
  let totalCount = null;
  let range = '365';
  let query = '';
  let statusFilter = '';

  async function load() {
    const opts = { status: 'all', limit: 5000 };
    if (range === 'all') opts.allTime = true;
    else {
      const d = new Date();
      d.setDate(d.getDate() - Number(range));
      opts.startDate = d.toISOString().slice(0, 10);
    }
    const out = await listOrders(opts);
    orders = out.orders;
    totalCount = out.totalCount;
  }

  function matches(o) {
    if (statusFilter && o.status !== statusFilter) return false;
    if (!query) return true;
    const q = query.toLowerCase();
    return String(o.orderNo || '').toLowerCase().includes(q)
      || (o.tableLabel || '').toLowerCase().includes(q)
      || (o.itemNames || '').toLowerCase().includes(q);
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

    const searchInput = h('input', { type: 'text', placeholder: 'Order #, table, or item…', onInput: (e) => { query = e.target.value; draw(); } });
    const statusPicker = h('select', { onChange: (e) => { statusFilter = e.target.value; draw(); } },
      h('option', { value: '' }, 'All statuses'),
      h('option', { value: 'served' }, 'Served'),
      h('option', { value: 'ready' }, 'Ready'),
      h('option', { value: 'in_progress' }, 'In progress'),
      h('option', { value: 'punched' }, 'Punched'));
    const rangePicker = h('select', {
      onChange: async (e) => { range = e.target.value; await refresh(); },
    }, ...RANGE_OPTIONS.map((o) => h('option', { value: o.value, selected: o.value === range ? 'selected' : undefined }, o.label)));

    const truncated = totalCount != null && totalCount > orders.length;
    draw();

    mount(shell, Card({},
      CardHeader({
        title: `Order history (${orders.length}${truncated ? ` of ${totalCount}` : ''})`,
        desc: truncated
          ? `Showing the most recent ${orders.length.toLocaleString()} of ${totalCount.toLocaleString()} orders in this range — narrow the range or widen it in smaller steps to see the rest.`
          : 'Voided orders included and clearly marked',
        right: h('label', { class: 'field' }, rangePicker),
      }),
      h('div', { style: { display: 'flex', gap: '10px', marginBottom: '14px', flexWrap: 'wrap' } },
        h('label', { class: 'field', style: { minWidth: '220px' } }, 'Search', searchInput),
        h('label', { class: 'field', style: { minWidth: '160px' } }, 'Status', statusPicker)),
      host));
  }

  async function refresh() {
    mount(shell, Loading('Loading…'));
    await load();
    drawCard();
  }

  await refresh();
  return shell;
}
