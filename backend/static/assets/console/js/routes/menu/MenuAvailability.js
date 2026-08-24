import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { RailCard } from '../../components/ui/RailCard.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { count, money, str } from '../../lib/format.js';
import { listMenuItems, editMenuPrice } from '../../lib/api/menu.js';

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
 */
export async function render(ctx) {
  const { items, sellable, blocked } = await listMenuItems();
  const tableHost = h('div');

  function drawTable() {
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
      rows: items.slice().sort((a, b) => Number(a.sellable) - Number(b.sellable)),
      empty: EmptyState({
        icon: '☰',
        title: 'No menu items yet',
        description: 'Items appear here once the menu has been imported and mapped to recipes.',
      }),
    }));
  }

  drawTable();

  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)', marginBottom: '14px' } },
      RailCard({ severity: 'good' }, Stat({ value: count(sellable), label: 'Sellable now' })),
      RailCard({ severity: blocked > 0 ? 'crit' : 'good' },
        Stat({ value: count(blocked), label: 'Blocked — missing stock or setup' })),
      Card({}, Stat({ value: count(items.length), label: 'Menu items total' }))),

    Card({},
      CardHeader({
        title: 'Why an item is blocked',
        desc: 'Traced straight to the short ingredient by the availability view · click a price to change it',
      }),
      tableHost));
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
    const input = h('input', {
      type: 'number', min: '0.01', step: '0.01', value: m.price ?? '',
      style: { width: '90px', padding: '3px 6px', fontSize: '12px' },
    });
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

    mount(cell, h('div', { style: { display: 'flex', alignItems: 'center', gap: '4px' } }, input, save, cancel, err));
    input.focus();
  }

  showDisplay();
  return cell;
}
