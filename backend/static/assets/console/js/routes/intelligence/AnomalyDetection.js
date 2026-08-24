import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { palette } from '../../components/charts/base.js';
import { count, delta, stamp, humanise } from '../../lib/format.js';
import { getAnomalies } from '../../lib/api/intelligence.js';

const LOOKBACK_DAYS = 90;

/**
 * Forecast & Insights > Anomaly Detection.
 *
 * Copy discipline (spec section 6 point 5): these are informational flags, not
 * accusations. The "why flagged" text is the model's own factual sentence
 * ("2.4x this ingredient's typical wastage"). Nothing on this screen implies
 * theft, blame or intent — that judgement is the manager's, and the UI's job
 * is to hand them the fact, not the conclusion. The header copy says so
 * explicitly so the framing survives future edits.
 */
export async function render(ctx) {
  const { stockLoss, consumption } = await getAnomalies(LOOKBACK_DAYS);

  const ingredientsInvolved = new Set(stockLoss.map((a) => a.ingredientName)).size;

  // Where losses cluster: count of flags per ingredient, biggest first.
  const byIngredient = new Map();
  for (const a of stockLoss) {
    byIngredient.set(a.ingredientName, (byIngredient.get(a.ingredientName) || 0) + 1);
  }
  const clusters = [...byIngredient.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);

  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', marginBottom: '14px' } },
      Card({}, Stat({ value: count(consumption.length), label: 'Unusual service days' })),
      Card({}, Stat({ value: count(stockLoss.length), label: 'Flagged stock losses' })),
      Card({}, Stat({ value: count(ingredientsInvolved), label: 'Ingredients involved' })),
      Card({}, Stat({ value: `${LOOKBACK_DAYS}d`, label: 'Lookback window' }))),

    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1.3fr' } },
      Card({},
        h('div', { class: 'card-title', style: { marginBottom: '10px' } }, 'Where losses cluster'),
        BarChart({
          labels: clusters.map(([name]) => name),
          data: clusters.map(([, n]) => n),
          color: palette.crit,
          height: 180,
          empty: {
            icon: '✓',
            title: 'No losses flagged in this window',
            description: `Nothing in the last ${LOOKBACK_DAYS} days stood out against each ingredient's own usual pattern.`,
            tight: true,
          },
        })),

      Card({},
        CardHeader({
          title: 'Flagged, for review',
          desc: 'Waste and count entries larger than usual for that ingredient — a flag to look into, not a finding',
        }),
        Table({
          columns: [
            { key: 'ingredient', label: 'Ingredient', render: (a) => a.ingredientName },
            {
              key: 'when',
              label: 'When',
              render: (a) => h('span', { class: 'mono t-xs' }, stamp(a.timestamp)),
            },
            {
              key: 'amount',
              label: 'Amount',
              render: (a) => h('span', { class: 't-crit mono t-sm' }, delta(a.amount, a.baseUnit)),
            },
            {
              key: 'why',
              label: 'Why flagged',
              render: (a) => h('div', {},
                h('span', { class: 't-sm t-soft' }, a.flaggedBecause),
                a.reasonCode
                  ? h('div', { class: 't-xs t-faint' }, humanise(a.reasonCode))
                  : null),
            },
            {
              key: 'priority',
              label: 'Priority',
              render: (a) => Pill(a.severity,
                a.priority === 'review_soon' ? 'review soon' : 'worth a look'),
            },
          ],
          rows: stockLoss,
          empty: EmptyState({
            icon: '✓',
            title: 'Nothing flagged for review',
            description: 'Waste and stock-count entries are all within each ingredient’s normal range for this window.',
          }),
        }))));
}
