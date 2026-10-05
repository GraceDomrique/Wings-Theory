import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Loading } from '../../components/ui/Feedback.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { DoughnutChart } from '../../components/charts/DoughnutChart.js';
import { palette, destroyAllCharts } from '../../components/charts/base.js';
import { money, count, nodata } from '../../lib/format.js';
import { getSales } from '../../lib/api/reports.js';

/**
 * Sales & History > Sales Overview (Owner's sidebar label: "Sales") — spec
 * section 13: "How is the restaurant performing?" Revenue/orders/AOV, a
 * trend chart, and understandable date controls — nothing else. Individual
 * transactions live on Order History, rankings on Best Sellers, and
 * inventory value/the printable attention list on Business Reports: this
 * page deliberately shows none of those (spec section 43, "one metric, one
 * home").
 */
const RANGE_OPTIONS = [
  { value: '7', label: 'Last 7 days' },
  { value: '30', label: 'Last 30 days' },
  { value: '90', label: 'Last 90 days' },
  { value: '365', label: 'Last 12 months' },
  { value: 'all', label: 'All time' },
];

export async function render(ctx) {
  let range = '30';
  const host = h('div');

  const rangePicker = h('select', {
    onChange: (e) => { range = e.target.value; draw(); },
  }, ...RANGE_OPTIONS.map((o) => h('option', { value: o.value, selected: o.value === range ? 'selected' : undefined }, o.label)));

  async function draw() {
    destroyAllCharts();
    mount(host, Loading('Loading…'));
    const sales = await getSales(range === 'all' ? { all_time: true } : Number(range));
    const rangeLabel = sales.allTime ? 'all time' : `${range}d`;

    mount(host,
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', marginBottom: '14px' } },
        Card({}, Stat({ value: money(sales.revenue, 'no sales yet'), label: `Revenue · ${rangeLabel}` })),
        Card({}, Stat({ value: count(sales.orders), label: 'Orders' })),
        Card({}, Stat({ value: count(sales.covers), label: 'Covers served' })),
        Card({}, Stat({
          value: sales.averageOrder === null ? nodata('no orders yet') : money(sales.averageOrder),
          label: 'Average order',
        }))),

      h('div', { class: 'grid', style: { gridTemplateColumns: '2fr 1fr' } },
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
            height: 180,
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
            height: 200,
          }))));
  }

  await draw();
  return host;
}
