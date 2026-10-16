import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { LineChart } from '../../components/charts/LineChart.js';
import { Loading } from '../../components/ui/Feedback.js';
import { palette } from '../../components/charts/base.js';
import { listDemandSummaries, getForecast } from '../../lib/api/intelligence.js';
import { getModelDetails } from '../../lib/api/system.js';
import { session } from '../../lib/api/client.js';

/**
 * Forecast & Insights > Demand Forecasts (restored 2026-10 — the route id
 * changed from the pre-redesign 'forecasting' to 'forecasts' and this
 * module lives under the new frontend/ tree, but the page itself is
 * unchanged apart from the freshness chip added below).
 *
 * The rule that governs this whole screen (spec section 4): if a menu item has
 * fewer than the required days of real sales history, the chart is NOT drawn.
 * Not drawn flat, not drawn at zero, not drawn from a trailing average dressed
 * up as a prediction — the gap is stated instead. A fabricated line here is
 * worse than no line, because it looks like knowledge.
 */
export async function render(ctx) {
  const isOwner = (session.user || {}).role === 'owner';
  const items = await listDemandSummaries();

  // getModelDetails() is NOT awaited alongside the data this page actually
  // needs to render: /console/model-details is a known-slow endpoint (see
  // Losses.js's own fix for the same issue) that otherwise blocks the whole
  // chart from ever appearing. freshnessHost starts with a neutral loading
  // placeholder and is swapped for the real banner once the call resolves,
  // same decoupled pattern as Losses.js's modelStatusHost.
  const freshnessHost = h('div', { class: 'load-note' }, 'Checking purchase-recommendation freshness…');
  getModelDetails()
    .then((models) => mount(freshnessHost, freshnessBanner(models, isOwner)))
    .catch(() => mount(freshnessHost, freshnessBanner(null, isOwner)));

  const ready = items.filter((i) => !i.insufficientData);
  const best = items.slice().sort((a, b) => b.daysOfHistory - a.daysOfHistory)[0];
  const requiredDays = best ? best.requiredDays : 7;

  const banner = ready.length === 0
    ? h('div', { class: 'callout callout-warn' },
      h('p', {},
        h('strong', {}, 'Forecast status — building history. '),
        isOwner
          ? `The forecasting system needs ${requiredDays}+ days of real sales per menu item before it can forecast demand reliably. `
          : `Prophet needs ${requiredDays}+ days of real sales per menu item before it can forecast demand reliably. `,
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
      label: 'Actual sales',
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
      height: 340,
    }));

    const methodLabel = series.prophetUnavailable || series.method === 'trailing_average'
      ? 'trailing average (Prophet unavailable)'
      : 'Prophet';
    const methodNote = isOwner ? '' : ` Method used for this chart: ${methodLabel}.`;
    mount(chartNote, h('div', { class: 't-xs t-faint', style: { marginTop: '6px' } },
      aggregated
        ? `${rawHistory.length} real days on file, shown here as ${history.length} weekly totals — daily points get noisy past ${DAILY_POINT_LIMIT} days. Forecast (dashed) stays daily.${methodNote}`
        : `${rawHistory.length} real days on file, shown daily.${methodNote}`));
  }

  const chartNote = h('div');

  await drawChart();

  const legend = h('div', { class: 'forecast-legend' },
    h('span', { class: 'forecast-legend-item' }, h('span', { class: 'forecast-legend-swatch', style: { background: palette.info } }), 'Actual sales (history)'),
    h('span', { class: 'forecast-legend-item' }, h('span', { class: 'forecast-legend-swatch dashed', style: { borderColor: palette.amber } }), 'Forecast (projected)'));

  return h('div', {},
    banner,
    freshnessHost,
    Card({},
      CardHeader({
        title: 'Next 7 days — top menu items',
        desc: 'Forecasts units sold, not raw ingredient grams',
        right: h('label', { class: 'field', style: { minWidth: 'min(200px, 100%)' } }, picker),
      }),
      h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px', flexWrap: 'wrap', gap: '8px' } },
        legend,
        h('label', { class: 'field', style: { minWidth: 'min(150px, 100%)' } },
          h('span', { class: 't-xs t-faint' }, 'History shown'), lookbackPicker)),
      chartHost,
      chartNote));
}

/**
 * 2026-10 fix: this page's Prophet fit always runs live per request (see
 * wt/forecasting.menu_item_demand_prophet) regardless of the ingredient
 * depletion_demand_model setting — but the REPLENISHMENT side (What to
 * Buy) depends on a separate background refresh actually having run
 * recently (see wt/depletion.run_prophet_refresh_job). Surfacing that
 * status here, where an owner/admin is already looking at forecasting
 * health, answers "is hybrid actually fitted, or just configured?" without
 * requiring a trip to System & Models.
 */
function freshnessBanner(models, isOwner) {
  const status = models && models.prophet_baseline_refresh;
  if (!status) {
    return h('div', { class: 'callout callout-warn' },
      h('p', {},
        h('strong', {}, 'Purchase recommendations are still building history. '),
        isOwner
          ? 'They are running on a basic estimate until the system has enough data for a full refresh.'
          : 'Ingredient purchase recommendations are running on the simple trailing-average fallback '
            + 'until the first scheduled or manual refresh completes.'));
  }
  if (!status.lastRunOk) {
    return h('div', { class: 'callout callout-warn' },
      h('p', {},
        h('strong', {}, 'The last purchase-recommendation refresh did not complete. '),
        isOwner
          ? `(At ${status.lastRunAt}.) `
          : `(${status.error || 'unknown error'}, at ${status.lastRunAt}.) `,
        isOwner
          ? 'Recommendations are still using the most recent data available.'
          : 'Purchase recommendations keep using each item\'s last good fit, or the trailing average where none exists.'));
  }
  return h('div', { class: 'callout callout-info' },
    h('p', {},
      isOwner
        ? h('strong', {}, `Purchase recommendations were last updated ${status.lastRunAt}.`)
        : [h('strong', {}, `Replenishment baselines last refreshed ${status.lastRunAt} `),
          `(${status.fitted} item(s) fitted, triggered by ${status.triggeredBy}).`]));
}

/**
 * Model accuracy card removed (2026-10): the WAPE/MAE/MASE rolling-backtest
 * breakdown it showed was technical detail nobody on this page's audience
 * (owner or admin) acted on directly — the chart's own "X real days on
 * file" note and the insufficient-history gap messaging above already say
 * what matters ("can this be trusted yet"). The backtest itself still runs
 * server-side (wt.forecasting.evaluate_demand_model()) and is unaffected;
 * only this console surface for it is gone.
 */
