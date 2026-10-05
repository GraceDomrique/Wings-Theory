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
import { getAnomalies, getIngredientVariance, reviewAnomaly } from '../../lib/api/intelligence.js';
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
  const active = ['loss', 'variance'].includes(ctx.tab) ? ctx.tab : 'loss';
  const body = h('div');

  const shell = h('div', {},
    Tabbar({
      items: [
        { id: 'loss', label: 'Stock loss' },
        { id: 'variance', label: 'Consumption variance' },
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
    mount(body, id === 'loss' ? await stockLossView(isAdmin) : await varianceView());
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
          render: (i) => i.hasCountData
            ? h('span', { class: 'mono t-sm' }, pct(i.varianceRatio === null ? null : i.varianceRatio * 100, 'n/a', 0))
            : h('span', { class: 't-xs t-faint' }, '—'),
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
