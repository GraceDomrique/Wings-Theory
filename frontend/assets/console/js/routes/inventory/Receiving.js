import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Button } from '../../components/ui/Button.js';
import { Table } from '../../components/ui/Table.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { stamp, delta, str } from '../../lib/format.js';
import { listIngredients, listMovements, receive } from '../../lib/api/inventory.js';

/**
 * Inventory > Receiving — log a delivery.
 *
 * The endpoint takes purchase_qty (in the ingredient's own purchase unit), an
 * optional lot number and an optional expiry date. It does NOT take a cost:
 * unit cost lives on the ingredient and is changed through Verification, so a
 * delivery cannot quietly restate the valuation of stock already on the shelf.
 * The prototype's fourth field was "Cost per unit"; it is "Lot / batch no."
 * here — same layout, but every field on the form is one the API actually
 * accepts rather than one that would be silently discarded.
 */
export async function render(ctx) {
  const ingredients = await listIngredients({ limit: 500, withRunway: false });

  const picker = h('select', {},
    h('option', { value: '' }, 'Select ingredient…'),
    ...ingredients.map((i) => h('option', { value: String(i.pk) },
      `${i.name} — bought by the ${i.onHandUnit}`)));
  if (ctx.selection.ingredient) picker.value = String(ctx.selection.ingredient.pk);

  // There is no free choice of unit here: each ingredient has exactly one
  // purchase unit it is always received in (set once, under Verification),
  // and the backend's receive() call takes the quantity in THAT unit only
  // — a dropdown offering "kg / g / pieces" as if any could be picked per
  // delivery would silently misrecord stock the moment someone chose the
  // wrong one. Instead, the ingredient's actual unit is shown as a fixed
  // badge right on the field (not just a hint underneath), so it is
  // unmistakable without pretending there's a choice to make.
  const qtyInput = h('input', { type: 'number', step: '0.01', min: '0', placeholder: '0.00', class: 'no-spinner' });
  const unitBadge = h('span', { class: 'field-unit-badge' }, 'unit');
  const lotInput = h('input', { type: 'text', placeholder: 'optional' });
  const expiryInput = h('input', { type: 'date' });
  const unitHint = h('div', { class: 'hint' }, 'Select an ingredient to see its purchase unit.');

  picker.addEventListener('change', () => {
    const ing = ingredients.find((i) => String(i.pk) === picker.value);
    unitBadge.textContent = ing ? ing.onHandUnit : 'unit';
    unitHint.textContent = ing
      ? `Stock is held internally in ${ing.baseUnit}.`
      : 'Select an ingredient to see its purchase unit.';
  });

  const submit = Button({ variant: 'primary' }, 'Confirm delivery');
  const error = h('div', { class: 'login-err', style: { display: 'none' } });
  const historyHost = h('div');

  submit.addEventListener('click', async () => {
    error.style.display = 'none';
    const pk = Number(picker.value);
    const purchaseQty = Number(qtyInput.value);

    if (!pk) return fail('Choose which ingredient arrived.');
    if (!(purchaseQty > 0)) return fail('Enter how much was delivered — it must be more than zero.');

    submit.disabled = true;
    submit.textContent = 'Recording…';
    try {
      await receive(pk, {
        purchaseQty,
        lotNo: lotInput.value.trim() || null,
        expiresAt: expiryInput.value || null,
      });
      const name = ingredients.find((i) => i.pk === pk).name;
      toast(`Delivery recorded — a new FEFO batch is open for ${name}.`, 'good');
      qtyInput.value = '';
      lotInput.value = '';
      expiryInput.value = '';
      // Stock and batches both moved; the deliveries list is the visible part.
      await drawHistory();
    } catch (err) {
      fail(err.message);
    } finally {
      submit.disabled = false;
      submit.textContent = 'Confirm delivery';
    }
  });

  function fail(message) {
    error.textContent = message;
    error.style.display = '';
  }

  async function drawHistory() {
    const { entries } = await listMovements({ reason: 'purchase_receipt', limit: 25 });
    mount(historyHost, Card({ style: { maxWidth: '640px', marginTop: '14px' } },
      CardHeader({ title: 'Recent deliveries', desc: 'Straight from the ledger — newest first' }),
      Table({
        columns: [
          { key: 'when', label: 'When', render: (m) => h('span', { class: 'mono t-xs' }, stamp(m.timestamp)) },
          { key: 'what', label: 'Ingredient', render: (m) => m.ingredientName },
          {
            key: 'qty',
            label: 'Received',
            render: (m) => h('span', { class: 'mono t-good' }, delta(m.changeQty, m.baseUnit)),
          },
          { key: 'note', label: 'Note', render: (m) => h('span', { class: 't-xs t-soft' }, str(m.note, 'no note')) },
        ],
        rows: entries,
        empty: EmptyState({
          icon: '🚚',
          title: 'No deliveries recorded yet',
          description: 'Log one above once stock physically arrives — it opens a FEFO batch and increases on-hand.',
        }),
      })));
  }

  await drawHistory();

  return h('div', {},
    Card({ style: { maxWidth: '640px' } },
      CardHeader({
        title: 'Log a delivery',
        desc: 'Creates a new FEFO batch and increases on-hand stock',
      }),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '12px' } },
        h('label', { class: 'field' }, 'Ingredient', picker),
        h('label', { class: 'field' }, 'Quantity received',
          h('div', { class: 'field-with-badge' }, qtyInput, unitBadge), unitHint),
        h('label', { class: 'field' }, 'Lot / batch no.', lotInput,
          h('div', { class: 'hint' }, 'Optional — helps trace a recall.')),
        h('label', { class: 'field' }, 'Expiry date', expiryInput,
          h('div', { class: 'hint' }, 'Leave blank if the pack carries no printed date.'))),
      h('div', { class: 'divider' }),
      submit,
      error,
      h('div', { class: 't-xs t-faint', style: { marginTop: '10px' } },
        'Cost per unit is not set here — it belongs to the ingredient and is confirmed under Verification, so a delivery never restates the value of stock already on the shelf.')),
    historyHost);
}
