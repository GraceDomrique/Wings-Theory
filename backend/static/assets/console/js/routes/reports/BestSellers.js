import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading } from '../../components/ui/Feedback.js';
import { money, count, pct } from '../../lib/format.js';
import { getSales } from '../../lib/api/reports.js';

/**
 * Sales & History > Best Sellers — spec section 15: "What products sell
 * the most?" Plain rankings from the same getSales() Sales Overview reads;
 * no algorithmic detail clutters the primary view here.
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
    mount(host, Loading('Loading…'));
    const sales = await getSales(rangeDays);
    mount(host, Table({
      columns: [
        { key: 'item', label: 'Item', render: (i) => i.name },
        { key: 'qty', label: 'Qty sold', render: (i) => h('span', { class: 'mono t-sm' }, count(i.qtySold)), sortValue: (i) => i.qtySold },
        { key: 'revenue', label: 'Revenue', render: (i) => h('span', { class: 'mono t-sm' }, money(i.revenue, 'not priced')), sortValue: (i) => i.revenue },
        { key: 'share', label: 'Share', render: (i) => h('span', { class: 'mono t-sm' }, pct(i.sharePct, 'n/a')) },
      ],
      rows: sales.byItem,
      empty: EmptyState({ icon: '▤', title: 'No sales in this window', description: 'Best sellers rank themselves once completed orders exist.' }),
    }));
  }

  await draw();

  return Card({},
    CardHeader({ title: 'Best sellers', right: h('label', { class: 'field' }, rangePicker) }),
    host);
}
