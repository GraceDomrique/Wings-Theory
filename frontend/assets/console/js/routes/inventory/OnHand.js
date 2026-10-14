import { h, mount, icon } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { RailCard } from '../../components/ui/RailCard.js';
import { Stat } from '../../components/ui/Stat.js';
import { Pill } from '../../components/ui/Pill.js';
import { Table } from '../../components/ui/Table.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError } from '../../components/ui/Modal.js';
import { toast } from '../../components/ui/Feedback.js';
import {
  count, moneyShort, qty,
} from '../../lib/format.js';
import { listIngredients, createIngredient, bulkCount } from '../../lib/api/inventory.js';
import { getValuation } from '../../lib/api/reports.js';
import { getExpiry } from '../../lib/api/expiry.js';
import { openIngredientDrawer } from '../../components/domain/IngredientDrawer.js';
import { severityFromDays, severityFromRisk } from '../../lib/severity.js';
import { ICONS } from '../../lib/icons.js';
import {
  nameCell, outlookCell, statusCell,
} from '../../components/domain/IngredientRow.js';

const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'low', label: 'Low stock' },
  { id: 'expiring', label: 'Expiring' },
  { id: 'healthy', label: 'Healthy' },
];

/**
 * Inventory — the single primary workspace for "what do I have, what needs
 * attention, what do I do about it" (2026-10-14 rebuild). Previously this
 * page sat behind a 7-item sidebar accordion (On Hand/Receiving/Expiry/
 * Physical Stock Count/Losses & Variance/Unlimited Wings/Movements) that an
 * employee had to already know how to navigate. Those pages are untouched —
 * same routes, same components, same endpoints — but they are no longer
 * separate destinations to find; they're reached FROM here (Quick Actions,
 * "Needs attention", a table row), which is where the question that sends
 * someone looking for them actually starts.
 *
 * Every number on this page is read straight from the existing endpoints
 * (listIngredients, getExpiry, getValuation) — nothing here is a second,
 * parallel inventory mechanism.
 */
export async function render(ctx) {
  let ingredients = [];
  let valuation = { available: false, reason: '' };
  let expiry = { batches: [], buckets: { expired: [], soon: [], later: [] } };
  let lastQuery = '';
  let activeFilter = 'all';
  const tableHost = h('div');
  const statsHost = h('div');
  const attentionHost = h('div');
  const filterHost = h('div');
  const tableCard = h('div');

  async function reload() {
    const [ingredientsR, valuationR, expiryR] = await Promise.all([
      listIngredients({ limit: 500 }),
      getValuation(),
      getExpiry(90),
    ]);
    ingredients = ingredientsR;
    valuation = valuationR;
    expiry = expiryR;
    drawStats();
    drawAttention();
    drawFilters();
    drawTable(lastQuery);
    if (ctx.refreshBadges) ctx.refreshBadges();
  }

  function lowStockIngredients() {
    return ingredients.filter((i) => {
      const sev = i.risk ? severityFromRisk(i.risk) : severityFromDays(i.daysLeft);
      return sev === 'crit' || sev === 'warn';
    });
  }

  /** ingredient pk -> its single nearest-expiring open batch, for the table's
   * "Nearest expiry" column — one relevant date per row, not every batch. */
  function nearestExpiryByIngredient() {
    const map = new Map();
    for (const b of expiry.batches) {
      if (b.daysToExpiry === null) continue;
      const cur = map.get(b.ingredientPk);
      if (!cur || b.daysToExpiry < cur.daysToExpiry) map.set(b.ingredientPk, b);
    }
    return map;
  }

  function drawStats() {
    const low = lowStockIngredients();
    statsHost.replaceChildren();
    mount(statsHost,
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', gap: '10px' } },
        // Neutral Information Card — a plain count, not a status; no tint.
        Card({ className: 'kpi-neutral' }, Stat({ value: count(ingredients.length), label: 'Ingredients tracked' })),
        // Status Cards — tinted by severity, each with its own icon so the
        // signal survives in text + icon, not color alone.
        RailCard({ severity: low.length ? 'warn' : 'good', icon: ICONS.packageMinus },
          Stat({ value: count(low.length), label: 'Low or need restock' })),
        RailCard({ severity: expiry.buckets.soon.length ? 'warn' : 'good', icon: ICONS.calendarClock },
          Stat({ value: count(expiry.buckets.soon.length), label: 'Expiring soon' })),
        RailCard({ severity: expiry.buckets.expired.length ? 'crit' : 'good', icon: ICONS.alertTriangle },
          Stat({ value: count(expiry.buckets.expired.length), label: 'Expired batches' }))),
      // A one-line figure, not another KPI tile — valuation duplicates
      // nothing above and carries two real caveats a bare number can't say:
      // only ingredients with a CONFIRMED cost per unit are counted
      // (reservation.inventory_valuation), and the total is TOTAL RECORDED
      // stock (on_hand_base) — it does NOT subtract expired quantity the way
      // "Usable" elsewhere on this page does. Labelled explicitly so this
      // number is never read as "what's actually usable, priced."
      h('div', { class: 't-xs t-faint', style: { marginTop: '10px' } },
        'Total recorded stock value: ',
        h('strong', {}, valuation.available ? moneyShort(valuation.totalValue).textContent : 'not available'),
        valuation.available ? ' · confirmed unit costs only, includes any expired quantity' : ` · ${valuation.reason}`));
  }

  /** "Needs attention" — two summary rows, not a repeated item-by-item list
   * (that's what the filtered table below is for). Clicking one filters the
   * table and scrolls it into view, so the number and the rows it describes
   * are never two different places to reconcile. */
  function drawAttention() {
    const expiredCount = expiry.buckets.expired.length;
    const lowCount = lowStockIngredients().length;

    if (expiredCount === 0 && lowCount === 0) {
      mount(attentionHost);
      return;
    }

    const rows = [];
    if (expiredCount > 0) {
      rows.push(attentionSummaryRow({
        iconPaths: ICONS.alertTriangle,
        severity: 'crit',
        title: 'Expired ingredients',
        sub: `${expiredCount} batch${expiredCount === 1 ? '' : 'es'} need checking`,
        onClick: () => { activeFilter = 'expiring'; drawFilters(); drawTable(lastQuery); scrollToTable(); },
      }));
    }
    if (lowCount > 0) {
      rows.push(attentionSummaryRow({
        iconPaths: ICONS.packageMinus,
        severity: 'warn',
        title: 'Low stock',
        sub: `${lowCount} ingredient${lowCount === 1 ? '' : 's'} need reviewing`,
        onClick: () => { activeFilter = 'low'; drawFilters(); drawTable(lastQuery); scrollToTable(); },
      }));
    }

    mount(attentionHost, h('div', { class: 'attn-card' },
      h('div', { class: 'eyebrow', style: { marginBottom: '8px' } }, 'What needs attention'),
      h('div', { class: 'attn-grid' }, ...rows)));
  }

  function attentionSummaryRow({
    iconPaths, severity, title, sub, onClick,
  }) {
    return h('button', { class: 'attn-item', onClick },
      h('div', { class: 'attn-icon', style: { color: `var(--${severity})` } }, icon(iconPaths, 17)),
      h('span', { style: { flex: '1', minWidth: '0' } },
        h('div', { class: 't-sm t-strong' }, title),
        h('div', { class: 'sub' }, sub)));
  }

  function scrollToTable() {
    tableCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function drawFilters() {
    mount(filterHost, ...FILTERS.map((f) => h('button', {
      class: `btn btn-sm${activeFilter === f.id ? ' btn-primary' : ''}`,
      onClick: () => { activeFilter = f.id; drawFilters(); drawTable(lastQuery); },
    }, f.label)));
  }

  function filteredByChip(list) {
    if (activeFilter === 'all') return list;
    const trackedExpiring = new Set(expiry.buckets.expired.concat(expiry.buckets.soon).map((b) => b.ingredientPk));
    if (activeFilter === 'low') return list.filter((i) => lowStockIngredients().includes(i));
    if (activeFilter === 'expiring') return list.filter((i) => trackedExpiring.has(i.pk));
    if (activeFilter === 'healthy') {
      const low = new Set(lowStockIngredients().map((i) => i.pk));
      return list.filter((i) => !low.has(i.pk) && !trackedExpiring.has(i.pk));
    }
    return list;
  }

  function drawTable(query) {
    lastQuery = query || '';
    const q = lastQuery.trim().toLowerCase();
    let rows = q
      ? ingredients.filter((i) => i.name.toLowerCase().includes(q)
        || String(i.id).toLowerCase().includes(q))
      : ingredients;
    rows = filteredByChip(rows);

    const nearest = nearestExpiryByIngredient();

    mount(tableHost, Table({
      columns: [
        { key: 'name', label: 'Ingredient', render: nameCell, sortValue: (i) => i.name.toLowerCase() },
        { key: 'recorded', label: 'Recorded', render: (i) => h('span', { class: 'mono t-sm' }, i.onHandDisplay || qty(i.onHandBaseQty, i.baseUnit)) },
        { key: 'usable', label: 'Usable', render: (i) => h('span', { class: 'mono t-sm' }, qty(i.usableBaseQty, i.baseUnit)) },
        {
          key: 'days',
          label: 'Days remaining',
          render: outlookCell,
          // Unknown runway sorts last in both directions (see Table) — an
          // ingredient with no measured usage is not the most urgent one.
          sortValue: (i) => i.daysLeft,
        },
        {
          key: 'expiry',
          label: 'Nearest expiry',
          render: (i) => {
            const b = nearest.get(i.pk);
            if (!b) return h('span', { class: 'nodata' }, 'not tracked');
            const label = b.daysToExpiry < 0 ? `expired ${Math.abs(b.daysToExpiry)}d ago`
              : b.daysToExpiry === 0 ? 'expires today' : `in ${b.daysToExpiry}d`;
            return Pill(b.severity, label);
          },
          sortValue: (i) => (nearest.get(i.pk) ? nearest.get(i.pk).daysToExpiry : null),
        },
        { key: 'status', label: 'Status', render: statusCell },
      ],
      rows,
      onRowClick: (ing) => openIngredientDrawer({ pk: ing.pk, onChanged: reload }),
      empty: EmptyState({
        icon: '⌕',
        title: q ? `Nothing matches “${query}”` : 'No ingredients match this filter',
        description: q
          ? 'Try part of the ingredient name, or its ING- code.'
          : 'Ingredients appear here once the inventory list has been imported.',
      }),
    }));
  }

  function quickActionTile({
    iconPaths, label, severity = 'flat', onClick,
  }) {
    return h('button', { class: 'quick-action', onClick },
      h('span', { class: `kpi-icon kpi-icon-${severity}` }, icon(iconPaths, 16)),
      h('span', {}, label));
  }

  await reload();
  ctx.setSearchHandler(drawTable);

  mount(tableCard, Card({},
    CardHeader({
      title: 'All Ingredients',
      desc: 'Click a row to receive, count, record waste, or edit it',
      right: Button({ size: 'sm', onClick: () => openAddIngredientModal(reload) }, '+ Add ingredient'),
    }),
    h('div', { style: { display: 'flex', gap: '8px', marginBottom: '10px' } }, filterHost),
    tableHost));

  return h('div', {},
    statsHost,
    attentionHost,
    Card({ style: { marginTop: '14px' } },
      CardHeader({ title: 'Quick Actions' }),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', gap: '10px' } },
        quickActionTile({
          iconPaths: ICONS.packagePlus, label: 'Receive Delivery', severity: 'info', onClick: () => openReceiveModal(ingredients, reload),
        }),
        quickActionTile({
          iconPaths: ICONS.clipboardCheck, label: 'Count Stock', severity: 'good', onClick: () => openBulkCountModal(ingredients, reload),
        }),
        quickActionTile({
          iconPaths: ICONS.trash2, label: 'Record Waste', severity: 'crit', onClick: () => openWasteModal(ingredients, reload),
        }),
        quickActionTile({
          iconPaths: ICONS.history, label: 'Stock Activity', severity: 'flat', onClick: () => ctx.navigate('movements'),
        }))),
    h('div', { style: { marginTop: '14px' } }, tableCard));
}

/**
 * "Receive Delivery" entry point for the dashboard, where no ingredient is
 * already selected. Picks an ingredient, then hands straight off to
 * openIngredientDrawer's existing Receive action (reservation.receive_stock
 * / POST .../receive) — this is a picker in front of the ONE shared
 * receiving workflow (also used by a table row click and by Receiving.js),
 * not a second receiving mechanism.
 */
function openReceiveModal(ingredients, onDone) {
  openIngredientPickerModal({
    title: 'Receive a delivery',
    desc: 'Choose which ingredient arrived',
    ingredients,
    subLabel: (ing) => `bought by the ${ing.onHandUnit}`,
    onPick: (ing) => openIngredientDrawer({ pk: ing.pk, initialAction: 'receive', onChanged: onDone }),
  });
}

/**
 * "Record Waste" entry point — same picker-then-drawer pattern as Receive,
 * landing on the drawer's Waste action instead (reservation.record_wastage).
 */
function openWasteModal(ingredients, onDone) {
  openIngredientPickerModal({
    title: 'Record waste',
    desc: 'Choose which ingredient was lost',
    ingredients,
    subLabel: (ing) => qty(ing.onHandBaseQty, ing.baseUnit).textContent + ' on hand',
    onPick: (ing) => openIngredientDrawer({ pk: ing.pk, initialAction: 'waste', onChanged: onDone }),
  });
}

function openIngredientPickerModal({
  title, desc, ingredients, subLabel, onPick,
}) {
  const search = h('input', { type: 'text', placeholder: 'Search ingredient…' });
  const listHost = h('div', { style: { maxHeight: '360px', overflowY: 'auto' } });

  function drawRows(query) {
    const q = (query || '').trim().toLowerCase();
    const rows = q ? ingredients.filter((i) => i.name.toLowerCase().includes(q)) : ingredients;
    mount(listHost, ...rows.slice(0, 200).map((ing) => h('button', {
      class: 'mini-row',
      style: { display: 'flex', width: '100%', textAlign: 'left', background: 'none', border: 'none', padding: '8px 4px', cursor: 'pointer' },
      onClick: () => { modal.close(); onPick(ing); },
    },
    h('span', { style: { flex: '1' } }, ing.name, h('div', { class: 'sub' }, subLabel(ing))))));
  }
  drawRows('');
  search.addEventListener('input', () => drawRows(search.value));

  const modal = Modal({ title, desc, onClose: () => {} }, search, listHost);
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
    title: 'Count stock',
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
