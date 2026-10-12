import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast, Loading } from '../../components/ui/Feedback.js';
import { count, qty, pct, num, nodata, str } from '../../lib/format.js';
import {
  getReconciliation, submitReconciliation, getReconciliationHistory,
  reviewReconciliation, setTolerance, recalibrate, listFlavors, setFlavor,
  previewReconciliationRevision, reviseReconciliation, getReconciliationRevisions,
} from '../../lib/api/intelligence.js';
import { session } from '../../lib/api/client.js';

/**
 * Forecast & Insights > Reconciliation.
 *
 * The three-stage provisional/actual model for unlimited streams. The till
 * deducts an estimate per head; the kitchen weighs what actually went out;
 * only the DIFFERENCE is posted to stock. Every write on this page calls the
 * one backend endpoint that already owns that arithmetic — nothing here
 * recomputes a variance, a coefficient, or a correction.
 *
 * Manager-only controls (sign-off, tolerance, recalibration, flavor pricing —
 * all gated server-side on unli.approve / recipe.write, manager or admin) are
 * shown only to those roles as a matter of UX, but the backend is what
 * actually enforces it: a 403 here is expected and handled, not a bug.
 */
const canApprove = () => ['manager', 'admin'].includes((session.user || {}).role);
/** Same roles the weigh-in form itself is built for (unli.reconcile.write,
 * server-side) -- revising is the same "someone at the scale" workflow,
 * just applied to a day already on record. */
const canRevise = () => ['kitchen', 'admin'].includes((session.user || {}).role);

export async function render(ctx) {
  const today = new Date().toISOString().slice(0, 10);
  let serviceDate = today;

  const host = h('div');

  const datePicker = h('input', {
    type: 'date', value: today,
    onChange: (e) => { serviceDate = e.target.value || today; draw(); },
  });

  async function draw() {
    mount(host, Loading('Loading streams…'));
    const [{ pending, streams }, history] = await Promise.all([
      getReconciliation(serviceDate),
      getReconciliationHistory({ limit: 120 }),
    ]);
    const rows = pending.streams || [];
    const tolerance = pending.tolerance_ratio ?? null;
    const awaiting = history.filter((h2) => h2.validationState === 'pending_review');

    const doneCount = rows.filter((r) => r.reconciled).length;
    const outsideTolerance = rows.filter((r) => r.reconciled
      && r.reconciled.status && r.reconciled.status !== 'within_tolerance').length;

    mount(host,
      h('div', { class: 'callout callout-info' },
        h('p', {},
          h('strong', {}, 'How this works. '),
          'Unlimited orders deduct a provisional estimate at the till. The kitchen weighs what actually went out at end of shift; only the gap between estimate and measured is posted to stock. Days within ',
          tolerance === null ? 'the configured tolerance' : `${(tolerance * 100).toFixed(0)}%`,
          ' sign off automatically; anything further out waits for a manager.')),

      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', marginBottom: '14px' } },
        Card({}, Stat({ value: `${doneCount}/${rows.length}`, label: 'Streams reconciled' })),
        Card({}, Stat({ value: count(rows.length - doneCount), label: 'Still to weigh' })),
        Card({}, Stat({ value: count(awaiting.length), label: 'Awaiting sign-off' })),
        toleranceTile(tolerance, draw)),

      Card({ style: { marginBottom: '14px' } },
        CardHeader({
          title: `Streams for ${serviceDate}`,
          desc: 'Weigh out, weigh back, and the difference posts itself',
        }),
        Table({
          columns: [
            { key: 'stream', label: 'Stream', render: (r) => h('strong', {}, r.label) },
            { key: 'from', label: 'Deducts from', render: (r) => r.ingredient_name },
            { key: 'heads', label: 'Covers', render: (r) => h('span', { class: 'mono t-sm' }, count(r.heads)) },
            {
              key: 'prov',
              label: 'Provisional',
              render: (r) => h('span', { class: 'mono t-sm' }, qty(r.theoretical_base, r.base_unit)),
            },
            { key: 'state', label: 'State', render: stateCell },
            {
              key: 'act',
              label: '',
              render: (r) => (r.reconciled
                ? h('span', { class: 't-xs t-faint' }, 'recorded')
                : Button({ size: 'sm', onClick: () => focusWeighIn(r.item_key) }, 'Weigh in')),
            },
          ],
          rows,
          empty: EmptyState({
            icon: '⚖',
            title: 'No unlimited streams served on this date',
            description: 'Streams appear here once an unlimited order has been taken for the day.',
          }),
        })),

      weighInCard(rows, serviceDate, draw, tolerance),

      awaitingCard(awaiting, draw),

      flavorsCard(),

      Card({ style: { marginBottom: '14px' } },
        CardHeader({
          title: 'Portion coefficients',
          desc: 'The frozen baseline is what variance is measured against; the rolling value is what the till deducts. This is the measured average total across a whole sitting (multiple refills) — not the 3-piece serving/refill size used per round.',
          right: canApprove() ? [
            Button({ size: 'sm', onClick: () => runRecalibrate(true, draw) }, 'Preview recalibration'),
            Button({ variant: 'primary', size: 'sm', onClick: () => runRecalibrate(false, draw) }, 'Recalibrate'),
          ] : null,
        }),
        Table({
          columns: [
            { key: 'stream', label: 'Stream', render: (s) => s.stream },
            { key: 'from', label: 'Deducts from', render: (s) => s.deductsFrom },
            {
              key: 'baseline',
              label: 'Baseline λ',
              render: (s) => h('span', { class: 'mono t-sm t-soft' },
                s.baselineCoefficient === null ? nodata('n/a') : `${num(s.baselineCoefficient, { decimals: 1 }).textContent} ${s.baseUnit}`),
            },
            {
              key: 'lambda',
              label: 'Rolling λ',
              render: (s) => h('span', { class: 'mono t-sm' },
                s.coefficient === null
                  ? nodata('not calibrated')
                  : `${num(s.coefficient, { decimals: 2 }).textContent} ${s.baseUnit} / head`),
            },
            { key: 'drift', label: 'Drift', render: driftCell },
            {
              key: 'window',
              label: 'Window',
              render: (s) => h('span', { class: 'mono t-sm' },
                s.windowDays === null ? nodata('n/a') : `${s.windowDays}d`),
            },
            {
              key: 'revisions',
              label: 'Revisions',
              render: (s) => h('span', { class: 'mono t-sm t-faint' }, String(s.revisions)),
            },
          ],
          rows: streams,
          empty: EmptyState({
            icon: 'λ',
            title: 'No unlimited streams configured',
            description: 'Coefficients appear once unlimited items are set up.',
          }),
        })),

      historyCard(history, draw));
  }

  await draw();

  return h('div', {},
    h('label', { class: 'field', style: { maxWidth: '220px', marginBottom: '14px' } },
      'Service date', datePicker),
    host);
}

function stateCell(r) {
  if (!r.reconciled) return Pill('warn', 'not weighed yet');
  const status = r.reconciled.status || r.reconciled.validation_state;
  if (status === 'within_tolerance') return Pill('good', 'signed off');
  return Pill('crit', 'outside tolerance');
}

/**
 * Drift is only meaningful against a frozen baseline. No baseline means there
 * is no drift to report — not 0%.
 */
function driftCell(s) {
  if (s.driftPct === null) return h('span', { class: 'nodata' }, 'no baseline yet');
  const sev = Math.abs(s.driftPct) < 5 ? 'good' : Math.abs(s.driftPct) < 15 ? 'warn' : 'crit';
  return h('span', { class: `t-${sev}` }, pct(s.driftPct));
}

/**
 * The end-of-shift weigh-in, as an inline form built from the same field and
 * button primitives as the rest of the console. Deliberately three explicit
 * measurements rather than one "actual used" box: the kitchen weighs what it
 * took out, what came back, and any top-ups — the subtraction is the
 * backend's, not something a tired closer does in their head.
 *
 * The preview line below mirrors the server's own arithmetic so the person at
 * the scale sees the likely verdict before submitting — it is informational
 * only; the figures actually recorded are whatever POST /unli/reconciliation
 * returns, not this preview.
 */
function weighInCard(rows, serviceDate, refresh, tolerance) {
  const open = rows.filter((r) => !r.reconciled);

  if (open.length === 0) {
    return Card({ style: { marginBottom: '14px' } }, EmptyState({
      icon: '✓',
      title: rows.length ? 'Every stream is weighed in for this date' : 'Nothing to weigh in',
      description: rows.length
        ? 'The variances have been posted and signed off where they fell inside tolerance.'
        : 'Once an unlimited order is served on this date, its stream appears here to weigh in.',
      tight: true,
    }));
  }

  const stream = h('select', { id: 'weighin-stream' },
    ...open.map((r) => h('option', { value: r.item_key }, r.label)));
  const opening = h('input', { type: 'number', step: '0.01', min: '0', placeholder: '0.00' });
  const closing = h('input', { type: 'number', step: '0.01', min: '0', placeholder: '0.00' });
  const added = h('input', { type: 'number', step: '0.01', min: '0', value: '0' });
  const error = h('div', { class: 'login-err', style: { display: 'none' } });
  const preview = h('div', { style: { marginTop: '10px' } });
  const submit = Button({ variant: 'primary' }, 'Record measurement');

  const currentStream = () => open.find((r) => r.item_key === stream.value);
  const unitNote = h('div', { class: 'hint' }, `Measured in ${(currentStream() || {}).base_unit || ''}.`);

  function updatePreview() {
    const s = currentStream();
    unitNote.textContent = `Measured in ${s ? s.base_unit : ''}.`;
    const o = Number(opening.value); const a = Number(added.value) || 0; const c = Number(closing.value);
    if (!s || Number.isNaN(o) || Number.isNaN(c) || opening.value === '' || closing.value === '') {
      mount(preview, h('div'));
      return;
    }
    if (c > o + a) {
      mount(preview, h('div', { class: 'callout callout-warn' }, h('p', {}, 'Leftover cannot be more than what went out.')));
      return;
    }
    const actual = o + a - c;
    const variance = actual - (s.theoretical_base || 0);
    const ratio = s.theoretical_base > 0 ? variance / s.theoretical_base : 0;
    const within = tolerance !== null && Math.abs(ratio) <= tolerance;
    mount(preview, h('div', { class: `callout ${within ? 'callout-info' : 'callout-warn'}` },
      h('p', {},
        h('strong', {}, `${actual.toFixed(0)} ${s.base_unit} actually used`),
        ` · expected ${(s.theoretical_base || 0).toFixed(0)} ${s.base_unit} · `,
        `variance ${variance > 0 ? '+' : ''}${variance.toFixed(0)} ${s.base_unit} (${ratio > 0 ? '+' : ''}${(ratio * 100).toFixed(1)}%) · `,
        within ? 'inside tolerance — signs off automatically' : 'outside tolerance — a manager will be asked to confirm it')));
  }
  ['input', 'change'].forEach((ev) => {
    [stream, opening, closing, added].forEach((el) => el.addEventListener(ev, updatePreview));
  });
  updatePreview();

  submit.addEventListener('click', async () => {
    error.style.display = 'none';
    const nums = [opening.value, closing.value, added.value].map(Number);
    if (opening.value === '' || closing.value === '' || nums.some((n) => Number.isNaN(n) || n < 0)) {
      error.textContent = 'Weighed out and leftover are both required, and none of these can be negative.';
      error.style.display = '';
      return;
    }
    if (nums[1] > nums[0] + nums[2]) {
      error.textContent = 'More came back than went out. Check the figures — nothing has been recorded.';
      error.style.display = '';
      return;
    }

    submit.disabled = true;
    submit.textContent = 'Recording…';
    try {
      const label = (open.find((r) => r.item_key === stream.value) || {}).label || 'Stream';
      const out = await submitReconciliation({
        serviceDate,
        itemKey: stream.value,
        openingBase: nums[0],
        closingBase: nums[1],
        addedBase: nums[2],
      });
      const within = (out.status || out.validation_state) === 'within_tolerance';
      toast(within
        ? `${label} reconciled — inside tolerance, signed off automatically.`
        : `${label} recorded — the variance is outside tolerance and needs a manager’s review.`,
      within ? 'good' : 'warn');
      refresh();
    } catch (err) {
      error.textContent = err.message;
      error.style.display = '';
      submit.disabled = false;
      submit.textContent = 'Record measurement';
    }
  });

  return Card({ style: { marginBottom: '14px' } },
    CardHeader({
      title: 'End-of-shift weigh-in',
      desc: 'Only the difference between this and the provisional deduction is posted to stock',
    }),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', gap: '12px' } },
      h('label', { class: 'field' }, 'Stream', stream, unitNote),
      h('label', { class: 'field' }, 'Weighed out at start', opening),
      h('label', { class: 'field' }, 'Leftover at close', closing),
      h('label', { class: 'field' }, 'Top-ups during service', added,
        h('div', { class: 'hint' }, 'Zero if none were issued.'))),
    preview,
    h('div', { class: 'divider' }),
    submit,
    error);
}

/** Row button jumps to the form with that stream chosen. */
function focusWeighIn(itemKey) {
  const select = document.getElementById('weighin-stream');
  if (!select) return;
  select.value = itemKey;
  select.dispatchEvent(new Event('change'));
  select.scrollIntoView({ behavior: 'smooth', block: 'center' });
  select.focus();
}

/**
 * Days that landed outside tolerance — POST /unli/reconciliation/{pk}/review.
 * Restored from the old console: stock was already corrected when the
 * measurement was recorded, so approving/rejecting here only decides whether
 * the day may influence a future coefficient. Requires unli.approve.
 */
function awaitingCard(awaiting, refresh) {
  if (awaiting.length === 0) return null;
  return Card({ style: { marginBottom: '14px' } },
    CardHeader({
      title: 'Waiting for sign-off',
      desc: 'Outside tolerance — stock is already corrected; this only decides whether the day teaches the coefficient anything',
    }),
    Table({
      columns: [
        { key: 'date', label: 'Service date', render: (h2) => h2.serviceDate },
        { key: 'stream', label: 'Stream', render: (h2) => h('strong', {}, h2.stream) },
        { key: 'heads', label: 'Covers', render: (h2) => h('span', { class: 'mono t-sm' }, count(h2.heads)) },
        { key: 'expected', label: 'Expected', render: (h2) => h('span', { class: 'mono t-sm t-soft' }, qty(h2.theoreticalBase, h2.baseUnit)) },
        { key: 'measured', label: 'Measured', render: (h2) => h('span', { class: 'mono t-sm' }, qty(h2.actualBase, h2.baseUnit)) },
        {
          key: 'variance',
          label: 'Variance',
          render: (h2) => h('span', { class: `mono t-sm ${h2.variancePct > 0 ? 't-crit' : 't-good'}` },
            h2.variancePct == null ? nodata('n/a') : `${h2.variancePct > 0 ? '+' : ''}${h2.variancePct.toFixed(1)}%`),
        },
        { key: 'note', label: 'Note', render: (h2) => str(h2.note, '—') },
        {
          key: 'act',
          label: '',
          render: (h2) => (canApprove()
            ? h('div', { style: { display: 'flex', gap: '6px' } },
              Button({ variant: 'primary', size: 'sm', onClick: () => review(h2, true, refresh) }, 'Sign off'),
              Button({ size: 'sm', onClick: () => review(h2, false, refresh) }, 'Exclude'))
            : h('span', { class: 't-xs t-faint' }, 'manager only')),
        },
      ],
      rows: awaiting,
    }));
}

async function review(row, approve, refresh) {
  try {
    await reviewReconciliation(row.id, approve);
    toast(approve
      ? `${row.stream} on ${row.serviceDate} signed off — will count toward the next recalibration.`
      : `${row.stream} on ${row.serviceDate} excluded — will not influence the coefficient.`,
    approve ? 'good' : 'info');
    refresh();
  } catch (e) {
    toast(e.message, 'crit');
  }
}

/** Full reconciliation history — restored from the old console's own table.
 * 2026-10-14: each row can now be revised — see reviseModal() below — when
 * an order for that service date committed after it was first reconciled.
 */
function historyCard(history, refresh) {
  return Card({ style: { marginBottom: '14px' } },
    CardHeader({
      title: 'Reconciliation history',
      desc: 'Every measurement, verdict and correction · most recent first',
    }),
    Table({
      columns: [
        { key: 'date', label: 'Service date', render: (h2) => h('span', { class: 't-sm' }, h2.serviceDate) },
        { key: 'stream', label: 'Stream', render: (h2) => h('strong', {}, h2.stream) },
        { key: 'heads', label: 'Covers', render: (h2) => h('span', { class: 'mono t-sm' }, count(h2.heads)) },
        { key: 'measured', label: 'Measured', render: (h2) => h('span', { class: 'mono t-sm' }, qty(h2.actualBase, h2.baseUnit)) },
        { key: 'expected', label: 'Expected', render: (h2) => h('span', { class: 'mono t-sm t-soft' }, qty(h2.theoreticalBase, h2.baseUnit)) },
        {
          key: 'variance',
          label: 'Variance',
          render: (h2) => h('span', { class: `mono t-sm ${h2.varianceBase > 0 ? 't-crit' : h2.varianceBase < 0 ? 't-good' : ''}` },
            h2.variancePct == null ? nodata('n/a') : `${h2.variancePct > 0 ? '+' : ''}${h2.variancePct.toFixed(1)}%`),
        },
        { key: 'status', label: 'Status', render: (h2) => Pill(h2.status === 'within_tolerance' ? 'good' : 'warn', h2.status.replace(/_/g, ' ')) },
        { key: 'validation', label: 'Validation', render: validationCell },
        {
          key: 'revise',
          label: '',
          render: (h2) => (canRevise()
            ? Button({ size: 'sm', onClick: () => reviseModal(h2, refresh) }, 'Revise')
            : null),
        },
      ],
      rows: history,
      empty: EmptyState({
        icon: '⚖',
        title: 'Nothing reconciled yet',
        description: 'Once a service date with unlimited orders has been weighed and submitted, it appears here and becomes part of what the coefficient and the anomaly model learn from.',
      }),
    }));
}

function validationCell(h2) {
  if (h2.validationState === 'validated') return Pill('good', 'validated');
  if (h2.validationState === 'rejected') return Pill('crit', 'rejected');
  if (h2.validationState === 'pending_review') return Pill('warn', 'pending review');
  return Pill('flat', h2.validationState || 'n/a');
}

/**
 * 2026-10-14 fix: the missing half of the reconciliation-revision workflow
 * — wt/unli.revise_reconciliation() and preview_revision() have existed
 * server-side since the earlier fix, with no way to reach either from the
 * console. This modal is that path: inspect a past reconciliation,
 * re-weigh it, see the freshly recomputed head count (a late order shows
 * up here as `heads` differing from what the original row recorded) and
 * the exact incremental correction a submission would post — all before
 * anything is written — then submit it, or not.
 *
 * Mirrors weighInCard's own three-field shape (weighed out / leftover /
 * top-ups) and its "preview before submit" discipline exactly, so this
 * reads as the same workflow applied to an older day rather than a
 * different tool. The three-piece-per-head serving/refill rule itself is
 * untouched by any of this — a revision only ever re-measures and
 * recomputes against orders that already exist; it never changes how a
 * round was served.
 */
function reviseModal(row, refresh) {
  const opening = h('input', { type: 'number', step: '0.01', min: '0', placeholder: '0.00' });
  const closing = h('input', { type: 'number', step: '0.01', min: '0', placeholder: '0.00' });
  const added = h('input', { type: 'number', step: '0.01', min: '0', value: '0' });
  const err = ModalError();
  const previewHost = h('div', { style: { marginTop: '10px' } });
  const historyHost = h('div', { style: { marginTop: '14px' } });
  const submit = Button({ variant: 'primary' }, 'Submit revision');
  submit.disabled = true;

  let lastPreview = null;

  function loadHistory() {
    mount(historyHost, Loading('Loading revision history…'));
    getReconciliationRevisions(row.id).then((revisions) => {
      mount(historyHost, revisionHistoryTable(revisions));
    }).catch((e) => mount(historyHost, h('div', { class: 'login-err' }, e.message)));
  }
  loadHistory();

  async function runPreview() {
    err.style.display = 'none';
    lastPreview = null;
    submit.disabled = true;
    const o = Number(opening.value); const a = Number(added.value) || 0; const c = Number(closing.value);
    if (opening.value === '' || closing.value === '' || Number.isNaN(o) || Number.isNaN(c)) {
      mount(previewHost, h('div'));
      return;
    }
    if (o < 0 || c < 0 || a < 0) {
      mount(previewHost, h('div', { class: 'callout callout-warn' }, h('p', {}, 'None of these can be negative.')));
      return;
    }
    if (c > o + a) {
      mount(previewHost, h('div', { class: 'callout callout-warn' }, h('p', {}, 'Leftover cannot be more than what went out.')));
      return;
    }
    mount(previewHost, Loading('Checking for late orders…'));
    try {
      const preview = await previewReconciliationRevision(row.id, {
        openingBase: o, closingBase: c, addedBase: a,
      });
      lastPreview = preview;
      mount(previewHost, previewPanel(preview));
      submit.disabled = false;
    } catch (e) {
      mount(previewHost, h('div', { class: 'callout callout-warn' }, h('p', {}, e.message)));
    }
  }
  ['input', 'change'].forEach((ev) => {
    [opening, closing, added].forEach((el) => el.addEventListener(ev, runPreview));
  });

  submit.addEventListener('click', async () => {
    if (!lastPreview) return; // defence in depth -- the button is disabled without one
    submit.disabled = true;
    submit.textContent = 'Submitting…';
    try {
      const out = await reviseReconciliation(row.id, {
        openingBase: Number(opening.value), closingBase: Number(closing.value),
        addedBase: Number(added.value) || 0,
      });
      toast(
        lastPreview.lateOrdersDetected
          ? `${row.stream} on ${row.serviceDate} revised — ${lastPreview.headsAtOriginal} → ${lastPreview.heads} covers, `
            + `${out.correction_base >= 0 ? '+' : ''}${out.correction_base.toFixed(1)} ${row.baseUnit} posted.`
          : `${row.stream} on ${row.serviceDate} revised — ${out.correction_base >= 0 ? '+' : ''}${out.correction_base.toFixed(1)} ${row.baseUnit} posted.`,
        'good',
      );
      modal.close();
      refresh();
    } catch (e) {
      err.textContent = e.message;
      err.style.display = '';
      submit.disabled = false;
      submit.textContent = 'Submit revision';
    }
  });

  const modal = Modal({
    title: h('span', {}, 'Revise — ', row.stream, ' — ', row.serviceDate),
    desc: 'Re-weigh this service date. The original record is never changed — only the gap between it and today\'s fresh numbers is posted, and only once.',
    wide: true,
    onClose: () => {},
  },
  h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(2,1fr)', gap: '14px', marginBottom: '10px' } },
    h('div', { class: 't-sm t-soft' },
      h('div', {}, 'Originally recorded: ', h('strong', {}, count(row.heads), ' covers')),
      h('div', {}, 'Measured: ', qty(row.actualBase, row.baseUnit), ' vs expected ', qty(row.theoreticalBase, row.baseUnit)),
      h('div', {}, 'Already applied to stock: ', qty(row.correctionBase ?? 0, row.baseUnit))),
    h('div', { class: 'hint' },
      'Re-weighing picks up any order for this date that committed after the original record — if one has, the fresh head count below will differ from what was originally recorded.')),
  h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)', gap: '12px' } },
    h('label', { class: 'field' }, 'Weighed out at start', opening),
    h('label', { class: 'field' }, 'Leftover at close', closing),
    h('label', { class: 'field' }, 'Top-ups during service', added)),
  previewHost,
  h('div', { class: 'divider' }),
  submit,
  err,
  historyHost);
}

/** The read-only dry-run result — what a submission would do, shown before
 * it is possible to submit one. */
function previewPanel(p) {
  const lateCallout = p.lateOrdersDetected
    ? h('div', { class: 'callout callout-warn' }, h('p', {},
      h('strong', {}, 'Late order detected. '),
      // count()/num() return DOM Text nodes, not strings -- plain numbers
      // here, same discipline runRecalibrate()'s own pesoStr()-style
      // helpers already use for template-literal interpolation.
      `This service date recorded ${p.headsAtOriginal} covers originally — it now has ${p.heads}. `,
      'The figures below already reflect the fresh count.'))
    : h('div', { class: 'callout callout-info' }, h('p', {}, 'No change in covers since the original record — re-weighing a fresh measurement only.'));

  const clampedCallout = p.correctionClamped
    ? h('div', { class: 'callout callout-warn' }, h('p', {},
      'The full correction would exceed what is physically on hand — the amount actually posted is capped at the available stock.'))
    : null;

  return h('div', {},
    lateCallout,
    clampedCallout,
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', gap: '12px', marginTop: '10px' } },
      Stat({ value: qty(p.theoreticalBase, p.baseUnit), label: 'Fresh expected' }),
      Stat({ value: qty(p.actualBase, p.baseUnit), label: 'Measured now' }),
      Stat({
        value: h('span', { class: p.varianceBase > 0 ? 't-crit' : p.varianceBase < 0 ? 't-good' : '' },
          `${p.varianceBase > 0 ? '+' : ''}${p.varianceBase.toFixed(0)} ${p.baseUnit}`),
        label: 'Variance',
      }),
      Stat({
        value: h('span', { style: { fontWeight: 700 } },
          `${p.incrementalCorrectionBase >= 0 ? '+' : ''}${p.incrementalCorrectionBase.toFixed(1)} ${p.baseUnit}`),
        label: 'Will post now',
      })),
    h('div', { class: 't-xs t-faint', style: { marginTop: '8px' } },
      `Already applied: ${p.alreadyAppliedBase.toFixed(1)} ${p.baseUnit} · cumulative after this revision: `
      + `${p.cumulativeCorrectionBase.toFixed(1)} ${p.baseUnit} · `,
      p.status === 'within_tolerance' ? 'within tolerance' : 'outside tolerance — will need sign-off'));
}

/** Append-only trail — every earlier revision against this same original
 * reconciliation, so a reviser can see what has already changed. */
function revisionHistoryTable(revisions) {
  if (revisions.length === 0) {
    return h('div', {},
      h('div', { class: 'card-title', style: { marginBottom: '6px' } }, 'Revision history'),
      EmptyState({ icon: '⟳', title: 'No earlier revisions', description: 'This is the first time this service date has been revised.', tight: true }));
  }
  return h('div', {},
    h('div', { class: 'card-title', style: { marginBottom: '6px' } }, 'Revision history'),
    Table({
      columns: [
        { key: 'when', label: 'When', render: (r) => h('span', { class: 't-xs' }, r.createdAt) },
        { key: 'heads', label: 'Covers', render: (r) => h('span', { class: 'mono t-sm' }, count(r.heads)) },
        { key: 'correction', label: 'Posted', render: (r) => h('span', { class: 'mono t-sm' }, `${r.correctionBase >= 0 ? '+' : ''}${r.correctionBase.toFixed(1)}`) },
        { key: 'cumulative', label: 'Cumulative', render: (r) => h('span', { class: 'mono t-sm t-soft' }, r.cumulativeCorrectionBase.toFixed(1)) },
        { key: 'status', label: 'Status', render: (r) => Pill(r.status === 'within_tolerance' ? 'good' : 'warn', r.status.replace(/_/g, ' ')) },
        { key: 'by', label: 'By', render: (r) => str(r.recordedBy, '—') },
      ],
      rows: revisions,
    }));
}

function toleranceTile(tolerance, refresh) {
  const tile = Card({}, Stat({
    value: tolerance === null ? nodata('not set') : `${(tolerance * 100).toFixed(0)}%`,
    label: 'Auto sign-off tolerance',
  }));
  if (canApprove()) {
    const editBtn = Button({ size: 'sm', onClick: () => openToleranceModal(tolerance, refresh) }, 'Change');
    editBtn.style.marginTop = '8px';
    tile.appendChild(editBtn);
  }
  return tile;
}

/** PATCH /unli/tolerance. Already-reconciled days keep the tolerance they were judged against. */
function openToleranceModal(tolerance, refresh) {
  const input = h('input', {
    type: 'number', min: '1', max: '100', step: '1',
    value: tolerance === null ? '' : Math.round(tolerance * 100),
  });
  const err = ModalError();
  const submit = Button({ variant: 'primary' }, 'Save');
  submit.addEventListener('click', async () => {
    const v = Number(input.value);
    if (!(v > 0 && v <= 100)) { err.textContent = 'Enter a percentage between 1 and 100.'; err.style.display = ''; return; }
    submit.disabled = true;
    try {
      await setTolerance(v / 100);
      modal.close();
      toast(`Tolerance set to ${v}%.`, 'good');
      refresh();
    } catch (e) {
      err.textContent = e.message; err.style.display = ''; submit.disabled = false;
    }
  });
  const modal = Modal({
    title: 'Variance tolerance',
    desc: 'How far a measured day may land from the estimate before a manager is asked to confirm it. Days already reconciled keep the tolerance they were judged against.',
    onClose: () => {},
  }, h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Tolerance (%)', input), err, submit);
}

/** POST /unli/coefficients/recalibrate — dry-run preview, or a live write. */
async function runRecalibrate(dryRun, refresh) {
  let r;
  try {
    r = await recalibrate({ dryRun });
  } catch (e) {
    toast(e.message, 'crit');
    return;
  }
  const moved = (r.streams || []).filter((s) => s.new_lambda != null);
  Modal({
    title: dryRun ? 'Recalibration preview' : 'Recalibration applied',
    desc: `λ = total measured ÷ total covers, over each stream's signed-off days of at least ${r.min_heads ?? '—'} covers.${dryRun ? ' Nothing has been written.' : ''}`,
    wide: true,
    onClose: () => {},
  }, Table({
    columns: [
      { key: 'stream', label: 'Stream', render: (s) => s.label },
      { key: 'days', label: 'Days', render: (s) => h('span', { class: 'mono t-sm' }, String(s.validated_days)) },
      { key: 'covers', label: 'Covers', render: (s) => h('span', { class: 'mono t-sm' }, String(s.total_heads)) },
      { key: 'now', label: 'Now', render: (s) => h('span', { class: 'mono t-sm t-soft' }, s.current_lambda != null ? num(s.current_lambda, { decimals: 1 }).textContent : '—') },
      {
        key: 'new',
        label: dryRun ? 'Would be' : 'New',
        render: (s) => (s.new_lambda != null
          ? h('span', { class: 'mono t-sm' }, `${num(s.new_lambda, { decimals: 1 }).textContent} ${s.base_unit}`)
          : h('span', { class: 't-xs t-faint' }, s.reason || 'no change')),
      },
    ],
    rows: r.streams || [],
  }));
  if (!dryRun) {
    toast(moved.length ? `${moved.length} coefficient(s) updated.` : 'No stream had enough signed-off history to recalibrate.', 'good');
    refresh();
  }
}

/**
 * Flavor pricing lives here rather than in the POS's own JavaScript: the till
 * must never decide what a special flavor costs, only read the price back
 * from this configuration — same as a menu price. Restored from the old
 * console, which kept this on the same page as reconciliation.
 */
function flavorsCard() {
  const host = h('div');
  const card = Card({ style: { marginBottom: '14px' } },
    CardHeader({
      title: 'Flavors',
      desc: 'Which flavors are included in the unlimited price, and what a premium one adds',
    }),
    host);

  function loadAndDraw() {
    mount(host, Loading('Loading flavors…'));
    listFlavors().then((flavors) => {
      mount(host, Table({
        columns: [
          { key: 'name', label: 'Flavor', render: (f) => f.name },
          {
            key: 'type',
            label: 'Type',
            render: (f) => (f.flavorType === 'special' ? Pill('warn', 'Special') : Pill('good', 'Included')),
          },
          {
            key: 'addon',
            label: 'Add-on',
            render: (f) => (f.flavorType === 'special'
              ? h('span', { class: 'mono t-sm' }, num(f.addonPrice, { decimals: 2 }).textContent)
              : h('span', { class: 't-faint' }, '—')),
          },
          {
            key: 'offered',
            label: 'Offered',
            render: (f) => {
              if (!f.isDeductable) return Pill('crit', 'no recipe');
              return f.isActive ? Pill('good', 'yes') : Pill('flat', 'withdrawn');
            },
          },
          {
            key: 'act',
            label: '',
            render: (f) => (canApprove()
              ? Button({ size: 'sm', onClick: () => openFlavorModal(f, loadAndDraw) }, 'Edit')
              : h('span', { class: 't-xs t-faint' }, '—')),
          },
        ],
        rows: flavors,
        empty: EmptyState({
          icon: '✦',
          title: 'No flavors registered',
          description: 'Every house sauce that is not a kitchen prep becomes a selectable flavor automatically.',
        }),
      }));
    }).catch((e) => mount(host, h('div', { class: 'login-err' }, e.message)));
  }

  loadAndDraw();
  return card;
}

/** PATCH /unli/flavors/{pk}. Marking a flavor 'included' forces its price back to zero server-side. */
function openFlavorModal(f, onSaved) {
  const typeSelect = h('select', {},
    h('option', { value: 'included', selected: f.flavorType === 'included' ? 'selected' : null }, 'Included in the unlimited price'),
    h('option', { value: 'special', selected: f.flavorType === 'special' ? 'selected' : null }, 'Special — adds to the bill'));
  const priceInput = h('input', { type: 'number', min: '0', step: '1', value: f.addonPrice ?? '' });
  const priceWrap = h('label', {
    class: 'field', style: { marginBottom: '10px', display: f.flavorType === 'special' ? 'block' : 'none' },
  }, 'Add-on price (₱)', priceInput);
  const activeSelect = h('select', {},
    h('option', { value: '1', selected: f.isActive ? 'selected' : null }, 'Yes'),
    h('option', { value: '0', selected: !f.isActive ? 'selected' : null }, 'Withdrawn'));
  const err = ModalError();
  const submit = Button({ variant: 'primary' }, 'Save');

  typeSelect.addEventListener('change', () => {
    priceWrap.style.display = typeSelect.value === 'special' ? 'block' : 'none';
  });

  submit.addEventListener('click', async () => {
    const body = { flavorType: typeSelect.value, isActive: activeSelect.value === '1' };
    if (typeSelect.value === 'special') {
      const v = Number(priceInput.value);
      if (!(v >= 0)) { err.textContent = 'Enter the add-on price.'; err.style.display = ''; return; }
      body.addonPrice = v;
    }
    submit.disabled = true;
    try {
      await setFlavor(f.pk, body);
      modal.close();
      toast('Saved.', 'good');
      onSaved();
    } catch (e) {
      err.textContent = e.message; err.style.display = ''; submit.disabled = false;
    }
  });

  const modal = Modal({
    title: f.name,
    desc: 'A special flavor adds its price to the bill as a separate, linked charge each time it is served. The original paid order is never changed.',
    onClose: () => {},
  },
  h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Type', typeSelect),
  priceWrap,
  h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Offered on the POS', activeSelect),
  err, submit);
}
