import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { stamp } from '../../lib/format.js';
import { getLoginAudit, getActionAudit } from '../../lib/api/system.js';

/**
 * Administration > Audit Trail — "who performed the action, what they
 * touched, who/what was affected, when, where in the system, and the
 * before/after value where one applies." Straight from the append-only
 * login_event/admin_audit_log tables, most-recent-first, not filtered or
 * summarised beyond "most recent N" — this IS the accountability record.
 *
 * Three tables, not one undifferentiated feed:
 *   - sign-ins (login_event) — unchanged, its own concern.
 *   - data changes (admin_audit_log, entity_type != 'page') — ingredient
 *     edits, recipe/menu changes, instructions, password resets, purchase
 *     orders. Each row names WHO, the AREA it happened in, WHAT/WHO was
 *     affected, and the before/after value where there is one.
 *   - page opens (admin_audit_log, entity_type == 'page') — which
 *     tab/page a user opened, kept separate so it never drowns out an
 *     actual data change in the same list.
 */

/** entity_type -> a human area label, so "ingredient" reads as
 * "Inventory" and a reader never has to know the underlying table name. */
const AREA_BY_ENTITY = {
  ingredient: 'Inventory',
  recipe: 'Menu & Recipes',
  menu_item: 'Menu & Recipes',
  purchase_order: 'Purchasing',
  owner_instruction: 'Instructions',
  order: 'Sales',
  user: 'Users & Access',
  role_permission: 'Users & Access',
};

const ACTION_LABELS = {
  create: 'Created', edit: 'Updated', price_change: 'Changed price',
  password_reset: 'Reset password', create_owner_bootstrap: 'Provisioned owner',
  receive: 'Received stock', count: 'Recorded count', operational_use: 'Recorded use',
  order: 'Marked ordered', cancel: 'Cancelled', void: 'Voided',
  acknowledge: 'Acknowledged', start: 'Started', complete: 'Completed',
  set_grant: 'Changed page access', view: 'Opened page',
};

function actionLabel(e) {
  return ACTION_LABELS[e.action] || e.action.replace(/_/g, ' ');
}

function affectedLabel(e) {
  if (e.entityType === 'owner_instruction') return `Instruction #${e.entityId}`;
  if (e.entityType === 'purchase_order') return `PO #${e.entityId}`;
  if (e.entityType === 'order') return `Order ${e.entityId}`;
  if (e.entityType === 'role_permission') return e.entityId;
  return e.note && (e.entityType === 'recipe' || e.entityType === 'user')
    ? e.note.split('·')[0].trim() || e.entityId
    : e.entityId;
}

export async function render(ctx) {
  const [logins, actions, pageViews] = await Promise.all([
    getLoginAudit(100).catch(() => null),
    getActionAudit(100, undefined).then((all) => all.filter((e) => e.entityType !== 'page')).catch(() => null),
    getActionAudit(50, 'page').catch(() => null),
  ]);

  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', marginBottom: '20px' } },
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
        CardHeader({ title: 'Pages opened', desc: 'Which tab/page each user opened, and when' }),
        pageViews
          ? Table({
            columns: [
              { key: 'when', label: 'When', render: (e) => h('span', { class: 'mono t-xs' }, stamp(e.at)) },
              { key: 'who', label: 'User', render: (e) => e.userId },
              { key: 'where', label: 'Area', render: (e) => e.note || e.entityId },
            ],
            rows: pageViews,
          })
          : h('div', { class: 't-sm t-faint' }, 'Could not load page-open history.'))),
    Card({},
      CardHeader({ title: 'Data changes', desc: 'Who changed what, where in the system, and the before/after value' }),
      actions
        ? Table({
          columns: [
            { key: 'when', label: 'When', render: (e) => h('span', { class: 'mono t-xs' }, stamp(e.at)) },
            { key: 'who', label: 'User', render: (e) => e.userId },
            { key: 'action', label: 'Action', render: (e) => actionLabel(e) },
            { key: 'area', label: 'Area', render: (e) => AREA_BY_ENTITY[e.entityType] || e.entityType },
            { key: 'affected', label: 'Affected', render: (e) => affectedLabel(e) },
            {
              key: 'changed', label: 'Changed',
              render: (e) => (e.field ? h('span', { class: 't-xs' }, e.field) : h('span', { class: 't-xs t-faint' }, '—')),
            },
            {
              key: 'change', label: 'Before → After',
              render: (e) => (e.previousValue !== null || e.newValue !== null
                ? h('span', { class: 't-xs' }, `${e.previousValue ?? '—'} → ${e.newValue ?? '—'}`)
                : h('span', { class: 't-xs t-faint' }, '—')),
            },
          ],
          rows: actions,
        })
        : h('div', { class: 't-sm t-faint' }, 'Could not load action history.')));
}
