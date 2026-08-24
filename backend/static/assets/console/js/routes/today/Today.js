import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { RailCard } from '../../components/ui/RailCard.js';
import { Stat } from '../../components/ui/Stat.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { AlertRow } from '../../components/domain/AlertRow.js';
import { LineChart } from '../../components/charts/LineChart.js';
import { count } from '../../lib/format.js';
import { getSummary } from '../../lib/api/dashboard.js';

/**
 * Today — spec section 6 point 1: renders without its own scroll on a
 * 1366x768 viewport.
 *
 * How that is held: `.today-grid` is a fixed-height CSS grid sized to
 * calc(100vh - chrome), with five KPI tiles across the top and three panels
 * below. The panels scroll INTERNALLY (.scrollpane); the page does not. A
 * sixth KPI cannot be appended — the grid has five columns and adding one
 * would shrink all five below legibility, which is the mechanical version of
 * the rule "a new KPI replaces one of the five, it does not get appended".
 *
 * Data comes from ONE aggregation call (lib/api/dashboard.js). This route
 * deliberately does not fan out to six endpoints and merge — see spec
 * section 4, Today.
 */
export async function render(ctx) {
  const s = await getSummary();

  const grid = h('div', { class: 'today-grid' + (s.isSample ? ' has-banner' : '') },
    kpiStrip(s, ctx),
    alertsPanel(s, ctx),
    forecastPanel(s),
    expiryPanel(s, ctx),
    verifyPanel(s, ctx));

  if (!s.isSample) return grid;

  // The stub is in use. Say so loudly and permanently — sample figures read
  // as real numbers is the exact failure this rebuild exists to remove.
  return h('div', {},
    h('div', { class: 'callout callout-warn', style: { marginBottom: '14px' } },
      h('p', {},
        h('strong', {}, 'Sample figures — not your real numbers. '),
        'Today needs one aggregation endpoint, ',
        h('span', { class: 'mono' }, 'GET /dashboard/summary'),
        ', which this backend does not serve yet. The layout below is wired and will show real data the moment that endpoint exists; nothing here has been read from your database.')),
    grid);
}

/** The five KPI tiles. Order is fixed and meaningful: soonest-to-hurt first. */
function kpiStrip(s, ctx) {
  return h('div', { class: 'g-kpis' }, ...s.kpis.map((k) => RailCard(
    { severity: k.severity, className: 'kpi-card' },
    h('div', { class: 'top' },
      Stat({
        // A KPI the backend could not compute shows the "no data" state, never
        // a zero — "0 expired batches" and "we could not check" are different
        // statements and must look different.
        value: k.display ? k.display : count(k.value, 'not checked'),
        label: k.label,
      }),
      h('div', { class: `kpi-icon kpi-icon-${k.severity}` }, k.icon)),
    h('div', { class: 'foot' }, s.isSample ? 'sample' : k.foot))));
}

function alertsPanel(s, ctx) {
  const open = s.alerts.length;
  const body = open === 0
    ? EmptyState({
      icon: '✓',
      title: 'Nothing needs you right now',
      description: 'No expired stock, no blocked menu items, and no flagged losses in the current window.',
      tight: true,
    })
    : h('div', { class: 'scrollpane' }, ...s.alerts.map((a) => AlertRow({
      severity: a.severity,
      headline: a.headline,
      detail: a.detail,
      onClick: a.route ? () => ctx.navigate(a.route) : undefined,
    })));

  return Card({ className: 'g-alerts' },
    CardHeader({
      title: 'Action Required',
      desc: 'Ranked by what breaks first if ignored',
      right: open ? Pill(worstOf(s.alerts), `${open} open`) : Pill('good', 'all clear'),
    }),
    body);
}

/** The most severe severity present, for the header pill. */
function worstOf(alerts) {
  const order = ['crit', 'warn', 'info', 'good', 'flat'];
  return order.find((sev) => alerts.some((a) => a.severity === sev)) || 'flat';
}

function forecastPanel(s) {
  const f = s.forecastPreview || {};
  return Card({ className: 'g-forecast' },
    CardHeader({
      title: 'Demand Forecast',
      desc: 'Prophet · next 7 days, top movers',
      right: f.insufficientData
        ? Pill('flat', 'building history')
        : Pill('good', 'forecasting'),
    }),
    LineChart({
      labels: f.labels || [],
      data: f.values || [],
      height: 110,
      // Below the history threshold the chart is not drawn at all. A flat line
      // at zero is indistinguishable from a real prediction of no sales.
      insufficient: f.insufficientData ? {
        icon: '◔',
        title: `Needs ${f.requiredDays || 7}+ days of sales history`,
        description: f.note || `The closest menu item has ${f.daysOfHistory || 0} days on file. This is a data gap, not an error.`,
        tight: true,
      } : null,
    }));
}

function expiryPanel(s, ctx) {
  const rows = s.expiryPreview || [];
  return Card({ className: 'g-expiry' },
    CardHeader({
      title: 'Use First',
      desc: 'Oldest batches, soonest expiry',
      right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('expiry') }, 'View all →'),
    }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'Nothing turning soon', description: 'No open batch is inside the expiry window.', tight: true })
      : h('div', { class: 'scrollpane' }, ...rows.map((r) => h('div', { class: 'mini-row' },
        h('span', {}, r.name, r.qty ? h('div', { class: 'sub' }, r.qty) : null),
        Pill(r.severity, r.label)))));
}

function verifyPanel(s, ctx) {
  const rows = s.verifyPreview || [];
  return Card({ className: 'g-verify' },
    CardHeader({
      title: 'Needs Confirming',
      desc: 'Affects stock deductions',
      right: Button({ variant: 'ghost', size: 'sm', onClick: () => ctx.navigate('verify') }, 'Review →'),
    }),
    rows.length === 0
      ? EmptyState({ icon: '✓', title: 'Every value is confirmed', description: 'No placeholder numbers are left in setup.', tight: true })
      // Flat pills here, matching the prototype: on Today these are a queue to
      // work through, not four items each shouting a different severity. The
      // priority-coloured treatment lives on the Verification page itself.
      : h('div', { class: 'scrollpane' }, ...rows.map((r) => h('div', { class: 'mini-row' },
        h('span', {}, r.label),
        Pill('flat', `Priority ${r.priority}`)))));
}
