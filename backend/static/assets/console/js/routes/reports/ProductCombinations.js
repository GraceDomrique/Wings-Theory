import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading } from '../../components/ui/Feedback.js';
import { pct } from '../../lib/format.js';
import { getCombinations } from '../../lib/api/reports.js';

/**
 * Sales & History > Product Combinations — spec section 16: "What do
 * customers commonly order together?" Business interpretation first ("X of
 * Y times bought together"); lift/support/FP-Growth terminology is
 * available but never the headline. This is the read-only report version
 * of the same pairing data Menu & Recipes > Combos uses to actually draft a
 * combo — spec section 43's "one metric, one home" is about the number, not
 * the screen, so the two pages are deliberately separate (report vs.
 * create/manage).
 */
const LOOKBACK_OPTIONS = [
  { value: '90', label: 'Last 90 days' },
  { value: '180', label: 'Last 6 months' },
  { value: '365', label: 'Last 12 months' },
];

export async function render(ctx) {
  const shell = h('div');
  let lookbackDays = 365;

  async function draw() {
    const host = h('div');
    mount(host, Loading('Loading…'));
    mount(shell, Card({}, CardHeader({
      title: 'Frequently bought together',
      right: h('label', { class: 'field' }, lookbackPicker),
    }), host));

    const combos = await getCombinations(lookbackDays);

    if (combos.insufficientData) {
      mount(host, EmptyState({ icon: '✦', title: 'Not enough order history yet', description: `Needs at least ${combos.required ?? 30} orders to find a reliable pattern — has ${combos.transactionCount ?? 0} so far.` }));
    } else if (combos.combos.length === 0) {
      mount(host, EmptyState({ icon: '✦', title: 'No reliable pairings found', description: `Enough orders were analysed (${combos.transactionCount}), but no pair cleared the lift and support thresholds. That is a result, not a gap.` }));
    } else {
      mount(host, h('div', {},
        Table({
          columns: [
            { key: 'pair', label: 'Pair', render: (c) => `${c.itemA} + ${c.itemB}` },
            {
              key: 'observed', label: 'Observed together',
              render: (c) => h('span', { class: 'mono t-sm' }, pct(c.confidence === null ? null : c.confidence * 100, 'n/a', 0)),
            },
          ],
          rows: combos.combos,
        }),
        h('div', { class: 'accordion', style: { marginTop: '10px' } },
          h('details', {},
            h('summary', { class: 't-xs t-soft' }, 'Technical details'),
            h('div', { class: 't-xs t-faint', style: { marginTop: '8px' } },
              `FP-Growth over ${combos.transactionCount} real orders (last ${lookbackDays} days). `,
              combos.combos.map((c) => `${c.itemA} + ${c.itemB}: lift ${c.lift === null ? 'n/a' : c.lift.toFixed(2) + '×'}, support ${c.support === null ? 'n/a' : c.support}`).join(' · '))))));
    }
  }

  const lookbackPicker = h('select', {
    onChange: (e) => { lookbackDays = Number(e.target.value); draw(); },
  }, ...LOOKBACK_OPTIONS.map((o) => h('option', { value: o.value, selected: Number(o.value) === lookbackDays ? 'selected' : undefined }, o.label)));

  await draw();
  return shell;
}
