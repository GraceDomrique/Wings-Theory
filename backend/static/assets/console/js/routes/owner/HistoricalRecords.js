import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Button } from '../../components/ui/Button.js';
import { count } from '../../lib/format.js';
import { listOrders } from '../../lib/api/orders.js';
import { listPurchaseOrders } from '../../lib/api/purchaseOrders.js';

/**
 * Owner > Reports & History > Historical Records — spec section 48/27: "the
 * business must not force the Owner to navigate through operational pages
 * simply to answer 'what happened last month?'" This is a jumping-off
 * point into every historical record Owner already has access to, each
 * with a quick count so the destination isn't a total unknown before
 * clicking through — not a fourth copy of any of these tables.
 */
export async function render(ctx) {
  const [orders, pos] = await Promise.all([
    listOrders({ status: 'all', limit: 200 }),
    listPurchaseOrders(),
  ]);
  const closedPos = pos.items.filter((po) => po.status === 'received' || po.status === 'cancelled');

  return h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(2,1fr)' } },
    record('Order history', `Every past order, searchable by date, table or item.`, count(orders.orders.length), () => ctx.navigate('orderhistory')),
    record('Purchase history', `Every purchase order that was received or cancelled.`, count(closedPos.length), () => ctx.navigate('purchasehistory')),
    record('Losses & variance', `Unusual stock loss and expected-vs-counted variance, with review status.`, null, () => ctx.navigate('losses')),
    record('Business reports', `Inventory value and a printable attention list.`, null, () => ctx.navigate('reports')));
}

function record(title, desc, statValue, onOpen) {
  return Card({},
    CardHeader({ title, desc }),
    statValue !== null ? Stat({ value: statValue, label: 'records' }) : null,
    h('div', { style: { marginTop: '10px' } }, Button({ size: 'sm', onClick: onOpen }, 'Open')));
}
