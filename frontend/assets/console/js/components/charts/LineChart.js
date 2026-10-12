import { chartBox, cartesianOptions, palette } from './base.js';
import { EmptyState } from '../ui/EmptyState.js';

/**
 * Forecast and sales-trend line chart.
 *
 * Refuses to draw when there is nothing real to draw (spec section 4,
 * Forecasting: "never draw a flat/fabricated line"). A line sitting at zero
 * across seven days is indistinguishable from a genuine prediction of no
 * sales, so instead of drawing it we hand back the caller's gap message.
 *
 * @param {Object} opts
 * @param {string[]} opts.labels
 * @param {(number|null)[]} opts.data
 * @param {string} [opts.color]
 * @param {number} [opts.height]
 * @param {{title:string, description:string, icon?:string}} [opts.insufficient]
 *        when supplied, rendered instead of the chart
 * @param {{label:string, data:(number|null)[], color:string, dashed?:boolean}[]} [opts.extraSeries]
 */
export function LineChart({ labels, data, color, height = 130, insufficient, extraSeries = [] }) {
  const hasReal = Array.isArray(data) && data.some((v) => v !== null && v !== undefined);
  if (insufficient || !hasReal) {
    return EmptyState(insufficient || {
      icon: '◔',
      title: 'Not enough sales history yet',
      description: 'A forecast line here would be invented, not measured — so nothing is drawn until real history exists.',
      tight: true,
    });
  }

  const stroke = color || palette.amber;
  const showLegend = extraSeries.length > 0;

  return chartBox(height, () => ({
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Forecast',
          data,
          borderColor: stroke,
          backgroundColor: stroke + '22',
          fill: true,
          tension: 0.35,
          pointRadius: 3,
          pointBackgroundColor: stroke,
          borderWidth: 2,
          spanGaps: false, // a gap in history stays a visible gap, not a bridge
        },
        ...extraSeries.map((s) => ({
          label: s.label,
          data: s.data,
          borderColor: s.color,
          backgroundColor: 'transparent',
          borderDash: s.dashed ? [4, 4] : undefined,
          fill: false,
          tension: 0.35,
          pointRadius: 2,
          borderWidth: 2,
          spanGaps: false,
        })),
      ],
    },
    options: cartesianOptions({
      plugins: showLegend
        ? { legend: { display: true, position: 'bottom', labels: { boxWidth: 8, font: { size: 10.5 }, padding: 12 } } }
        : {},
    }),
  }));
}
