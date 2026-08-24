import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { toast } from '../../components/ui/Feedback.js';
import { stamp } from '../../lib/format.js';
import { session } from '../../lib/api/client.js';
import {
  getChecks, getModelDetails, getLoginAudit, getActionAudit, CAPABILITY_REFERENCE,
} from '../../lib/api/system.js';
import { changePassword, resetPassword, MIN_PASSWORD_LENGTH } from '../../lib/api/account.js';

/**
 * System & Settings — readiness checks, model details, access reference, and
 * account administration.
 *
 * Each model reports its own availability. When Prophet has no holdout test,
 * or FP-Growth has too few transactions, the reason the backend gives is
 * repeated verbatim. Nothing here fills an unavailable metric with a zero.
 */
export async function render(ctx) {
  const isAdmin = (session.user || {}).role === 'admin';
  const [state, models, logins, actions] = await Promise.all([
    getChecks(),
    getModelDetails().catch(() => null),
    isAdmin ? getLoginAudit(50).catch(() => null) : Promise.resolve(null),
    isAdmin ? getActionAudit(50).catch(() => null) : Promise.resolve(null),
  ]);

  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: '2fr 1fr', marginBottom: '14px' } },
      Card({},
        h('div', { class: 'card-title', style: { marginBottom: '12px' } }, 'End-to-end checks'),
        ...state.checks.map((c) => h('div', { class: 'mini-row' },
          h('span', {}, c.label),
          Pill(c.severity, c.detail)))),
      Card({ style: { display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' } },
        h('div', { style: { fontFamily: 'var(--font-display)', fontSize: '34px', fontWeight: '700' } },
          `${state.passing}/${state.total}`),
        h('div', { class: 't-sm t-soft', style: { textAlign: 'center', marginTop: '4px' } },
          state.passing === state.total
            ? 'checks passing — ready for service'
            : 'checks passing — setup still in progress'))),

    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)', marginBottom: '14px' } },
      modelCard('Demand forecasting', 'Prophet', models && models.demand_forecast,
        'Not available — needs more real sales history for a holdout test.'),
      modelCard('Anomaly detection', 'Isolation Forest', models && models.stock_loss_detection,
        'Retrains on every request once enough comparable entries exist.'),
      modelCard('Product combinations', 'FP-Growth', models && models.product_combinations,
        'Waiting on enough order history to find reliable pairings.')),

    Card({ style: { marginBottom: '14px' } },
      CardHeader({
        title: 'Who can do what',
        desc: 'Reference — the policy the server enforces lives in wt/auth.py',
      }),
      Table({
        columns: [
          { key: 'cap', label: 'Capability', render: (r) => r.capability },
          { key: 'cashier', label: 'Cashier', render: (r) => tick(r.cashier) },
          { key: 'kitchen', label: 'Kitchen', render: (r) => tick(r.kitchen) },
          { key: 'manager', label: 'Manager', render: (r) => tick(r.manager) },
          { key: 'admin', label: 'Admin', render: (r) => tick(r.admin) },
        ],
        rows: CAPABILITY_REFERENCE,
      }),
      h('div', { class: 't-xs t-faint', style: { marginTop: '10px' } },
        `Signed in as ${(session.user || {}).displayName || 'unknown'} · role: ${(session.user || {}).role || 'unknown'}`)),

    h('div', { class: 'grid', style: { gridTemplateColumns: isAdmin ? '1fr 1fr' : '1fr', marginBottom: '14px' } },
      changePasswordCard(),
      isAdmin ? resetPasswordCard() : null),

    isAdmin ? auditLogCard(logins, actions) : null);
}

/**
 * Admin-only (user.manage) — recent login attempts and recent sensitive
 * mutations, straight from the append-only login_event/admin_audit_log
 * tables. Not filtered or summarised beyond "most recent N": this is the
 * accountability record, so it shows exactly what was written, in order.
 */
function auditLogCard(logins, actions) {
  return h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr' } },
    Card({},
      CardHeader({ title: 'Recent sign-ins', desc: 'Every /auth/login attempt, successful or not' }),
      logins
        ? Table({
          columns: [
            { key: 'when', label: 'When', render: (e) => h('span', { class: 'mono t-xs' }, stamp(e.at)) },
            { key: 'who', label: 'Username', render: (e) => e.username },
            { key: 'role', label: 'Role', render: (e) => e.role || '—' },
            {
              key: 'result', label: 'Result',
              render: (e) => e.success
                ? Pill('good', 'signed in')
                : Pill('warn', (e.reason || 'failed').replace(/_/g, ' ')),
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
              render: (e) => e.previousValue !== null || e.newValue !== null
                ? h('span', { class: 't-xs' }, `${e.previousValue ?? '—'} → ${e.newValue ?? '—'}`)
                : h('span', { class: 't-xs t-faint' }, '—'),
            },
          ],
          rows: actions,
        })
        : h('div', { class: 't-sm t-faint' }, 'Could not load action history.')));
}

/**
 * POST /auth/change_password — self-service. Requires the caller's own
 * current password even with a valid session token. On success the backend
 * invalidates every other token already issued for this account (every other
 * signed-in device is logged out) and hands back a fresh one for this session.
 */
function changePasswordCard() {
  const current = h('input', { type: 'password', autocomplete: 'current-password' });
  const next = h('input', { type: 'password', autocomplete: 'new-password' });
  const confirm = h('input', { type: 'password', autocomplete: 'new-password' });
  const err = h('div', { class: 'login-err', style: { display: 'none' } });
  const submit = Button({ variant: 'primary', size: 'sm' }, 'Change password');

  submit.addEventListener('click', async () => {
    err.style.display = 'none';
    if (!current.value) { err.textContent = 'Enter your current password.'; err.style.display = ''; return; }
    if (next.value.length < MIN_PASSWORD_LENGTH) {
      err.textContent = `New password must be at least ${MIN_PASSWORD_LENGTH} characters.`; err.style.display = ''; return;
    }
    if (next.value !== confirm.value) { err.textContent = 'New password and confirmation do not match.'; err.style.display = ''; return; }
    submit.disabled = true;
    try {
      await changePassword({ currentPassword: current.value, newPassword: next.value, confirmNewPassword: confirm.value });
      current.value = ''; next.value = ''; confirm.value = '';
      toast('Password changed. Every other signed-in device has been logged out.', 'good');
    } catch (e) {
      err.textContent = e.message; err.style.display = '';
    } finally {
      submit.disabled = false;
    }
  });

  return Card({},
    CardHeader({ title: 'Change my password', desc: `At least ${MIN_PASSWORD_LENGTH} characters, not the shared default` }),
    h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Current password', current),
    h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'New password', next),
    h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Confirm new password', confirm),
    err,
    submit);
}

/**
 * POST /console/users/{username}/reset_password — admin-only (user.manage).
 * Generates a random password server-side and returns it exactly once; it is
 * never logged or stored anywhere else. Shown here once, in a dismissible
 * result panel, exactly as the backend's own docstring promises.
 */
function resetPasswordCard() {
  const username = h('input', { type: 'text', placeholder: 'e.g. cashier1', autocomplete: 'off' });
  const err = h('div', { class: 'login-err', style: { display: 'none' } });
  const resultHost = h('div', { style: { marginTop: '10px' } });
  const submit = Button({ size: 'sm' }, 'Reset password');

  submit.addEventListener('click', async () => {
    err.style.display = 'none';
    mount(resultHost, h('div'));
    const u = username.value.trim();
    if (!u) { err.textContent = 'Enter a username.'; err.style.display = ''; return; }
    submit.disabled = true;
    try {
      const r = await resetPassword(u);
      mount(resultHost, h('div', { class: 'callout callout-warn' },
        h('p', {}, h('strong', {}, `New password for ${r.username}: `),
          h('span', { class: 'mono' }, r.temporary_password)),
        h('p', { class: 't-xs', style: { marginTop: '6px' } }, r.note)));
      username.value = '';
    } catch (e) {
      err.textContent = e.message; err.style.display = '';
    } finally {
      submit.disabled = false;
    }
  });

  return Card({},
    CardHeader({ title: "Reset a user's password", desc: 'Admin only — hand the temporary password to the account holder directly' }),
    h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Username', username),
    err,
    submit,
    resultHost);
}

/**
 * One model panel. `detail` is whatever the backend reported — its live
 * parameters when it is working, its own stated reason when it is not.
 */
function modelCard(eyebrow, name, detail, fallbackNote) {
  let note = fallbackNote;
  let pill = null;

  if (detail && typeof detail === 'object') {
    if (detail.available === false) {
      note = detail.reason || fallbackNote;
      pill = Pill('flat', 'not available');
    } else {
      pill = Pill('good', 'live');
      const bits = [];
      if (detail.n_estimators) bits.push(`${detail.n_estimators} estimators`);
      if (detail.min_support) bits.push(`min support ${detail.min_support}`);
      if (detail.min_lift) bits.push(`min lift ${detail.min_lift}×`);
      if (detail.records_evaluated != null) bits.push(`${detail.records_evaluated} records evaluated`);
      if (detail.transactions_evaluated != null) bits.push(`${detail.transactions_evaluated} transactions analysed`);
      if (detail.min_pooled_samples != null) bits.push(`needs ${detail.min_pooled_samples} pooled samples`);
      if (detail.mae != null) bits.push(`MAE ${detail.mae}`);
      if (bits.length) note = bits.join(' · ');
    }
  }

  return Card({},
    h('div', { class: 'eyebrow' }, eyebrow),
    h('div', { style: { fontWeight: '600', marginBottom: '6px', display: 'flex', gap: '8px', alignItems: 'center' } },
      name, pill),
    h('div', { class: 't-sm t-soft' }, note));
}

function tick(allowed) {
  return allowed
    ? h('span', { class: 't-good t-strong' }, '✓')
    : h('span', { class: 't-faint' }, '—');
}
