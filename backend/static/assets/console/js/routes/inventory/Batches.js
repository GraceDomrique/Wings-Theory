import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading } from '../../components/ui/Feedback.js';
import { date, qty } from '../../lib/format.js';
import { severityFromDays } from '../../lib/severity.js';
import { listIngredients, getIngredient } from '../../lib/api/inventory.js';

/**
 * Inventory > Batches (FEFO).
 *
 * FEFO order is server-computed. The backend returns batches in the exact
 * order deplete_fefo() draws them down — expiry ascending, undated batches
 * last, id breaking ties — and `drawOrder` is simply that array position.
 * Nothing here re-sorts by date: expiry SOURCE matters (an estimated
 * shelf-life date and a printed date are not interchangeable), which is why
 * spec section 4 says never to sort this client-side. That is also why the
 * Draw order column offers no sort handle.
 *
 * The backend exposes batches per ingredient (inside the ingredient detail
 * payload), not as one global list, so this view is scoped to one ingredient
 * with a picker. Fetching every ingredient's detail to build a cross-ingredient
 * table would be dozens of requests per page load.
 */
export async function render(ctx) {
  const ingredients = await listIngredients({ limit: 500, withRunway: false });

  if (ingredients.length === 0) {
    return Card({}, EmptyState({
      icon: '◫',
      title: 'No ingredients to show batches for',
      description: 'Batches appear once stock has been received against an ingredient.',
    }));
  }

  let selected = ctx.selection.ingredient
    ? ingredients.find((i) => i.pk === ctx.selection.ingredient.pk) || ingredients[0]
    : ingredients[0];

  const picker = h('select', {
    onChange: (e) => {
      selected = ingredients.find((i) => String(i.pk) === e.target.value);
      ctx.selection.ingredient = selected;
      draw();
    },
  }, ...ingredients.map((i) => h('option', {
    value: String(i.pk), selected: i.pk === selected.pk ? 'selected' : null,
  }, i.name)));

  const host = h('div');

  async function draw() {
    mount(host, Loading('Loading batches…'));
    const detail = await getIngredient(selected.pk);
    const batches = detail.batches;

    // On-hand is maintained as the sum of open batches, so a mismatch here is
    // a real integrity signal worth surfacing rather than hiding.
    const batchSum = batches.reduce((a, b) => a + (b.qtyRemaining || 0), 0);
    const onHand = detail.ingredient.onHandBaseQty;
    const inSync = onHand === null || Math.abs(batchSum - onHand) < 0.01;

    mount(host, Card({},
      CardHeader({
        title: 'FEFO batches',
        desc: 'First Expired, First Out — the oldest batch is drawn down before newer stock',
        right: inSync
          ? Pill('good', 'batches ↔ on-hand in sync')
          : Pill('crit', 'batches ↔ on-hand disagree'),
      }),
      Table({
        columns: [
          { key: 'batch', label: 'Batch', render: (b) => h('span', { class: 'mono' }, b.batchId) },
          { key: 'ingredient', label: 'Ingredient', render: () => selected.name },
          { key: 'received', label: 'Received', render: (b) => date(b.receivedDate, 'not recorded') },
          { key: 'expiry', label: 'Expiry', render: expiryCell },
          {
            key: 'qty',
            label: 'Qty remaining',
            render: (b) => h('span', { class: 'mono' }, qty(b.qtyRemaining, b.baseUnit)),
          },
          { key: 'draw', label: 'Draw order', render: drawOrderCell },
        ],
        rows: batches,
        empty: EmptyState({
          icon: '◫',
          title: `No open batches for ${selected.name}`,
          description: 'Log a delivery under Receiving to open the first batch.',
        }),
      })));
  }

  await draw();

  return h('div', {},
    h('div', { style: { display: 'flex', alignItems: 'flex-end', gap: '12px', marginBottom: '14px' } },
      h('label', { class: 'field', style: { maxWidth: '320px', flex: '1' } }, 'Ingredient', picker)),
    host);
}

/**
 * The expiry date, and — just as important — where it came from. An estimate
 * derived from shelf life is labelled as an estimate; a batch with neither is
 * "not tracked", which is a deliberate configuration for some items, not
 * missing data to be filled with a guess.
 */
function expiryCell(b) {
  if (b.expirySource === 'none') {
    return h('div', {}, h('span', { class: 'nodata' }, 'not tracked'));
  }
  const sev = severityFromDays(b.daysToExpiry);
  return h('div', {},
    h('span', {
      class: sev === 'crit' ? 't-crit t-strong' : sev === 'warn' ? 't-warn t-strong' : '',
    }, date(b.expiryDate)),
    h('div', { class: 't-xs t-faint' },
      b.expirySource === 'estimated' ? 'estimated from shelf life' : 'printed date'));
}

function drawOrderCell(b) {
  const label = b.drawOrder === 1 ? 'use now' : b.drawOrder === 2 ? 'next' : 'queued';
  const sev = b.expirySource === 'none' ? 'flat' : severityFromDays(b.daysToExpiry);
  return Pill(sev, `${label} — #${b.drawOrder}`);
}
