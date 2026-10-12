import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { money, count, nodata } from '../../lib/format.js';
import { getValuation } from '../../lib/api/reports.js';
import { getReplenishment, getAnomalies } from '../../lib/api/intelligence.js';
import { getExpiry } from '../../lib/api/expiry.js';
import { listMenuItems } from '../../lib/api/menu.js';

/**
 * Owner > Inventory & Losses > Inventory Overview — spec section 26: Owner
 * oversight, not Admin's operational On Hand table. Summarizes and points
 * at the detail pages (Expiry, Losses) rather than reproducing their full
 * tables here — spec section 44, "do not overload dashboards."
 */
export async function render(ctx) {
  const [valuation, replenishment, expiry, anomalies, menu] = await Promise.all([
    getValuation(),
    getReplenishment(30),
    getExpiry(90),
    getAnomalies(90).catch(() => ({ stockLoss: [] })),
    listMenuItems().catch(() => ({ items: [] })),
  ]);
  const unavailable = (menu.items || []).filter((m) => !m.sellable);

  const needsRestock = (replenishment.rows || []).filter((r) => r.status === 'crit' || r.status === 'warn').length;
  const expiringOrExpired = expiry.buckets.expired.length + expiry.buckets.soon.length;
  const unreviewedLosses = (anomalies.stockLoss || []).filter((a) => !a.reviewStatus).length;

  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', marginBottom: '14px' } },
      Card({}, Stat({ value: valuation.available ? money(valuation.totalValue) : nodata('not available'), label: 'Inventory value' })),
      Card({}, Stat({ value: count(needsRestock), label: 'Need restocking' }),
        needsRestock ? h('div', { style: { marginTop: '8px' } }, Button({ size: 'sm', onClick: () => ctx.navigate('whattobuy') }, 'View')) : null),
      Card({}, Stat({ value: count(expiringOrExpired), label: 'Expiring or expired' }),
        expiringOrExpired ? h('div', { style: { marginTop: '8px' } }, Button({ size: 'sm', onClick: () => ctx.navigate('expiry') }, 'View')) : null),
      Card({}, Stat({ value: count(unreviewedLosses), label: 'Unusual losses, unreviewed' }),
        unreviewedLosses ? h('div', { style: { marginTop: '8px' } }, Button({ size: 'sm', onClick: () => ctx.navigate('losses') }, 'View')) : null)),

    !valuation.available
      ? h('div', { style: { marginBottom: '14px' } },
        Card({}, CardHeader({ title: 'Inventory value' }), h('div', { class: 't-sm t-soft' }, valuation.reason)))
      : null,

    Card({},
      CardHeader({ title: 'Unavailable right now', desc: 'Verbatim reason from the system — never guessed from stock numbers' }),
      unavailable.length === 0
        ? EmptyState({ icon: '✓', title: 'Everything on the menu can be sold', description: `All ${menu.items.length} items are available.`, tight: true })
        : h('div', {}, ...unavailable.map((m) => h('div', { style: { padding: '9px 0', borderBottom: '1px solid var(--line-soft)' } },
          h('div', { class: 't-sm', style: { fontWeight: '600' } }, m.name),
          h('div', { class: 't-xs t-soft' }, m.blockReason || 'no reason given'))))));
}
