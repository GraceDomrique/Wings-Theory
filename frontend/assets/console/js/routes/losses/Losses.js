import { h, mount, icon } from '../../lib/dom.js';
import { Card } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { count, delta, qty, pct, stamp, humanise, nodata } from '../../lib/format.js';
import { ICONS } from '../../lib/icons.js';
import {
  getAnomalies, getIngredientVariance, reviewAnomaly, getFlavorAnomalyEvents,
} from '../../lib/api/intelligence.js';
import { getModelDetails } from '../../lib/api/system.js';
import { session } from '../../lib/api/client.js';

const ANOMALY_LOOKBACK_DAYS = 90;
const VARIANCE_LOOKBACK_DAYS = 30;

/**
 * Stock & Usage Reviews — 2026-10-15 rebuild (spec: "Review Center").
 * Replaces the old three-full-width-tab layout (Stock Loss / Consumption
 * Variance / Unlimited Wings) with one unified review queue + contextual
 * inspector. This is a PRESENTATION change: the same four data sources
 * below (stock-loss Isolation Forest, ingredient variance reconciliation,
 * Unlimited Wings service-day Isolation Forest, Unlimited Wings
 * flavor-level Isolation Forest) are fetched exactly as before, through
 * the same adapters, and are NEVER merged into one score or one meaning of
 * "anomaly" — every finding keeps its own `detector` identity, shown on
 * every row and in the inspector, per the explicit instruction that these
 * are different statistical methods answering different questions.
 *
 * Only stock-loss anomalies have a real backend review action
 * (POST /console/anomalies/{id}/review) — variance, stream and flavor
 * findings are calculation/detection-only views with no invented actions
 * for ones the backend doesn't support (spec: "Do not invent review
 * actions for a calculation-only view").
 */
const DETECTORS = {
  stock_loss: { label: 'Stock Loss', icon: ICONS.trash, color: 'crit' },
  variance: { label: 'Usage Variance', icon: ICONS.packageMinus, color: 'warn' },
  unli_stream: { label: 'Unlimited Wings', icon: ICONS.utensils, color: 'info' },
  unli_flavor: { label: 'Unlimited Wings (Flavor)', icon: ICONS.utensils, color: 'info' },
};

const CATEGORY_LABEL = {
  wastage: 'Wastage',
  spoilage: 'Spoilage',
  count_adjustment: 'Count adjustment',
};

/**
 * 2026-10-15 fix (same root cause documented in varianceDenominatorReliable
 * below): variance_ratio divides by expected consumption with no floor
 * beyond "not literally zero" — a tiny-but-real denominator next to a real
 * absolute discrepancy produces a mathematically correct but practically
 * meaningless percentage (confirmed live: ratios past 1000%+). This is NOT
 * a unit-conversion bug (consumed_base/count_variance_base are already the
 * same base unit server-side) — purely presentational; the backend's own
 * ratio, flag threshold and which rows get flagged are untouched.
 */
function varianceDenominatorReliable(i) {
  if (!(i.consumedBase > 0)) return false;
  if (i.receivedBase > 0 && i.consumedBase < i.receivedBase * 0.01) return false;
  return true;
}

export async function render(ctx) {
  const isAdmin = (session.user || {}).role === 'admin';

  // 2026-10-15: getModelDetails() is NOT in this Promise.all — the old
  // per-tab code only ever called it lazily, when the Unlimited Wings tab
  // was actually opened, so a slow /console/model-details response never
  // blocked the page. Fetching it eagerly here (needed for the Model/data
  // status tile) made that latency block the ENTIRE queue from rendering
  // at all — found live while verifying this page (stuck on "Loading..."
  // past 10s). It now loads separately and fills in its own tile once it
  // resolves; the queue itself never waits on it.
  let models = null;
  const modelStatusHost = h('div');
  getModelDetails().then((m) => { models = m; drawModelStatus(); }).catch(() => { drawModelStatus(); });

  const [{ stockLoss, consumption_anomalies: streamAnomalies }, varianceReport, flavorEvents] = await Promise.all([
    getAnomalies(ANOMALY_LOOKBACK_DAYS),
    getIngredientVariance(VARIANCE_LOOKBACK_DAYS),
    getFlavorAnomalyEvents(ANOMALY_LOOKBACK_DAYS),
  ]);
  const varianceItems = (varianceReport.items || []).filter((i) => i.flagged);

  // ---------------------------------------------------------------------
  // Normalize all four sources into one queue. Each finding keeps a
  // `detector` tag (never collapsed into a shared "anomaly score") plus
  // its own evidence shape in `raw`, read back out by the inspector below
  // per detector type.
  // ---------------------------------------------------------------------
  const findings = [
    ...stockLoss.map((a) => ({
      key: `stock_loss_${a.id}`,
      detector: 'stock_loss',
      title: a.ingredientName,
      date: a.timestamp,
      severity: a.severity,
      status: a.reviewStatus === 'confirmed_anomaly' ? 'confirmed'
        : a.reviewStatus === 'false_positive' ? 'false_positive' : 'needs_review',
      summary: a.flaggedBecause,
      raw: a,
    })),
    ...varianceItems.map((i) => ({
      key: `variance_${i.ingredient_id}`,
      detector: 'variance',
      title: i.name,
      date: null,
      severity: 'warn',
      status: 'calculation_only',
      summary: `Count variance ${delta(i.countVarianceBase, i.baseUnit).textContent} against expected consumption`,
      raw: i,
    })),
    ...streamAnomalies.map((a, idx) => ({
      key: `unli_stream_${a.service_date}_${idx}`,
      detector: 'unli_stream',
      title: a.label || a.ingredient_name || 'Unlimited Wings',
      date: a.service_date,
      severity: 'warn',
      status: 'needs_review',
      summary: a.why,
      raw: a,
    })),
    ...flavorEvents.map((e, idx) => ({
      key: `unli_flavor_${e.serviceDate}_${idx}`,
      detector: 'unli_flavor',
      title: e.primaryFlavors.join(', ') || 'Flavor shift',
      date: e.serviceDate,
      severity: 'warn',
      status: 'needs_review',
      summary: e.why,
      raw: e,
    })),
  ];

  const openCount = findings.filter((f) => f.status === 'needs_review' || f.status === 'calculation_only').length;
  const completedCount = findings.filter((f) => f.status === 'confirmed' || f.status === 'false_positive').length;
  const followUpCount = findings.filter((f) => f.status === 'confirmed').length;

  let categoryFilter = 'all';
  let query = '';
  let selectedKey = findings[0] ? findings[0].key : null;

  const queueHost = h('div');
  const inspectorHost = h('div');
  const filterStripHost = h('div', { class: 'action-strip' });

  function visibleFindings() {
    const q = query.trim().toLowerCase();
    return findings.filter((f) => (categoryFilter === 'all' || f.detector === categoryFilter
      || (categoryFilter === 'unlimited' && (f.detector === 'unli_stream' || f.detector === 'unli_flavor')))
      && (!q || f.title.toLowerCase().includes(q) || (f.summary || '').toLowerCase().includes(q)));
  }

  const FILTER_CHIPS = [
    ['all', 'All Findings'],
    ['stock_loss', 'Stock Loss'],
    ['variance', 'Usage'],
    ['unlimited', 'Unlimited Wings'],
  ];

  function drawFilterStrip() {
    mount(filterStripHost, ...FILTER_CHIPS.map(([id, label]) => h('button', {
      class: `action-chip${categoryFilter === id ? ' is-active' : ''}`,
      onClick: () => { categoryFilter = id; drawFilterStrip(); drawQueue(); },
    }, label)));
  }

  function statusPill(f) {
    if (f.status === 'confirmed') return Pill('crit', 'confirmed');
    if (f.status === 'false_positive') return Pill('flat', 'false positive');
    if (f.status === 'calculation_only') return Pill('warn', 'flagged');
    return Pill('warn', 'needs review');
  }

  function select(key) {
    selectedKey = key;
    drawQueue();
    drawInspector();
  }

  function drawQueue() {
    const visible = visibleFindings();
    if (visible.length === 0) {
      mount(queueHost, EmptyState({
        icon: '✓',
        title: findings.length ? 'Nothing matches this filter' : 'Nothing flagged for review',
        description: findings.length
          ? 'Widen or clear the filter above to see the rest of the queue.'
          : `Stock loss, usage variance and Unlimited Wings are all within normal range for their analysis windows.`,
        tight: true,
      }));
      return;
    }
    mount(queueHost, h('div', { class: 'scrollpane', style: { maxHeight: '620px' } },
      ...visible.map((f) => {
        const det = DETECTORS[f.detector];
        const isSel = f.key === selectedKey;
        return h('button', {
          class: `review-queue-row${isSel ? ' is-active' : ''}`,
          onClick: () => select(f.key),
        },
        h('span', { class: `kpi-icon kpi-icon-${det.color}`, style: { width: '30px', height: '30px', flexShrink: '0' } }, icon(det.icon, 14)),
        h('span', { style: { flex: '1', minWidth: '0' } },
          h('div', { class: 't-xs t-faint' }, det.label, f.date ? ` · ${f.date.length > 10 ? stamp(f.date).textContent : f.date}` : ''),
          h('div', { class: 't-sm t-strong' }, f.title),
          h('div', { class: 'sub' }, f.summary)),
        statusPill(f));
      })));
  }

  function fieldRow(label, ...value) {
    return h('div', { class: 'card card-sunk', style: { padding: '9px 11px' } },
      h('div', { class: 't-xs t-faint' }, label),
      h('div', { class: 't-strong', style: { marginTop: '2px', fontSize: '13px' } }, ...value));
  }

  function doReview(a, status) {
    const noteInput = h('input', { type: 'text', placeholder: 'optional' });
    const err = ModalError();
    const submit = Button({ variant: 'primary' }, status === 'confirmed_anomaly' ? 'Confirm anomaly' : 'Mark false positive');
    submit.addEventListener('click', async () => {
      submit.disabled = true;
      try {
        await reviewAnomaly(a.id, status, noteInput.value.trim() || null);
        modal.close();
        a.reviewStatus = status;
        const f = findings.find((x) => x.detector === 'stock_loss' && x.raw === a);
        if (f) f.status = status === 'confirmed_anomaly' ? 'confirmed' : 'false_positive';
        toast(status === 'confirmed_anomaly' ? 'Marked as a confirmed anomaly.' : 'Marked as a false positive.', 'good');
        drawQueue();
        drawInspector();
      } catch (e) { err.textContent = e.message; err.style.display = ''; } finally { submit.disabled = false; }
    });
    const modal = Modal({
      title: h('span', {}, a.ingredientName, ' — ', stamp(a.timestamp)),
      desc: a.flaggedBecause,
      onClose: () => {},
    },
    h('label', { class: 'field' }, 'Note (optional)', noteInput),
    err,
    h('div', { style: { marginTop: '14px', display: 'flex', justifyContent: 'flex-end' } }, submit));
  }

  function inspectorHeader(f) {
    const det = DETECTORS[f.detector];
    return h('div', { style: { marginBottom: '14px' } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' } },
        h('span', { class: `kpi-icon kpi-icon-${det.color}`, style: { width: '26px', height: '26px' } }, icon(det.icon, 13)),
        h('span', { class: 't-xs t-faint', style: { fontWeight: '700', letterSpacing: '.03em', textTransform: 'uppercase' } }, det.label)),
      h('div', { class: 'card-title', style: { fontSize: '17px' } }, f.title));
  }

  function drawStockLossInspector(f) {
    const a = f.raw;
    mount(inspectorHost, Card({},
      inspectorHeader(f),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '8px', marginBottom: '14px' } },
        fieldRow('Event date', stamp(a.timestamp)),
        fieldRow('Amount', delta(a.amount, a.baseUnit)),
        a.category ? fieldRow('Category', CATEGORY_LABEL[a.category] || humanise(a.category)) : null,
        fieldRow('Priority', a.priority === 'review_soon' ? 'Review soon' : 'Worth a look'),
        fieldRow('Detection method', 'Isolation Forest (stock-loss model)'),
        fieldRow('Analysis window', `${ANOMALY_LOOKBACK_DAYS} days`)),
      h('div', { class: 't-sm t-soft', style: { marginBottom: '14px' } }, a.flaggedBecause),
      a.reasonCode ? h('div', { class: 't-xs t-faint', style: { marginBottom: '14px' } }, 'Ledger reason: ', humanise(a.reasonCode)) : null,
      statusPill(f),
      isAdmin && f.status === 'needs_review'
        ? h('div', { style: { display: 'flex', gap: '8px', marginTop: '14px' } },
          Button({ onClick: () => doReview(a, 'confirmed_anomaly') }, 'Confirm'),
          Button({ variant: 'ghost', onClick: () => doReview(a, 'false_positive') }, 'False positive'))
        : f.status !== 'needs_review'
          ? h('div', { class: 't-xs t-faint', style: { marginTop: '10px' } }, 'Already reviewed — this is the recorded decision, not a live recommendation.')
          : h('div', { class: 't-xs t-faint', style: { marginTop: '10px' } }, 'Admin reviews stock-loss flags from this panel.')));
  }

  function drawVarianceInspector(f) {
    const i = f.raw;
    const reliable = varianceDenominatorReliable(i);
    mount(inspectorHost, Card({},
      inspectorHeader(f),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '8px', marginBottom: '14px' } },
        fieldRow('Expected consumption', qty(i.consumedBase, i.baseUnit, 'none')),
        fieldRow('Received', qty(i.receivedBase, i.baseUnit, 'none')),
        fieldRow('Absolute count variance', delta(i.countVarianceBase, i.baseUnit)),
        fieldRow('% of expected', reliable
          ? pct(i.varianceRatio === null ? null : i.varianceRatio * 100, 'n/a', 0)
          : h('span', { title: 'Expected consumption is too small relative to this ingredient\'s real activity for a percentage to be meaningful.' }, nodata('not meaningful'))),
        fieldRow('Method', 'Expected-vs-counted reconciliation (not a trained model)'),
        fieldRow('Analysis window', `${varianceReport.days ?? VARIANCE_LOOKBACK_DAYS} days`)),
      h('div', { class: 'callout callout-info', style: { marginBottom: '0' } },
        h('p', {}, h('strong', {}, 'Calculation-only. '),
          'This compares recipe + waste-log consumption against the last physical count — there is no backend review/confirm action for this view. Investigate via a new Stock Count or the ingredient\'s own Stock Activity history.'))));
  }

  function drawUnliStreamInspector(f) {
    const a = f.raw;
    const details = models && models.anomaly_detection;
    mount(inspectorHost, Card({},
      inspectorHeader(f),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '8px', marginBottom: '14px' } },
        fieldRow('Service date', a.service_date),
        fieldRow('Covers', count(a.heads)),
        fieldRow('Variance vs. provisional', pct(a.variance_pct, 'n/a', 1)),
        fieldRow('Method', a.method === 'isolation_forest' ? 'Isolation Forest' : humanise(a.method)),
        fieldRow('Analysis window', `${ANOMALY_LOOKBACK_DAYS} days`),
        fieldRow('Model status', details ? (details.trained_this_call ? 'Trained' : 'Dormant — insufficient real data') : 'Unknown')),
      h('div', { class: 't-sm t-soft', style: { marginBottom: '0' } }, a.why),
      h('div', { class: 't-xs t-faint', style: { marginTop: '10px' } },
        'Informational only — a normal fixed-portion refill never appears here; only a whole reconciled service-day whose measured total looked unusual.')));
  }

  function drawUnliFlavorInspector(f) {
    const e = f.raw;
    const details = models && models.flavor_anomaly_detection;
    mount(inspectorHost, Card({},
      inspectorHeader(f),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '8px', marginBottom: '14px' } },
        fieldRow('Service date', e.serviceDate),
        fieldRow('Flags that day', count(e.totalFlagged)),
        fieldRow('Primary flavor(s)', e.primaryFlavors.join(', ') || '—'),
        fieldRow('Secondary (likely compositional)', e.secondaryFlavors.join(', ') || '—'),
        fieldRow('Method', 'Isolation Forest (flavor-level model)'),
        fieldRow('Model status', details ? (details.trained_this_call ? 'Trained' : 'Dormant — insufficient real data') : 'Unknown')),
      h('div', { class: 't-sm t-soft' }, e.why)));
  }

  function drawInspector() {
    const f = findings.find((x) => x.key === selectedKey);
    if (!f) {
      mount(inspectorHost, Card({ className: 'kpi-neutral', style: { minHeight: '220px', display: 'flex', alignItems: 'center', justifyContent: 'center' } },
        EmptyState({ icon: icon(ICONS.clipboardList, 26), title: 'Select a finding', description: 'Choose a record on the left to see its full evidence and review options.', tight: true })));
      return;
    }
    if (f.detector === 'stock_loss') return drawStockLossInspector(f);
    if (f.detector === 'variance') return drawVarianceInspector(f);
    if (f.detector === 'unli_stream') return drawUnliStreamInspector(f);
    return drawUnliFlavorInspector(f);
  }

  function drawModelStatus() {
    mount(modelStatusHost, models
      ? h('div', { class: 't-xs', style: { marginTop: '4px' } },
        modelStatusLine('Stock loss', models.anomaly_detection),
        modelStatusLine('Unli service-day', models.anomaly_detection),
        modelStatusLine('Unli flavor', models.flavor_anomaly_detection))
      : h('div', { class: 't-xs t-faint', style: { marginTop: '4px' } }, 'Loading…'));
  }

  drawModelStatus();
  drawFilterStrip();
  drawQueue();
  drawInspector();
  ctx.setSearchHandler((q) => { query = q; drawQueue(); });

  return h('div', { class: 'review-center' },
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', gap: '10px', marginBottom: '14px' } },
      Card({ className: 'kpi-neutral' }, Stat({ value: count(openCount), label: 'Open Reviews' })),
      Card({ className: 'kpi-neutral' }, Stat({ value: count(completedCount), label: 'Completed Reviews' })),
      Card({ className: 'kpi-neutral' }, Stat({ value: count(followUpCount), label: 'Needs Follow-up' })),
      Card({ className: 'kpi-neutral' }, h('div', { class: 't-xs t-faint' }, 'Model / data status'), modelStatusHost)),
    h('div', { class: 'eyebrow', style: { marginBottom: '8px' } }, 'Review queue'),
    filterStripHost,
    h('div', { class: 'studio-layout', style: { gridTemplateColumns: '1.5fr 1fr', marginTop: '10px' } },
      h('div', { class: 'studio-col' }, Card({ style: { padding: '8px' } }, queueHost)),
      h('div', { class: 'studio-col' }, inspectorHost)));
}

function modelStatusLine(label, details) {
  if (!details) return h('div', {}, label, ': ', nodata('unknown'));
  return h('div', {}, label, ': ', details.trained_this_call ? h('span', { class: 't-good' }, 'trained') : h('span', { class: 't-warn' }, 'dormant'));
}
