import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { stamp, asText } from '../../lib/format.js';
import { listPurchaseOrders } from '../../lib/api/purchaseOrders.js';

const DONE_STATUSES = ['received', 'cancelled'];

function statusLabel(status) {
  return status === 'received' ? ['good', 'Received'] : ['flat', 'Cancelled'];
}

/**
 * Purchasing > Purchase History — spec section 13: "What have we purchased
 * before?" Every purchase order that reached a final state (received or
 * cancelled) — the in-flight ones live on Purchase Orders instead so this
 * list doesn't grow forever with things still being worked. Client-side
 * supplier/date filtering: the backend's own list endpoint only filters by
 * status, and this history is small enough (one row per PO, not per line)
 * that fetching once and filtering here is simpler than adding a new
 * server-side query for it.
 */
export async function render(ctx) {
  const all = (await listPurchaseOrders()).items.filter((po) => DONE_STATUSES.includes(po.status));
  const host = h('div');

  let supplierQuery = '';
  let statusFilter = '';
  let fromDate = '';
  let toDate = '';

  function matches(po) {
    if (statusFilter && po.status !== statusFilter) return false;
    if (supplierQuery && !(po.supplier || '').toLowerCase().includes(supplierQuery.toLowerCase())) return false;
    const day = (po.updatedAt || po.createdAt || '').slice(0, 10);
    if (fromDate && day < fromDate) return false;
    if (toDate && day > toDate) return false;
    return true;
  }

  function draw() {
    const rows = all.filter(matches);
    mount(host, Table({
      columns: [
        { key: 'id', label: 'PO', render: (po) => `PO-${po.id}` },
        { key: 'status', label: 'Status', render: (po) => { const [sev, label] = statusLabel(po.status); return Pill(sev, label); } },
        { key: 'supplier', label: 'Supplier', render: (po) => po.supplier || h('span', { class: 't-xs t-faint' }, '—') },
        {
          key: 'items', label: 'Items',
          render: (po) => h('span', { class: 't-xs t-soft' }, po.items.map((it) => `${it.ingredientName} × ${asText.num(it.quantity, '', 2)}`).join(', ')),
        },
        { key: 'created', label: 'Created', render: (po) => h('span', { class: 'mono t-xs' }, stamp(po.createdAt)) },
        {
          key: 'closed', label: 'Closed',
          render: (po) => h('span', { class: 'mono t-xs' }, stamp(po.updatedAt || po.createdAt)),
        },
      ],
      rows,
      empty: EmptyState({
        icon: '≡',
        title: all.length ? 'No purchase orders match these filters' : 'No purchase history yet',
        description: all.length ? 'Widen or clear the filters above.' : 'Purchase orders appear here once they are received or cancelled.',
      }),
    }));
  }

  const supplierInput = h('input', { type: 'text', placeholder: 'Filter by supplier…', onInput: (e) => { supplierQuery = e.target.value; draw(); } });
  const statusPicker = h('select', { onChange: (e) => { statusFilter = e.target.value; draw(); } },
    h('option', { value: '' }, 'All'),
    h('option', { value: 'received' }, 'Received'),
    h('option', { value: 'cancelled' }, 'Cancelled'));
  const fromInput = h('input', { type: 'date', onChange: (e) => { fromDate = e.target.value; draw(); } });
  const toInput = h('input', { type: 'date', onChange: (e) => { toDate = e.target.value; draw(); } });
  const clearBtn = h('a', {
    href: '#', class: 't-xs',
    onClick: (e) => {
      e.preventDefault();
      supplierQuery = ''; statusFilter = ''; fromDate = ''; toDate = '';
      supplierInput.value = ''; statusPicker.value = ''; fromInput.value = ''; toInput.value = '';
      draw();
    },
  }, 'Clear filters');

  draw();

  return Card({},
    CardHeader({ title: `Purchase history (${all.length})`, desc: 'Received and cancelled purchase orders' }),
    h('div', { style: { display: 'flex', gap: '10px', marginBottom: '14px', flexWrap: 'wrap', alignItems: 'flex-end' } },
      h('label', { class: 'field', style: { minWidth: '180px' } }, 'Supplier', supplierInput),
      h('label', { class: 'field', style: { minWidth: '140px' } }, 'Status', statusPicker),
      h('label', { class: 'field' }, 'From', fromInput),
      h('label', { class: 'field' }, 'To', toInput),
      clearBtn),
    host);
}
