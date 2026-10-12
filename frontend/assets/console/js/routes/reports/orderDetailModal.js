import { h } from '../../lib/dom.js';
import { Modal } from '../../components/ui/Modal.js';
import { Pill } from '../../components/ui/Pill.js';
import { money, stamp, count } from '../../lib/format.js';
import { getOrder } from '../../lib/api/orders.js';

/**
 * Shared "open an order's real detail" action for Owner Sales / Admin
 * Sales' Recent Orders tables — GET /pos/orders/{id} (same reader Order
 * History's own detail modal uses, see that route's openDetail()). The
 * lifecycle timestamps shown under "Status History" ARE this schema's
 * status history; there is no separate status-change log, so nothing here
 * is invented beyond what the order record itself carries.
 */
export async function openOrderDetail(orderId) {
  let o;
  try {
    o = await getOrder(orderId);
  } catch (e) {
    Modal({ title: 'Could not load order', onClose: () => {} },
      h('p', { class: 't-sm t-soft' }, e.message || String(e)));
    return;
  }

  const field = (label, value) => h('div', {}, h('div', { class: 't-xs t-faint' }, label), h('div', { class: 't-sm', style: { fontWeight: '600' } }, value));
  const historyRow = (label, iso) => (iso ? h('div', { class: 'mini-row' }, h('span', {}, label), h('span', { class: 't-xs' }, stamp(iso))) : null);

  Modal({ title: `Order ${o.orderNo || `#${o.id}`}`, desc: stamp(o.createdAt), wide: true, onClose: () => {} },
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: '10px', marginBottom: '14px' } },
      field('Status', o.voidedAt ? 'Voided' : o.status),
      field('Table', o.tableLabel || '—'),
      field('Head count', o.headCount ?? '—'),
      field('Subtotal', money(o.subtotal, 'n/a')),
      field('Service charge', money(o.serviceCharge, '—')),
      field('Total', money(o.total, 'n/a'))),
    h('div', { class: 't-xs t-faint', style: { marginBottom: '6px' } }, 'Items'),
    o.lines.length === 0
      ? h('p', { class: 't-xs t-faint' }, 'No line items recorded.')
      : h('div', { style: { marginBottom: '14px' } }, ...o.lines.map((l) => h('div', { class: 'mini-row' },
        h('span', {}, l.name, ' ', Pill('flat', `×${count(l.qty)}`)),
        money(l.unitPrice * l.qty)))),
    h('div', { class: 't-xs t-faint', style: { marginBottom: '6px' } }, 'Status History'),
    historyRow('Created', o.createdAt),
    historyRow('Committed', o.committedAt),
    historyRow('Served', o.servedAt),
    historyRow('Voided', o.voidedAt),
    o.voidReason ? h('div', { class: 'callout callout-crit', style: { marginTop: '10px' } }, h('p', {}, 'Void reason: ', o.voidReason)) : null);
}
