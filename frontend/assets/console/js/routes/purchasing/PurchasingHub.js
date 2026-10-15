import { h, mount, icon } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError, confirmModal } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import {
  qty, days, asText, nodata, count, stamp, date,
} from '../../lib/format.js';
import { severityFromDepletionStatus } from '../../lib/severity.js';
import { ICONS } from '../../lib/icons.js';
import { getReplenishment } from '../../lib/api/intelligence.js';
import {
  listPurchaseOrders, createPurchaseOrder, markOrdered, cancelPurchaseOrder, receiveLine,
} from '../../lib/api/purchaseOrders.js';
import { session } from '../../lib/api/client.js';

const NO_SUPPLIER = 'No supplier assigned';

/**
 * Purchasing — 2026-10-15 integrated workspace replacing the Demand
 * Forecasts / What to Buy / Purchase three-way split (spec: "Workspace 2 —
 * Purchasing"). One purchasing-needs list (left) with a contextual panel
 * (right) that shows, for whichever ingredient or purchase order is
 * selected, the supporting calculation or the processing actions — no page
 * change to go from "this needs buying" to "why" to "draft it" to "receive
 * it".
 *
 * Still the same two data sources the old three pages read
 * (getReplenishment / listPurchaseOrders + create/markOrdered/cancel/
 * receiveLine) — a composition change, not a new data layer. Permissions
 * are unchanged: Owner alone drafts, Admin alone processes/receives — both
 * enforced server-side regardless of what this page shows or hides.
 *
 * Demand Forecasts (Prophet, per MENU ITEM units sold) is a genuinely
 * different model than ingredient replenishment (per INGREDIENT days of
 * cover, read from getReplenishment() below) — conflating them into one
 * chart would be inventing a relationship the backend doesn't compute. It
 * stays its own route ('forecasts', linked from the right panel) as the
 * spec's own allowed "focused analytical workspace" exception, same as
 * Menu Studio links out to the full Combo Builder.
 */
export async function render(ctx) {
  const isOwner = (session.user || {}).role === 'owner';
  const isAdmin = (session.user || {}).role === 'admin';

  const [{ rows }, poList] = await Promise.all([
    getReplenishment(),
    listPurchaseOrders(),
  ]);
  let orders = poList.items;

  const needsBuying = (r) => r.status === 'crit' || r.status === 'warn' || r.belowReorderPoint;
  const urgent = rows.filter(needsBuying);

  let selection = null; // { kind: 'ingredient'|'po', ref }
  const healthHost = h('div');
  const needsHost = h('div');
  const rightHost = h('div');
  const poHost = h('div');

  function select(kind, ref) {
    selection = { kind, ref };
    drawRight();
    drawNeeds();
    drawPurchaseOrders();
  }

  async function reload() {
    orders = (await listPurchaseOrders()).items;
    drawPurchaseOrders();
    drawRight();
  }

  // ---------------------------------------------------------------------
  // Top strip — compact, one line (same device as Menu Studio's health
  // strip: see MenuStudio.js / app.css's .menu-health-strip).
  // ---------------------------------------------------------------------
  function drawHealth() {
    const draftCount = orders.filter((po) => po.status === 'draft').length;
    const inProgressCount = orders.filter((po) => po.status === 'ordered' || po.status === 'partially_received').length;
    const overdueCount = orders.filter((po) => po.status !== 'received' && po.status !== 'cancelled'
      && po.expectedDeliveryDate && new Date(po.expectedDeliveryDate) < new Date()).length;
    mount(healthHost, h('div', { class: 'menu-health-strip' },
      h('span', { class: `mhs-item ${urgent.length ? 'mhs-crit' : 'mhs-good'}` }, icon(ICONS.alertTriangle, 13), ' ', h('b', {}, count(urgent.length)), ' need buying'),
      h('span', { class: 'mhs-dot' }),
      h('span', { class: 'mhs-item' }, icon(ICONS.clipboardList, 13), ' ', h('b', {}, count(draftCount)), ' draft orders'),
      h('span', { class: 'mhs-dot' }),
      h('span', { class: `mhs-item ${inProgressCount ? '' : 'mhs-good'}` }, icon(ICONS.shoppingCart, 13), ' ', h('b', {}, count(inProgressCount)), ' in progress'),
      h('span', { class: 'mhs-dot' }),
      h('span', { class: `mhs-item ${overdueCount ? 'mhs-crit' : 'mhs-good'}` }, icon(ICONS.calendarClock, 13), ' ', h('b', {}, count(overdueCount)), ' overdue ', overdueCount === 1 ? 'delivery' : 'deliveries')));
  }

  // ---------------------------------------------------------------------
  // Left/center — purchasing needs, grouped by supplier. Same data and
  // grouping as the old What to Buy page; a row click updates the right
  // panel in place instead of opening a Drawer overlay (spec: "contextual
  // right panel", not a modal on top of the page).
  // ---------------------------------------------------------------------
  let needsTab = 'attention';

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

  function supplierGroup({ supplier, items, urgentCount }) {
    return Card({ style: { marginBottom: '14px' } },
      CardHeader({
        title: supplier,
        desc: urgentCount > 0 ? `${urgentCount} ingredient${urgentCount === 1 ? '' : 's'} need buying` : 'Nothing urgent from this supplier',
        right: urgentCount > 1 && isOwner
          ? Button({ size: 'sm', onClick: () => openGroupDraftModal(supplier, items.filter(needsBuying)) }, `Draft PO (${urgentCount} items)`)
          : null,
      }),
      Table({
        columns: [
          { key: 'name', label: 'Ingredient', render: (r) => r.name },
          { key: 'status', label: 'Status', render: statusPill },
          { key: 'usable', label: 'Usable stock', render: (r) => h('span', { class: 'mono t-sm' }, qty(r.onHand, r.baseUnit)) },
          { key: 'cover', label: 'Cover', render: (r) => h('span', { class: 'mono t-sm' }, days(r.daysLeft)) },
          {
            key: 'buy',
            label: 'Recommended buy',
            render: (r) => (needsBuying(r)
              ? h('span', { class: 't-sm t-strong' }, r.suggestedOrderQty !== null ? `~${asText.num(r.suggestedOrderQty, '', 2)} ${r.suggestedOrderUnit || r.baseUnit}` : nodata('not enough history'))
              : h('span', { class: 't-xs t-faint' }, '—')),
          },
        ],
        rows: items,
        onRowClick: (r) => select('ingredient', r),
      }));
  }

  function drawNeeds() {
    const visible = needsTab === 'attention' ? urgent : rows;
    if (visible.length === 0) {
      mount(needsHost, Card({}, EmptyState({
        icon: '✓',
        title: needsTab === 'attention' ? 'Nothing needs buying right now' : 'No ingredients yet',
        description: needsTab === 'attention'
          ? (rows.length === 0 ? 'No ingredient has a measured usage rate yet — a data gap, not an all-clear.' : 'Every ingredient has a comfortable buffer against its supplier lead time.')
          : 'Ingredients will appear here once they exist in the catalog.',
        tight: true,
      })));
      return;
    }
    mount(needsHost, ...groupBySupplier(visible).map(supplierGroup));
  }

  // ---------------------------------------------------------------------
  // Below — purchase orders needing processing (Draft + In Progress only;
  // the full status-tabbed history/search stays its own advanced workspace
  // — 'purchasing' route, linked below, per the spec's explicit allowance).
  // ---------------------------------------------------------------------
  function drawPurchaseOrders() {
    const actionable = orders.filter((po) => po.status === 'draft' || po.status === 'ordered' || po.status === 'partially_received')
      .sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || ''));
    mount(poHost, Card({},
      CardHeader({
        title: 'Purchase orders needing processing',
        desc: 'Draft and in-progress orders — completed and cancelled history is in the full workspace',
        right: Button({ size: 'sm', variant: 'ghost', onClick: () => ctx.navigate('purchasing') }, 'Full purchase order history →'),
      }),
      actionable.length === 0
        ? EmptyState({ icon: '✓', title: 'Nothing needs processing', tight: true })
        : Table({
          columns: [
            { key: 'id', label: 'PO', render: (po) => `PO-${po.id}` },
            { key: 'supplier', label: 'Supplier', render: (po) => po.supplier || '—' },
            { key: 'status', label: 'Status', render: (po) => poStatusPill(po.status) },
            { key: 'expected', label: 'Expected', render: (po) => (po.expectedDeliveryDate ? date(po.expectedDeliveryDate) : h('span', { class: 't-xs t-faint' }, '—')) },
            { key: 'items', label: 'Items', render: (po) => count(po.items.length) },
          ],
          rows: actionable,
          onRowClick: (po) => select('po', po),
        })));
  }

  function poStatusPill(status) {
    switch (status) {
      case 'draft': return Pill('flat', 'Draft');
      case 'ordered': return Pill('warn', 'Ordered');
      case 'partially_received': return Pill('warn', 'Partially received');
      case 'received': return Pill('good', 'Received');
      case 'cancelled': return Pill('flat', 'Cancelled');
      default: return Pill('flat', status);
    }
  }

  // ---------------------------------------------------------------------
  // Right — contextual panel: the selected ingredient's supporting
  // calculation, or the selected PO's items and processing actions.
  // ---------------------------------------------------------------------
  function drawRight() {
    if (!selection) {
      mount(rightHost, Card({}, EmptyState({ icon: '☰', title: 'Nothing selected', description: 'Click an ingredient or a purchase order to see details here.', tight: true })));
      return;
    }
    if (selection.kind === 'ingredient') { drawIngredientPanel(selection.ref); return; }
    drawPoPanel(selection.ref);
  }

  function drawerField(label, ...value) {
    return h('div', { class: 'card card-sunk', style: { padding: '10px 12px' } },
      h('div', { class: 't-xs t-faint' }, label),
      h('div', { class: 't-strong', style: { marginTop: '3px' } }, ...value));
  }

  function reasonText(r) {
    if (r.replenishmentMessage) return r.replenishmentMessage;
    if (r.risk === 'out') return 'This ingredient is out of stock right now.';
    if (r.risk === 'critical') return 'Current stock may run out before the next delivery arrives.';
    if (r.belowReorderPoint) return 'Stock has fallen below the level set as safe for this ingredient.';
    if (r.risk === 'at_risk') return 'Current stock may fall below the safe level before the next delivery.';
    if (r.risk === 'idle') return 'No recent usage to measure a rate from — this is a data gap, not a stock issue.';
    if (r.risk === 'excess') return 'Well above the usual level — no action needed.';
    return 'Comfortably covered until the next scheduled delivery.';
  }

  function drawIngredientPanel(r) {
    const buying = needsBuying(r);
    const relatedPOs = orders.filter((po) => po.items.some((it) => it.ingredientId === r.ingredientId) && po.status !== 'cancelled');
    mount(rightHost, Card({},
      CardHeader({ title: r.name, desc: 'Purchase recommendation', right: statusPill(r) }),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '10px', marginBottom: '14px' } },
        drawerField('Usable stock', qty(r.onHand, r.baseUnit)),
        drawerField('Estimated stock cover', days(r.daysLeft)),
        drawerField('Current usage rate', r.usedPerDay === null ? nodata('not measured') : `~${asText.num(r.usedPerDay, '', 2)} ${r.baseUnit}/day`),
        drawerField('Supplier lead time', r.leadTimeDays === null ? nodata('not set') : `${r.leadTimeDays} day${r.leadTimeDays === 1 ? '' : 's'}`),
        r.safetyStockBase !== null ? drawerField('Safety stock', qty(r.safetyStockBase, r.baseUnit)) : null,
        r.packSize ? drawerField('Pack size', qty(r.packSize, r.baseUnit), ` per ${r.purchaseUnit || 'pack'}`) : null,
        r.minimumOrderQty ? drawerField('Minimum order', `${asText.num(r.minimumOrderQty, '', 2)} ${r.suggestedOrderUnit || r.purchaseUnit || 'pack'}`) : null,
        r.incomingOrderedBase > 0 ? drawerField('Incoming confirmed orders', qty(r.incomingOrderedBase, r.baseUnit)) : null),
      h('div', { class: 't-sm t-soft', style: { marginBottom: '8px' } }, reasonText(r)),
      h('div', { class: 't-xs t-faint', style: { marginBottom: '16px' } },
        'Figures are estimates derived from recipes and recorded sales — physical stock can differ due to spills, over-portioning, or spoilage.'),
      buying
        ? h('div', { class: 'card card-sunk' },
          h('div', { class: 'eyebrow' }, 'Recommended'),
          h('div', { style: { fontWeight: '600', marginBottom: '12px' } },
            r.suggestedOrderQty !== null ? `Buy ~${asText.num(r.suggestedOrderQty, '', 2)} ${r.suggestedOrderUnit || r.baseUnit}` : 'Not enough history yet to suggest a quantity'),
          isOwner
            ? Button({ variant: 'primary', size: 'sm', onClick: () => openDraftModal(r) }, 'Draft purchase order')
            : h('div', { style: { display: 'flex', alignItems: 'center', gap: '6px' } }, icon(ICONS.clipboardList, 14), h('span', { class: 't-xs t-soft' }, 'Owner drafts this from their own Purchase Orders view.')))
        : null,
      relatedPOs.length ? h('div', { style: { marginTop: '14px' } },
        h('div', { class: 't-xs t-faint', style: { marginBottom: '6px' } }, 'On an existing purchase order'),
        ...relatedPOs.map((po) => h('div', { class: 'mini-row', style: { cursor: 'pointer' }, onClick: () => select('po', po) },
          h('span', {}, `PO-${po.id}`), poStatusPill(po.status)))) : null,
      h('div', { style: { marginTop: '14px', paddingTop: '14px', borderTop: '1px solid var(--line-soft)' } },
        Button({ size: 'sm', variant: 'ghost', onClick: () => ctx.navigate('forecasts') }, 'View demand forecast evidence →'))));
  }

  function drawPoPanel(po) {
    const canCancel = po.status === 'draft' || po.status === 'ordered';
    const outstanding = po.items.some((it) => (it.receivedQtyBase || 0) < it.quantity);
    mount(rightHost, Card({},
      CardHeader({
        title: `PO-${po.id}`,
        desc: `Supplier: ${po.supplier || '—'} · Created ${stamp(po.createdAt).textContent} by ${po.createdBy || '—'}`,
        right: poStatusPill(po.status),
      }),
      h('div', { class: 'eyebrow', style: { marginBottom: '8px' } }, `Items (${po.items.length})`),
      Table({
        columns: [
          { key: 'ingredient', label: 'Ingredient', render: (it) => it.ingredientName },
          { key: 'qty', label: 'Ordered', render: (it) => `${asText.num(it.quantity, '', 2)} ${it.unit || ''}` },
          { key: 'received', label: 'Received', render: (it) => `${asText.num(it.receivedQtyBase || 0, '', 2)} ${it.baseUnit || ''}` },
        ],
        rows: po.items,
      }),
      po.notes ? h('p', { class: 't-sm t-soft', style: { marginTop: '10px' } }, h('strong', {}, 'Note: '), po.notes) : null,
      po.expectedDeliveryDate ? h('div', { class: 't-xs t-faint', style: { marginTop: '6px' } }, 'Expected delivery: ', date(po.expectedDeliveryDate)) : null,
      h('div', { style: { display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '14px', paddingTop: '14px', borderTop: '1px solid var(--line-soft)' } },
        isAdmin && po.status === 'draft' ? Button({ size: 'sm', onClick: () => doMarkOrdered(po) }, 'Mark ordered') : null,
        isAdmin && (po.status === 'ordered' || po.status === 'partially_received') && outstanding
          ? Button({ size: 'sm', variant: 'primary', onClick: () => openReceiveModal(po) }, 'Receive delivery')
          : null,
        canCancel ? Button({ size: 'sm', variant: 'ghost', onClick: () => doCancel(po) }, 'Cancel order') : null)));
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

  function openDraftModal(r) {
    const qtyInput = h('input', { type: 'number', step: '0.01', min: '0.01', value: r.suggestedOrderQty !== null ? String(r.suggestedOrderQty) : '' });
    const unitLabel = h('span', { class: 't-xs t-faint' }, r.suggestedOrderUnit || r.baseUnit);
    const supplierInput = h('input', { type: 'text', placeholder: 'optional', value: r.supplier || '' });
    const dateInput = h('input', { type: 'date' });
    const noteInput = h('input', { type: 'text', placeholder: 'optional' });
    const err = ModalError();
    const submit = Button({ variant: 'primary' }, 'Create draft');
    submit.addEventListener('click', async () => {
      err.textContent = ''; err.style.display = 'none';
      const quantity = Number(qtyInput.value);
      if (!(quantity > 0)) { err.textContent = 'Enter how much to order — it must be more than zero.'; err.style.display = ''; return; }
      submit.disabled = true;
      try {
        await createPurchaseOrder({
          supplier: supplierInput.value.trim() || null,
          notes: noteInput.value.trim() || null,
          expectedDeliveryDate: dateInput.value || null,
          items: [{ ingredientId: r.ingredientId, quantity, recommendedQtyBase: r.suggestedOrderQty }],
        });
        modal.close();
        toast(`Draft purchase order created for ${r.name}.`, 'good');
        await reload();
      } catch (e) { err.textContent = e.message; err.style.display = ''; } finally { submit.disabled = false; }
    });
    const modal = Modal({
      title: `Draft a purchase order — ${r.name}`,
      desc: 'This does not affect stock or notify a supplier. Admin sees it and marks it ordered when it is placed.',
      onClose: () => {},
    },
    h('label', { class: 'field' }, 'Quantity', h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px' } }, qtyInput, unitLabel)),
    h('label', { class: 'field', style: { marginTop: '10px' } }, 'Supplier', supplierInput),
    h('label', { class: 'field', style: { marginTop: '10px' } }, 'Expected delivery date', dateInput),
    h('label', { class: 'field', style: { marginTop: '10px' } }, 'Note', noteInput),
    err,
    h('div', { style: { marginTop: '14px', display: 'flex', justifyContent: 'flex-end' } }, submit));
  }

  function openGroupDraftModal(supplier, items) {
    const lines = items.map((r) => ({
      r,
      include: h('input', { type: 'checkbox', checked: true }),
      qtyInput: h('input', { type: 'number', step: '0.01', min: '0.01', value: r.suggestedOrderQty !== null ? String(r.suggestedOrderQty) : '' }),
    }));
    const supplierInput = h('input', { type: 'text', placeholder: 'optional', value: supplier === NO_SUPPLIER ? '' : supplier });
    const dateInput = h('input', { type: 'date' });
    const noteInput = h('input', { type: 'text', placeholder: 'optional' });
    const err = ModalError();
    const submit = Button({ variant: 'primary' }, `Create draft (${items.length} items)`);
    const lineRows = lines.map((line) => h('div', {
      style: { display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 0', borderBottom: '1px solid var(--line)' },
    },
    line.include,
    h('span', { class: 't-sm', style: { flex: '1' } }, line.r.name),
    line.qtyInput,
    h('span', { class: 't-xs t-faint' }, line.r.suggestedOrderUnit || line.r.baseUnit)));
    submit.addEventListener('click', async () => {
      err.textContent = ''; err.style.display = 'none';
      const chosen = lines.filter((l) => l.include.checked);
      if (chosen.length === 0) { err.textContent = 'Include at least one ingredient.'; err.style.display = ''; return; }
      const invalid = chosen.find((l) => !(Number(l.qtyInput.value) > 0));
      if (invalid) { err.textContent = `Enter how much to order for ${invalid.r.name} — it must be more than zero.`; err.style.display = ''; return; }
      submit.disabled = true;
      try {
        await createPurchaseOrder({
          supplier: supplierInput.value.trim() || null,
          notes: noteInput.value.trim() || null,
          expectedDeliveryDate: dateInput.value || null,
          items: chosen.map((l) => ({ ingredientId: l.r.ingredientId, quantity: Number(l.qtyInput.value), recommendedQtyBase: l.r.suggestedOrderQty })),
        });
        modal.close();
        toast(`Draft purchase order created for ${supplier} (${chosen.length} items).`, 'good');
        await reload();
      } catch (e) { err.textContent = e.message; err.style.display = ''; } finally { submit.disabled = false; }
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

  drawHealth();
  drawNeeds();
  drawPurchaseOrders();
  drawRight();

  const tabStrip = h('div', { class: 'action-strip', style: { marginBottom: '10px' } },
    ...[['attention', `Needs attention (${urgent.length})`], ['all', `All ingredients (${rows.length})`]].map(([id, label]) => h('button', {
      class: `action-chip${needsTab === id ? ' is-active' : ''}`,
      onClick: () => { needsTab = id; drawNeeds(); },
    }, label)));

  return h('div', { class: 'purchasing-hub' },
    healthHost,
    h('div', { class: 'studio-layout', style: { gridTemplateColumns: '1.6fr 1fr' } },
      h('div', { class: 'studio-col' }, tabStrip, needsHost),
      h('div', { class: 'studio-col' }, rightHost)),
    h('div', { style: { marginTop: '14px' } }, poHost));
}
