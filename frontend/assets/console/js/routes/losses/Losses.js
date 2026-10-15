import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Tabbar } from '../../components/ui/Tabbar.js';
import { toast } from '../../components/ui/Feedback.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { palette, destroyAllCharts } from '../../components/charts/base.js';
import { count, delta, qty, pct, stamp, humanise, nodata } from '../../lib/format.js';
import {
  getAnomalies, getIngredientVariance, reviewAnomaly, getFlavorAnomalyEvents,
} from '../../lib/api/intelligence.js';
import { getModelDetails } from '../../lib/api/system.js';
import { session } from '../../lib/api/client.js';

const ANOMALY_LOOKBACK_DAYS = 90;
const VARIANCE_LOOKBACK_DAYS = 30;

/**
 * Losses & Stock Issues — merges the old Intelligence > Anomaly Detection
 * and Intelligence > Ingredient Variance tabs (2026-09-30 UX/IA redesign):
 * both answer the same business question ("did something go wrong with
 * this ingredient's stock, worth a look") from two different data sources
 * (Isolation Forest flags vs. expected-vs-counted), so they read as one
 * task, not two unrelated "uses ML" screens.
 *
 * Copy discipline carried over unchanged from the old AnomalyDetection.js
 * (spec section 6 point 5): these are informational flags, never
 * accusations. The confirm/false-positive actions added here are new --
 * `POST /console/anomalies/{id}/review` existed server-side with no UI
 * caller at all until now, which is also why validation_metrics() had zero
 * confirmed labels to score against (see the ML validation notes).
 */
export async function render(ctx) {
  const isAdmin = (session.user || {}).role === 'admin';
  const active = ['loss', 'variance', 'consumption'].includes(ctx.tab) ? ctx.tab : 'loss';
  const body = h('div');

  const shell = h('div', {},
    Tabbar({
      items: [
        { id: 'loss', label: 'Stock loss' },
        { id: 'variance', label: 'Consumption variance' },
        { id: 'consumption', label: 'Unlimited Wings consumption' },
      ],
      activeId: active,
      onChange: (id) => {
        destroyAllCharts();
        history.replaceState(null, '', `#/losses/${id}`);
        draw(id);
      },
    }),
    body);

  async function draw(id) {
    mount(body, id === 'loss' ? await stockLossView(isAdmin)
      : id === 'variance' ? await varianceView()
      : await consumptionAnomalyView());
  }

  await draw(active);
  return shell;
}

const CATEGORY_LABEL = {
  wastage: 'Wastage',
  spoilage: 'Spoilage',
  count_adjustment: 'Count adjustment',
};

async function stockLossView(isAdmin) {
  let { stockLoss } = await getAnomalies(ANOMALY_LOOKBACK_DAYS);
  const listHost = h('div');

  const ingredientsInvolved = new Set(stockLoss.map((a) => a.ingredientName)).size;
  const byIngredient = new Map();
  for (const a of stockLoss) byIngredient.set(a.ingredientName, (byIngredient.get(a.ingredientName) || 0) + 1);
  const clusters = [...byIngredient.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);

  // Category/priority/review filters -- so a manager can jump straight to
  // "spoilage, review soon, not yet reviewed" instead of scanning the full
  // flagged list every time. Filtering never changes the stats/chart above,
  // which stay computed against the full unfiltered window.
  const categoriesPresent = [...new Set(stockLoss.map((a) => a.category).filter(Boolean))];
  let categoryFilter = '';
  let priorityFilter = '';
  let reviewFilter = '';

  function filteredRows() {
    return stockLoss.filter((a) => (!categoryFilter || a.category === categoryFilter)
      && (!priorityFilter || a.priority === priorityFilter)
      && (!reviewFilter || (reviewFilter === 'unreviewed' ? !a.reviewStatus : a.reviewStatus === reviewFilter)));
  }

  const categoryPicker = h('select', {
    onChange: (e) => { categoryFilter = e.target.value; drawList(); },
  },
  h('option', { value: '' }, 'All categories'),
  ...categoriesPresent.map((c) => h('option', { value: c }, CATEGORY_LABEL[c] || humanise(c))));

  const priorityPicker = h('select', {
    onChange: (e) => { priorityFilter = e.target.value; drawList(); },
  },
  h('option', { value: '' }, 'All priorities'),
  h('option', { value: 'review_soon' }, 'Review soon'),
  h('option', { value: 'worth_a_look' }, 'Worth a look'));

  const reviewPicker = h('select', {
    onChange: (e) => { reviewFilter = e.target.value; drawList(); },
  },
  h('option', { value: '' }, 'All statuses'),
  h('option', { value: 'unreviewed' }, 'Not yet reviewed'),
  h('option', { value: 'confirmed_anomaly' }, 'Confirmed'),
  h('option', { value: 'false_positive' }, 'False positive'));

  function drawList() {
    const rows = filteredRows();
    mount(listHost, Table({
      columns: [
        { key: 'ingredient', label: 'Ingredient', render: (a) => a.ingredientName },
        { key: 'when', label: 'When', render: (a) => h('span', { class: 'mono t-xs' }, stamp(a.timestamp)) },
        { key: 'amount', label: 'Amount', render: (a) => h('span', { class: 't-crit mono t-sm' }, delta(a.amount, a.baseUnit)) },
        {
          key: 'category', label: 'Category',
          render: (a) => a.category ? Pill('flat', CATEGORY_LABEL[a.category] || humanise(a.category)) : null,
        },
        {
          key: 'why', label: 'Why flagged',
          render: (a) => h('div', {},
            h('span', { class: 't-sm t-soft' }, a.flaggedBecause),
            a.reasonCode ? h('div', { class: 't-xs t-faint' }, humanise(a.reasonCode)) : null,
            a.reviewStatus
              ? h('div', { class: 't-xs', style: { marginTop: '2px' } },
                Pill(a.reviewStatus === 'confirmed_anomaly' ? 'crit' : a.reviewStatus === 'false_positive' ? 'flat' : 'info',
                  a.reviewStatus === 'confirmed_anomaly' ? 'confirmed'
                    : a.reviewStatus === 'false_positive' ? 'false positive' : humanise(a.reviewStatus)))
              : null),
        },
        {
          key: 'priority', label: 'Priority',
          render: (a) => Pill(a.severity, a.priority === 'review_soon' ? 'review soon' : 'worth a look'),
        },
        !isAdmin ? null : {
          key: 'review', label: 'Review',
          render: (a) => a.reviewStatus
            ? null
            : h('div', { style: { display: 'flex', gap: '6px' } },
              Button({ size: 'sm', onClick: () => doReview(a, 'confirmed_anomaly') }, 'Confirm'),
              Button({ size: 'sm', onClick: () => doReview(a, 'false_positive') }, 'False positive')),
        },
      ].filter(Boolean),
      rows,
      empty: EmptyState({
        icon: '✓',
        title: stockLoss.length ? 'No flags match this filter' : 'Nothing flagged for review',
        description: stockLoss.length
          ? 'Widen or clear the filters above to see the rest of the flagged list.'
          : 'Waste and stock-count entries are all within each ingredient’s normal range for this window.',
      }),
    }));
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
        toast(status === 'confirmed_anomaly' ? 'Marked as a confirmed anomaly.' : 'Marked as a false positive.', 'good');
        drawList();
      } catch (e) { err.textContent = e.message; err.style.display = ''; } finally { submit.disabled = false; }
    });
    const modal = Modal({
      // stamp() returns a Node (lib/format.js), not a string -- Modal's
      // title accepts either, but only if handed one directly, never
      // string-interpolated (that stringifies a Node to "[object Text]").
      title: h('span', {}, a.ingredientName, ' — ', stamp(a.timestamp)),
      desc: a.flaggedBecause,
      onClose: () => {},
    },
    h('label', { class: 'field' }, 'Note (optional)', noteInput),
    err,
    h('div', { style: { marginTop: '14px', display: 'flex', justifyContent: 'flex-end' } }, submit));
  }

  drawList();

  return h('div', {},
    h('div', { class: 'callout callout-info', style: { marginBottom: '14px' } },
      h('p', {}, h('strong', {}, 'What this is. '),
        'Every time waste is logged or a physical stock count doesn’t match what the system expected, it’s checked against that ingredient’s own normal pattern. Anything unusually large shows up below — a flag worth a look, not proof anything went wrong.')),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)', marginBottom: '14px' } },
      Card({}, Stat({ value: count(stockLoss.length), label: 'Flagged stock losses' })),
      Card({}, Stat({ value: count(ingredientsInvolved), label: 'Ingredients involved' })),
      Card({}, Stat({ value: `${ANOMALY_LOOKBACK_DAYS}d`, label: 'Lookback window' }))),

    h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1.3fr' } },
      Card({},
        h('div', { class: 'card-title', style: { marginBottom: '10px' } }, 'Where losses cluster'),
        BarChart({
          labels: clusters.map(([name]) => name),
          data: clusters.map(([, n]) => n),
          color: palette.crit,
          height: 180,
          empty: {
            icon: '✓',
            title: 'No losses flagged in this window',
            description: `Nothing in the last ${ANOMALY_LOOKBACK_DAYS} days stood out against each ingredient's own usual pattern.`,
            tight: true,
          },
        })),
      Card({},
        CardHeader({
          title: 'Flagged, for review',
          desc: isAdmin
            ? 'Waste and count entries larger than usual for that ingredient — confirm or mark false positive so this model can be scored against real outcomes'
            : 'Waste and count entries larger than usual for that ingredient — a flag to look into, not a finding',
        }),
        h('div', { style: { display: 'flex', gap: '10px', marginBottom: '12px', flexWrap: 'wrap' } },
          h('label', { class: 'field', style: { minWidth: '160px' } }, 'Category', categoryPicker),
          h('label', { class: 'field', style: { minWidth: '160px' } }, 'Priority', priorityPicker),
          h('label', { class: 'field', style: { minWidth: '160px' } }, 'Review status', reviewPicker)),
        listHost)));
}

/**
 * 2026-10-15 fix: variance_ratio (backend/wt/reservation.py) divides the
 * absolute count variance by expected consumption with no floor beyond
 * "not literally zero" (EPS = 1e-6) — an ingredient with a real absolute
 * discrepancy but near-zero expected consumption this window produces a
 * mathematically correct but practically meaningless percentage (confirmed
 * live: ratios over 1000%, e.g. 213,565%, when consumedBase was a tiny
 * fraction relative to the ingredient's real throughput). This is NOT a
 * unit-conversion bug — consumed_base and count_variance_base are both
 * already in the same base unit (see reservation.py's own column names)
 * — it's a denominator-too-small problem, so the fix is presentational,
 * not a backend recalculation: the raw ratio/flag threshold (10%, backend-
 * owned) is untouched, only what this column PRINTS changes.
 *
 * "Reliable" is judged against receivedBase for the SAME ingredient, SAME
 * window — an independent real-activity measure already in this row,
 * rather than an arbitrary fixed number (meaningless across grams vs.
 * pieces vs. litres). If this ingredient received real stock but
 * "expected consumption" is under 1% of that, the denominator clearly
 * isn't representative of real throughput.
 */
function varianceDenominatorReliable(i) {
  if (!(i.consumedBase > 0)) return false;
  if (i.receivedBase > 0 && i.consumedBase < i.receivedBase * 0.01) return false;
  return true;
}

async function varianceView() {
  const report = await getIngredientVariance(VARIANCE_LOOKBACK_DAYS);
  const items = report.items || [];
  const flagged = items.filter((i) => i.flagged);
  const counted = items.filter((i) => i.hasCountData);
  const listHost = h('div');

  // Same "categorize so it's fast to find" filter as the Stock loss tab,
  // over this table's own three-way status instead of the anomaly reason.
  let statusFilter = '';
  function statusOf(i) { return !i.hasCountData ? 'no_count' : i.flagged ? 'review_needed' : 'within_range'; }
  function filteredItems() {
    return statusFilter ? items.filter((i) => statusOf(i) === statusFilter) : items;
  }

  const statusPicker = h('select', {
    onChange: (e) => { statusFilter = e.target.value; drawList(); },
  },
  h('option', { value: '' }, 'All statuses'),
  h('option', { value: 'review_needed' }, 'Review needed'),
  h('option', { value: 'within_range' }, 'Within range'),
  h('option', { value: 'no_count' }, 'No count data'));

  function drawList() {
    const rows = filteredItems();
    mount(listHost, Table({
      columns: [
        { key: 'ingredient', label: 'Ingredient', render: (i) => i.name },
        { key: 'consumed', label: 'Expected consumption', render: (i) => h('span', { class: 'mono t-sm' }, qty(i.consumedBase, i.baseUnit, 'none')) },
        { key: 'received', label: 'Received', render: (i) => h('span', { class: 'mono t-sm' }, qty(i.receivedBase, i.baseUnit, 'none')) },
        {
          key: 'variance', label: 'Count variance',
          render: (i) => i.hasCountData
            ? h('span', { class: `mono t-sm ${i.flagged ? 't-crit' : ''}` }, delta(i.countVarianceBase, i.baseUnit))
            : h('span', { class: 't-xs t-faint' }, nodata('no count in window')),
        },
        {
          key: 'ratio', label: '% of expected',
          render: (i) => {
            if (!i.hasCountData) return h('span', { class: 't-xs t-faint' }, '—');
            if (!varianceDenominatorReliable(i)) {
              return h('span', {
                class: 't-xs t-faint',
                title: `Expected consumption this window (${qty(i.consumedBase, i.baseUnit, 'none').textContent}) is too small relative to this ingredient's real activity for a percentage to be meaningful — see the absolute count variance instead.`,
              }, 'not meaningful');
            }
            return h('span', { class: 'mono t-sm' }, pct(i.varianceRatio === null ? null : i.varianceRatio * 100, 'n/a', 0));
          },
        },
        {
          key: 'status', label: 'Status',
          render: (i) => !i.hasCountData ? Pill('flat', 'no count data') : i.flagged ? Pill('warn', 'review needed') : Pill('good', 'within range'),
        },
      ],
      rows,
      empty: EmptyState({
        icon: '✓',
        title: items.length ? 'No ingredients match this filter' : 'No ingredient activity in this window',
        description: items.length
          ? 'Widen or clear the status filter above to see the rest of the list.'
          : `Nothing moved through receiving, recipes, wastage or a physical count in the last ${VARIANCE_LOOKBACK_DAYS} days.`,
      }),
    }));
  }

  drawList();

  return h('div', {},
    h('div', { class: 'callout callout-info', style: { marginBottom: '14px' } },
      h('p', {}, h('strong', {}, 'What this is. '),
        'For each ingredient, this compares what the recipes + waste log say should have been used against what a physical count actually found. A big gap ("review needed") is worth checking — it could be a missed count, an unrecorded waste, or a real loss.')),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', marginBottom: '14px' } },
      Card({}, Stat({ value: count(items.length), label: 'Ingredients with activity' })),
      Card({}, Stat({ value: count(counted.length), label: 'Physically counted' })),
      Card({}, Stat({ value: count(flagged.length), label: 'Flagged for review' })),
      Card({}, Stat({ value: `${report.days ?? VARIANCE_LOOKBACK_DAYS}d`, label: 'Window' }))),

    Card({},
      CardHeader({
        title: 'Expected vs. measured, per ingredient',
        desc: 'Recipe and wastage consumption from the ledger, against what the last physical count in this window found — a variance is context for a manager to review, not a finding of cause',
      }),
      h('div', { style: { display: 'flex', gap: '10px', marginBottom: '12px', flexWrap: 'wrap' } },
        h('label', { class: 'field', style: { minWidth: '160px' } }, 'Status', statusPicker)),
      listHost));
}

/**
 * 2026-10 fix: the backend has returned consumption_anomalies (stream-level,
 * reconciled Unlimited Wings service-days) and flavor-level anomaly events
 * since they were built, but nothing in the console rendered either — this
 * tab is that missing UI. Two separate sections, never merged into one list
 * (wt/anomaly.py's own module docstring: the stock-loss pass above, this
 * stream-level pass, and the flavor-level pass answer three different
 * questions and must never blend). Each section shows its own model's
 * dormant/trained state honestly (from GET /console/model-details) instead
 * of letting an empty list read as "nothing wrong" when the real reason is
 * "no real reconciled data exists yet to train on" — see the 2026-10-05
 * forensic audit finding that unli_reconciliation currently has zero real
 * rows in this deployment.
 *
 * A routine, fixed 3-pieces-per-head refill is never flagged here: both
 * passes operate on reconciled SERVICE-DAYS (unli.record_reconciliation()
 * output), not individual refill rounds — there is no granularity at which
 * a normal refill could even appear as a row to flag.
 */
async function consumptionAnomalyView() {
  const [{ consumption_anomalies: streamAnomalies }, flavorEvents, models] = await Promise.all([
    getAnomalies(ANOMALY_LOOKBACK_DAYS),
    getFlavorAnomalyEvents(ANOMALY_LOOKBACK_DAYS),
    getModelDetails().catch(() => null),
  ]);

  const streamDetails = models && models.anomaly_detection;
  const flavorDetails = models && models.flavor_anomaly_detection;

  return h('div', {},
    h('div', { class: 'callout callout-info', style: { marginBottom: '14px' } },
      h('p', {}, h('strong', {}, 'What this is. '),
        'Each day’s unlimited-wings weigh-in is compared against that stream’s own usual pattern. A normal 3-piece refill round never shows up here — only a whole reconciled day whose total measured consumption looked unusually high or low.')),
    Card({},
      CardHeader({
        title: 'Unlimited Wings Consumption Anomaly Detection',
        desc: 'Reconciled service-days whose measured consumption looked unusual against this stream\'s own history — informational only, never an accusation, and never raised by an ordinary fixed-portion refill',
      }),
      consumptionModelStatus(streamDetails),
      streamAnomalies.length === 0
        ? EmptyState({
          icon: '✓',
          title: (streamDetails && streamDetails.trained_this_call)
            ? 'Nothing flagged for review'
            : 'Nothing to show yet',
          description: (streamDetails && streamDetails.trained_this_call)
            ? `Reconciled days in the last ${ANOMALY_LOOKBACK_DAYS} days are all within this stream's normal pattern.`
            : 'This model has not trained on enough real reconciled service-days yet — see the status above.',
        })
        : Table({
          columns: [
            { key: 'date', label: 'Service date', render: (a) => h('span', { class: 'mono t-xs' }, a.service_date) },
            { key: 'stream', label: 'Stream', render: (a) => a.label || a.ingredient_name },
            { key: 'heads', label: 'Covers', render: (a) => h('span', { class: 'mono t-sm' }, count(a.heads)) },
            {
              key: 'variance', label: 'Variance vs. provisional',
              render: (a) => h('span', { class: 'mono t-sm t-crit' }, pct(a.variance_pct, 'n/a', 1)),
            },
            { key: 'why', label: 'Why flagged', render: (a) => h('span', { class: 't-sm t-soft' }, a.why) },
            {
              key: 'method', label: 'Method',
              render: (a) => Pill('flat', a.method === 'isolation_forest' ? 'isolation forest' : humanise(a.method)),
            },
          ],
          rows: streamAnomalies,
          empty: EmptyState({ icon: '✓', title: 'Nothing flagged', description: '' }),
        })),

    Card({ style: { marginTop: '14px' } },
      CardHeader({
        title: 'Flavor-Level Consumption Anomaly Detection',
        desc: 'The same reconciled-consumption question, one dimension finer — per-flavour share of a stream\'s wings on a given day, grouped into one event per date so a compositional shift reads as a single alert',
      }),
      consumptionModelStatus(flavorDetails),
      flavorEvents.length === 0
        ? EmptyState({
          icon: '✓',
          title: (flavorDetails && flavorDetails.trained_this_call)
            ? 'Nothing flagged for review'
            : 'Nothing to show yet',
          description: (flavorDetails && flavorDetails.trained_this_call)
            ? `Reconciled flavour-days in the last ${ANOMALY_LOOKBACK_DAYS} days are all within normal range.`
            : 'This model has not trained on enough real reconciled flavour-days yet — see the status above.',
        })
        : Table({
          columns: [
            { key: 'date', label: 'Service date', render: (e) => h('span', { class: 'mono t-xs' }, e.serviceDate) },
            {
              key: 'primary', label: 'Primary flavour(s)',
              render: (e) => e.primaryFlavors.map((f) => Pill('warn', f)),
            },
            {
              key: 'secondary', label: 'Secondary (likely compositional)',
              render: (e) => e.secondaryFlavors.length
                ? h('span', { class: 't-xs t-soft' }, e.secondaryFlavors.join(', '))
                : h('span', { class: 't-xs t-faint' }, '—'),
            },
            { key: 'count', label: 'Flags that day', render: (e) => count(e.totalFlagged) },
            { key: 'why', label: 'Why', render: (e) => h('span', { class: 't-sm t-soft' }, e.why) },
          ],
          rows: flavorEvents,
          empty: EmptyState({ icon: '✓', title: 'Nothing flagged', description: '' }),
        })));
}

/** Honest model-status line shared by both consumption sections above —
 * never fabricates a signal when there isn't enough real data to train on
 * (per the 2026-10-05 forensic audit, this deployment's own
 * unli_reconciliation table currently has zero real, non-synthetic rows). */
function consumptionModelStatus(details) {
  if (!details) return null;
  const trained = !!details.trained_this_call;
  return h('div', { class: 't-xs t-faint', style: { marginBottom: '10px' } },
    trained
      ? `Model trained on ${details.records_evaluated} real reconciled day(s) in this window.`
      : `Model dormant — ${details.records_evaluated} real reconciled day(s) in this window `
        + `(needs ${details.min_reconciled_days}+ to train). Synthetic/simulated rows are excluded `
        + 'from training and flagging, never presented as real evidence.');
}
