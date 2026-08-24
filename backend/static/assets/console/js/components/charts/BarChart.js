import { chartBox, cartesianOptions, palette } from './base.js';
import { EmptyState } from '../ui/EmptyState.js';

/**
 * Anomaly-loss and sales-by-day bar chart.
 *
 * @param {Object} opts
 * @param {string[]} opts.labels
 * @param {number[]} opts.data
 * @param {string} [opts.color]
 * @param {string[]} [opts.colors]  per-bar colors, for severity-coded bars
 * @param {number} [opts.height]
 * @param {{title:string, description:string, icon?:string}} [opts.empty]
 * @param {boolean} [opts.horizontal]
 */
export function BarChart({ labels, data, color, colors, height = 180, empty, horizontal = false }) {
  if (!data || data.length === 0) {
    return EmptyState(empty || {
      icon: '▤',
      title: 'Nothing recorded in this window',
      description: 'No entries fall inside the lookback period yet.',
      tight: true,
    });
  }

  return chartBox(height, () => ({
    type: 'bar',
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: colors || color || palette.crit,
        borderRadius: 5,
        maxBarThickness: 38,
      }],
    },
    options: cartesianOptions({ rest: horizontal ? { indexAxis: 'y' } : {} }),
  }));
}
