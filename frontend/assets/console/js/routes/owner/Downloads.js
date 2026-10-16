import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Button } from '../../components/ui/Button.js';
import { asText } from '../../lib/format.js';
import { listOrders } from '../../lib/api/orders.js';

/**
 * Owner > Reports & History > Downloads — spec section 22ish: printable and
 * exportable records. The "Attention Needed" print-to-PDF (window.print(),
 * spec section 27) already lives on Business Reports — this page's own job
 * is a plain CSV of the order ledger, the same client-side export pattern
 * Admin's Movements page already uses (a read of what's on screen; nothing
 * server-side changes).
 */
export async function render(ctx) {
  return h('div', {},
    Card({ style: { marginBottom: '14px' } },
      CardHeader({ title: 'Attention needed (printable)', desc: 'Low stock and expiring/expired batches, formatted to print or save as PDF' }),
      Button({ size: 'sm', onClick: () => ctx.navigate('reports') }, 'Open Business Reports')),
    Card({},
      CardHeader({ title: 'Order history (CSV)', desc: 'Last 200 orders, exactly as shown on Order History' }),
      Button({ size: 'sm', onClick: downloadOrdersCsv }, '↓ Download CSV')));
}

async function downloadOrdersCsv() {
  const { orders } = await listOrders({ status: 'all', limit: 200 });
  const head = ['order_no', 'when', 'table', 'status', 'total', 'items'];
  const lines = [head.join(',')];
  for (const o of orders) {
    lines.push([
      o.orderNo ?? o.id,
      o.createdAt,
      o.tableLabel || '',
      o.voidedAt ? 'voided' : o.status,
      asText.num(o.currentTotal ?? o.total ?? 0, '', 2),
      o.itemNames || '',
    ].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = h('a', { href: URL.createObjectURL(blob), download: `wings-theory-orders-${new Date().toISOString().slice(0, 10)}.csv` });
  document.body.appendChild(a);
  a.click();
  a.remove();
}
