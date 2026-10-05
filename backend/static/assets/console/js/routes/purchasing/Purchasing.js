import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Tabbar } from '../../components/ui/Tabbar.js';
import { Modal, ModalError, confirmModal } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { qty, days, asText, nodata } from '../../lib/format.js';
import { severityFromDepletionStatus } from '../../lib/severity.js';
import { getReplenishment } from '../../lib/api/intelligence.js';
import {
  listPurchaseOrders, createPurchaseOrder, markOrdered, cancelPurchaseOrder, receiveLine,
} from '../../lib/api/purchaseOrders.js';
import { session } from '../../lib/api/client.js';

/**
 * Purchasing — Owner's UX/IA redesign proposal (2026-09), section B/D.
 *
 * The recommendation list is the SAME data Intelligence > Replenishment
 * already reads (getReplenishment(), backed by ingredient_forecast()) —
 * this page does not recompute or duplicate the depletion model, it just
 * answers "what do I buy" instead of "here is a runway table" with the
 * identical numbers. wt/purchasing.py's Draft -> Ordered -> Partially
 * Received -> Received lifecycle already existed server-side with a
 * complete frontend API client (lib/api/purchaseOrders.js) and no UI
 * consumer at all until this page.
 *
 * Both Owner and Admin land here: `listPurchaseOrders()` is a shared board
 * (purchasing.list_all), not a per-user inbox, so there is one list, not
 * two. Which actions render is capability-shaped: only Owner drafts
 * (`purchase_order.create`), only Admin marks ordered / receives
 * (`purchase_order.manage`), both can cancel a cancellable one
 * (`purchase_order.cancel`) — the backend enforces the real rule (own vs.
 * any, draft-only vs. draft/ordered) regardless of what this page shows;
 * hiding a button here is UX, not the authorization boundary.
 */
export async function render(ctx) {
  const role = (session.user || {}).role;
  const isOwner = role === 'owner';
  const isAdmin = role === 'admin';

  const { rows } = await getReplenishment();
  let pos = (await listPurchaseOrders()).items;
  let activeTab = ['attention', 'all'].includes(ctx.tab) ? ctx.tab : 'attention';

  const recHost = h('div');
  const poHost = h('div');

  const urgent = rows.filter((r) => r.status === 'crit' || r.status === 'warn' || r.belowReorderPoint);

  function drawRecommendations() {
    const visible = activeTab === 'attention' ? urgent : rows;
    if (visible.length === 0) {
      mount(recHost, Card({}, EmptyState({
        icon: '✓',
        title: activeTab === 'attention' ? 'Nothing needs buying right now' : 'No ingredients yet',
        description: activeTab === 'attention'
          ? (rows.length === 0
            ? 'No ingredient has a measured usage rate yet, so there is nothing to recommend buying. This is a data gap, not an all-clear.'
            : 'Every ingredient with a measured usage rate has a comfortable buffer against its supplier lead time.')
          : 'Ingredients will appear here once they exist in the catalog.',
        tight: true,
      })));
      return;
    }
    mount(recHost, ...visible.map(recommendationCard));
  }

  function recommendationCard(r) {
    const needsBuying = r.status === 'crit' || r.status === 'warn' || r.belowReorderPoint;
    return Card({ style: { marginBottom: '14px' } },
      h('div', { class: 'card-head' },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: '10px' } },
          h('span', { class: 'card-title' }, r.name),
          statusPill(r)),
        needsBuying && isOwner
          ? Button({ variant: 'primary', size: 'sm', onClick: () => openDraftModal(r) }, 'Draft purchase order')
          : (needsBuying && isAdmin
            ? Pill('flat', 'Owner drafts this')
            : null)),
      h('div', { style: { display: 'flex', gap: '22px', flexWrap: 'wrap', margin: '6px 0 10px' } },
        field('On hand', qty(r.onHand, r.baseUnit)),
        field('Expected use', r.usedPerDay === null
          ? nodata('not measured')
          : `~${asText.num(r.usedPerDay, '', 2)} ${r.baseUnit}/day`),
        field('Stock cover', days(r.daysLeft)),
        field('Supplier lead time', r.leadTimeDays === null ? nodata('not set') : `${r.leadTimeDays} day${r.leadTimeDays === 1 ? '' : 's'}`),
        r.safetyStockBase !== null ? field('Safety stock', qty(r.safetyStockBase, r.baseUnit)) : null),
      h('div', { class: 't-sm t-soft' }, reasonText(r)),
      needsBuying
        ? h('div', {
          style: {
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            marginTop: '12px', paddingTop: '12px', borderTop: '1px solid var(--line)',
          },
        },
        h('div', {},
          h('div', { class: 'eyebrow' }, 'Recommended'),
          h('div', { style: { fontWeight: '600' } },
            r.suggestedOrderQty !== null
              ? `Buy ~${asText.num(r.suggestedOrderQty, '', 2)} ${r.suggestedOrderUnit || r.baseUnit}`
              : 'Not enough history yet to suggest a quantity')))
        : null);
  }

  function reasonText(r) {
    if (r.risk === 'out') return 'This ingredient is out of stock right now.';
    if (r.risk === 'critical') return 'Current stock may run out before the next delivery arrives.';
    if (r.belowReorderPoint) return 'Stock has fallen below the level set as safe for this ingredient.';
    if (r.risk === 'at_risk') return 'Current stock may fall below the safe level before the next delivery.';
    if (r.risk === 'idle') return 'No recent usage to measure a rate from — this is a data gap, not a stock issue.';
    if (r.risk === 'excess') return 'Well above the usual level — no action needed.';
    return 'Comfortably covered until the next scheduled delivery.';
  }

  function statusPill(r) {
    const sev = severityFromDepletionStatus(r.risk);
    const label = r.risk === 'out' ? 'out of stock'
      : r.risk === 'critical' ? 'buy now'
        : (r.risk === 'at_risk' || r.belowReorderPoint) ? 'buy soon'
          : r.risk === 'excess' ? 'excess'
            : r.risk === 'idle' ? 'no usage data'
              : 'healthy';
    return Pill(sev, label);
  }

  function field(label, value) {
    return h('span', { class: 't-sm' },
      h('span', { class: 't-soft' }, `${label} `),
      h('strong', {}, value));
  }

  function openDraftModal(r) {
    const qtyInput = h('input', {
      type: 'number', step: '0.01', min: '0.01',
      value: r.suggestedOrderQty !== null ? String(r.suggestedOrderQty) : '',
    });
    const unitLabel = h('span', { class: 't-xs t-faint' }, r.suggestedOrderUnit || r.baseUnit);
    const supplierInput = h('input', { type: 'text', placeholder: 'optional' });
    const dateInput = h('input', { type: 'date' });
    const noteInput = h('input', { type: 'text', placeholder: 'optional' });
    const err = ModalError();
    const submit = Button({ variant: 'primary' }, 'Create draft');

    submit.addEventListener('click', async () => {
      err.textContent = ''; err.style.display = 'none';
      const quantity = Number(qtyInput.value);
      if (!(quantity > 0)) {
        err.textContent = 'Enter how much to order — it must be more than zero.';
        err.style.display = '';
        return;
      }
      submit.disabled = true;
      try {
        await createPurchaseOrder({
          supplier: supplierInput.value.trim() || null,
          notes: noteInput.value.trim() || null,
          expectedDeliveryDate: dateInput.value || null,
          items: [{
            ingredientId: r.ingredientId,
            quantity,
            recommendedQtyBase: r.suggestedOrderQty,
          }],
        });
        modal.close();
        toast(`Draft purchase order created for ${r.name}.`, 'good');
        pos = (await listPurchaseOrders()).items;
        drawPurchaseOrders();
      } catch (e) {
        err.textContent = e.message; err.style.display = '';
      } finally {
        submit.disabled = false;
      }
    });

    const modal = Modal({
      title: `Draft a purchase order — ${r.name}`,
      desc: 'This does not affect stock or notify a supplier. Admin sees it and marks it ordered when it is placed.',
      onClose: () => {},
    },
    h('label', { class: 'field' }, 'Quantity',
      h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px' } }, qtyInput, unitLabel)),
    h('label', { class: 'field', style: { marginTop: '10px' } }, 'Supplier', supplierInput),
    h('label', { class: 'field', style: { marginTop: '10px' } }, 'Expected delivery date', dateInput),
    h('label', { class: 'field', style: { marginTop: '10px' } }, 'Note', noteInput),
    err,
    h('div', { style: { marginTop: '14px', display: 'flex', justifyContent: 'flex-end' } }, submit));
  }

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

  function drawPurchaseOrders() {
    if (pos.length === 0) {
      mount(poHost, EmptyState({
        icon: '≡',
        title: 'No purchase orders yet',
        description: isOwner
          ? 'Draft one from a recommendation on the left to get started.'
          : 'None have been drafted by the Owner yet.',
        tight: true,
      }));
      return;
    }
    mount(poHost, ...pos.map((po) => {
      const [sev, label] = statusLabel(po.status);
      const itemSummary = po.items.map((it) => `${it.ingredientName} × ${asText.num(it.quantity, '', 2)}${it.unit ? ' ' + it.unit : ''}`).join(', ');
      const canCancel = po.status === 'draft' || po.status === 'ordered';
      const outstanding = po.items.some((it) => (it.receivedQtyBase || 0) < it.quantity);
      return h('div', { style: { padding: '14px 0', borderBottom: '1px solid var(--line-soft)' } },
        h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' } },
          h('strong', { class: 't-sm' }, `PO-${po.id}`),
          Pill(sev, label)),
        h('div', { class: 't-xs t-soft', style: { marginBottom: '8px' } }, itemSummary),
        po.expectedDeliveryDate
          ? h('div', { class: 't-xs t-faint', style: { marginBottom: '8px' } }, `Expected ${po.expectedDeliveryDate}`)
          : null,
        h('div', { style: { display: 'flex', gap: '8px' } },
          isAdmin && po.status === 'draft'
            ? Button({ size: 'sm', onClick: () => doMarkOrdered(po) }, 'Mark ordered')
            : null,
          isAdmin && (po.status === 'ordered' || po.status === 'partially_received') && outstanding
            ? Button({ size: 'sm', variant: 'primary', onClick: () => openReceiveModal(po) }, 'Receive')
            : null,
          canCancel
            ? h('a', { href: '#', class: 't-xs', onClick: (e) => { e.preventDefault(); doCancel(po); } }, 'Cancel')
            : null));
    }));
  }

  function doMarkOrdered(po) {
    confirmModal({
      title: `Mark PO-${po.id} as ordered?`,
      body: 'This tells Admin the order has been placed with the supplier. It does not affect stock yet — receiving does that.',
      confirmLabel: 'Mark ordered',
      onConfirm: async () => {
        await markOrdered(po.id);
        toast(`PO-${po.id} marked ordered.`, 'good');
        pos = (await listPurchaseOrders()).items;
        drawPurchaseOrders();
      },
    });
  }

  function doCancel(po) {
    confirmModal({
      title: `Cancel PO-${po.id}?`,
      body: 'This cannot be undone. Nothing already received is affected.',
      confirmLabel: 'Cancel order',
      danger: true,
      onConfirm: async () => {
        await cancelPurchaseOrder(po.id, null);
        toast(`PO-${po.id} cancelled.`, 'info');
        pos = (await listPurchaseOrders()).items;
        drawPurchaseOrders();
      },
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
            h('div', { class: 't-sm', style: { fontWeight: '600', marginBottom: '6px' } },
              `${it.ingredientName} — outstanding ${asText.num(outstanding, '', 2)} ${it.baseUnit}`),
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
          await receiveLine(row.item.id, {
            qtyBase: q,
            lotNo: row.lotInput.value.trim() || null,
            expiresAt: row.expiryInput.value || null,
          });
          anyOk = true;
        } catch (e) {
          failures.push(`${row.item.ingredientName}: ${e.message}`);
        }
      }
      submit.disabled = false;
      if (failures.length) {
        err.textContent = failures.join(' · ');
        err.style.display = '';
      }
      if (anyOk) {
        modal.close();
        toast(`PO-${po.id} updated.`, 'good');
        pos = (await listPurchaseOrders()).items;
        drawPurchaseOrders();
      }
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

  drawRecommendations();
  drawPurchaseOrders();

  return h('div', { style: { display: 'flex', gap: '24px' } },
    h('div', { style: { flex: '1.6', minWidth: '0' } },
      h('div', { style: { marginBottom: '12px' } },
        Tabbar({
          items: [
            { id: 'attention', label: `Needs attention${urgent.length ? ` (${urgent.length})` : ''}` },
            { id: 'all', label: `All ingredients (${rows.length})` },
          ],
          activeId: activeTab,
          onChange: (id) => {
            activeTab = id;
            history.replaceState(null, '', `#/purchasing/${id}`);
            drawRecommendations();
          },
        })),
      recHost),
    h('div', { style: { flex: '1', minWidth: '0' } },
      Card({},
        CardHeader({ title: 'Purchase orders', desc: 'Shared with Owner and Admin — nothing here affects stock until a line is received.' }),
        poHost)));
}
