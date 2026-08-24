import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading } from '../../components/ui/Feedback.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { DoughnutChart } from '../../components/charts/DoughnutChart.js';
import { palette, destroyAllCharts } from '../../components/charts/base.js';
import { money, count, pct, nodata } from '../../lib/format.js';
import { getSales, getCombinations, getValuation } from '../../lib/api/reports.js';

/**
 * Reports — sales, product combinations and inventory value.
 *
 * Voids are excluded from every total by the backend and stay excluded here.
 * Where a figure genuinely cannot be computed (an average order value with no
 * orders, a share of a zero total, a valuation with no cost data) the screen
 * says so instead of printing a zero that looks like a measurement.
 */
export async function render(ctx) {
  let rangeDays = 30;
  const host = h('div');

  const rangePicker = h('select', {
    onChange: (e) => { rangeDays = Number(e.target.value); draw(); },
  },
  h('option', { value: '7' }, 'Last 7 days'),
  h('option', { value: '30', selected: 'selected' }, 'Last 30 days'),
  h('option', { value: '90' }, 'Last 90 days'));

  async function draw() {
    destroyAllCharts();
    mount(host, Loading('Loading reports…'));
    const [sales, combos, valuation] = await Promise.all([
      getSales(rangeDays),
      getCombinations(90),
      getValuation(),
    ]);

    mount(host,
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', marginBottom: '14px' } },
        Card({}, Stat({ value: money(sales.revenue, 'no sales yet'), label: `Revenue · ${rangeDays}d` })),
        Card({}, Stat({ value: count(sales.orders), label: 'Orders' })),
        Card({}, Stat({ value: count(sales.covers), label: 'Covers served' })),
        Card({}, Stat({
          // An average of nothing is not zero.
          value: sales.averageOrder === null
            ? nodata('no orders yet')
            : money(sales.averageOrder),
          label: 'Average order',
        }))),

      h('div', { class: 'grid', style: { gridTemplateColumns: '2fr 1fr', marginBottom: '14px' } },
        Card({},
          CardHeader({
            title: 'Sales trend',
            desc: 'Real completed orders only — voids excluded',
            right: h('label', { class: 'field' }, rangePicker),
          }),
          BarChart({
            labels: sales.byDay.map((d) => String(d.day).slice(5)),
            data: sales.byDay.map((d) => d.revenue),
            color: palette.info,
            height: 160,
            empty: {
              icon: '▤',
              title: 'No completed orders in this window',
              description: 'Widen the range, or take an order — voided orders are deliberately not counted here.',
              tight: true,
            },
          })),
        Card({},
          CardHeader({ title: 'Revenue mix' }),
          DoughnutChart({
            labels: sales.byItem.slice(0, 6).map((i) => i.name),
            data: sales.byItem.slice(0, 6).map((i) => i.revenue),
            height: 190,
          }))),

      h('div', { class: 'grid', style: { gridTemplateColumns: '1.3fr 1fr' } },
        Card({},
          CardHeader({ title: 'Best sellers' }),
          Table({
            columns: [
              { key: 'item', label: 'Item', render: (i) => i.name },
              {
                key: 'qty',
                label: 'Qty sold',
                render: (i) => h('span', { class: 'mono t-sm' }, count(i.qtySold)),
                sortValue: (i) => i.qtySold,
              },
              {
                key: 'revenue',
                label: 'Revenue',
                render: (i) => h('span', { class: 'mono t-sm' }, money(i.revenue, 'not priced')),
                sortValue: (i) => i.revenue,
              },
              {
                key: 'share',
                label: 'Share',
                // Share of a zero total is undefined, not 0%.
                render: (i) => h('span', { class: 'mono t-sm' }, pct(i.sharePct, 'n/a')),
              },
            ],
            rows: sales.byItem,
            empty: EmptyState({
              icon: '▤',
              title: 'No sales in this window',
              description: 'Best sellers rank themselves once completed orders exist.',
            }),
          })),
        combosCard(combos)),

      valuationCard(valuation));
  }

  await draw();
  return host;
}

/**
 * FP-Growth combinations. "Not enough history" and "no combinations found" are
 * different statements — the first is a shortfall, the second is a finding —
 * so they get different copy.
 */
function combosCard(combos) {
  return Card({},
    CardHeader({
      title: 'Frequently bought together',
      desc: 'FP-Growth over real order history',
    }),
    combos.insufficientData
      ? EmptyState({
        icon: '✦',
        title: 'Not enough order history yet',
        description: `Needs at least ${combos.required ?? 30} orders to find a reliable pattern — has ${combos.transactionCount ?? 0} so far.`,
      })
      : combos.combos.length === 0
        ? EmptyState({
          icon: '✦',
          title: 'No reliable pairings found',
          description: `Enough orders were analysed (${combos.transactionCount}), but no pair cleared the lift and support thresholds. That is a result, not a gap.`,
        })
        : Table({
          columns: [
            { key: 'pair', label: 'Pair', render: (c) => `${c.itemA} + ${c.itemB}` },
            { key: 'lift', label: 'Lift', render: (c) => h('span', { class: 'mono t-sm' }, c.lift === null ? nodata('n/a') : `${c.lift.toFixed(2)}×`), sortValue: (c) => c.lift },
            { key: 'conf', label: 'Confidence', render: (c) => h('span', { class: 'mono t-sm' }, pct(c.confidence === null ? null : c.confidence * 100, 'n/a', 0)) },
            { key: 'price', label: 'Combo price', render: (c) => h('span', { class: 'mono t-sm' }, money(c.suggestedComboPrice, 'not priced')) },
          ],
          rows: combos.combos,
        }));
}

function valuationCard(valuation) {
  if (valuation.available) {
    return Card({ style: { marginTop: '14px' } },
      CardHeader({ title: 'Inventory value', desc: 'On-hand stock at confirmed cost' }),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)' } },
        Card({ sunk: true }, Stat({ value: money(valuation.totalValue), label: 'Valued on hand' })),
        Card({ sunk: true }, Stat({ value: count(valuation.pricedItems), label: 'Items priced' })),
        Card({ sunk: true }, Stat({ value: count(valuation.totalItems), label: 'Items tracked' }))));
  }

  return Card({ style: { marginTop: '14px' } },
    CardHeader({ title: 'Inventory value', desc: 'On-hand stock at confirmed cost' }),
    EmptyState({
      icon: '₱',
      title: 'Inventory value is not available yet',
      description: valuation.reason,
    }));
}
