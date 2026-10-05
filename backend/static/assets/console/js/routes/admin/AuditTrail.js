import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { stamp } from '../../lib/format.js';
import { getLoginAudit, getActionAudit } from '../../lib/api/system.js';

/**
 * Administration > Audit Trail — spec section 21: "Who changed what, and
 * when?" Straight from the append-only login_event/admin_audit_log tables,
 * most-recent-first, not filtered or summarised beyond "most recent N" —
 * this IS the accountability record, so it shows exactly what was written.
 */
export async function render(ctx) {
  const [logins, actions] = await Promise.all([
    getLoginAudit(100).catch(() => null),
    getActionAudit(100).catch(() => null),
  ]);

  return h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr' } },
    Card({},
      CardHeader({ title: 'Recent sign-ins', desc: 'Every sign-in attempt, successful or not' }),
      logins
        ? Table({
          columns: [
            { key: 'when', label: 'When', render: (e) => h('span', { class: 'mono t-xs' }, stamp(e.at)) },
            { key: 'who', label: 'Username', render: (e) => e.username },
            { key: 'role', label: 'Role', render: (e) => e.role || '—' },
            {
              key: 'result', label: 'Result',
              render: (e) => (e.success ? Pill('good', 'signed in') : Pill('warn', (e.reason || 'failed').replace(/_/g, ' '))),
            },
          ],
          rows: logins,
        })
        : h('div', { class: 't-sm t-faint' }, 'Could not load sign-in history.')),
    Card({},
      CardHeader({ title: 'Recent admin actions', desc: 'Ingredient edits, recipe changes, price changes, password resets' }),
      actions
        ? Table({
          columns: [
            { key: 'when', label: 'When', render: (e) => h('span', { class: 'mono t-xs' }, stamp(e.at)) },
            { key: 'who', label: 'By', render: (e) => e.userId },
            { key: 'what', label: 'What', render: (e) => `${e.action} · ${e.entityType} ${e.entityId}${e.field ? ' · ' + e.field : ''}` },
            {
              key: 'change', label: 'Change',
              render: (e) => (e.previousValue !== null || e.newValue !== null
                ? h('span', { class: 't-xs' }, `${e.previousValue ?? '—'} → ${e.newValue ?? '—'}`)
                : h('span', { class: 't-xs t-faint' }, '—')),
            },
          ],
          rows: actions,
        })
        : h('div', { class: 't-sm t-faint' }, 'Could not load action history.')));
}
