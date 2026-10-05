import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { stamp } from '../../lib/format.js';
import { session } from '../../lib/api/client.js';
import {
  getChecks, getModelDetails, getLoginAudit, getActionAudit, getDataQuality,
} from '../../lib/api/system.js';
import { getCapabilityMatrix } from '../../lib/api/permissions.js';
import { changePassword, resetPassword, MIN_PASSWORD_LENGTH } from '../../lib/api/account.js';
import { getComboPricingConfig, setComboPricingConfig } from '../../lib/api/comboPricing.js';

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
  const [state, models, logins, actions, capabilities, comboConfig, dataIntegrity] = await Promise.all([
    getChecks(),
    getModelDetails().catch(() => null),
    isAdmin ? getLoginAudit(50).catch(() => null) : Promise.resolve(null),
    isAdmin ? getActionAudit(50).catch(() => null) : Promise.resolve(null),
    getCapabilityMatrix().catch(() => []),
    isAdmin ? getComboPricingConfig().catch(() => null) : Promise.resolve(null),
    isAdmin ? getDataQuality().catch(() => null) : Promise.resolve(null),
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
        rows: capabilities,
      }),
      h('div', { class: 't-xs t-faint', style: { marginTop: '10px' } },
        `Signed in as ${(session.user || {}).displayName || 'unknown'} · role: ${(session.user || {}).role || 'unknown'}`)),

    h('div', { class: 'grid', style: { gridTemplateColumns: isAdmin ? '1fr 1fr' : '1fr', marginBottom: '14px' } },
      changePasswordCard(),
      isAdmin ? resetPasswordCard() : null),

    isAdmin && comboConfig ? h('div', { style: { marginBottom: '14px' } }, comboPricingConfigCard(comboConfig)) : null,

    isAdmin && dataIntegrity ? h('div', { style: { marginBottom: '14px' } }, dataIntegrityCard(dataIntegrity)) : null,

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
 * Combo pricing settings — PATCH /console/combo-pricing-config
 * (wt/combo_pricing.py's get/set_combo_pricing_config), admin-only
 * (inventory.write). Every field is None ("not configured") until set
 * here; Sales & Menu's "Suggest a combo" reads this same config to turn a
 * cost estimate into a suggested price range, and shows its own "Configure
 * a target food-cost percentage..." message when target_food_cost_pct is
 * still unset — this card is where that gets fixed.
 *
 * The backend's own PATCH contract only ever WRITES the fields present in
 * the request body (exclude_unset=True on its side), but since this form
 * always submits its own full current state, sending every field every
 * time is equivalent and idempotent — a field left blank round-trips back
 * to null ("not configured"), which is the same "explicit null clears it"
 * behaviour the backend already documents for partial updates.
 */
function comboPricingConfigCard(config) {
  const num = (v) => (v === null || v === undefined ? '' : String(v));

  const targetInput = h('input', { type: 'number', step: '0.1', min: '1', max: '100', value: num(config.targetFoodCostPct), placeholder: 'e.g. 30' });
  const laborMethodSelect = h('select', {},
    h('option', { value: '' }, 'Not set'),
    h('option', { value: 'fixed_per_serving', selected: config.laborMethod === 'fixed_per_serving' ? 'selected' : undefined }, 'Fixed amount per serving'),
    h('option', { value: 'pct_of_ingredient_cost', selected: config.laborMethod === 'pct_of_ingredient_cost' ? 'selected' : undefined }, '% of ingredient cost'));
  laborMethodSelect.value = config.laborMethod || '';
  const laborFixedInput = h('input', { type: 'number', step: '0.01', min: '0', value: num(config.laborFixedAmount), placeholder: '0.00' });
  const laborPctInput = h('input', { type: 'number', step: '0.1', min: '0', max: '100', value: num(config.laborPct), placeholder: 'e.g. 15' });
  const overheadInput = h('input', { type: 'number', step: '0.1', min: '0', max: '100', value: num(config.overheadPct), placeholder: 'e.g. 10' });
  const minPriceInput = h('input', { type: 'number', step: '0.01', min: '0', value: num(config.preferredMinPrice), placeholder: 'optional' });
  const maxPriceInput = h('input', { type: 'number', step: '0.01', min: '0', value: num(config.preferredMaxPrice), placeholder: 'optional' });
  const maxDiscountInput = h('input', { type: 'number', step: '0.1', min: '0', max: '100', value: num(config.maxDiscountPct), placeholder: 'optional' });

  const laborFixedRow = h('label', { class: 'field', style: { marginTop: '10px' } }, 'Labor cost per serving (₱)', laborFixedInput);
  const laborPctRow = h('label', { class: 'field', style: { marginTop: '10px' } }, 'Labor cost (% of ingredient cost)', laborPctInput);
  const syncLaborRows = () => {
    laborFixedRow.style.display = laborMethodSelect.value === 'fixed_per_serving' ? '' : 'none';
    laborPctRow.style.display = laborMethodSelect.value === 'pct_of_ingredient_cost' ? '' : 'none';
  };
  syncLaborRows();
  laborMethodSelect.addEventListener('change', syncLaborRows);

  const err = h('div', { class: 'login-err', style: { display: 'none' } });
  const submit = Button({ variant: 'primary', size: 'sm' }, 'Save pricing settings');

  submit.addEventListener('click', async () => {
    err.style.display = 'none';
    const toFloat = (input) => (input.value.trim() === '' ? null : Number(input.value));
    const method = laborMethodSelect.value || null;
    const body = {
      target_food_cost_pct: toFloat(targetInput),
      labor_method: method,
      labor_fixed_amount: method === 'fixed_per_serving' ? toFloat(laborFixedInput) : null,
      labor_pct: method === 'pct_of_ingredient_cost' ? toFloat(laborPctInput) : null,
      overhead_pct: toFloat(overheadInput),
      preferred_min_price: toFloat(minPriceInput),
      preferred_max_price: toFloat(maxPriceInput),
      max_discount_pct: toFloat(maxDiscountInput),
    };
    if (body.preferred_min_price !== null && body.preferred_max_price !== null
        && body.preferred_min_price > body.preferred_max_price) {
      err.textContent = 'Preferred minimum price cannot be above the preferred maximum.';
      err.style.display = '';
      return;
    }
    submit.disabled = true;
    try {
      await setComboPricingConfig(body);
      toast('Combo pricing settings saved.', 'good');
    } catch (e) {
      err.textContent = e.message; err.style.display = '';
    } finally {
      submit.disabled = false;
    }
  });

  return Card({},
    CardHeader({
      title: 'Combo pricing settings',
      desc: 'Feeds "Suggest a combo" on Sales & Menu — a target food-cost % is required before it can suggest a price range',
    }),
    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '10px 16px' } },
      h('label', { class: 'field' }, 'Target food-cost (%)', targetInput),
      h('label', { class: 'field' }, 'Overhead (% of ingredient cost)', overheadInput),
      h('label', { class: 'field' }, 'Labor method', laborMethodSelect),
      h('div'),
      laborFixedRow,
      laborPctRow,
      h('label', { class: 'field' }, 'Preferred minimum price (₱)', minPriceInput),
      h('label', { class: 'field' }, 'Preferred maximum price (₱)', maxPriceInput),
      h('label', { class: 'field' }, 'Max discount off individual total (%)', maxDiscountInput)),
    err,
    h('div', { style: { marginTop: '12px' } }, submit));
}

/**
 * Data integrity audit — GET /console/data-quality (wt/data_quality.py),
 * a set of read-only, deterministic SQL checks over the raw tables the ML
 * layer reads (duplicate orders, orphaned rows, missing links, invalid
 * quantities). Distinct from the "Data Quality" nav page (renamed from
 * Verification — placeholder VALUES an owner/admin confirms): this is
 * structural correctness of the DATA ITSELF, found by code, never entered
 * by a person. Named "Data integrity audit" here specifically so the two
 * are never confused in the UI the way their backend module names
 * (wt/data_quality.py vs. wt/verification.py) already risk being confused
 * in the code.
 */
function dataIntegrityCard(report) {
  return Card({},
    CardHeader({
      title: 'Data integrity audit',
      desc: `${report.checksWithFindings} of ${report.checksRun} checks found something — structural checks over raw tables, separate from Data Quality's placeholder-value confirmations`,
      right: h('div', { class: 't-xs t-faint' }, 'as of ', stamp(report.generatedAt)),
    }),
    Table({
      columns: [
        { key: 'check', label: 'Check', render: (c) => h('span', { class: 'mono t-xs' }, c.check) },
        { key: 'description', label: 'Description', render: (c) => h('span', { class: 't-sm' }, c.description) },
        { key: 'count', label: 'Found', render: (c) => h('span', { class: 'mono t-sm' }, String(c.count)) },
        {
          key: 'status', label: 'Status',
          render: (c) => c.status === 'ok'
            ? Pill('good', 'ok')
            : h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center' } },
              Pill('warn', `${c.count} found`),
              c.examples.length
                ? h('a', { href: '#', class: 't-xs', onClick: (e) => { e.preventDefault(); openExamplesModal(c); } }, 'examples')
                : null),
        },
      ],
      rows: report.checks,
    }));
}

function openExamplesModal(check) {
  const rows = check.examples.map((ex) => h('div', { style: { padding: '8px 0', borderBottom: '1px solid var(--line-soft)' } },
    h('div', { class: 't-xs mono' }, Object.entries(ex).map(([k, v]) => `${k}: ${v}`).join('  ·  '))));
  Modal({ title: check.check, desc: check.description, onClose: () => {} },
    rows.length ? h('div', {}, ...rows) : EmptyState({ icon: '—', title: 'No example rows', tight: true }));
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
      // Rolling-backtest shape (demand_forecast / store_demand_forecast —
      // see evaluate_demand_model() / evaluate_store_demand_model() in
      // wt/forecasting.py): no top-level mae/n_estimators, so without this
      // branch `bits` stays empty and the card silently falls back to
      // fallbackNote's "not available" text even though the model is live.
      if (detail.items_evaluated != null) {
        bits.push(`${detail.items_evaluated} of ${detail.items_considered} items backtested`);
        const prophetWape = (detail.fleet_summary || {}).prophet
          ? detail.fleet_summary.prophet.mean_wape : null;
        if (prophetWape != null) bits.push(`Prophet mean WAPE ${prophetWape}%`);
        const pvb = detail.prophet_vs_baseline;
        if (pvb && pvb.prophet_actually_better != null) {
          bits.push(pvb.prophet_actually_better
            ? `beats best baseline by ${pvb.absolute_improvement_wape_pts} pts`
            : 'not measurably better than best baseline');
        }
      }
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
