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

  const chartHost = h('div');

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
    const series = await getForecast(selected.pk || selected.menuItemId, 7);

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

    const history = series.history || [];
    const forecast = series.series || [];
    const labels = [...history.map((p) => p.date.slice(5)), ...forecast.map((p) => p.date.slice(5))];

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
  }

  await drawChart();

  return h('div', {},
    banner,
    h('div', { class: 'grid', style: { gridTemplateColumns: '2fr 1fr' } },
      Card({},
        CardHeader({
          title: 'Next 7 days — top menu items',
          desc: 'Forecasts units sold, not raw ingredient grams',
          right: h('label', { class: 'field', style: { minWidth: '260px' } }, picker),
        }),
        chartHost),
      accuracyCard(models)));
}

/**
 * Model accuracy. When Prophet has no holdout test the backend explains why,
 * and that reason is repeated verbatim — an accuracy of "—" with a reason is
 * honest; an accuracy of 0 would not be.
 */
function accuracyCard(models) {
  const d = models && models.demand_forecast ? models.demand_forecast : null;

  if (!d || !d.available) {
    return Card({},
      h('div', { class: 'card-title', style: { marginBottom: '10px' } }, 'Model accuracy'),
      Stat({ value: nodata('—'), label: 'Not available yet' }),
      h('div', { class: 't-sm t-soft', style: { marginTop: '12px', lineHeight: '1.6' } },
        d && d.reason
          ? d.reason
          : 'MAE, RMSE and MAPE populate once a holdout test has enough sales history to evaluate against.'));
  }

  return Card({},
    h('div', { class: 'card-title', style: { marginBottom: '10px' } }, 'Model accuracy'),
    h('div', { style: { display: 'flex', gap: '18px', marginBottom: '12px', flexWrap: 'wrap' } },
      Stat({ value: num(d.mae, { decimals: 2 }), label: 'MAE' }),
      Stat({ value: num(d.rmse, { decimals: 2 }), label: 'RMSE' }),
      Stat({ value: d.mape === null ? nodata('n/a') : num(d.mape, { decimals: 1 }), label: 'MAPE %' })),
    h('div', { class: 't-sm t-soft', style: { lineHeight: '1.6' } },
      `Backtested on ${d.item_name} — trained on ${d.trained_on_days} days, held out ${d.holdout_days}.`),
    h('div', { style: { marginTop: '10px' } }, Pill('good', 'holdout tested')));
}
