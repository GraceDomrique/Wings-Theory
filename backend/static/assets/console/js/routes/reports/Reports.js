import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading } from '../../components/ui/Feedback.js';
import { money, count, nodata, daysShort, qty } from '../../lib/format.js';
import { getValuation } from '../../lib/api/reports.js';
import { getReplenishment } from '../../lib/api/intelligence.js';
import { getExpiry } from '../../lib/api/expiry.js';

/**
 * Business Reports (Sales & History > Business Reports for Admin; Reports &
 * History > Business Reports for Owner) — inventory value and the
 * printable "Attention Needed" consolidated list. Sales KPIs/trend moved to
 * Sales Overview, rankings to Best Sellers, pairing data to Product
 * Combinations (2026-10-01 split) — this page deliberately keeps only the
 * two things that don't belong on any of those: a point-in-time valuation
 * and a printable export, per spec section 43's "one metric, one home."
 */
export async function render(ctx) {
  const host = h('div');
  mount(host, Loading('Loading reports…'));
  const [valuation, replenishment, expiry] = await Promise.all([
    getValuation(),
    getReplenishment(30),
    getExpiry(90),
  ]);

  mount(host, attentionCard(replenishment, expiry), valuationCard(valuation));
  return host;
}

/**
 * "Attention Needed" — a single consolidated, printable list of what a
 * manager would otherwise have to check ingredient-by-ingredient: stock
 * that's low or projected to run out, plus batches already expired or
 * expiring soon. Every value here is exactly what What to Buy and Expiry
 * already compute server-side (wt/forecasting.py's ingredient_depletion,
 * wt/api.py's expiry_monitor) — this only merges the two into one list, it
 * derives nothing new.
 *
 * 2026-08 audit: no PDF/report generation existed anywhere in this
 * codebase. Rather than add a server-side PDF dependency, this uses the
 * browser's own print-to-PDF (window.print()); the on-screen preview and
 * the printed output are the SAME dom node (.print-area, see styles/
 * app.css's @media print block).
 */
function attentionCard(replenishment, expiry) {
  const lowStock = (replenishment.rows || []).filter((r) => r.status === 'crit' || r.status === 'warn');
  const expiring = [...expiry.buckets.expired, ...expiry.buckets.soon];

  const rows = [
    ...lowStock.map((r) => ({
      kind: 'Low stock', name: r.name, severity: r.status,
      detail: r.daysLeft === null ? nodata('no usage rate yet') : h('span', {}, daysShort(r.daysLeft), ' left · ', qty(r.onHand, r.baseUnit)),
      action: r.suggestedOrderQty ? h('span', {}, 'Order ~', qty(r.suggestedOrderQty, r.suggestedOrderUnit || r.baseUnit)) : nodata('no suggestion yet'),
    })),
    ...expiring.map((b) => ({
      kind: b.expired ? 'Expired' : 'Expiring soon', name: b.ingredientName,
      severity: b.expired ? 'crit' : 'warn',
      detail: h('span', {}, daysShort(b.daysToExpiry), ' · ', qty(b.qtyRemaining, b.baseUnit), b.batchId ? ` · ${b.batchId}` : ''),
      action: b.expired ? 'Remove from stock' : 'Use first (FEFO)',
    })),
  ];

  return h('div', { class: 'print-area' },
    Card({ style: { marginBottom: '14px' } },
      CardHeader({
        title: 'Attention needed',
        desc: 'Low stock and expiring/expired batches, consolidated from What to Buy and Expiry — nothing here is computed differently for this report',
        right: h('div', { class: 'no-print' }, Button({ size: 'sm', onClick: () => window.print() }, 'Print / Save as PDF')),
      }),
      h('div', { class: 'print-only t-xs t-faint', style: { marginBottom: '8px' } },
        `Wings Theory — Attention Needed — printed ${new Date().toLocaleString('en-PH')}`),
      Table({
        columns: [
          { key: 'kind', label: 'Type', render: (r) => Pill(r.severity, r.kind) },
          { key: 'name', label: 'Ingredient', render: (r) => r.name },
          { key: 'detail', label: 'Detail', render: (r) => h('span', { class: 't-sm' }, r.detail) },
          { key: 'action', label: 'Suggested action', render: (r) => h('span', { class: 't-sm' }, r.action) },
        ],
        rows,
        empty: EmptyState({
          icon: '✓',
          title: 'Nothing needs attention right now',
          description: 'No ingredient is low/critical on stock cover, and nothing is expired or expiring within 7 days.',
        }),
      })));
}

function valuationCard(valuation) {
  if (valuation.available) {
    return Card({},
      CardHeader({ title: 'Inventory value', desc: 'On-hand stock at confirmed cost' }),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)' } },
        Card({ sunk: true }, Stat({ value: money(valuation.totalValue), label: 'Valued on hand' })),
        Card({ sunk: true }, Stat({ value: count(valuation.pricedItems), label: 'Items priced' })),
        Card({ sunk: true }, Stat({ value: count(valuation.totalItems), label: 'Items tracked' }))));
  }

  return Card({},
    CardHeader({ title: 'Inventory value', desc: 'On-hand stock at confirmed cost' }),
    EmptyState({ icon: '₱', title: 'Inventory value is not available yet', description: valuation.reason }));
}
