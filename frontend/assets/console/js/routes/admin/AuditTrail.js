import { h, mount } from '../../lib/dom.js';
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

/** Consecutive identical (user, area) page-view rows collapsed into one,
 * carrying a "×N" count and its own time span — a user bouncing between the
 * same two tabs for twenty minutes no longer reads as twenty indistinguishable
 * rows. Nothing is deleted: every underlying admin_audit_log row is still
 * counted in `n` and still exists exactly as recorded; this only changes how
 * the already-fetched list is grouped for display. Only ADJACENT duplicates
 * collapse (the list is already newest-first) — two non-adjacent visits to
 * the same page stay two separate rows, since something else happened
 * between them. */
function collapseRepeats(views) {
  const out = [];
  for (const v of views) {
    const key = `${v.userId}|${v.note || v.entityId}`;
    const last = out[out.length - 1];
    if (last && last.__key === key) {
      last.n += 1;
      last.oldestAt = v.at; // list is newest-first, so the span's older end moves back
    } else {
      out.push({ ...v, __key: key, n: 1, oldestAt: v.at });
    }
  }
  return out;
}

function matchesFilter(e, { user, from, to }) {
  if (user && !String(e.userId || '').toLowerCase().includes(user)) return false;
  if (from && e.at < from) return false;
  if (to && e.at > `${to}T23:59:59`) return false;
  return true;
}

export async function render(ctx) {
  const [logins, actions, pageViews] = await Promise.all([
    getLoginAudit(100).catch(() => null),
    getActionAudit(100, undefined).then((all) => all.filter((e) => e.entityType !== 'page')).catch(() => null),
    getActionAudit(50, 'page').catch(() => null),
  ]);

  const filter = { user: '', from: '', to: '' };
  const changesHost = h('div');

  function drawChanges() {
    if (!actions) { mount(changesHost, h('div', { class: 't-sm t-faint' }, 'Could not load action history.')); return; }
    const rows = actions.filter((e) => matchesFilter(e, filter));
    mount(changesHost, Table({
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
      rows,
      empty: h('div', { class: 't-sm t-faint', style: { padding: '14px 0' } }, 'No data changes match this filter.'),
    }));
  }

  const userInput = h('input', {
    type: 'text', placeholder: 'Filter by username…',
    onInput: (e) => { filter.user = e.target.value.trim().toLowerCase(); drawChanges(); },
  });
  const fromInput = h('input', { type: 'date', onChange: (e) => { filter.from = e.target.value; drawChanges(); } });
  const toInput = h('input', { type: 'date', onChange: (e) => { filter.to = e.target.value; drawChanges(); } });

  const collapsedViews = pageViews ? collapseRepeats(pageViews) : null;

  drawChanges();

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
        CardHeader({ title: 'Pages opened', desc: 'Which tab/page each user opened, and when — consecutive repeats of the same page collapse into one row' }),
        collapsedViews
          ? Table({
            columns: [
              {
                key: 'when',
                label: 'When',
                render: (e) => (e.n > 1
                  ? h('span', { class: 'mono t-xs' }, stamp(e.oldestAt), ' – ', stamp(e.at))
                  : h('span', { class: 'mono t-xs' }, stamp(e.at))),
              },
              { key: 'who', label: 'User', render: (e) => e.userId },
              { key: 'where', label: 'Area', render: (e) => e.note || e.entityId },
              { key: 'count', label: 'Times', render: (e) => (e.n > 1 ? Pill('info', `×${e.n}`) : '') },
            ],
            rows: collapsedViews,
          })
          : h('div', { class: 't-sm t-faint' }, 'Could not load page-open history.'))),
    Card({},
      CardHeader({
        title: 'Data changes',
        desc: 'Who changed what, where in the system, and the before/after value',
        right: h('div', { style: { display: 'flex', gap: '8px' } }, userInput, fromInput, toInput),
      }),
      changesHost));
}
