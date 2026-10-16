import { h, mount } from '../../lib/dom.js';
import { Modal } from '../../components/ui/Modal.js';
import { money, stamp, qty } from '../../lib/format.js';
import { getOrder } from '../../lib/api/orders.js';
import { listMovements, LEDGER_REASON } from '../../lib/api/inventory.js';
import { Pill } from '../../components/ui/Pill.js';

/**
 * Shared "open an order's real detail" action — used by Order History's own
 * View button and by Owner/Admin Sales' Recent Orders tables, so there is
 * exactly one order-detail layout in the console, not two drifting
 * implementations. GET /pos/orders/{id}. The lifecycle timestamps shown in
 * the timeline below ARE this schema's status history; there is no separate
 * status-change log, so nothing here is invented beyond what the order
 * record itself carries.
 *
 * Laid out as a receipt rather than a form: a status ribbon up top, a stat
 * strip of the numbers that matter at a glance, an itemized list that reads
 * like an actual receipt, and a horizontal timeline of what happened when —
 * instead of the generic "label over value" grid every other detail view
 * in the console already uses, which is the right call for a settings form
 * but reads as an afterthought for an actual transaction record.
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

  const stat = (label, value) => h('div', { class: 'order-stat' },
    h('div', { class: 'order-stat-value' }, value),
    h('div', { class: 'order-stat-label' }, label));

  const voided = !!o.voidedAt;

  const ribbon = h('div', { class: `order-ribbon${voided ? ' voided' : ''}` },
    h('span', { class: 'order-ribbon-badge' }, voided ? 'Voided' : (o.status || '').replace('_', ' ')),
    h('span', { class: 'order-ribbon-table' }, o.tableLabel ? `Table ${o.tableLabel}` : (o.source || '—')));

  const statStrip = h('div', { class: 'order-stat-strip' },
    stat('Head count', o.headCount ?? '—'),
    stat('Subtotal', money(o.subtotal, 'n/a')),
    stat('Service charge', money(o.serviceCharge, '—')),
    stat('Total', money(o.total, 'n/a')));

  const receipt = o.lines.length === 0
    ? h('p', { class: 't-xs t-faint' }, 'No line items recorded.')
    : h('div', { class: 'order-receipt' }, ...o.lines.map((l) => h('div', { class: 'order-receipt-line' },
      h('span', { class: 'order-receipt-qty' }, `${l.qty}×`),
      h('span', { class: 'order-receipt-name' }, l.name),
      h('span', { class: 'order-receipt-amount mono' }, money(l.unitPrice * l.qty)))));

  const steps = [
    { label: 'Created', iso: o.createdAt },
    { label: 'Committed', iso: o.committedAt },
    { label: voided ? 'Voided' : 'Served', iso: voided ? o.voidedAt : o.servedAt },
  ].filter((s) => s.iso);

  const timeline = h('div', { class: 'order-timeline' },
    ...steps.map((s, i) => h('div', { class: `order-timeline-step${voided && i === steps.length - 1 ? ' crit' : ''}` },
      h('div', { class: 'order-timeline-dot' }),
      h('div', {},
        h('div', { class: 't-xs', style: { fontWeight: '600' } }, s.label),
        h('div', { class: 't-xs t-faint mono' }, stamp(s.iso))))));

  // "Ingredients Used" — real stock-ledger entries for this order, not a
  // recomputation from the recipe. KDS-commit is what actually deducts stock
  // (reservation.commit_order -> deplete_fefo), so a punched-but-not-yet-
  // committed order has nothing here yet, which this says plainly rather
  // than guessing what WOULD be deducted.
  const ingredientsHost = h('div');

  Modal({ title: `Order ${o.orderNo || `#${o.id}`}`, desc: stamp(o.createdAt), wide: true, onClose: () => {} },
    ribbon,
    statStrip,
    h('div', { class: 't-xs t-faint', style: { margin: '16px 0 6px' } }, 'Items'),
    receipt,
    h('div', { class: 't-xs t-faint', style: { margin: '16px 0 10px' } }, 'Timeline'),
    timeline,
    o.voidReason ? h('div', { class: 'callout callout-crit', style: { marginTop: '14px' } }, h('p', {}, 'Void reason: ', o.voidReason)) : null,
    h('div', { class: 't-xs t-faint', style: { margin: '16px 0 6px' } }, 'Ingredients used'),
    ingredientsHost);

  if (!o.committedAt) {
    mount(ingredientsHost, h('p', { class: 't-xs t-faint' }, 'Not committed at KDS yet — stock has not been deducted for this order.'));
    return;
  }
  try {
    const { entries } = await listMovements({ orderNo: o.orderNo, limit: 100 });
    if (entries.length === 0) {
      mount(ingredientsHost, h('p', { class: 't-xs t-faint' }, 'No ledger entries found for this order.'));
      return;
    }
    mount(ingredientsHost, h('div', { class: 'order-receipt' },
      ...entries.map((e) => {
        const info = LEDGER_REASON[e.reason] || { label: e.reason, severity: 'flat' };
        return h('div', { class: 'order-receipt-line' },
          h('span', { class: 'order-receipt-name' },
            e.ingredientName, ' ', Pill(info.severity, info.label)),
          h('span', { class: 'order-receipt-amount mono' },
            e.changeQty != null ? qty(Math.abs(e.changeQty), e.baseUnit) : '—'));
      })));
  } catch (e) {
    mount(ingredientsHost, h('p', { class: 't-xs t-crit' }, `Could not load ledger entries: ${e.message || e}`));
  }
}
