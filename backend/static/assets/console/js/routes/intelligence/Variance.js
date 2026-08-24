import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { count, qty, delta, pct, nodata } from '../../lib/format.js';
import { getIngredientVariance } from '../../lib/api/intelligence.js';

const LOOKBACK_DAYS = 30;

/**
 * Forecast & Insights > Ingredient Variance.
 *
 * The general-ingredient counterpart to the Reconciliation tab, which only
 * ever covers unlimited-refill streams. Expected (recipe/wastage-driven)
 * consumption vs. what a physical count actually found, per ingredient,
 * over the window — the same "flag, don't accuse" framing as Anomaly
 * Detection: a flagged row is a variance worth a look, evidence for a
 * manager to weigh, never a stated cause. An ingredient with no physical
 * count in the window shows as "no count data", never as a silent zero —
 * a variance can only be known where a count actually happened.
 */
export async function render(ctx) {
  const report = await getIngredientVariance(LOOKBACK_DAYS);
  const items = report.items || [];
  const flagged = items.filter((i) => i.flagged);
  const counted = items.filter((i) => i.hasCountData);

  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', marginBottom: '14px' } },
      Card({}, Stat({ value: count(items.length), label: 'Ingredients with activity' })),
      Card({}, Stat({ value: count(counted.length), label: 'Physically counted' })),
      Card({}, Stat({ value: count(flagged.length), label: 'Flagged for review' })),
      Card({}, Stat({ value: `${report.days ?? LOOKBACK_DAYS}d`, label: 'Window' }))),

    Card({},
      CardHeader({
        title: 'Expected vs. measured, per ingredient',
        desc: 'Recipe and wastage consumption from the ledger, against what the last physical count in this window found — a variance is context for a manager to review, not a finding of cause',
      }),
      Table({
        columns: [
          { key: 'ingredient', label: 'Ingredient', render: (i) => i.name },
          {
            key: 'consumed',
            label: 'Expected consumption',
            render: (i) => h('span', { class: 'mono t-sm' }, qty(i.consumedBase, i.baseUnit, 'none')),
          },
          {
            key: 'received',
            label: 'Received',
            render: (i) => h('span', { class: 'mono t-sm' }, qty(i.receivedBase, i.baseUnit, 'none')),
          },
          {
            key: 'variance',
            label: 'Count variance',
            render: (i) => i.hasCountData
              ? h('span', { class: `mono t-sm ${i.flagged ? 't-crit' : ''}` },
                delta(i.countVarianceBase, i.baseUnit))
              : h('span', { class: 't-xs t-faint' }, nodata('no count in window')),
          },
          {
            key: 'ratio',
            label: '% of expected',
            render: (i) => i.hasCountData
              ? h('span', { class: 'mono t-sm' }, pct(i.varianceRatio === null ? null : i.varianceRatio * 100, 'n/a', 0))
              : h('span', { class: 't-xs t-faint' }, '—'),
          },
          {
            key: 'status',
            label: 'Status',
            render: (i) => !i.hasCountData
              ? Pill('flat', 'no count data')
              : i.flagged
                ? Pill('warn', 'review needed')
                : Pill('good', 'within range'),
          },
        ],
        rows: items,
        empty: EmptyState({
          icon: '✓',
          title: 'No ingredient activity in this window',
          description: `Nothing moved through receiving, recipes, wastage or a physical count in the last ${LOOKBACK_DAYS} days.`,
        }),
      })));
}
