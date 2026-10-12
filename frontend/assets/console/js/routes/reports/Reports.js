import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Loading } from '../../components/ui/Feedback.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { money, count } from '../../lib/format.js';
import { getValuation } from '../../lib/api/reports.js';

/**
 * Business Reports (Sales & History > Business Reports for Admin; Reports &
 * History > Business Reports for Owner) — inventory valuation only. Sales
 * KPIs/trend live on Sales Overview, rankings on Best Sellers, pairing data
 * on Menu & Recipes > Combos. The "Attention Needed" list that used to sit
 * here was removed (2026-10): it was a straight merge of What to Buy and
 * Expiry's own numbers with nothing computed differently, so it was a
 * second place to read the same two lists rather than a distinct report —
 * see those pages directly instead.
 */
export async function render(ctx) {
  const host = h('div');
  mount(host, Loading('Loading reports…'));
  const valuation = await getValuation();
  mount(host, valuationCard(valuation));
  return host;
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
