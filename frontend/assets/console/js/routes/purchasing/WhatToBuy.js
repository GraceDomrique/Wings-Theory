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
const NO_SUPPLIER = 'No supplier assigned';

export async function render(ctx) {
  const isOwner = (session.user || {}).role === 'owner';
  const isAdmin = (session.user || {}).role === 'admin';

  const { rows } = await getReplenishment();
  let activeTab = ['attention', 'all'].includes(ctx.tab) ? ctx.tab : 'attention';
  const recHost = h('div');
  const needsBuying = (r) => r.status === 'crit' || r.status === 'warn' || r.belowReorderPoint;
  const urgent = rows.filter(needsBuying);

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
    mount(recHost, ...groupBySupplier(visible).map(supplierGroup));
  }

  // Grouped by supplier so an owner can see everything to order from one
  // place in one pass, rather than checking each ingredient individually.
  // Groups with something that needs buying sort first; within that, by
  // supplier name. Ingredients with no supplier recorded land in one group
  // at the end, never silently dropped.
  function groupBySupplier(list) {
    const bySupplier = new Map();
    for (const r of list) {
      const key = r.supplier || NO_SUPPLIER;
      if (!bySupplier.has(key)) bySupplier.set(key, []);
      bySupplier.get(key).push(r);
    }
    return [...bySupplier.entries()]
      .map(([supplier, items]) => ({ supplier, items, urgentCount: items.filter(needsBuying).length }))
      .sort((a, b) => {
        if (a.supplier === NO_SUPPLIER) return 1;
        if (b.supplier === NO_SUPPLIER) return -1;
        if ((b.urgentCount > 0) !== (a.urgentCount > 0)) return (b.urgentCount > 0) - (a.urgentCount > 0);
        return a.supplier.localeCompare(b.supplier);
      });
  }

  function supplierGroup({ supplier, items, urgentCount }) {
    return h('div', { style: { marginBottom: '22px' } },
      h('div', {
        style: {
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          gap: '10px', margin: '0 0 8px',
        },
      },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
        h('span', { class: 'eyebrow' }, supplier),
        urgentCount > 0 ? Pill('flat', `${urgentCount} to buy`) : null),
      urgentCount > 1 && isOwner
        ? Button({
          variant: 'default', size: 'sm',
          onClick: () => openGroupDraftModal(supplier, items.filter(needsBuying)),
        }, `Draft purchase order (${urgentCount} items)`)
        : null),
      ...items.map(recommendationCard));
  }

  function recommendationCard(r) {
    const buying = needsBuying(r);
    return Card({ style: { marginBottom: '14px' } },
      h('div', { class: 'card-head' },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: '10px' } },
          h('span', { class: 'card-title' }, r.name),
          statusPill(r)),
        buying && isOwner
          ? Button({ variant: 'primary', size: 'sm', onClick: () => openDraftModal(r) }, 'Draft purchase order')
          : (buying && isAdmin ? Pill('flat', 'Owner drafts this') : null)),
      h('div', { style: { display: 'flex', gap: '22px', flexWrap: 'wrap', margin: '6px 0 10px' } },
        field('Estimated on hand', qty(r.onHand, r.baseUnit)),
        field('Current usage rate', r.usedPerDay === null ? nodata('not measured') : `~${asText.num(r.usedPerDay, '', 2)} ${r.baseUnit}/day`),
        field('Estimated stock cover', days(r.daysLeft)),
        field('Supplier lead time', r.leadTimeDays === null ? nodata('not set') : `${r.leadTimeDays} day${r.leadTimeDays === 1 ? '' : 's'}`),
        r.safetyStockBase !== null ? field('Safety stock', qty(r.safetyStockBase, r.baseUnit)) : null,
        r.packSize ? field('Pack size', qty(r.packSize, r.baseUnit), ` per ${r.purchaseUnit || 'pack'}`) : null,
        r.minimumOrderQty ? field('Minimum order', `${asText.num(r.minimumOrderQty, '', 2)} ${r.suggestedOrderUnit || r.purchaseUnit || 'pack'}`) : null,
        r.incomingOrderedBase > 0 ? field('Incoming confirmed orders', qty(r.incomingOrderedBase, r.baseUnit)) : null),
      h('div', { class: 't-sm t-soft' }, reasonText(r)),
      h('div', { class: 't-xs t-faint' }, 'Figures are estimates derived from recipes and recorded sales — physical '
        + 'stock can differ due to spills, over-portioning, or spoilage.'),
      buying
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
    // Server-computed (wt/depletion.py's ingredient_forecast) — names the
    // specific days-of-supply figure and whether today's usage is running
    // above the ingredient's usual rate, rather than a generic status line.
    if (r.replenishmentMessage) return r.replenishmentMessage;
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

  function field(label, ...value) {
    return h('span', { class: 't-sm' }, h('span', { class: 't-soft' }, `${label} `), h('strong', {}, ...value));
  }

  function openDraftModal(r) {
    const qtyInput = h('input', {
      type: 'number', step: '0.01', min: '0.01',
      value: r.suggestedOrderQty !== null ? String(r.suggestedOrderQty) : '',
    });
    const unitLabel = h('span', { class: 't-xs t-faint' }, r.suggestedOrderUnit || r.baseUnit);
    const supplierInput = h('input', { type: 'text', placeholder: 'optional', value: r.supplier || '' });
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

  // One draft purchase order covering every item currently flagged for a
  // single supplier, instead of drafting each ingredient one at a time.
  // Each line keeps its own editable quantity and can be dropped from the
  // order without affecting the others.
  function openGroupDraftModal(supplier, items) {
    const lines = items.map((r) => ({
      r,
      include: h('input', { type: 'checkbox', checked: true }),
      qtyInput: h('input', {
        type: 'number', step: '0.01', min: '0.01',
        value: r.suggestedOrderQty !== null ? String(r.suggestedOrderQty) : '',
      }),
    }));
    const supplierInput = h('input', {
      type: 'text', placeholder: 'optional',
      value: supplier === NO_SUPPLIER ? '' : supplier,
    });
    const dateInput = h('input', { type: 'date' });
    const noteInput = h('input', { type: 'text', placeholder: 'optional' });
    const err = ModalError();
    const submit = Button({ variant: 'primary' }, `Create draft (${items.length} items)`);

    const lineRows = lines.map((line) => h('div', {
      style: {
        display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 0',
        borderBottom: '1px solid var(--line)',
      },
    },
    line.include,
    h('span', { class: 't-sm', style: { flex: '1' } }, line.r.name),
    line.qtyInput,
    h('span', { class: 't-xs t-faint' }, line.r.suggestedOrderUnit || line.r.baseUnit)));

    submit.addEventListener('click', async () => {
      err.textContent = ''; err.style.display = 'none';
      const chosen = lines.filter((l) => l.include.checked);
      if (chosen.length === 0) {
        err.textContent = 'Include at least one ingredient.';
        err.style.display = '';
        return;
      }
      const invalid = chosen.find((l) => !(Number(l.qtyInput.value) > 0));
      if (invalid) {
        err.textContent = `Enter how much to order for ${invalid.r.name} — it must be more than zero.`;
        err.style.display = '';
        return;
      }
      submit.disabled = true;
      try {
        await createPurchaseOrder({
          supplier: supplierInput.value.trim() || null,
          notes: noteInput.value.trim() || null,
          expectedDeliveryDate: dateInput.value || null,
          items: chosen.map((l) => ({
            ingredientId: l.r.ingredientId,
            quantity: Number(l.qtyInput.value),
            recommendedQtyBase: l.r.suggestedOrderQty,
          })),
        });
        modal.close();
        toast(`Draft purchase order created for ${supplier} (${chosen.length} items) — see Purchase Orders.`, 'good');
      } catch (e) {
        err.textContent = e.message; err.style.display = '';
      } finally {
        submit.disabled = false;
      }
    });

    const modal = Modal({
      title: `Draft a purchase order — ${supplier}`,
      desc: 'This does not affect stock or notify a supplier. Admin sees it and marks it ordered when it is placed.',
      onClose: () => {},
    },
    ...lineRows,
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
