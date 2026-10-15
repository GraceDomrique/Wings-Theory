import { h, mount, icon } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { count, delta, qty, pct, stamp, humanise, nodata } from '../../lib/format.js';
import { severityFromAnomaly } from '../../lib/severity.js';
import { ICONS } from '../../lib/icons.js';
import {
  getAnomalies, getIngredientVariance, reviewAnomaly, getFlavorAnomalyEvents,
} from '../../lib/api/intelligence.js';
import { getModelDetails } from '../../lib/api/system.js';
import { session } from '../../lib/api/client.js';

const ANOMALY_LOOKBACK_DAYS = 90;
const VARIANCE_LOOKBACK_DAYS = 30;

/**
 * Stock & Usage Reviews — 2026-10-15 rebuild, replacing Losses.js's 3-tab
 * layout (Stock loss / Consumption variance / Unlimited Wings) with one
 * review-center workspace: a single filterable queue (left) and a
 * contextual inspector for whichever finding is selected (right).
 *
 * This does NOT merge the underlying analysis. Four genuinely separate
 * detectors feed the queue — stock-loss anomaly detection (Isolation
 * Forest over stock_ledger), ingredient consumption variance (a plain
 * expected-vs-counted calculation, no model), Unlimited Wings service-day
 * consumption anomaly detection, and Unlimited Wings flavor-level anomaly
 * detection — each keeps its own detector tag, its own evidence shape, and
 * its own review rules (confirm/false-positive is only ever offered where
 * Losses.js already offered it: stock-loss rows, via the exact same
 * reviewAnomaly() call; variance and Unlimited Wings findings stay
 * read-only investigation context, same as before — "Do not invent review
 * actions for a calculation-only view"). Same four endpoints Losses.js
 * read (getAnomalies/getIngredientVariance/getFlavorAnomalyEvents/
 * getModelDetails), same reviewAnomaly() write — a presentation change,
 * not a new data layer, and not a new anomaly score.
 *
 * Route id stays 'losses' (app.js just points it at this file now) — no
 * nav.js change needed, since this was already a deep link, not a sidebar
 * entry (reached from Inventory's "Stock Differences" card and Today's
 * Operations Intelligence tiles).
 */
export async function render(ctx) {
  const isAdmin = (session.user || {}).role === 'admin';

  // getModelDetails() is deliberately NOT in this Promise.all. It's the one
  // endpoint behind "System Health" (wt/api.py's model_details_endpoint) and
  // also runs a full Prophet demand-forecast backtest across every menu
  // item to populate fields this page never shows — the three anomaly
  // findings sources below are each fast on their own (confirmed live: all
  // three return before model-details does). Blocking this whole review
  // queue on an expensive evaluation it doesn't need turned "open the
  // queue" into a multi-second-to-much-longer spinner. The queue now
  // renders as soon as its own three real sources are back; model
  // freshness/trained-status fills in underneath a moment later without
  // holding up anything a reviewer actually came here to do.
  const [{ stockLoss, consumption_anomalies: streamAnomalies }, varianceReport, flavorEvents] = await Promise.all([
    getAnomalies(ANOMALY_LOOKBACK_DAYS),
    getIngredientVariance(VARIANCE_LOOKBACK_DAYS),
    getFlavorAnomalyEvents(ANOMALY_LOOKBACK_DAYS),
  ]);
  const varianceItems = (varianceReport.items || []).filter((i) => i.hasCountData);
  let streamDetails = null;
  let flavorDetails = null;
  let modelsLoaded = false;

  // -------------------------------------------------------------------
  // Normalize all four sources into one finding shape for the queue —
  // `raw` and `detector` keep every category's own evidence and identity
  // intact for the inspector; nothing here recomputes a score or a
  // combined "anomaly-ness".
  // -------------------------------------------------------------------
  const findings = [
    ...stockLoss.map((a) => ({
      key: `loss-${a.id}`, detector: 'stock_loss', detectorLabel: 'Stock loss',
      icon: ICONS.packageMinus, title: a.ingredientName, subtitle: a.flaggedBecause,
      date: a.timestamp, severity: a.severity, reviewStatus: a.reviewStatus, reviewable: true, raw: a,
    })),
    ...varianceItems.filter((i) => i.flagged).map((i) => ({
      key: `variance-${i.ingredientId}`, detector: 'consumption_variance', detectorLabel: 'Consumption variance',
      icon: ICONS.history, title: i.name, subtitle: 'Expected consumption differs from physical count',
      date: varianceReport.since, severity: 'warn', reviewStatus: null, reviewable: false, raw: i,
    })),
    ...streamAnomalies.map((a, idx) => ({
      key: `unli-stream-${a.service_date}-${idx}`, detector: 'unli_stream', detectorLabel: 'Unlimited Wings',
      icon: ICONS.utensils, title: a.label || a.ingredient_name, subtitle: a.why,
      date: a.service_date, severity: 'warn', reviewStatus: null, reviewable: false, raw: a,
    })),
    ...flavorEvents.map((e, idx) => ({
      key: `unli-flavor-${e.serviceDate}-${idx}`, detector: 'unli_flavor', detectorLabel: 'Unlimited Wings (flavour)',
      icon: ICONS.utensils, title: e.primaryFlavors.join(', ') || 'Flavour shift', subtitle: e.why,
      date: e.serviceDate, severity: 'warn', reviewStatus: null, reviewable: false, raw: e,
    })),
  ].sort((a, b) => String(b.date).localeCompare(String(a.date)));

  const openCount = findings.filter((f) => f.reviewable && !f.reviewStatus).length;
  const completedCount = findings.filter((f) => f.reviewable && f.reviewStatus).length;
  const followUpCount = findings.filter((f) => !f.reviewable).length;

  let categoryFilter = 'all';
  let query = '';
  let selected = findings[0] || null;
  const listHost = h('div');
  const rightHost = h('div');
  const topHost = h('div');

  function select(f) { selected = f; drawList(); drawRight(); }

  function drawTop() {
    mount(topHost, h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', gap: '10px', marginBottom: '14px' } },
      h('div', { class: 'card rail rail-warn', style: { padding: '12px' } },
        h('div', { style: { fontSize: '20px', fontWeight: '700' } }, count(openCount)),
        h('div', { class: 't-xs t-faint' }, 'Awaiting review')),
      h('div', { class: 'card rail rail-good', style: { padding: '12px' } },
        h('div', { style: { fontSize: '20px', fontWeight: '700' } }, count(completedCount)),
        h('div', { class: 't-xs t-faint' }, 'Reviewed')),
      h('div', { class: 'card rail rail-info', style: { padding: '12px' } },
        h('div', { style: { fontSize: '20px', fontWeight: '700' } }, count(followUpCount)),
        h('div', { class: 't-xs t-faint' }, 'Needs follow-up')),
      h('div', { class: 'card card-sunk', style: { padding: '12px' } },
        h('div', { class: 't-xs t-strong' }, modelStatusSummary()),
        h('div', { class: 't-xs t-faint' }, 'Model / data status'))));
  }

  function modelStatusSummary() {
    if (!modelsLoaded) return 'Checking…';
    const parts = [];
    if (streamDetails) parts.push(streamDetails.trained_this_call ? 'Unlimited Wings trained' : 'Unlimited Wings dormant');
    if (flavorDetails) parts.push(flavorDetails.trained_this_call ? 'Flavour trained' : 'Flavour dormant');
    return parts.length ? parts.join(' · ') : 'Status unavailable';
  }

  // Fired after the queue itself is already on screen — see the
  // Promise.all comment above for why this is separate.
  async function loadModelStatus() {
    try {
      const models = await getModelDetails();
      streamDetails = models && models.anomaly_detection;
      flavorDetails = models && models.flavor_anomaly_detection;
    } catch { /* leaves streamDetails/flavorDetails null — honest "status unavailable", never fabricated as healthy */ }
    modelsLoaded = true;
    drawTop();
    if (selected && (selected.detector === 'unli_stream' || selected.detector === 'unli_flavor')) drawRight();
  }

  const CATEGORIES = [
    ['all', 'All Findings'],
    ['stock_loss', 'Stock Loss'],
    ['consumption_variance', 'Consumption Variance'],
    ['unli_stream', 'Unlimited Wings'],
  ];
  const filterHost = h('div', { class: 'action-strip', style: { marginBottom: '12px' } });
  function drawFilters() {
    mount(filterHost, ...CATEGORIES.map(([id, label]) => h('button', {
      class: `action-chip${categoryFilter === id ? ' is-active' : ''}`,
      onClick: () => { categoryFilter = id; drawFilters(); drawList(); },
    }, label)));
  }

  const searchInput = h('input', {
    type: 'text', placeholder: 'Search ingredient, flavour or stream…',
    onInput: (e) => { query = e.target.value; drawList(); },
  });

  function visibleFindings() {
    const q = query.trim().toLowerCase();
    return findings.filter((f) => {
      if (categoryFilter !== 'all') {
        if (categoryFilter === 'unli_stream' && f.detector !== 'unli_stream' && f.detector !== 'unli_flavor') return false;
        if (categoryFilter !== 'unli_stream' && f.detector !== categoryFilter) return false;
      }
      if (q && !(`${f.title} ${f.subtitle}`.toLowerCase().includes(q))) return false;
      return true;
    });
  }

  function drawList() {
    const rows = visibleFindings();
    if (rows.length === 0) {
      mount(listHost, Card({}, EmptyState({ icon: '✓', title: 'Nothing matches', description: 'Clear the search or category filter to see the rest of the queue.', tight: true })));
      return;
    }
    mount(listHost, h('div', { class: 'card', style: { padding: '4px' } },
      ...rows.map((f) => h('button', {
        class: `studio-list-row${selected === f ? ' is-active' : ''}`,
        style: { padding: '10px 12px', alignItems: 'flex-start' },
        onClick: () => select(f),
      },
      h('span', { class: `kpi-icon kpi-icon-${f.severity}`, style: { flexShrink: '0' } }, icon(f.icon, 14)),
      h('span', { style: { flex: '1', minWidth: '0' } },
        h('div', { class: 't-xs t-faint' }, f.detectorLabel),
        h('div', { class: 't-sm t-strong' }, f.title),
        h('div', { class: 'sub' }, f.subtitle)),
      f.reviewable
        ? Pill(f.reviewStatus === 'confirmed_anomaly' ? 'crit' : f.reviewStatus === 'false_positive' ? 'flat' : 'warn',
          f.reviewStatus === 'confirmed_anomaly' ? 'confirmed' : f.reviewStatus === 'false_positive' ? 'false positive' : 'needs review')
        : Pill('info', 'context')))));
  }

  function field(label, ...value) {
    return h('div', { class: 'card card-sunk', style: { padding: '10px 12px' } },
      h('div', { class: 't-xs t-faint' }, label),
      h('div', { class: 't-strong', style: { marginTop: '3px' } }, ...value));
  }

  function drawRight() {
    if (!selected) {
      mount(rightHost, Card({ className: 'kpi-neutral', style: { minHeight: '220px', display: 'flex', alignItems: 'center', justifyContent: 'center' } },
        EmptyState({ icon: icon(ICONS.clipboardList, 26), title: 'Select a finding', description: 'Choose a record on the left to see its full evidence and review actions.', tight: true })));
      return;
    }
    const f = selected;
    if (f.detector === 'stock_loss') { drawStockLossInspector(f); return; }
    if (f.detector === 'consumption_variance') { drawVarianceInspector(f); return; }
    if (f.detector === 'unli_stream') { drawUnliStreamInspector(f); return; }
    drawUnliFlavorInspector(f);
  }

  function drawStockLossInspector(f) {
    const a = f.raw;
    mount(rightHost, Card({},
      CardHeader({ title: a.ingredientName, desc: 'Stock loss — flagged ledger event', right: Pill(a.severity, a.priority === 'review_soon' ? 'review soon' : 'worth a look') }),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '10px', marginBottom: '14px' } },
        field('Event date', stamp(a.timestamp)),
        field('Amount', h('span', { class: 't-crit' }, delta(a.amount, a.baseUnit))),
        a.category ? field('Category', humanise(a.category)) : null,
        a.reasonCode ? field('Reason recorded', humanise(a.reasonCode)) : null),
      h('div', { class: 't-sm t-soft', style: { marginBottom: '8px' } }, a.flaggedBecause),
      h('details', {},
        h('summary', { class: 't-xs t-soft' }, 'Technical detail'),
        h('div', { class: 't-xs t-faint', style: { marginTop: '6px' } },
          'Detector: Isolation Forest (stock-loss model) · ', a.score !== null ? `score ${a.score.toFixed(3)} · ` : '', `ledger #${a.id}`)),
      a.reviewStatus
        ? h('div', { class: 'callout callout-info', style: { marginTop: '14px' } },
          h('p', {}, h('strong', {}, a.reviewStatus === 'confirmed_anomaly' ? 'Confirmed anomaly. ' : 'Marked false positive. '),
            a.note ? a.note : 'No note recorded.'))
        : isAdmin
          ? h('div', { style: { display: 'flex', gap: '8px', marginTop: '14px' } },
            Button({ size: 'sm', onClick: () => doStockLossReview(a, 'confirmed_anomaly') }, 'Confirm'),
            Button({ size: 'sm', variant: 'ghost', onClick: () => doStockLossReview(a, 'false_positive') }, 'False positive'))
          : h('div', { class: 't-xs t-faint', style: { marginTop: '14px' } }, 'Awaiting admin review.')));
  }

  function doStockLossReview(a, status) {
    const noteInput = h('input', { type: 'text', placeholder: 'optional' });
    const err = ModalError();
    const submit = Button({ variant: 'primary' }, status === 'confirmed_anomaly' ? 'Confirm anomaly' : 'Mark false positive');
    submit.addEventListener('click', async () => {
      submit.disabled = true;
      try {
        await reviewAnomaly(a.id, status, noteInput.value.trim() || null);
        modal.close();
        a.reviewStatus = status; a.note = noteInput.value.trim() || null;
        drawList(); drawRight(); drawTop();
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

  // Spec 4 (Consumption Variance): "Investigate implausibly large
  // percentage values and denominator problems... Display percentage as
  // unavailable when the denominator is not meaningful... must not
  // cosmetically cap or hide those values." Two separate ways a denominator
  // stops being meaningful, both checked:
  //   1. consumedBase itself is tiny (e.g. 0.03g, a rounding artifact of a
  //      fractional-gram recipe line) — any real-sized count difference
  //      reads as a six-figure percentage.
  //   2. consumedBase is a normal size but the variance is many times
  //      LARGER than it (confirmed live on real seeded data: "Bacon" shows
  //      740g expected consumption against a +21.647kg variance — 2925%,
  //      not the six-figure case #1 guards against, but just as not
  //      "2925% more usage than planned"). A variance several times its own
  //      expected-consumption denominator is itself evidence of a
  //      reconciliation/unit problem upstream, not a genuine multi-thousand-
  //      percent usage swing — exactly what the request asked to flag
  //      rather than paper over with a smaller-looking capped number.
  // Neither path hides or recalculates countVarianceBase itself — the
  // absolute, unit-normalized difference is always shown in full.
  const VARIANCE_DENOMINATOR_FLOOR = 10;
  const VARIANCE_RATIO_SANITY_LIMIT = 10; // 1000% — past this the ratio itself is the problem, not the ingredient

  function drawVarianceInspector(f) {
    const i = f.raw;
    const tinyDenominator = i.consumedBase !== null && i.consumedBase < VARIANCE_DENOMINATOR_FLOOR;
    const implausibleRatio = i.varianceRatio !== null && Math.abs(i.varianceRatio) > VARIANCE_RATIO_SANITY_LIMIT;
    const percentNotMeaningful = tinyDenominator || implausibleRatio;
    mount(rightHost, Card({},
      CardHeader({ title: i.name, desc: 'Consumption variance — calculation only, no model', right: Pill('warn', 'review needed') }),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '10px', marginBottom: '14px' } },
        field('Expected consumption', qty(i.consumedBase, i.baseUnit, 'none')),
        field('Received', qty(i.receivedBase, i.baseUnit, 'none')),
        field('Absolute variance', h('span', { class: 't-crit' }, delta(i.countVarianceBase, i.baseUnit))),
        field('Percent of expected', percentNotMeaningful
          ? nodata(tinyDenominator ? 'not meaningful — expected consumption too small to divide by' : 'not meaningful — variance dwarfs expected consumption')
          : pct(i.varianceRatio === null ? null : i.varianceRatio * 100, 'n/a', 0))),
      percentNotMeaningful
        ? h('div', { class: 'callout callout-info', style: { marginBottom: '14px' } },
          h('p', {}, h('strong', {}, 'Percentage not shown. '),
            tinyDenominator
              ? `Expected consumption (${qty(i.consumedBase, i.baseUnit).textContent}) is too small a denominator for a percentage to mean anything — a tiny count difference would read as an enormous swing. The absolute variance above is the reliable number here.`
              : `The measured variance (${delta(i.countVarianceBase, i.baseUnit).textContent}) is several times larger than expected consumption itself (${qty(i.consumedBase, i.baseUnit).textContent}) — a ratio this far out usually points to a unit mismatch or a reconciliation data problem upstream, not a real multi-thousand-percent usage swing. Worth checking the underlying count/receiving records directly rather than trusting the percentage.`))
        : null,
      i.repeatedVarianceSignal && i.repeatedVarianceSignal.active
        ? h('div', { class: 'callout callout-warn', style: { marginBottom: '14px' } },
          h('p', {}, h('strong', {}, 'Repeated pattern. '),
            `This ingredient has run ${i.repeatedVarianceSignal.direction} expected across ${i.repeatedVarianceSignal.observationCount} independent physical counts, not just this one window.`))
        : null,
      h('div', { class: 't-xs t-faint' }, 'This view compares recipe + wastage consumption against the last physical count — context for a manager to review, not a model finding. There is no review/confirm action for a calculation-only comparison.')));
  }

  function drawUnliStreamInspector(f) {
    const a = f.raw;
    mount(rightHost, Card({},
      CardHeader({ title: a.label || a.ingredient_name, desc: 'Unlimited Wings — reconciled service-day', right: Pill('warn', 'context') }),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '10px', marginBottom: '14px' } },
        field('Service date', a.service_date),
        field('Covers', count(a.heads)),
        field('Variance vs. provisional', h('span', { class: 't-crit' }, pct(a.variance_pct, 'n/a', 1)))),
      h('div', { class: 't-sm t-soft', style: { marginBottom: '8px' } }, a.why),
      h('details', {},
        h('summary', { class: 't-xs t-soft' }, 'Technical detail'),
        h('div', { class: 't-xs t-faint', style: { marginTop: '6px' } },
          'Method: ', a.method === 'isolation_forest' ? 'Isolation Forest' : humanise(a.method),
          ' · ', streamDetails ? modelFreshnessLine(streamDetails) : (modelsLoaded ? 'status unavailable' : 'checking model status…'))),
      h('div', { class: 't-xs t-faint', style: { marginTop: '14px' } }, 'A normal, fixed-portion refill never appears here — only a whole reconciled service-day whose total measured consumption looked unusual. No review action is exposed for this stream in the current backend.')));
  }

  function drawUnliFlavorInspector(f) {
    const e = f.raw;
    mount(rightHost, Card({},
      CardHeader({ title: 'Flavour shift', desc: 'Unlimited Wings — flavour-level', right: Pill('warn', 'context') }),
      h('div', { style: { marginBottom: '10px' } },
        field('Service date', e.serviceDate)),
      h('div', { class: 't-xs t-faint', style: { marginBottom: '4px' } }, 'Primary flavour(s)'),
      h('div', { style: { marginBottom: '10px' } }, ...e.primaryFlavors.map((fl) => Pill('warn', fl))),
      e.secondaryFlavors.length
        ? h('div', { style: { marginBottom: '10px' } },
          h('div', { class: 't-xs t-faint', style: { marginBottom: '4px' } }, 'Secondary (likely compositional)'),
          h('div', { class: 't-xs t-soft' }, e.secondaryFlavors.join(', ')))
        : null,
      h('div', { class: 't-sm t-soft', style: { marginBottom: '8px' } }, e.why),
      h('details', {},
        h('summary', { class: 't-xs t-soft' }, 'Technical detail'),
        h('div', { class: 't-xs t-faint', style: { marginTop: '6px' } },
          `${e.totalFlagged} flag(s) that day · `, flavorDetails ? modelFreshnessLine(flavorDetails) : (modelsLoaded ? 'status unavailable' : 'checking model status…')))));
  }

  function modelFreshnessLine(details) {
    return details.trained_this_call
      ? `trained on ${details.records_evaluated} real reconciled day(s)`
      : `dormant — only ${details.records_evaluated} real reconciled day(s) (needs ${details.min_reconciled_days}+)`;
  }

  drawTop();
  drawFilters();
  drawList();
  drawRight();
  loadModelStatus(); // fire-and-forget — see its own comment above

  return h('div', { class: 'stock-usage-reviews' },
    h('div', { style: { marginBottom: '4px' } },
      h('h2', { style: { fontFamily: 'var(--font-display)', fontSize: '20px', margin: '0 0 2px' } }, 'Stock & Usage Reviews'),
      h('p', { class: 't-sm t-faint', style: { margin: '0 0 14px' } }, 'Investigate unusual inventory and consumption activity')),
    topHost,
    h('div', { class: 'card', style: { padding: '12px', marginBottom: '12px' } },
      h('label', { class: 'field', style: { display: 'block', marginBottom: '10px' } }, searchInput),
      filterHost),
    h('div', { class: 'studio-layout', style: { gridTemplateColumns: '1.3fr 1fr' } },
      h('div', { class: 'studio-col' }, listHost),
      h('div', { class: 'studio-col' }, rightHost)));
}
