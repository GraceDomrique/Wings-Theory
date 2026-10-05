import { h, mount } from '../../lib/dom.js';
import { Card } from '../../components/ui/Card.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Tabbar } from '../../components/ui/Tabbar.js';
import { toast } from '../../components/ui/Feedback.js';
import { qty, days, asText, nodata } from '../../lib/format.js';
import { severityFromDepletionStatus } from '../../lib/severity.js';
import { getReplenishment } from '../../lib/api/intelligence.js';
import { createPurchaseOrder } from '../../lib/api/purchaseOrders.js';
import { session } from '../../lib/api/client.js';

/**
 * Purchasing > What to Buy (Owner's sidebar label: "Purchase Needs") — spec
 * section 13: "What should we purchase next?" business-first, never leading
 * with Prophet/EWMA/reorder-point formulas.
 *
 * Same data Unlimited Wings... no — same data the OLD composite Purchasing
 * page's left pane read (getReplenishment(), backed by
 * wt/forecasting.py's ingredient_forecast()); this is that half, alone, now
 * that "what to buy" and "what's on order" are two separate sidebar pages
 * rather than a two-pane split on one screen. Drafting (`purchase_order.
 * create`) is Owner-only; Admin sees the same recommendations read-only and
 * a note that Owner drafts it — the backend enforces the real rule
 * regardless of what this page shows.
 */
export async function render(ctx) {
  const isOwner = (session.user || {}).role === 'owner';
  const isAdmin = (session.user || {}).role === 'admin';

  const { rows } = await getReplenishment();
  let activeTab = ['attention', 'all'].includes(ctx.tab) ? ctx.tab : 'attention';
  const recHost = h('div');
  const urgent = rows.filter((r) => r.status === 'crit' || r.status === 'warn' || r.belowReorderPoint);

  function draw() {
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
          : (needsBuying && isAdmin ? Pill('flat', 'Owner drafts this') : null)),
      h('div', { style: { display: 'flex', gap: '22px', flexWrap: 'wrap', margin: '6px 0 10px' } },
        field('On hand', qty(r.onHand, r.baseUnit)),
        field('Expected use', r.usedPerDay === null ? nodata('not measured') : `~${asText.num(r.usedPerDay, '', 2)} ${r.baseUnit}/day`),
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
    return h('span', { class: 't-sm' }, h('span', { class: 't-soft' }, `${label} `), h('strong', {}, value));
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
          items: [{ ingredientId: r.ingredientId, quantity, recommendedQtyBase: r.suggestedOrderQty }],
        });
        modal.close();
        toast(`Draft purchase order created for ${r.name} — see Purchase Orders.`, 'good');
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

  draw();

  return h('div', {},
    h('div', { style: { marginBottom: '12px' } },
      Tabbar({
        items: [
          { id: 'attention', label: `Needs attention${urgent.length ? ` (${urgent.length})` : ''}` },
          { id: 'all', label: `All ingredients (${rows.length})` },
        ],
        activeId: activeTab,
        onChange: (id) => { activeTab = id; draw(); },
      })),
    recHost);
}
