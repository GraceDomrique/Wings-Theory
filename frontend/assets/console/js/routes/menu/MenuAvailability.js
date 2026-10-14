import { h, mount, icon } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { RailCard } from '../../components/ui/RailCard.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { count, money, str } from '../../lib/format.js';
import { ICONS } from '../../lib/icons.js';
import { listMenuItems, editMenuPrice } from '../../lib/api/menu.js';

const STATUS_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'available', label: 'Available' },
  { id: 'blocked', label: 'Blocked' },
];

/**
 * Recipes & Menu > Menu Availability.
 *
 * `blockReason` is authored by the availability view and is shown verbatim.
 * It is never inferred from stock levels here (spec section 4): an item can be
 * blocked for reasons a quantity cannot express — an unmapped recipe line, or
 * an ingredient with no pack size, both of which read as "plenty in stock" if
 * you only look at numbers.
 *
 * Price is editable in place. `PATCH /console/menu-items/{pk}/price` writes
 * the same `menu_item.price` column the POS reads on its own next menu
 * load — there is exactly one price, so this is the only place it needs to
 * change for the till to charge it.
 *
 * There is no backend endpoint to create a standalone menu item — the only
 * real path that produces a new `menu_item` row is publishing a combo
 * (POST /console/combos/publish). Rather than add a dead "+ Add menu item"
 * button with nothing to call, the header links to Combo Builder, which is
 * the actual supported creation path (2026-10-15 redesign round).
 */
export async function render(ctx) {
  const { items, sellable, blocked } = await listMenuItems();
  const tableHost = h('div');
  const problemsHost = h('div');
  const filterHost = h('div');
  let lastQuery = '';
  let activeStatus = 'all';
  let problemsExpanded = false;
  const expandedRows = new Set();
  const tableCard = h('div');

  /**
   * Many blocked items share the exact same `blockReason` string (the
   * availability view reports it per blocking ingredient, e.g. "out of
   * stock: Butter") — grouping on that string turns "12 separate blocked
   * dishes" into "1 ingredient problem affecting 12 dishes", which is the
   * question a manager is actually asking.
   */
  function problemGroups() {
    const groups = new Map();
    for (const m of items) {
      if (m.sellable) continue;
      // Plain string, not str()'s DOM-node return — a Map key needs value
      // equality, and str() returns a distinct Text node per call even for
      // identical text, which silently defeats grouping.
      const reason = (m.blockReason && String(m.blockReason).trim()) || 'blocked, reason not reported';
      if (!groups.has(reason)) groups.set(reason, []);
      groups.get(reason).push(m);
    }
    return [...groups.entries()].sort((a, b) => b[1].length - a[1].length);
  }

  function problemRow(reason, affected, i) {
    const open = expandedRows.has(reason);
    const row = h('button', {
      class: 'alert-row clickable',
      onClick: () => { if (open) expandedRows.delete(reason); else expandedRows.add(reason); drawProblems(); },
    },
    h('span', { class: `dot dot-crit` }),
    h('span', { style: { flex: '1', minWidth: '0', textAlign: 'left' } },
      h('div', { class: 't-sm t-strong' }, reason),
      open ? h('div', { class: 'sub', style: { marginTop: '4px', whiteSpace: 'normal' } },
        affected.map((m) => m.name).join(' · ')) : null),
    Pill('crit', `${affected.length} dish${affected.length === 1 ? '' : 'es'}`),
    h('span', { class: 't-faint', style: { fontSize: '11px' } }, open ? '▲' : '▼'));
    return row;
  }

  function drawProblems() {
    const groups = problemGroups();
    if (groups.length === 0) {
      mount(problemsHost, EmptyState({ icon: '✓', title: 'Nothing blocked', description: 'Every menu item is sellable right now.' }));
      return;
    }
    const visible = problemsExpanded ? groups : groups.slice(0, 3);
    const remaining = groups.length - visible.length;
    mount(problemsHost,
      h('div', {}, ...visible.map(([reason, affected], i) => problemRow(reason, affected, i))),
      remaining > 0
        ? h('div', { style: { marginTop: '8px', textAlign: 'center' } },
          Button({
            size: 'sm', variant: 'ghost',
            onClick: () => { problemsExpanded = true; drawProblems(); },
          }, `View all ${groups.length} issues (${remaining} more)`))
        : (problemsExpanded && groups.length > 3
          ? h('div', { style: { marginTop: '8px', textAlign: 'center' } },
            Button({ size: 'sm', variant: 'ghost', onClick: () => { problemsExpanded = false; drawProblems(); } }, 'Show fewer'))
          : null));
  }

  function drawFilters() {
    mount(filterHost, ...STATUS_FILTERS.map((f) => h('button', {
      class: `btn btn-sm${activeStatus === f.id ? ' btn-primary' : ''}`,
      onClick: () => { activeStatus = f.id; drawFilters(); drawTable(lastQuery); },
    }, f.label)));
  }

  function drawTable(query) {
    lastQuery = query || '';
    const q = lastQuery.trim().toLowerCase();
    let rows = q
      ? items.filter((m) => m.name.toLowerCase().includes(q) || (m.category || '').toLowerCase().includes(q))
      : items;
    if (activeStatus === 'available') rows = rows.filter((m) => m.sellable);
    if (activeStatus === 'blocked') rows = rows.filter((m) => !m.sellable);

    mount(tableHost, Table({
      columns: [
        { key: 'name', label: 'Menu item', render: (m) => h('strong', {}, m.name), sortValue: (m) => m.name.toLowerCase() },
        { key: 'category', label: 'Category', render: (m) => str(m.category, 'uncategorised') },
        { key: 'price', label: 'Price', render: priceCell },
        {
          key: 'status',
          label: 'Status',
          render: (m) => (m.sellable ? Pill('good', 'available') : Pill('crit', 'unavailable')),
          sortValue: (m) => (m.sellable ? 1 : 0),
        },
        {
          key: 'why',
          label: 'Why unavailable',
          render: (m) => (m.sellable
            // A sellable item has no blocking reason — that is an absence,
            // and it reads as one rather than as an empty cell.
            ? h('span', { class: 't-faint' }, '—')
            : h('span', { class: 't-sm t-crit' }, str(m.blockReason, 'blocked, reason not reported'))),
        },
      ],
      // Blocked items first: this table exists to answer "what can't I sell".
      rows: rows.slice().sort((a, b) => Number(a.sellable) - Number(b.sellable)),
      empty: EmptyState({
        icon: '☰',
        title: q || activeStatus !== 'all' ? `Nothing matches` : 'No menu items yet',
        description: q || activeStatus !== 'all'
          ? 'Try a different name, category, or clear the filter.'
          : 'Items appear here once the menu has been imported and mapped to recipes.',
      }),
    }));
  }

  drawTable('');
  drawProblems();
  drawFilters();
  ctx.setSearchHandler(drawTable);

  mount(tableCard, Card({},
    CardHeader({
      title: 'Why an item is blocked',
      desc: 'Traced straight to the short ingredient by the availability view · click a price to change it',
    }),
    h('div', { style: { display: 'flex', gap: '8px', marginBottom: '10px' } }, filterHost),
    tableHost));

  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)', gap: '10px', marginBottom: '14px' } },
      RailCard({ severity: 'good', icon: ICONS.checkCircle }, Stat({ value: count(sellable), label: 'Available' })),
      RailCard({ severity: blocked > 0 ? 'crit' : 'good', icon: ICONS.alertTriangle }, Stat({ value: count(blocked), label: 'Blocked' })),
      Card({ className: 'kpi-neutral' }, Stat({ value: count(items.length), label: 'Total' }))),

    Card({ style: { marginBottom: '14px' } },
      CardHeader({
        title: 'Menu problems',
        desc: 'Blocked dishes grouped by the one ingredient or setup gap causing it · click a row to see which dishes',
      }),
      problemsHost),

    tableCard);
}

function priceCell(m) {
  const cell = h('span', { class: 'mono t-sm' });

  function showDisplay() {
    mount(cell,
      h('button', {
        class: 'btn btn-ghost btn-sm',
        style: { padding: '2px 4px', fontFamily: 'var(--font-mono)' },
        title: 'Click to change the POS price',
        onClick: showEditor,
      }, money(m.price, 'no price set').textContent || 'set price'));
  }

  function showEditor() {
    const oldPrice = m.price ?? null;
    const input = h('input', {
      type: 'number', inputmode: 'decimal', min: '0.01', step: '0.01', value: m.price ?? '',
      style: { width: '90px', padding: '3px 6px', fontSize: '12px' },
    });
    // The difference vs. the current price, before confirming — a manager
    // reads "+₱20.00" faster than mentally subtracting two prices.
    const diff = h('span', { class: 't-xs', style: { minWidth: '62px' } });
    function updateDiff() {
      const value = Number(input.value);
      if (!(value > 0) || oldPrice === null) { diff.textContent = ''; return; }
      const delta = value - oldPrice;
      diff.className = `t-xs ${delta > 0 ? 't-good' : delta < 0 ? 't-crit' : 't-faint'}`;
      diff.textContent = Math.abs(delta) < 0.001 ? 'no change' : `${delta > 0 ? '+' : '−'}₱${Math.abs(delta).toFixed(2)}`;
    }
    input.addEventListener('input', updateDiff);
    updateDiff();

    const err = h('span', { class: 't-crit t-xs', style: { display: 'none' } });
    const save = h('button', { class: 'icon-btn', title: 'Save', style: { width: '24px', height: '24px' } }, '✓');
    const cancel = h('button', { class: 'icon-btn', title: 'Cancel', style: { width: '24px', height: '24px' } }, '✕');

    save.addEventListener('click', async () => {
      const value = Number(input.value);
      if (!(value > 0)) { err.textContent = 'must be > 0'; err.style.display = ''; return; }
      save.disabled = true; cancel.disabled = true;
      try {
        const r = await editMenuPrice(m.pk, value);
        m.price = r.price;
        toast(`${m.name} is now ₱${value.toFixed(2)} on the POS.`, 'good');
        showDisplay();
      } catch (e) {
        err.textContent = e.message; err.style.display = '';
        save.disabled = false; cancel.disabled = false;
      }
    });
    cancel.addEventListener('click', showDisplay);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save.click(); if (e.key === 'Escape') cancel.click(); });

    mount(cell, h('div', { style: { display: 'flex', alignItems: 'center', gap: '4px' } }, input, diff, save, cancel, err));
    input.focus();
  }

  showDisplay();
  return cell;
}
