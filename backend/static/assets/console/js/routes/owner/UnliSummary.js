import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { count, qty, stamp } from '../../lib/format.js';
import { getReconciliationHistory } from '../../lib/api/intelligence.js';

/**
 * Owner > Inventory & Losses > Unlimited Wings Summary — spec section 26.
 * Read-only: Owner holds no unli.* capability at all (wt/auth.py — see
 * Reconciliation.js's own docstring), so the approve/tolerance/recalibrate
 * controls on Admin's Unlimited Wings page are never reachable here, only
 * the history a completed reconciliation leaves behind. If that call ever
 * 403s for this role, the honest answer ("not available to your role") is
 * shown rather than a fabricated empty table.
 */
export async function render(ctx) {
  let history;
  try {
    history = await getReconciliationHistory({ limit: 60 });
  } catch (e) {
    return Card({},
      CardHeader({ title: 'Unlimited Wings Summary' }),
      EmptyState({ icon: '—', title: 'Not available to your role', description: e.message || 'This information is not accessible with your current access.' }));
  }

  const rows = Array.isArray(history) ? history : (history.items || []);
  const withVariance = rows.filter((r) => r.varianceBase !== undefined && r.varianceBase !== null);
  const avgVariancePct = withVariance.length
    ? withVariance.reduce((sum, r) => sum + (r.variancePct || 0), 0) / withVariance.length
    : null;

  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(2,1fr)', marginBottom: '14px' } },
      Card({}, Stat({ value: count(rows.length), label: 'Reconciled sessions (recent)' })),
      Card({}, Stat({ value: avgVariancePct === null ? '—' : `${avgVariancePct.toFixed(1)}%`, label: 'Average variance vs. estimate' }))),
    Card({},
      CardHeader({ title: 'Recent sessions', desc: 'Estimated consumption vs. what the kitchen actually weighed out' }),
      Table({
        columns: [
          { key: 'when', label: 'When', render: (r) => h('span', { class: 'mono t-xs' }, stamp(r.serviceDate)) },
          { key: 'stream', label: 'Stream', render: (r) => r.stream || '—' },
          { key: 'estimated', label: 'Estimated', render: (r) => qty(r.theoreticalBase, r.baseUnit, '—') },
          { key: 'actual', label: 'Actual', render: (r) => qty(r.actualBase, r.baseUnit, '—') },
          { key: 'status', label: 'Status', render: (r) => r.status || '—' },
        ],
        rows,
        empty: EmptyState({ icon: '—', title: 'No reconciled sessions yet', description: 'Sessions appear here once Admin signs one off.' }),
      })));
}
