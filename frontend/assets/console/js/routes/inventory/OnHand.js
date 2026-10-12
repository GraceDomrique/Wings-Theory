import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError } from '../../components/ui/Modal.js';
import { toast } from '../../components/ui/Feedback.js';
import { count, nodata } from '../../lib/format.js';
import { listIngredients, createIngredient, bulkCount } from '../../lib/api/inventory.js';
import { getValuation } from '../../lib/api/reports.js';
import { openIngredientDrawer } from '../../components/domain/IngredientDrawer.js';
import {
  nameCell, onHandCell, runwayCell, outlookCell, statusCell,
} from '../../components/domain/IngredientRow.js';

/**
 * Inventory > On Hand — the full ingredient record.
 *
 * Rows are server-ordered by name. Sorting is offered on the columns where
 * re-ordering is harmless (name, runway); it is not offered on anything whose
 * order carries meaning.
 *
 * Row click opens the ingredient's workflow drawer (Receive / Count / Waste /
 * Use / Edit) — restored from the old console, where this table's whole
 * purpose was to get a manager to the right ingredient fast so they could act
 * on it, not just read it.
 */
export async function render(ctx) {
  let ingredients = [];
  let valuation = { available: false, reason: '' };
  let lastQuery = '';
  const tableHost = h('div');
  const statsHost = h('div');

  async function reload() {
    [ingredients, valuation] = await Promise.all([
      listIngredients({ limit: 500 }),
      getValuation(),
    ]);
    drawStats();
    drawTable(lastQuery);
  }

  function drawStats() {
    const confirmed = ingredients.filter((i) => i.confidence === 'confirmed').length;
    const unconfirmed = ingredients.length - confirmed;
    statsHost.classList.add('grid');
    statsHost.style.gridTemplateColumns = 'repeat(4,1fr)';
    mount(statsHost,
      Card({}, Stat({ value: count(ingredients.length), label: 'Ingredients tracked' })),
      Card({}, Stat({ value: count(confirmed), label: 'Confirmed & stocked' })),
      Card({}, Stat({ value: count(unconfirmed), label: 'Needs confirming' })),
      Card({}, Stat({
        // No valuation endpoint exists yet, and most costs are unconfirmed —
        // so this reports that it cannot be valued rather than summing the
        // subset that happens to have a price and calling it the total.
        value: valuation.available ? count(valuation.totalValue) : nodata('not available'),
        label: 'Valued on hand',
      }), !valuation.available
        ? h('div', { class: 't-xs t-faint', style: { marginTop: '6px' } }, valuation.reason)
        : null));
  }

  function drawTable(query) {
    lastQuery = query || '';
    const q = lastQuery.trim().toLowerCase();
    const rows = q
      ? ingredients.filter((i) => i.name.toLowerCase().includes(q)
        || String(i.id).toLowerCase().includes(q))
      : ingredients;

    mount(tableHost, Table({
      columns: [
        { key: 'name', label: 'Ingredient', render: nameCell, sortValue: (i) => i.name.toLowerCase() },
        { key: 'category', label: 'Category', render: (i) => i.category, sortValue: (i) => i.category },
        { key: 'onhand', label: 'On hand', render: onHandCell },
        {
          key: 'runway',
          label: 'Runway',
          render: runwayCell,
          // Unknown runway sorts last in both directions (see Table) — an
          // ingredient with no measured usage is not the most urgent one.
          sortValue: (i) => i.daysLeft,
        },
        { key: 'outlook', label: 'Outlook', render: outlookCell },
        { key: 'status', label: 'Status', render: statusCell },
      ],
      rows,
      onRowClick: (ing) => openIngredientDrawer({ pk: ing.pk, onChanged: reload }),
      empty: EmptyState({
        icon: '⌕',
        title: q ? `Nothing matches “${query}”` : 'No ingredients yet',
        description: q
          ? 'Try part of the ingredient name, or its ING- code.'
          : 'Ingredients appear here once the inventory list has been imported.',
      }),
    }));
  }

  await reload();
  ctx.setSearchHandler(drawTable);

  return h('div', {},
    h('div', { style: { marginBottom: '14px' } }, statsHost),
    Card({},
      CardHeader({
        title: 'Full record',
        desc: 'Click a row to receive, count, log wastage, log use, or edit it',
        right: [
          Button({ size: 'sm', onClick: () => openBulkCountModal(ingredients, reload) }, 'Bulk update'),
          Button({ variant: 'primary', size: 'sm', onClick: () => openAddIngredientModal(reload) }, '+ Add ingredient'),
        ],
      }),
      tableHost));
}

/**
 * POST /console/ingredients — creates a real ingredient row with a fresh
 * ING-#### id. Deliberately minimal: only the fields the endpoint actually
 * requires up front (name, category, units); cost, supplier, reorder point
 * and expiry tracking are edited afterward from the ingredient's own drawer,
 * exactly where the old console put them too.
 */
function openAddIngredientModal(onDone) {
  const nameInput = h('input', { type: 'text', placeholder: 'e.g. Fresh Basil' });
  const categorySelect = h('select', {},
    h('option', { value: 'wet_food' }, 'Wet food'),
    h('option', { value: 'dry_food' }, 'Dry food'),
    h('option', { value: 'packaging' }, 'Operational supply'),
    h('option', { value: 'consumable' }, 'Consumable'),
    h('option', { value: 'resale' }, 'Resale item'));
  const baseUnitSelect = h('select', {},
    h('option', { value: 'g' }, 'grams (g)'),
    h('option', { value: 'ml' }, 'millilitres (ml)'),
    h('option', { value: 'pc' }, 'pieces (pc)'));
  const purchaseUnitInput = h('input', { type: 'text', placeholder: 'e.g. pack, bottle, kg' });
  const err = ModalError();
  const submit = Button({ variant: 'primary' }, 'Create ingredient');

  submit.addEventListener('click', async () => {
    err.textContent = ''; err.style.display = 'none';
    if (!nameInput.value.trim()) { err.textContent = 'Enter a name.'; err.style.display = ''; return; }
    if (!purchaseUnitInput.value.trim()) { err.textContent = 'Enter what it is purchased in.'; err.style.display = ''; return; }
    submit.disabled = true;
    try {
      const r = await createIngredient({
        name: nameInput.value.trim(),
        category: categorySelect.value,
        base_unit: baseUnitSelect.value,
        purchase_unit: purchaseUnitInput.value.trim(),
      });
      modal.close();
      toast(`${nameInput.value.trim()} created as ${r.ingredient_id}. Open it to set cost, pack size and reorder point.`, 'good');
      onDone();
    } catch (e) {
      err.textContent = e.message; err.style.display = ''; submit.disabled = false;
    }
  });

  const modal = Modal({
    title: 'Add ingredient',
    desc: 'Cost, pack size, supplier and reorder point are set afterward, from its own drawer.',
    onClose: () => {},
  },
  h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Name', nameInput),
  h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Category', categorySelect),
  h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Base unit (how stock is held internally)', baseUnitSelect),
  h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Purchase unit (what a delivery is counted in)', purchaseUnitInput),
  err,
  h('div', { style: { marginTop: '10px' } }, submit));
}

/**
 * POST /console/ingredients/bulk-count — a full physical stock take in one
 * submission. Each line still goes through record_count individually
 * server-side, so a bulk update carries exactly the same audit trail as
 * doing it one ingredient at a time — just faster to enter. One bad line
 * does not abort the rest; the endpoint reports failures back per line.
 */
function openBulkCountModal(ingredients, onDone) {
  const rows = ingredients.map((ing) => {
    const input = h('input', { type: 'number', step: '0.01', min: '0', placeholder: 'unchanged' });
    return { ing, input };
  });

  const search = h('input', { type: 'text', placeholder: 'Filter ingredients…', style: { marginBottom: '10px' } });
  const noteInput = h('input', { type: 'text', placeholder: 'e.g. weekly stock take (optional)' });
  const err = ModalError();
  const submit = Button({ variant: 'primary' }, 'Submit counts');
  const listHost = h('div', {
    style: { maxHeight: '360px', overflowY: 'auto', border: '1px solid var(--line)', borderRadius: '8px' },
  });

  function drawRows(query) {
    const q = (query || '').trim().toLowerCase();
    const visible = rows.filter((r) => !q || r.ing.name.toLowerCase().includes(q));
    mount(listHost, ...visible.map((r) => h('div', {
      style: {
        display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 12px',
        borderBottom: '1px solid var(--line-soft)',
      },
    },
    h('span', { style: { flex: '1', fontSize: '12.5px' } }, r.ing.name,
      h('span', { class: 't-xs t-faint', style: { marginLeft: '6px' } }, `(${r.ing.baseUnit})`)),
    r.input)));
  }
  drawRows('');
  search.addEventListener('input', () => drawRows(search.value));

  submit.addEventListener('click', async () => {
    err.textContent = ''; err.style.display = 'none';
    const items = rows
      .filter((r) => r.input.value !== '')
      .map((r) => ({ pk: r.ing.pk, actualBase: Number(r.input.value) }));
    if (items.length === 0) { err.textContent = 'Enter at least one counted quantity.'; err.style.display = ''; return; }
    submit.disabled = true;
    try {
      const r = await bulkCount(items, noteInput.value.trim() || null);
      modal.close();
      toast(`Bulk count submitted — ${r.updated} updated${r.failed ? `, ${r.failed} failed` : ''}.`,
        r.failed ? 'warn' : 'good');
      onDone();
    } catch (e) {
      err.textContent = e.message; err.style.display = ''; submit.disabled = false;
    }
  });

  const modal = Modal({
    title: 'Bulk stock count',
    desc: 'Enter a counted quantity only for the ingredients you actually counted — everything else is left untouched.',
    wide: true,
    onClose: () => {},
  },
  search,
  listHost,
  h('label', { class: 'field', style: { margin: '12px 0 10px', display: 'block' } }, 'Note (optional)', noteInput),
  err,
  submit);
}
