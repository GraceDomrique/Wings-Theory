import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { StatTile } from '../../components/ui/StatTile.js';
import { Table } from '../../components/ui/Table.js';
import { Tabbar } from '../../components/ui/Tabbar.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError, confirmModal } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { stamp, date, asText, count } from '../../lib/format.js';
import { listPurchaseOrders, markOrdered, cancelPurchaseOrder, receiveLine } from '../../lib/api/purchaseOrders.js';
import { session } from '../../lib/api/client.js';

const ICON = {
  cart: '<path d="M6 2l1.5 5h9L18 2"/><path d="M3 7h18l-1.7 10.2A2 2 0 0117.3 19H6.7a2 2 0 01-2-1.8L3 7z"/><circle cx="9" cy="22" r="1"/><circle cx="16" cy="22" r="1"/>',
};

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

const TABS = [
  { id: 'draft', label: 'Draft Orders', statuses: ['draft'] },
  { id: 'inprogress', label: 'In Progress', statuses: ['ordered', 'partially_received'] },
  { id: 'completed', label: 'Completed', statuses: ['received'] },
  { id: 'cancelled', label: 'Cancelled', statuses: ['cancelled'] },
  { id: 'history', label: 'Purchase History', statuses: null }, // null = every status
];

/**
 * Purchasing > Purchase — spec section 13's two questions ("what's being
 * processed right now?" and "what have we purchased before?"), both reached
 * through one status-tabbed table (2026-10 relayout, matching the approved
 * reference mockup) rather than a narrow card rail beside a separate history
 * table. Clicking a row opens that order's own detail panel below — items,
 * running totals, and whichever actions its status/role combination allows
 * — instead of the old pattern of one modal per action with no persistent
 * place to see "what IS this order" at a glance. Same data source
 * (listPurchaseOrders) split client-side by status, same as before.
 */
export async function render(ctx) {
  const role = (session.user || {}).role;
  const isOwner = role === 'owner';
  const isAdmin = role === 'admin';

  let all = (await listPurchaseOrders()).items;
  const listHost = h('div');
  const detailHost = h('div');

  let activeTab = TABS.some((t) => t.id === ctx.tab) ? ctx.tab : 'draft';
  let selectedId = null;
  let supplierQuery = '';
  let fromDate = '';
  let toDate = '';

  function rowsForTab() {
    const tab = TABS.find((t) => t.id === activeTab);
    let rows = tab.statuses ? all.filter((po) => tab.statuses.includes(po.status)) : all.slice();
    if (activeTab === 'history') {
      rows = rows.filter((po) => {
        if (supplierQuery && !(po.supplier || '').toLowerCase().includes(supplierQuery.toLowerCase())) return false;
        const day = (po.updatedAt || po.createdAt || '').slice(0, 10);
        if (fromDate && day < fromDate) return false;
        if (toDate && day > toDate) return false;
        return true;
      });
    }
    return rows.sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || ''));
  }

  async function reload() {
    all = (await listPurchaseOrders()).items;
    draw();
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

  const supplierInput = h('input', { type: 'text', placeholder: 'Filter by supplier…', onInput: (e) => { supplierQuery = e.target.value; draw(); } });
  const fromInput = h('input', { type: 'date', onChange: (e) => { fromDate = e.target.value; draw(); } });
  const toInput = h('input', { type: 'date', onChange: (e) => { toDate = e.target.value; draw(); } });
  const clearBtn = Button({
    size: 'sm', variant: 'ghost',
    onClick: () => {
      supplierQuery = ''; fromDate = ''; toDate = '';
      supplierInput.value = ''; fromInput.value = ''; toInput.value = '';
      draw();
    },
  }, 'Clear filters');
  const historyFilters = h('div', { style: { display: 'flex', gap: '10px', marginBottom: '14px', flexWrap: 'wrap', alignItems: 'flex-end' } },
    h('label', { class: 'field', style: { minWidth: '160px' } }, 'Supplier', supplierInput),
    h('label', { class: 'field' }, 'From', fromInput),
    h('label', { class: 'field' }, 'To', toInput),
    clearBtn);

  const tabbarHost = h('div');

  function drawTabbar() {
    mount(tabbarHost, Tabbar({
      items: TABS.map((t) => ({ id: t.id, label: `${t.label} (${(t.statuses ? all.filter((po) => t.statuses.includes(po.status)) : all).length})` })),
      activeId: activeTab,
      onChange: (id) => { activeTab = id; selectedId = null; draw(); },
    }));
  }

  function draw() {
    drawTabbar();
    const rows = rowsForTab();
    if (!rows.some((po) => po.id === selectedId)) selectedId = rows.length ? rows[0].id : null;

    mount(listHost,
      activeTab === 'history' ? historyFilters : null,
      Table({
        columns: [
          { key: 'id', label: 'PO Number', render: (po) => `PO-${po.id}` },
          { key: 'supplier', label: 'Supplier', render: (po) => po.supplier || h('span', { class: 't-xs t-faint' }, '—') },
          { key: 'created', label: 'Created Date', render: (po) => date(po.createdAt) },
          { key: 'items', label: 'Items', render: (po) => count(po.items.length) },
          { key: 'status', label: 'Status', render: (po) => { const [sev, label] = statusLabel(po.status); return Pill(sev, label); } },
          { key: 'expected', label: 'Expected Delivery', render: (po) => (po.expectedDeliveryDate ? date(po.expectedDeliveryDate) : h('span', { class: 't-xs t-faint' }, '—')) },
        ],
        rows,
        rowClass: (po) => (po.id === selectedId ? 'row-selected' : ''),
        onRowClick: (po) => { selectedId = po.id; drawDetail(); },
        empty: EmptyState({
          icon: '≡',
          title: all.length ? 'No purchase orders match this view' : 'No purchase orders yet',
          description: isOwner ? 'Draft one from Stock & Replenishment to get started.' : 'None have been drafted by the Owner yet.',
        }),
      }));
    drawDetail();
  }

  function drawDetail() {
    const po = all.find((p) => p.id === selectedId);
    if (!po) { mount(detailHost, h('div')); return; }
    const [sev, label] = statusLabel(po.status);
    const canCancel = po.status === 'draft' || po.status === 'ordered';
    const outstanding = po.items.some((it) => (it.receivedQtyBase || 0) < it.quantity);

    mount(detailHost, Card({},
      CardHeader({
        title: `PO-${po.id}`,
        // stamp() returns a DOM node, not a string — interpolating it
        // directly into the template literal below used to print the
        // literal "[object Text]" (2026-10 rendering bug fix).
        desc: `Supplier: ${po.supplier || '—'} · Created ${stamp(po.createdAt).textContent} by ${po.createdBy || '—'}`,
        right: Pill(sev, label),
      }),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1.6fr 1fr', gap: '18px' } },
        h('div', {},
          h('div', { class: 'eyebrow', style: { marginBottom: '8px' } }, `Items (${po.items.length})`),
          Table({
            columns: [
              { key: 'ingredient', label: 'Ingredient', render: (it) => it.ingredientName },
              { key: 'qty', label: 'Ordered', render: (it) => `${asText.num(it.quantity, '', 2)} ${it.unit || ''}` },
              { key: 'received', label: 'Received', render: (it) => `${asText.num(it.receivedQtyBase || 0, '', 2)} ${it.baseUnit || ''}` },
              { key: 'note', label: 'Note', render: (it) => it.note || h('span', { class: 't-xs t-faint' }, '—') },
            ],
            rows: po.items,
          }),
          po.notes ? h('p', { class: 't-sm t-soft', style: { marginTop: '10px' } }, h('strong', {}, 'Note: '), po.notes) : null,
          po.cancelReason ? h('div', { class: 'callout callout-info', style: { marginTop: '10px' } }, h('p', {}, h('strong', {}, 'Cancel reason: '), po.cancelReason)) : null),
        h('div', {},
          h('div', { class: 'eyebrow', style: { marginBottom: '8px' } }, 'Actions'),
          h('div', { style: { display: 'flex', flexDirection: 'column', gap: '8px' } },
            isAdmin && po.status === 'draft' ? Button({ size: 'sm', onClick: () => doMarkOrdered(po) }, 'Mark ordered') : null,
            isAdmin && (po.status === 'ordered' || po.status === 'partially_received') && outstanding
              ? Button({ size: 'sm', variant: 'primary', onClick: () => openReceiveModal(po) }, 'Receive delivery')
              : null,
            canCancel ? Button({ size: 'sm', variant: 'ghost', onClick: () => doCancel(po) }, 'Cancel order') : null,
            po.expectedDeliveryDate ? h('div', { class: 't-xs t-faint', style: { marginTop: '6px' } }, 'Expected delivery: ', date(po.expectedDeliveryDate)) : null)))));
  }

  draw();

  const draftCount = all.filter((po) => po.status === 'draft').length;
  const inProgressCount = all.filter((po) => po.status === 'ordered' || po.status === 'partially_received').length;
  const completedCount = all.filter((po) => po.status === 'received').length;
  const cancelledCount = all.filter((po) => po.status === 'cancelled').length;

  // 2026-10-15 recompose: the title/action line and the 4 stat tiles used to
  // be two separate full-width rows (title row, then a 4-up tile row below
  // it) — the same "stacked equal rows" shape fixed on Inventory and Menu.
  // The title+action block now pairs beside a compact 2x2 tile grid instead,
  // so the page's own "what you do here" and "what's the current count"
  // read as one composed header instead of two.
  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: '1.1fr 1fr', gap: '18px', marginBottom: '18px', alignItems: 'center' } },
      h('div', {},
        h('div', { class: 'eyebrow', style: { marginBottom: '4px' } }, 'Purchase orders'),
        h('div', { class: 't-sm t-faint', style: { marginBottom: '14px' } }, 'Draft, in progress, completed and cancelled, all in one place.'),
        isOwner ? Button({ variant: 'primary', onClick: () => ctx.navigate('stockreplenishment') }, '+ Create Purchase Order') : null),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(2,1fr)', gap: '10px' } },
        StatTile({ iconPath: ICON.cart, severity: 'flat', value: count(draftCount), label: 'Draft' }),
        StatTile({ iconPath: ICON.cart, severity: inProgressCount > 0 ? 'warn' : 'good', value: count(inProgressCount), label: 'In Progress' }),
        StatTile({ iconPath: ICON.cart, severity: 'good', value: count(completedCount), label: 'Completed' }),
        StatTile({ iconPath: ICON.cart, severity: 'flat', value: count(cancelledCount), label: 'Cancelled' }))),
    tabbarHost,
    h('div', { style: { marginTop: '14px', marginBottom: '14px' } }, listHost),
    detailHost);
}
