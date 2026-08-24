import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { qty, days, num, asText, nodata } from '../../lib/format.js';
import { getReplenishment } from '../../lib/api/intelligence.js';

/**
 * Forecast & Insights > Replenishment.
 *
 * Every number in this table is the backend's: runway, suggested order
 * quantity and risk all come from ingredient_depletion(). Nothing is
 * recomputed here (spec section 3).
 *
 * The reorder-point rule matters: an ingredient whose reorder point the owner
 * never set is NOT treated as having one of zero, by the backend or by this
 * screen. It shows "none set" plus the system's suggestion, clearly labelled
 * as a suggestion.
 */
export async function render(ctx) {
  const { rows, belowReorderPoint, configured } = await getReplenishment(30);

  const urgent = rows.filter((r) => r.status === 'crit' || r.status === 'warn' || r.belowReorderPoint);
  const measured = rows.filter((r) => r.usedPerDay !== null);

  const headline = urgent.length === 0
    ? Card({ style: { marginBottom: '14px' } }, EmptyState({
      icon: '✓',
      title: 'Nothing needs restocking right now',
      description: measured.length === 0
        ? 'No ingredient has a measured usage rate yet, so there is no runway to compare against a lead time. This is a data gap, not an all-clear.'
        : 'Every ingredient with a measured usage rate has a comfortable buffer against its supplier lead time.',
      tight: true,
    }))
    : h('div', { class: 'callout callout-warn' },
      h('p', {},
        h('strong', {}, `${urgent.length} ${urgent.length === 1 ? 'ingredient needs' : 'ingredients need'} attention. `),
        belowReorderPoint > 0
          ? `${belowReorderPoint} of them are below a reorder point the owner set.`
          : 'None are below a manager-set reorder point — these are runway warnings.'));

  return h('div', {},
    headline,
    Card({},
      CardHeader({
        title: 'Full replenishment list',
        desc: `Soonest to run out shown first · ${configured} of ${rows.length} have a reorder point set`,
        right: Button({ size: 'sm', onClick: () => exportCsv(rows) }, '↓ Download'),
      }),
      Table({
        columns: [
          { key: 'name', label: 'Ingredient', render: (r) => h('strong', {}, r.name), sortValue: (r) => r.name.toLowerCase() },
          {
            key: 'onhand',
            label: 'On hand',
            render: (r) => h('span', { class: 'mono t-sm' }, qty(r.onHand, r.baseUnit)),
          },
          {
            key: 'used',
            label: 'Used / day',
            render: (r) => h('span', { class: 'mono t-sm' },
              // No measured rate is not zero consumption — it means this item
              // has not moved in the lookback window.
              r.usedPerDay === null
                ? nodata('not measured')
                : qty(r.usedPerDay, r.baseUnit)),
          },
          {
            key: 'runway',
            label: 'Runway',
            render: (r) => h('span', { class: 'mono t-sm' }, days(r.daysLeft, 'no rate yet')),
            sortValue: (r) => r.daysLeft,
          },
          {
            key: 'lead',
            label: 'Lead time',
            render: (r) => h('span', { class: 'mono t-sm' }, days(r.leadTimeDays, 'not set')),
          },
          { key: 'rop', label: 'Reorder point', render: ropCell },
          {
            key: 'order',
            label: 'Suggested order',
            render: (r) => h('span', { class: 'mono t-sm' },
              r.suggestedOrderQty === null
                ? nodata('no basis yet')
                : `~${asText.num(r.suggestedOrderQty, '', 2)} ${r.suggestedOrderUnit || r.baseUnit}`),
          },
          { key: 'status', label: 'Status', render: statusCell },
        ],
        rows,
        empty: EmptyState({
          icon: '≡',
          title: 'No ingredients to replenish',
          description: 'This list fills in once ingredients exist and have movement history.',
        }),
      })));
}

function ropCell(r) {
  if (r.reorderPoint !== null) {
    return h('span', { class: 'mono t-sm' }, qty(r.reorderPoint, r.baseUnit));
  }
  return h('div', {},
    h('span', { class: 'nodata' }, 'none set'),
    r.suggestedReorderPoint !== null
      ? h('div', { class: 't-xs t-faint' },
        `suggested ~${asText.num(r.suggestedReorderPoint, '', 2)} ${r.baseUnit}`)
      : null);
}

function statusCell(r) {
  if (r.belowReorderPoint) return Pill('crit', 'below reorder point');
  // r.risk is the backend's own un-collapsed value (RISK_ORDER in
  // forecasting.py) -- 'out' and 'critical' both map to the 'crit'
  // severity for color, but they're different urgency, so distinguish the
  // label using the raw value rather than only the collapsed severity.
  if (r.risk === 'out') return Pill('crit', 'out of stock');
  switch (r.status) {
    case 'crit': return Pill('crit', 'running out');
    case 'warn': return Pill('warn', 'order soon');
    case 'good': return Pill('good', 'enough stock');
    default: return Pill('flat', 'no usage rate');
  }
}

function exportCsv(rows) {
  const head = ['ingredient', 'on_hand', 'unit', 'used_per_day', 'days_left',
    'lead_time_days', 'reorder_point', 'suggested_order_qty', 'status'];
  const lines = [head.join(',')];
  for (const r of rows) {
    lines.push([
      r.name, asText.num(r.onHand, '', 3), r.baseUnit,
      asText.num(r.usedPerDay, 'not measured', 4),
      asText.num(r.daysLeft, 'no rate yet', 1),
      asText.num(r.leadTimeDays, 'not set', 0),
      asText.num(r.reorderPoint, 'none set', 3),
      asText.num(r.suggestedOrderQty, 'no basis yet', 3),
      r.risk || '',
    ].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = h('a', {
    href: URL.createObjectURL(blob),
    download: `wings-theory-replenishment-${new Date().toISOString().slice(0, 10)}.csv`,
  });
  document.body.appendChild(a);
  a.click();
  a.remove();
}
