import { chartBox, palette } from './base.js';
import { EmptyState } from '../ui/EmptyState.js';

/**
 * Revenue mix doughnut.
 *
 * Categorical slices are the one place several colors appear side by side.
 * They still come only from the token palette — amber, info, good, warn, crit,
 * then faint — and are never generated or interpolated.
 *
 * @param {Object} opts
 * @param {string[]} opts.labels
 * @param {number[]} opts.data
 * @param {number} [opts.height]
 * @param {{title:string, description:string, icon?:string}} [opts.empty]
 */
export function DoughnutChart({ labels, data, height = 200, empty }) {
  const total = (data || []).reduce((a, b) => a + (b || 0), 0);
  if (!data || data.length === 0 || total <= 0) {
    return EmptyState(empty || {
      icon: '◍',
      title: 'No revenue in this window',
      description: 'Completed orders will break down by item here once there are some.',
      tight: true,
    });
  }

  const ring = [palette.amber, palette.info, palette.good, palette.warn, palette.crit, palette.faint];

  return chartBox(height, () => ({
    type: 'doughnut',
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: labels.map((_, i) => ring[i % ring.length]),
        borderWidth: 0,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '68%',
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 8, font: { size: 10.5 }, padding: 12 } },
      },
    },
  }));
}
