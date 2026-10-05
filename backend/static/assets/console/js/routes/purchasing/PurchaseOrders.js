import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError, confirmModal } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { asText } from '../../lib/format.js';
import { listPurchaseOrders, markOrdered, cancelPurchaseOrder, receiveLine } from '../../lib/api/purchaseOrders.js';
import { session } from '../../lib/api/client.js';

const ACTIVE_STATUSES = ['draft', 'ordered', 'partially_received'];

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
 * Purchasing > Purchase Orders — spec section 13: "What purchases are
 * currently being processed?" Only the in-flight lifecycle states
 * (draft/ordered/partially received); a completed or cancelled order moves
 * to Purchase History instead of sitting here forever. Shared board for
 * both roles — Owner drafts (Purchasing > What to Buy) and can cancel
 * their own; Admin marks ordered and receives. The backend enforces who can
 * do what regardless of which buttons this page shows.
 */
export async function render(ctx) {
  const role = (session.user || {}).role;
  const isOwner = role === 'owner';
  const isAdmin = role === 'admin';

  let pos = (await listPurchaseOrders()).items.filter((po) => ACTIVE_STATUSES.includes(po.status));
  const host = h('div');

  function draw() {
    if (pos.length === 0) {
      mount(host, EmptyState({
        icon: '≡',
        title: 'Nothing currently being processed',
        description: isOwner
          ? 'Draft one from What to Buy to get started.'
          : 'None have been drafted by the Owner yet.',
      }));
      return;
    }
    mount(host, ...pos.map(poCard));
  }

  function poCard(po) {
    const [sev, label] = statusLabel(po.status);
    const itemSummary = po.items.map((it) => `${it.ingredientName} × ${asText.num(it.quantity, '', 2)}${it.unit ? ' ' + it.unit : ''}`).join(', ');
    const canCancel = po.status === 'draft' || po.status === 'ordered';
    const outstanding = po.items.some((it) => (it.receivedQtyBase || 0) < it.quantity);
    return Card({ style: { marginBottom: '12px' } },
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
          ? h('a', { href: '#', class: 't-xs', onClick: (e) => { e.preventDefault(); doCancel(po); } }, 'Cancel')
          : null));
  }

  async function reload() {
    pos = (await listPurchaseOrders()).items.filter((po) => ACTIVE_STATUSES.includes(po.status));
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

  draw();

  return Card({},
    CardHeader({ title: `Currently processing (${pos.length})`, desc: 'Nothing here affects stock until a line is received.' }),
    host);
}
