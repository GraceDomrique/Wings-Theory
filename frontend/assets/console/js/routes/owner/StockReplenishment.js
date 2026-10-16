import { h, mount } from '../../lib/dom.js';
import { Card } from '../../components/ui/Card.js';
import { StatTile } from '../../components/ui/StatTile.js';
import { Button } from '../../components/ui/Button.js';
import { Pill } from '../../components/ui/Pill.js';
import { Table } from '../../components/ui/Table.js';
import { Modal, ModalError } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Tabbar } from '../../components/ui/Tabbar.js';
import { toast } from '../../components/ui/Feedback.js';
import {
  qty, days, date, count, asText, nodata,
} from '../../lib/format.js';
import { severityFromDepletionStatus } from '../../lib/severity.js';
import { getReplenishment } from '../../lib/api/intelligence.js';
import { getExpiry } from '../../lib/api/expiry.js';
import { createPurchaseOrder, listPurchaseOrders } from '../../lib/api/purchaseOrders.js';
import { session } from '../../lib/api/client.js';

/**
 * Owner > Stock & Replenishment — the one page that answers "what should we
 * buy, and what's about to go bad" together, plus the two mandatory
 * downloadable reports. Three tabs:
 *   Needs Purchasing — the exact WhatToBuy.js recommendation-card pattern
 *     (grouped by supplier, same draft-PO flow via createPurchaseOrder),
 *     with a per-ingredient checkbox added for the "Selected ingredients"
 *     report scope.
 *   All Ingredients  — read-only table of getReplenishment()'s full rows.
 *   Expiring Soon     — read-only table of getExpiry()'s batches, already
 *     sorted expired-first/soonest-first by the backend.
 *
 * Both "Generate & download" buttons call real server-side endpoints
 * (GET /console/reports/purchasing-list, GET /console/reports/expiry —
 * wt/reports_export.py) that re-run the exact same business logic this
 * page's own tabs read, so a downloaded report can never disagree with
 * what's on screen. Nothing on this page is fabricated: a figure the
 * backend hasn't supplied renders via nodata()/Pill('flat', ...), never a
 * guessed number.
 */
const NO_SUPPLIER = 'No supplier assigned';
const needsBuying = (r) => r.status === 'crit' || r.status === 'warn' || r.belowReorderPoint;

const ICON = {
  needsPurchase: '<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  runningLow: '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
  expiring: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
  incoming: '<path d="M6 2l1.5 5h9L18 2"/><path d="M3 7h18l-1.7 10.2A2 2 0 0117.3 19H6.7a2 2 0 01-2-1.8L3 7z"/><circle cx="9" cy="22" r="1"/><circle cx="16" cy="22" r="1"/>',
};

export async function render(ctx) {
  const isOwner = (session.user || {}).role === 'owner';

  const [replenishment, expiry, pos] = await Promise.all([
    getReplenishment(),
    getExpiry(365), // wide window — used for both the stat tiles and the
    // per-ingredient expiry cross-reference on the All Ingredients tab, not
    // just the nominal "soon" bucket.
    listPurchaseOrders().catch(() => ({ items: [] })),
  ]);

  const rows = replenishment.rows;
  const urgent = rows.filter(needsBuying);
  const criticalCount = rows.filter((r) => r.status === 'crit').length;
  const runningLowCount = rows.filter((r) => r.status === 'warn').length;
  const expiringSoonCount = expiry.buckets.expired.length + expiry.buckets.soon.length;
  const incomingItemCount = (pos.items || [])
    .filter((po) => po.status === 'ordered' || po.status === 'partially_received')
    .reduce((sum, po) => sum + (po.items || []).length, 0);

  // Ingredient ids checked on the Needs Purchasing tab, for the purchasing
  // report's "Selected ingredients" scope. Lives for the lifetime of this
  // render() call only, same as every other piece of this page's state.
  const selected = new Set();

  let activeTab = ['needsbuying', 'all', 'expiring'].includes(ctx.tab) ? ctx.tab : 'needsbuying';
  const body = h('div');
  const tabbar = Tabbar({
    items: tabItems(),
    activeId: activeTab,
    onChange: (id) => { activeTab = id; draw(); },
  });

  function tabItems() {
    return [
      { id: 'needsbuying', label: `Needs purchasing${urgent.length ? ` (${urgent.length})` : ''}` },
      { id: 'all', label: `All ingredients (${rows.length})` },
      { id: 'expiring', label: `Expiring soon (${expiry.batches.length})` },
    ];
  }

  function draw() {
    mount(body, activeTab === 'all' ? allIngredientsTab()
      : activeTab === 'expiring' ? expiringSoonTab()
        : needsPurchasingTab());
  }

  // =========================================================================
  // Needs Purchasing — WhatToBuy.js's own pattern, reused rather than
  // reimplemented, plus a selection checkbox for the report scope.
  // =========================================================================

  function needsPurchasingTab() {
    if (urgent.length === 0) {
      return Card({}, EmptyState({
        icon: '✓',
        title: 'Nothing needs buying right now',
        description: rows.length === 0
          ? 'No ingredient has a measured usage rate yet, so there is nothing to recommend buying. This is a data gap, not an all-clear.'
          : 'Every ingredient with a measured usage rate has a comfortable buffer against its supplier lead time.',
        tight: true,
      }));
    }
    return h('div', {}, ...groupBySupplier(urgent).map(supplierGroup));
  }

  function groupBySupplier(list) {
    const bySupplier = new Map();
    for (const r of list) {
      const key = r.supplier || NO_SUPPLIER;
      if (!bySupplier.has(key)) bySupplier.set(key, []);
      bySupplier.get(key).push(r);
    }
    return [...bySupplier.entries()]
      .map(([supplier, items]) => ({ supplier, items }))
      .sort((a, b) => {
        if (a.supplier === NO_SUPPLIER) return 1;
        if (b.supplier === NO_SUPPLIER) return -1;
        return a.supplier.localeCompare(b.supplier);
      });
  }

  function supplierGroup({ supplier, items }) {
    return h('div', { style: { marginBottom: '22px' } },
      h('div', {
        style: {
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          gap: '10px', margin: '0 0 8px',
        },
      },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
        h('span', { class: 'eyebrow' }, supplier),
        Pill('flat', `${items.length} to buy`)),
      items.length > 1 && isOwner
        ? Button({
          variant: 'default', size: 'sm',
          onClick: () => openGroupDraftModal(supplier, items),
        }, `Draft purchase order (${items.length} items)`)
        : null),
      ...items.map(recommendationCard));
  }

  function recommendationCard(r) {
    // Was a bare checkbox dropped into the title row with no label, easy to
    // mistake for a stray UI bug. It's real — it scopes the downloadable
    // purchasing report's "Selected ingredients" option, below — so it
    // moves to the footer, next to the recommendation it actually
    // qualifies, as a labeled toggle rather than an unexplained box beside
    // the ingredient's name.
    const includeToggle = h('input', {
      type: 'checkbox',
      checked: selected.has(r.ingredientId),
      onChange: (e) => {
        if (e.target.checked) selected.add(r.ingredientId);
        else selected.delete(r.ingredientId);
      },
    });
    return Card({ style: { marginBottom: '14px' } },
      h('div', { class: 'card-head' },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: '10px' } },
          h('span', { class: 'card-title' }, r.name),
          statusPill(r)),
        isOwner
          ? Button({ variant: 'primary', size: 'sm', onClick: () => openDraftModal(r) }, 'Draft purchase order')
          : Pill('flat', 'Owner drafts this')),
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
      h('div', {
        style: {
          display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px',
          marginTop: '12px', paddingTop: '12px', borderTop: '1px solid var(--line)',
        },
      },
      h('div', {},
        h('div', { class: 'eyebrow' }, 'Recommended'),
        h('div', { style: { fontWeight: '600' } },
          r.suggestedOrderQty !== null
            ? `Buy ~${asText.num(r.suggestedOrderQty, '', 2)} ${r.suggestedOrderUnit || r.baseUnit}`
            : 'Not enough history yet to suggest a quantity')),
      h('label', { class: 't-xs t-soft', style: { display: 'flex', alignItems: 'center', gap: '6px', flexShrink: '0' } },
        includeToggle, 'Include in report')));
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

  // =========================================================================
  // All Ingredients — read-only
  // =========================================================================

  function allIngredientsTab() {
    return Card({},
      Table({
        columns: [
          { key: 'name', label: 'Ingredient', render: (r) => r.name, sortValue: (r) => r.name },
          {
            key: 'category', label: 'Category',
            render: (r) => (r.category ? r.category : nodata('not recorded')),
            sortValue: (r) => r.category || '',
          },
          {
            key: 'onHand', label: 'Usable On Hand', align: 'right',
            render: (r) => qty(r.onHand, r.baseUnit), sortValue: (r) => r.onHand,
          },
          {
            key: 'daysLeft', label: 'Expected Runway', align: 'right',
            render: (r) => days(r.daysLeft), sortValue: (r) => r.daysLeft,
          },
          { key: 'status', label: 'Replenishment Status', render: (r) => statusPill(r) },
          { key: 'expiry', label: 'Expiry Status', render: (r) => expiryStatusForIngredient(r.ingredientId) },
          {
            key: 'supplier', label: 'Supplier',
            render: (r) => r.supplier || nodata('not recorded'),
            sortValue: (r) => r.supplier || '',
          },
        ],
        rows,
        onRowClick: (r) => openDetailModal(r),
      }));
  }

  // Worst (soonest) batch for this ingredient within the already-fetched
  // 365-day expiry window. No open batch at all is a distinct, honest state
  // from "healthy" — never shown as a green/good pill.
  function expiryStatusForIngredient(ingredientId) {
    const batches = expiry.batches.filter((b) => b.ingredientPk === ingredientId);
    if (batches.length === 0) return Pill('flat', 'no batches tracked');
    const worst = batches.slice().sort((a, b) => {
      const da = a.daysToExpiry === null ? Infinity : a.daysToExpiry;
      const db = b.daysToExpiry === null ? Infinity : b.daysToExpiry;
      return da - db;
    })[0];
    return expiryBatchPill(worst);
  }

  function expiryBatchPill(b) {
    if (b.expirySource === 'none' || b.daysToExpiry === null) return Pill('flat', 'Expiry not recorded');
    if (b.expired) return Pill('crit', 'Expired');
    if (b.daysToExpiry <= 7) return Pill('warn', 'Expiring soon');
    return Pill('good', 'OK');
  }

  function openDetailModal(r) {
    Modal({
      title: r.name,
      desc: r.supplier ? `Supplier: ${r.supplier}` : 'No supplier recorded',
      onClose: () => {},
    },
    detailField('On hand', qty(r.onHand, r.baseUnit)),
    detailField('Usage rate', r.usedPerDay === null ? nodata('not measured') : `~${asText.num(r.usedPerDay, '', 2)} ${r.baseUnit}/day`),
    detailField('Stock cover', days(r.daysLeft)),
    detailField('Lead time', r.leadTimeDays === null ? nodata('not set') : `${r.leadTimeDays} day${r.leadTimeDays === 1 ? '' : 's'}`),
    detailField('Safety stock', r.safetyStockBase !== null ? qty(r.safetyStockBase, r.baseUnit) : nodata('not set')),
    detailField('Reorder point', r.reorderPoint !== null ? qty(r.reorderPoint, r.baseUnit) : nodata('not set')),
    detailField('Replenishment status', statusPill(r)),
    detailField('Expiry', expiryStatusForIngredient(r.ingredientId)),
    h('div', { class: 't-sm t-soft', style: { marginTop: '12px' } }, reasonText(r)));
  }

  function detailField(label, ...value) {
    return h('div', {
      style: {
        display: 'flex', justifyContent: 'space-between', gap: '10px',
        padding: '7px 0', borderBottom: '1px solid var(--line-soft)',
      },
    }, h('span', { class: 't-sm t-soft' }, label), h('span', { class: 't-sm', style: { fontWeight: '600' } }, ...value));
  }

  // =========================================================================
  // Expiring Soon — read-only; batches already arrive expired-first,
  // soonest-first (wt/api.py's expiry_monitor sorts by days_to_expiry).
  // =========================================================================

  function expiringSoonTab() {
    return Card({},
      Table({
        columns: [
          { key: 'ingredientName', label: 'Ingredient', render: (b) => b.ingredientName, sortValue: (b) => b.ingredientName },
          { key: 'batchId', label: 'Batch / Lot', render: (b) => b.batchId },
          {
            key: 'qtyRemaining', label: 'Remaining Quantity', align: 'right',
            render: (b) => qty(b.qtyRemaining, b.baseUnit), sortValue: (b) => b.qtyRemaining,
          },
          {
            key: 'expiryDate', label: 'Expiry Date',
            render: (b) => date(b.expiryDate), sortValue: (b) => b.expiryDate || '',
          },
          {
            key: 'daysToExpiry', label: 'Days Until Expiry', align: 'right',
            render: (b) => days(b.daysToExpiry), sortValue: (b) => b.daysToExpiry,
          },
          { key: 'status', label: 'Status', render: (b) => expiryBatchPill(b) },
        ],
        rows: expiry.batches,
        empty: Card({ flush: true }, EmptyState({
          icon: '✓', title: 'Nothing expiring soon',
          description: 'No open batch is within this window of its expiry date.',
          tight: true,
        })),
      }));
  }

  // =========================================================================
  // Report downloads — real server-generated files (wt/reports_export.py).
  // The frontend only ever sends scope/filter/format; every row is a fresh
  // query at request time.
  // =========================================================================

  async function downloadReport(path, params) {
    const qs = '?' + new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''),
    ).toString();
    const headers = {};
    if (session.token) headers.Authorization = `Bearer ${session.token}`;
    const res = await fetch(path + qs, { headers });
    if (!res.ok) {
      let msg = `Request failed (${res.status}).`;
      try {
        const body = await res.json();
        if (typeof body.message === 'string') msg = body.message;
        else if (typeof body.detail === 'string') msg = body.detail;
      } catch { /* a binary/empty error body has no JSON to read */ }
      throw new Error(msg);
    }
    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = /filename="([^"]+)"/.exec(disposition);
    const filename = match ? match[1] : 'report';
    const a = h('a', { href: URL.createObjectURL(blob), download: filename });
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function openPurchasingReportModal() {
    const scopeSelect = h('select', {},
      h('option', { value: 'needs_purchasing' }, 'Needs purchasing (recommended today)'),
      h('option', { value: 'urgent_only' }, 'Urgent items only (out of stock / buy now)'),
      h('option', { value: 'selected', disabled: selected.size === 0 }, `Selected ingredients (${selected.size} checked)`),
      h('option', { value: 'all' }, 'All ingredients'));
    const groupCheckbox = h('input', { type: 'checkbox', checked: true });
    const formatSelect = h('select', {},
      h('option', { value: 'pdf' }, 'PDF'),
      h('option', { value: 'csv' }, 'CSV'),
      h('option', { value: 'xlsx' }, 'Excel (.xlsx)'));
    const costsCheckbox = h('input', { type: 'checkbox', checked: true });
    const err = ModalError();
    const submit = Button({ variant: 'primary' }, 'Generate & download');

    submit.addEventListener('click', async () => {
      err.textContent = ''; err.style.display = 'none';
      if (scopeSelect.value === 'selected' && selected.size === 0) {
        err.textContent = 'Check at least one ingredient on the Needs Purchasing tab first.';
        err.style.display = '';
        return;
      }
      submit.disabled = true;
      try {
        await downloadReport('/console/reports/purchasing-list', {
          scope: scopeSelect.value,
          ingredient_ids: scopeSelect.value === 'selected' ? [...selected].join(',') : undefined,
          group_by_supplier: groupCheckbox.checked,
          format: formatSelect.value,
          include_costs: costsCheckbox.checked,
        });
        modal.close();
        toast('Purchasing list downloaded.', 'good');
      } catch (e) {
        err.textContent = e.message; err.style.display = '';
      } finally {
        submit.disabled = false;
      }
    });

    const modal = Modal({
      title: 'Generate purchasing list',
      desc: 'Built fresh from the same stock and forecast data this page reads — never from what happens to be on screen.',
      onClose: () => {},
    },
    h('label', { class: 'field' }, 'Scope', scopeSelect),
    h('label', {
      class: 'field', style: { marginTop: '10px', display: 'flex', alignItems: 'center', gap: '8px' },
    }, groupCheckbox, 'Group by supplier (PDF only)'),
    h('label', { class: 'field', style: { marginTop: '10px' } }, 'Format', formatSelect),
    h('label', {
      class: 'field', style: { marginTop: '10px', display: 'flex', alignItems: 'center', gap: '8px' },
    }, costsCheckbox, 'Include estimated cost columns'),
    err,
    h('div', { style: { marginTop: '14px', display: 'flex', justifyContent: 'flex-end' } }, submit));
  }

  function openExpiryReportModal() {
    const filterSelect = h('select', {},
      h('option', { value: 'within_7' }, 'Expiring within 7 days'),
      h('option', { value: 'expired_only' }, 'Expired only'),
      h('option', { value: 'within_3' }, 'Expiring within 3 days'),
      h('option', { value: 'within_14' }, 'Expiring within 14 days'),
      h('option', { value: 'within_30' }, 'Expiring within 30 days'),
      h('option', { value: 'custom' }, 'Custom date range'));
    const fromInput = h('input', { type: 'date' });
    const toInput = h('input', { type: 'date' });
    const customRow = h('div', { style: { display: 'none', gap: '10px', marginTop: '10px' } },
      h('label', { class: 'field', style: { flex: '1' } }, 'From', fromInput),
      h('label', { class: 'field', style: { flex: '1' } }, 'To', toInput));
    filterSelect.addEventListener('change', () => {
      customRow.style.display = filterSelect.value === 'custom' ? 'flex' : 'none';
    });
    const missingCheckbox = h('input', { type: 'checkbox' });
    const formatSelect = h('select', {},
      h('option', { value: 'pdf' }, 'PDF'),
      h('option', { value: 'csv' }, 'CSV'),
      h('option', { value: 'xlsx' }, 'Excel (.xlsx)'));
    const err = ModalError();
    const submit = Button({ variant: 'primary' }, 'Generate & download');

    submit.addEventListener('click', async () => {
      err.textContent = ''; err.style.display = 'none';
      if (filterSelect.value === 'custom' && (!fromInput.value || !toInput.value)) {
        err.textContent = 'Enter both a start and end date for a custom range.';
        err.style.display = '';
        return;
      }
      submit.disabled = true;
      try {
        await downloadReport('/console/reports/expiry', {
          filter: filterSelect.value,
          date_from: filterSelect.value === 'custom' ? fromInput.value : undefined,
          date_to: filterSelect.value === 'custom' ? toInput.value : undefined,
          include_missing_expiry: missingCheckbox.checked,
          format: formatSelect.value,
        });
        modal.close();
        toast('Expiry report downloaded.', 'good');
      } catch (e) {
        err.textContent = e.message; err.style.display = '';
      } finally {
        submit.disabled = false;
      }
    });

    const modal = Modal({
      title: 'Generate expiry report',
      desc: 'Does not change stock — discarding expired stock still requires an Admin action.',
      onClose: () => {},
    },
    h('label', { class: 'field' }, 'Filter', filterSelect),
    customRow,
    h('label', {
      class: 'field', style: { marginTop: '10px', display: 'flex', alignItems: 'center', gap: '8px' },
    }, missingCheckbox, 'Include batches with no recorded expiry'),
    h('label', { class: 'field', style: { marginTop: '10px' } }, 'Format', formatSelect),
    err,
    h('div', { style: { marginTop: '14px', display: 'flex', justifyContent: 'flex-end' } }, submit));
  }

  draw();

  return h('div', {},
    h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: '10px', marginBottom: '14px', flexWrap: 'wrap' } },
      Button({ variant: 'primary', onClick: openPurchasingReportModal }, 'Generate Purchasing List'),
      Button({ variant: 'default', onClick: openExpiryReportModal }, 'Generate Expiry Report')),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', marginBottom: '14px' } },
      StatTile({
        iconPath: ICON.needsPurchase, severity: criticalCount > 0 ? 'crit' : 'good',
        value: count(criticalCount), label: 'Need Purchasing',
      }),
      StatTile({
        iconPath: ICON.runningLow, severity: runningLowCount > 0 ? 'warn' : 'good',
        value: count(runningLowCount), label: 'Running Low',
      }),
      StatTile({
        iconPath: ICON.expiring, severity: expiringSoonCount > 0 ? 'warn' : 'good',
        value: count(expiringSoonCount), label: 'Expiring Soon',
      }),
      StatTile({
        iconPath: ICON.incoming, severity: 'info',
        value: count(incomingItemCount), label: 'Incoming Orders',
      })),
    h('div', { style: { marginBottom: '12px' } }, tabbar),
    body);
}
