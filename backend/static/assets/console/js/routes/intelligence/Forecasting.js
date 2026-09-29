import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Pill } from '../../components/ui/Pill.js';
import { LineChart } from '../../components/charts/LineChart.js';
import { Loading } from '../../components/ui/Feedback.js';
import { palette } from '../../components/charts/base.js';
import { num, nodata } from '../../lib/format.js';
import { listDemandSummaries, getForecast } from '../../lib/api/intelligence.js';
import { getModelDetails } from '../../lib/api/system.js';

/**
 * Forecast & Insights > Forecasting.
 *
 * The rule that governs this whole screen (spec section 4): if a menu item has
 * fewer than the required days of real sales history, the chart is NOT drawn.
 * Not drawn flat, not drawn at zero, not drawn from a trailing average dressed
 * up as a prediction — the gap is stated instead. A fabricated line here is
 * worse than no line, because it looks like knowledge.
 */
export async function render(ctx) {
  const [items, models] = await Promise.all([
    listDemandSummaries(),
    getModelDetails().catch(() => null),
  ]);

  const ready = items.filter((i) => !i.insufficientData);
  const best = items.slice().sort((a, b) => b.daysOfHistory - a.daysOfHistory)[0];
  const requiredDays = best ? best.requiredDays : 7;

  const banner = ready.length === 0
    ? h('div', { class: 'callout callout-warn' },
      h('p', {},
        h('strong', {}, 'Forecast status — building history. '),
        `Prophet needs ${requiredDays}+ days of real sales per menu item before it can forecast demand reliably. `,
        best
          ? `The closest item so far has ${best.daysOfHistory} ${best.daysOfHistory === 1 ? 'day' : 'days'} on file. `
          : 'No menu item has any sales on file yet. ',
        'This is a data gap, not an error.'))
    : h('div', { class: 'callout callout-info' },
      h('p', {},
        h('strong', {}, `${ready.length} of ${items.length} items can be forecast. `),
        'The rest are still building history and are listed without a projection.'));

  // Item picker — defaults to whichever item has the most history, so the page
  // opens on the most informative chart rather than alphabetically.
  const ordered = items.slice().sort((a, b) => b.daysOfHistory - a.daysOfHistory);
  let selected = ordered[0] || null;

  const picker = h('select', {
    onChange: (e) => {
      selected = items.find((i) => String(i.menuItemId) === e.target.value);
      drawChart();
    },
  }, ...ordered.map((i) => h('option', { value: String(i.menuItemId) },
    `${i.name} — ${i.daysOfHistory} ${i.daysOfHistory === 1 ? 'day' : 'days'} of history`)));

  // How much history to pull. The server accepts up to 365 days (a full
  // year, the most this system tracks in one place — see wt/api.py's
  // forecast_demand) and a single-item request is already widened to at
  // least 90 server-side, so 90 is the real floor here, not 60.
  const LOOKBACK_OPTIONS = [
    { value: 90, label: 'Last 90 days' },
    { value: 180, label: 'Last 6 months' },
    { value: 365, label: 'Full year' },
  ];
  let lookbackDays = 90;
  const lookbackPicker = h('select', {
    onChange: (e) => { lookbackDays = Number(e.target.value); drawChart(); },
  }, ...LOOKBACK_OPTIONS.map((o) => h('option', { value: String(o.value) }, o.label)));

  const chartHost = h('div');

  // Daily points over a long window turn into visual noise long before the
  // trend does (per-item daily sales are low-volume — see the 2026-09 ML
  // validation). Past this many real days on the chart, the HISTORY portion
  // is aggregated into weekly totals instead; the forecast stays daily since
  // it is always exactly horizon_days points.
  const DAILY_POINT_LIMIT = 90;

  function toWeeklyBuckets(history) {
    const byWeek = new Map();
    for (const p of history) {
      const d = new Date(`${p.date}T00:00:00Z`);
      const day = (d.getUTCDay() + 6) % 7; // Monday = 0
      const monday = new Date(d);
      monday.setUTCDate(d.getUTCDate() - day);
      const key = monday.toISOString().slice(0, 10);
      byWeek.set(key, (byWeek.get(key) || 0) + p.qty);
    }
    return [...byWeek.entries()].sort(([a], [b]) => (a < b ? -1 : 1))
      .map(([date, qty]) => ({ date, qty }));
  }

  async function drawChart() {
    if (!selected) {
      mount(chartHost, LineChart({
        labels: [], data: [], height: 200,
        insufficient: {
          icon: '◔',
          title: 'No menu items to forecast',
          description: 'Items appear here once they are visible on the POS.',
          tight: true,
        },
      }));
      return;
    }
    mount(chartHost, Loading('Fitting…'));
    // The bulk list uses a cheap trailing average; asking for one item runs the
    // real Prophet fit. Only do that for the item actually being looked at.
    const series = await getForecast(selected.pk || selected.menuItemId, 7, lookbackDays);

    if (series.insufficientData) {
      mount(chartHost, LineChart({
        labels: [], data: [], height: 200,
        insufficient: {
          icon: '◔',
          title: `${series.name} needs ${series.requiredDays}+ days of sales`,
          description: `It has ${series.daysOfHistory} on file. Until then there is nothing to project from — so nothing is drawn.`,
          tight: true,
        },
      }));
      return;
    }

    const rawHistory = series.history || [];
    const forecast = series.series || [];
    const aggregated = rawHistory.length > DAILY_POINT_LIMIT;
    const history = aggregated ? toWeeklyBuckets(rawHistory) : rawHistory;

    const historyLabels = history.map((p) => p.date.slice(5));
    const labels = [...historyLabels, ...forecast.map((p) => p.date.slice(5))];

    mount(chartHost, LineChart({
      labels,
      // History and forecast are separate series so the reader can see where
      // measurement stops and projection starts. Padding with nulls (not
      // zeros) keeps them aligned without inventing data points.
      data: [...history.map((p) => p.qty), ...forecast.map(() => null)],
      extraSeries: [{
        label: 'Forecast',
        color: palette.amber,
        dashed: true,
        data: [...history.map(() => null), ...forecast.map((p) => p.predictedUnits)],
      }],
      color: palette.info,
      height: 200,
    }));

    mount(chartNote, h('div', { class: 't-xs t-faint', style: { marginTop: '6px' } },
      aggregated
        ? `${rawHistory.length} real days on file, shown here as ${history.length} weekly totals — daily points get noisy past ${DAILY_POINT_LIMIT} days. Forecast (dashed) stays daily.`
        : `${rawHistory.length} real days on file, shown daily.`));
  }

  const chartNote = h('div');

  await drawChart();

  return h('div', {},
    banner,
    h('div', { class: 'grid', style: { gridTemplateColumns: '2fr 1fr' } },
      Card({},
        CardHeader({
          title: 'Next 7 days — top menu items',
          desc: 'Forecasts units sold, not raw ingredient grams',
          right: h('label', { class: 'field', style: { minWidth: '200px' } }, picker),
        }),
        h('div', { style: { display: 'flex', justifyContent: 'flex-end', marginBottom: '8px' } },
          h('label', { class: 'field', style: { minWidth: '150px' } },
            h('span', { class: 't-xs t-faint' }, 'History shown'), lookbackPicker)),
        chartHost,
        chartNote),
      accuracyCard(models, selected)));
}

/**
 * Model accuracy — from wt.forecasting.evaluate_demand_model()'s real
 * rolling backtest (chronological expanding-window folds, WAPE/MAE/RMSE/
 * MASE per item, scored against three baselines plus Prophet; see
 * EVAL_METHOD_VERSION in wt/forecasting.py). `d.available` is only ever
 * false when there are no POS-visible menu items at all — do not read
 * d.reason/d.mae/d.item_name here, that was a stale shape from before the
 * 2026-09 rolling-backtest rewrite and no longer exists on the response.
 */
function accuracyCard(models, selected) {
  const d = models && models.demand_forecast ? models.demand_forecast : null;

  if (!d || d.available === false) {
    return Card({},
      h('div', { class: 'card-title', style: { marginBottom: '10px' } }, 'Model accuracy'),
      Stat({ value: nodata('—'), label: 'Not available yet' }),
      h('div', { class: 't-sm t-soft', style: { marginTop: '12px', lineHeight: '1.6' } },
        (d && d.reason) || 'No menu items to backtest against yet.'));
  }

  // The backtest only runs the full rolling evaluation on the
  // items_considered items with the most real sale history (see
  // ROLLING_MAX_ITEMS in wt/forecasting.py) — the currently-picked item may
  // or may not be one of them.
  const itemResult = selected
    ? (d.items || []).find((it) => it.item_id === selected.menuItemId)
    : null;
  const pvb = d.prophet_vs_baseline || {};
  const fleetProphet = (d.fleet_summary || {}).prophet || {};

  const perItemBlock = itemResult && !itemResult.insufficient_history
    ? (() => {
      const m = itemResult.methods || {};
      const sel = m[itemResult.selected_method] || {};
      return [
        h('div', { style: { display: 'flex', gap: '18px', marginBottom: '10px', flexWrap: 'wrap' } },
          Stat({ value: sel.wape == null ? nodata('n/a') : num(sel.wape, { decimals: 1 }), label: 'WAPE % (this item)' }),
          Stat({ value: sel.mae == null ? nodata('n/a') : num(sel.mae, { decimals: 2 }), label: 'MAE' }),
          Stat({ value: sel.mase == null ? nodata('n/a') : num(sel.mase, { decimals: 2 }), label: 'MASE' })),
        h('div', { class: 't-sm t-soft', style: { lineHeight: '1.6', marginBottom: '10px' } },
          `Selected method for ${itemResult.item_name}: ${itemResult.selected_method.replace(/_/g, ' ')} — ${itemResult.selection_reason}`),
      ];
    })()
    : [h('div', { class: 't-sm t-soft', style: { lineHeight: '1.6', marginBottom: '10px' } },
      selected
        ? `${selected.name} isn't one of the ${d.items_evaluated} items with the most sales history yet, `
          + 'so it wasn\'t individually backtested — the fleet numbers below still apply to the model as a whole.'
        : '')];

  return Card({},
    h('div', { class: 'card-title', style: { marginBottom: '10px' } }, 'Model accuracy'),
    ...perItemBlock,
    h('div', { class: 't-xs t-faint', style: { marginBottom: '4px' } }, 'Fleet-wide (Prophet)'),
    h('div', { style: { display: 'flex', gap: '18px', marginBottom: '12px', flexWrap: 'wrap' } },
      Stat({ value: fleetProphet.mean_wape == null ? nodata('n/a') : num(fleetProphet.mean_wape, { decimals: 1 }), label: 'Mean WAPE %' }),
      Stat({ value: fleetProphet.mean_mae == null ? nodata('n/a') : num(fleetProphet.mean_mae, { decimals: 2 }), label: 'Mean MAE' })),
    h('div', { class: 't-sm t-soft', style: { lineHeight: '1.6' } },
      // Plain string interpolation here, not num() — num() returns a DOM
      // Text node for use as a vdom child, and stringifies to "[object
      // Text]" if concatenated into a template literal instead.
      pvb.prophet_actually_better
        ? `Prophet beats the best baseline by ${fmt1(pvb.absolute_improvement_wape_pts)} WAPE points on this data.`
        : `Prophet is not measurably better than the best simple baseline on this data yet `
          + `(fleet mean WAPE ${fmt1(pvb.prophet_mean_wape)}% vs ${fmt1(pvb.best_baseline_mean_wape)}%) — `
          + 'still shown because it can beat the baseline on individual items even when the fleet average doesn\'t.'),
    h('div', { class: 't-sm t-soft', style: { marginTop: '6px' } },
      `Backtested ${d.items_evaluated} of ${d.items_considered} menu items`
      + (d.evaluated_date_range ? `, ${d.evaluated_date_range.from} → ${d.evaluated_date_range.to}.` : '.')),
    h('div', { style: { marginTop: '10px' } }, Pill('good', 'holdout tested')));
}

/** Plain-string one-decimal formatting for prose, as opposed to lib/format.js's
 * num(), which returns a DOM node for use as a vdom child. */
function fmt1(v) {
  return v == null ? 'n/a' : v.toFixed(1);
}
