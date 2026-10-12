import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader, CardTitle } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError, confirmModal } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { stamp, asText } from '../../lib/format.js';
import { listPurchaseOrders, markOrdered, cancelPurchaseOrder, receiveLine } from '../../lib/api/purchaseOrders.js';
import { session } from '../../lib/api/client.js';

const ACTIVE_STATUSES = ['draft', 'ordered', 'partially_received'];
const DONE_STATUSES = ['received', 'cancelled'];

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

/**
 * Purchasing > Purchase — spec section 13's two questions ("what's being
 * processed right now?" and "what have we purchased before?") side by side
 * on one page instead of two separate sibling routes: a narrower "Currently
 * processing" rail on the left (the small, active, actionable list) and the
 * wider searchable "History" table on the right (the large, append-only
 * record). Same data source (listPurchaseOrders) split client-side by
 * status, same as the two pages this replaces.
 */
export async function render(ctx) {
  const role = (session.user || {}).role;
  const isOwner = role === 'owner';
  const isAdmin = role === 'admin';

  let all = (await listPurchaseOrders()).items;
  const activeHost = h('div');
  const historyHost = h('div');
  const activeCountEl = h('span', {}, '');

  let supplierQuery = '';
  let statusFilter = '';
  let fromDate = '';
  let toDate = '';

  function activeOrders() { return all.filter((po) => ACTIVE_STATUSES.includes(po.status)); }
  function doneOrders() { return all.filter((po) => DONE_STATUSES.includes(po.status)); }

  function matchesHistory(po) {
    if (statusFilter && po.status !== statusFilter) return false;
    if (supplierQuery && !(po.supplier || '').toLowerCase().includes(supplierQuery.toLowerCase())) return false;
    const day = (po.updatedAt || po.createdAt || '').slice(0, 10);
    if (fromDate && day < fromDate) return false;
    if (toDate && day > toDate) return false;
    return true;
  }

  async function reload() {
    all = (await listPurchaseOrders()).items;
    drawActive();
    drawHistory();
  }

  function drawActive() {
    const pos = activeOrders();
    activeCountEl.textContent = `(${pos.length})`;
    if (pos.length === 0) {
      mount(activeHost, EmptyState({
        icon: '≡',
        title: 'Nothing currently being processed',
        description: isOwner
          ? 'Draft one from What to Buy to get started.'
          : 'None have been drafted by the Owner yet.',
      }));
      return;
    }
    mount(activeHost, ...pos.map(poCard));
  }

  function poCard(po) {
    const [sev, label] = statusLabel(po.status);
    const itemSummary = po.items.map((it) => `${it.ingredientName} × ${asText.num(it.quantity, '', 2)}${it.unit ? ' ' + it.unit : ''}`).join(', ');
    const canCancel = po.status === 'draft' || po.status === 'ordered';
    const outstanding = po.items.some((it) => (it.receivedQtyBase || 0) < it.quantity);
    return h('div', { class: 'po-card' },
      h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' } },
        h('strong', { class: 't-sm' }, `PO-${po.id}`),
        Pill(sev, label)),
      h('div', { class: 't-xs t-soft', style: { marginBottom: '8px' } }, itemSummary),
      po.expectedDeliveryDate ? h('div', { class: 't-xs t-faint', style: { marginBottom: '8px' } }, `Expected ${po.expectedDeliveryDate}`) : null,
      h('div', { style: { display: 'flex', gap: '8px' } },
        isAdmin && po.status === 'draft' ? Button({ size: 'sm', onClick: () => doMarkOrdered(po) }, 'Mark ordered') : null,
        isAdmin && (po.status === 'ordered' || po.status === 'partially_received') && outstanding
          ? Button({ size: 'sm', variant: 'primary', onClick: () => openReceiveModal(po) }, 'Receive')
          : null,
        canCancel
          ? Button({ size: 'sm', variant: 'ghost', onClick: () => doCancel(po) }, 'Cancel')
          : null));
  }

  function doMarkOrdered(po) {
    confirmModal({
      title: `Mark PO-${po.id} as ordered?`,
      body: 'This tells Admin the order has been placed with the supplier. It does not affect stock yet — receiving does that.',
      confirmLabel: 'Mark ordered',
      onConfirm: async () => { await markOrdered(po.id); toast(`PO-${po.id} marked ordered.`, 'good'); await reload(); },
    });
  }

  function doCancel(po) {
    confirmModal({
      title: `Cancel PO-${po.id}?`,
      body: 'This cannot be undone. Nothing already received is affected.',
      confirmLabel: 'Cancel order',
      danger: true,
      onConfirm: async () => { await cancelPurchaseOrder(po.id, null); toast(`PO-${po.id} cancelled.`, 'info'); await reload(); },
    });
  }

  function openReceiveModal(po) {
    const err = ModalError();
    const rowsEls = po.items
      .filter((it) => (it.receivedQtyBase || 0) < it.quantity)
      .map((it) => {
        const outstanding = it.quantity - (it.receivedQtyBase || 0);
        const qtyInput = h('input', { type: 'number', step: '0.01', min: '0', value: String(outstanding) });
        const lotInput = h('input', { type: 'text', placeholder: 'lot / batch no. (optional)' });
        const expiryInput = h('input', { type: 'date' });
        return {
          item: it, qtyInput, lotInput, expiryInput,
          el: h('div', { style: { marginBottom: '14px', paddingBottom: '14px', borderBottom: '1px solid var(--line-soft)' } },
            h('div', { class: 't-sm', style: { fontWeight: '600', marginBottom: '6px' } }, `${it.ingredientName} — outstanding ${asText.num(outstanding, '', 2)} ${it.baseUnit}`),
            h('div', { style: { display: 'flex', gap: '8px' } },
              h('label', { class: 'field', style: { flex: '1' } }, 'Received qty', qtyInput),
              h('label', { class: 'field', style: { flex: '1' } }, 'Lot no.', lotInput),
              h('label', { class: 'field', style: { flex: '1' } }, 'Expiry', expiryInput))),
        };
      });

    const submit = Button({ variant: 'primary' }, 'Confirm receipt');
    submit.addEventListener('click', async () => {
      err.textContent = ''; err.style.display = 'none';
      submit.disabled = true;
      let anyOk = false;
      const failures = [];
      for (const row of rowsEls) {
        const q = Number(row.qtyInput.value);
        if (!(q > 0)) continue;
        try {
          await receiveLine(row.item.id, { qtyBase: q, lotNo: row.lotInput.value.trim() || null, expiresAt: row.expiryInput.value || null });
          anyOk = true;
        } catch (e) { failures.push(`${row.item.ingredientName}: ${e.message}`); }
      }
      submit.disabled = false;
      if (failures.length) { err.textContent = failures.join(' · '); err.style.display = ''; }
      if (anyOk) { modal.close(); toast(`PO-${po.id} updated.`, 'good'); await reload(); }
    });

    const modal = Modal({
      title: `Receive delivery — PO-${po.id}`,
      desc: 'Each line becomes a new FEFO batch, same as receiving directly in Inventory.',
      wide: true,
      onClose: () => {},
    },
    ...rowsEls.map((r) => r.el),
    err,
    h('div', { style: { display: 'flex', justifyContent: 'flex-end' } }, submit));
  }

  function drawHistory() {
    const doneAll = doneOrders();
    const rows = doneAll.filter(matchesHistory);
    mount(historyHost, Table({
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
        title: doneAll.length ? 'No purchase orders match these filters' : 'No purchase history yet',
        description: doneAll.length ? 'Widen or clear the filters above.' : 'Purchase orders appear here once they are received or cancelled.',
      }),
    }));
  }

  const supplierInput = h('input', { type: 'text', placeholder: 'Filter by supplier…', onInput: (e) => { supplierQuery = e.target.value; drawHistory(); } });
  const statusPicker = h('select', { onChange: (e) => { statusFilter = e.target.value; drawHistory(); } },
    h('option', { value: '' }, 'All'),
    h('option', { value: 'received' }, 'Received'),
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

  drawActive();
  drawHistory();

  return h('div', { class: 'purchasing-layout' },
    Card({ className: 'purchasing-active' },
      CardHeader({ title: CardTitle(h('span', {}, 'Currently processing ', activeCountEl)), desc: 'Nothing here affects stock until a line is received.' }),
      activeHost),
    Card({ className: 'purchasing-history' },
      CardHeader({ title: 'History', desc: 'Received and cancelled purchase orders' }),
      h('div', { style: { display: 'flex', gap: '10px', marginBottom: '14px', flexWrap: 'wrap', alignItems: 'flex-end' } },
        h('label', { class: 'field', style: { minWidth: '160px' } }, 'Supplier', supplierInput),
        h('label', { class: 'field', style: { minWidth: '140px' } }, 'Status', statusPicker),
        h('label', { class: 'field' }, 'From', fromInput),
        h('label', { class: 'field' }, 'To', toInput),
        clearBtn),
      historyHost));
}
