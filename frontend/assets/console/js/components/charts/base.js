import { h } from '../../lib/dom.js';

/**
 * Shared Chart.js plumbing for the three chart components.
 *
 * Chart.js is loaded from the CDN in console.html; if it did not load we say
 * so rather than leaving an empty box that reads as "no sales".
 *
 * A canvas cannot be sized until it is in the document, so charts are created
 * on the next animation frame after the route has mounted. Instances are
 * registered here and torn down by the router on navigation — Chart.js keeps
 * global references, and leaking them across route swaps is what makes charts
 * come back blank on the second visit.
 */

const live = new Set();

export function destroyAllCharts() {
  for (const c of live) { try { c.destroy(); } catch { /* already gone */ } }
  live.clear();
}

function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export const palette = {
  get amber() { return token('--amber'); },
  get info() { return token('--info'); },
  get good() { return token('--good'); },
  get crit() { return token('--crit'); },
  get warn() { return token('--warn'); },
  get faint() { return token('--ink-faint'); },
  get gridline() { return token('--line-soft'); },
  get inkSoft() { return token('--ink-soft'); },
  get cocoa() { return token('--cocoa'); },
};

let defaultsApplied = false;
function applyDefaults() {
  if (defaultsApplied || typeof Chart === 'undefined') return;
  Chart.defaults.font.family = 'Inter';
  Chart.defaults.color = palette.inkSoft;
  Chart.defaults.animation = { duration: 220 };
  defaultsApplied = true;
}

/** Axis/legend options shared by the line and bar charts. */
export function cartesianOptions(extra = {}) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false }, ...(extra.plugins || {}) },
    scales: {
      y: { grid: { color: palette.gridline }, ticks: { font: { size: 10 } }, beginAtZero: true },
      x: { grid: { display: false }, ticks: { font: { size: 10 } } },
      ...(extra.scales || {}),
    },
    ...extra.rest,
  };
}

/**
 * Build a chart inside a fixed-height box.
 * @param {number} height css pixels
 * @param {() => object} config Chart.js config, evaluated at draw time
 */
export function chartBox(height, config) {
  const canvas = h('canvas');
  const box = h('div', { class: 'chart-box', style: { height: height + 'px' } }, canvas);

  requestAnimationFrame(() => {
    if (!box.isConnected) return;
    if (typeof Chart === 'undefined') {
      box.replaceChildren(h('div', { class: 'load-note' },
        'Chart library unavailable — check this terminal’s connection.'));
      return;
    }
    applyDefaults();
    const instance = new Chart(canvas.getContext('2d'), config());
    live.add(instance);
  });

  return box;
}
