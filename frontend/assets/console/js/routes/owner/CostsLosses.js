import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { StatTile } from '../../components/ui/StatTile.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Table } from '../../components/ui/Table.js';
import { Tabbar } from '../../components/ui/Tabbar.js';
import { ModalError } from '../../components/ui/Modal.js';
import { Drawer } from '../../components/ui/Drawer.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { DoughnutChart } from '../../components/charts/DoughnutChart.js';
import { ICONS } from '../../lib/icons.js';
import {
  count, qty, delta, stamp, pct, humanise, nodata, money,
} from '../../lib/format.js';
import { getValuation } from '../../lib/api/reports.js';
import {
  getAnomalies, getIngredientVariance, getReconciliationHistory, getMenuMargins,
} from '../../lib/api/intelligence.js';
import { createInstruction } from '../../lib/api/instructions.js';

const ANOMALY_LOOKBACK_DAYS = 90;
const VARIANCE_LOOKBACK_DAYS = 30;

const ICON = {
  value: '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
  losses: '<circle cx="12" cy="12" r="9"/><path d="M12 7v10M9.5 9.5a2.5 2.5 0 012.5-1h.5a2 2 0 010 4h-1a2 2 0 000 4h.5a2.5 2.5 0 002.5-1"/>',
  alert: '<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
};

const CATEGORY_LABEL = {
  wastage: 'Wastage',
  spoilage: 'Spoilage',
  count_adjustment: 'Count adjustment',
};

/**
 * Owner > Costs & Losses — one of the 7 rebuilt Owner-console pages.
 *
 * Entirely read-only: the Owner holds no inventory.write / unli.* / anomaly
 * review capability (see Losses.js's own docstring and wt/auth.py), so this
 * page never offers confirm/dismiss/relabel/record actions — only the
 * "Ask Admin to Investigate" escalation, which creates an Instruction rather
 * than touching the underlying record. Every figure traces to a field this
 * page actually reads from getValuation()/getAnomalies()/
 * getIngredientVariance()/getReconciliationHistory()/getMenuMargins(); where
 * none of those expose a number the spec imagined (e.g. a per-ingredient
 * unit cost to value recorded losses in pesos), this shows the honest
 * quantity-based figure instead of fabricating a currency total.
 */
export async function render(ctx) {
  const [valuation, anomalies, variance, reconHistory, margins] = await Promise.all([
    getValuation(),
    getAnomalies(ANOMALY_LOOKBACK_DAYS).catch(() => ({ stockLoss: [], consumption: [], consumption_anomalies: [] })),
    getIngredientVariance(VARIANCE_LOOKBACK_DAYS).catch(() => ({ items: [] })),
    getReconciliationHistory({ limit: 10 }).catch(() => []),
    getMenuMargins().catch(() => ({ items: [], negativeMargin: 0, thinMargin: 0, confirmedCostItems: 0, note: null })),
  ]);

  const stockLoss = anomalies.stockLoss || [];
  const consumptionAnomalies = anomalies.consumption_anomalies || anomalies.consumption || [];
  const lossRows = stockLoss.filter((a) => a.category === 'wastage' || a.category === 'spoilage');
  const wastageRows = lossRows.filter((a) => a.category === 'wastage');
  const spoilageRows = lossRows.filter((a) => a.category === 'spoilage');

  const varianceItems = variance.items || [];
  const countedVariance = varianceItems.filter((i) => i.hasCountData);
  const flaggedVariance = countedVariance.filter((i) => i.flagged);

  // No API on this page exposes a per-ingredient unit cost (getValuation()
  // only returns the inventory-wide total; getMenuMargins() is per menu
  // item, not per raw ingredient), so "Recorded Wastage/Spoilage Cost" would
  // have to be fabricated. Shown honestly as quantity totals instead, grouped
  // by base unit since different ingredients don't share one.
  function sumByUnit(rows) {
    const byUnit = new Map();
    for (const r of rows) {
      if (r.amount === null || r.amount === undefined) continue;
      const u = r.baseUnit || '';
      byUnit.set(u, (byUnit.get(u) || 0) + Math.abs(r.amount));
    }
    return byUnit;
  }
  const wastageByUnit = sumByUnit(wastageRows);
  const spoilageByUnit = sumByUnit(spoilageRows);

  function unitTotalsNode(byUnit, emptyLabel) {
    if (byUnit.size === 0) return nodata(emptyLabel);
    return h('span', {}, [...byUnit.entries()].map(([u, v], i) => h('span', {}, i ? ', ' : '', qty(v, u))));
  }

  // Top 5 by recorded loss quantity, per ingredient+unit (cost data isn't
  // available to rank by value — ranking by quantity is the honest fallback,
  // labeled as such).
  const byIngredient = new Map();
  for (const r of lossRows) {
    if (r.amount === null || r.amount === undefined) continue;
    const key = r.ingredientId || r.ingredientName;
    const prev = byIngredient.get(key) || { name: r.ingredientName, unit: r.baseUnit, total: 0 };
    prev.total += Math.abs(r.amount);
    byIngredient.set(key, prev);
  }
  const topLossIngredients = [...byIngredient.values()].sort((a, b) => b.total - a.total).slice(0, 5);

  const lowestMarginItems = (margins.items || [])
    .slice()
    .sort((a, b) => {
      if (a.marginPct === null || a.marginPct === undefined) return 1;
      if (b.marginPct === null || b.marginPct === undefined) return -1;
      return a.marginPct - b.marginPct;
    })
    .slice(0, 8);

  const monthLossQty = [...wastageByUnit.values(), ...spoilageByUnit.values()].reduce((a, b) => a + b, 0);

  // 2026-10 relayout: five sections used to run down one long page end to
  // end — a reader had to scroll past Menu Cost Insights' tables to get
  // back to Unusual Usage. Same five sections, now one tab each (matching
  // the approved reference mockup), with the four headline figures staying
  // visible above the tabs regardless of which one is open.
  const TABS = [
    { id: 'inventory', icon: ICON.value, label: 'Inventory Value' },
    { id: 'losses', icon: ICON.losses, label: 'Recorded Losses' },
    { id: 'usage', icon: ICON.alert, label: 'Unusual Usage' },
    { id: 'unli', icon: ICONS.chefHat, label: 'Unlimited Wings' },
    { id: 'margins', icon: ICONS.receipt, label: 'Menu Cost Insights' },
  ];
  let activeTab = TABS.some((t) => t.id === ctx.tab) ? ctx.tab : 'inventory';
  const body = h('div');
  const tabbar = Tabbar({
    items: TABS, activeId: activeTab,
    onChange: (id) => { activeTab = id; draw(); },
  });

  function draw() {
    mount(body,
      activeTab === 'inventory' ? inventoryValueSection(valuation)
        : activeTab === 'losses' ? recordedLossesSection({
          wastageRows, spoilageRows, wastageByUnit, spoilageByUnit, unitTotalsNode,
          varianceItems, countedVariance, flaggedVariance, topLossIngredients,
        })
          : activeTab === 'usage' ? unusualUsageSection(ctx, { stockLoss, consumptionAnomalies })
            : activeTab === 'unli' ? unliSection(ctx, reconHistory)
              : menuCostSection(margins, lowestMarginItems));
  }
  draw();

  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', gap: '14px', marginBottom: '14px' } },
      StatTile({
        iconPath: ICON.value, severity: 'good',
        value: valuation.available ? money(valuation.totalValue) : null,
        missing: 'not available', label: 'Estimated Inventory Value',
      }),
      StatTile({
        iconPath: ICON.losses, severity: monthLossQty > 0 ? 'warn' : 'good',
        value: count(lossRows.length), label: `Recorded Losses (last ${ANOMALY_LOOKBACK_DAYS}d)`,
      }),
      StatTile({
        iconPath: ICON.alert, severity: stockLoss.length > 0 ? 'crit' : 'good',
        value: count(stockLoss.length), label: 'Unusual Usage Items',
      }),
      StatTile({
        iconPath: ICON.losses, severity: consumptionAnomalies.length > 0 ? 'crit' : 'good',
        value: count(consumptionAnomalies.length), label: 'Unlimited Wings Alerts',
      })),
    tabbar,
    body);
}

function inventoryValueSection(valuation) {
  const hasBreakdown = valuation.available && valuation.pricedItems !== null && valuation.pricedItems !== undefined
    && valuation.totalItems !== null && valuation.totalItems !== undefined;
  const missing = hasBreakdown ? Math.max(0, valuation.totalItems - valuation.pricedItems) : null;

  return h('div', { style: { marginBottom: '14px' } },
    Card({},
      CardHeader({ title: 'Inventory Value', desc: hasBreakdown ? 'Ingredients priced vs. missing a verified cost' : undefined }),
      hasBreakdown
        ? DoughnutChart({
          labels: ['Priced', 'Missing cost'], data: [valuation.pricedItems, missing], height: 160,
          empty: { icon: '◍', title: 'No ingredients to break down', tight: true },
        })
        : null,
      h('div', { class: 'grid', style: { gridTemplateColumns: hasBreakdown ? 'repeat(3,1fr)' : 'repeat(1,1fr)', marginTop: '12px' } },
        Stat({
          value: valuation.available ? money(valuation.totalValue) : nodata('not available'),
          label: 'Estimated Value on Hand',
        }),
        hasBreakdown ? Stat({ value: count(valuation.pricedItems), label: 'Ingredients With Verified Cost' }) : null,
        hasBreakdown ? Stat({ value: count(missing), label: 'Ingredients Missing Cost Data' }) : null),
      !valuation.available
        ? h('div', { class: 't-sm t-soft', style: { marginTop: '10px' } }, valuation.reason)
        : null));
}

function recordedLossesSection({
  wastageRows, spoilageRows, wastageByUnit, spoilageByUnit, unitTotalsNode,
  varianceItems, countedVariance, flaggedVariance, topLossIngredients,
}) {
  return h('div', { style: { marginBottom: '14px' } },
    Card({ style: { marginBottom: '14px' } },
      CardHeader({
        title: 'Recorded stock losses',
        desc: `Wastage and spoilage logged in the last ${ANOMALY_LOOKBACK_DAYS} days. No per-ingredient unit cost is available to this page, so amounts are shown as quantity, not pesos.`,
      }),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)' } },
        Card({}, Stat({ value: count(wastageRows.length), label: 'Wastage entries' })),
        Card({}, Stat({ value: unitTotalsNode(wastageByUnit, 'no wastage recorded'), label: 'Recorded Wastage Quantity' })),
        Card({}, Stat({ value: count(spoilageRows.length), label: 'Spoilage entries' })),
        Card({}, Stat({ value: unitTotalsNode(spoilageByUnit, 'no spoilage recorded'), label: 'Recorded Spoilage Quantity' })))),

    Card({ style: { marginBottom: '14px' } },
      CardHeader({
        title: 'Stock count differences',
        desc: 'Expected consumption (recipes + waste log) vs. what a physical count actually found, per ingredient — a variance worth a look, not a finding of cause.',
      }),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)', marginBottom: '12px' } },
        Card({}, Stat({ value: count(varianceItems.length), label: 'Ingredients with activity' })),
        Card({}, Stat({ value: count(countedVariance.length), label: 'Physically counted' })),
        Card({}, Stat({ value: count(flaggedVariance.length), label: 'Flagged for review' }))),
      countedVariance.length === 0
        ? EmptyState({ icon: '—', title: 'No physical counts in this window', description: 'Count variance needs at least one physical stock count to compare against.' })
        : Table({
          columns: [
            { key: 'ingredient', label: 'Ingredient', render: (i) => i.name },
            {
              key: 'variance', label: 'Count variance',
              render: (i) => h('span', { class: `mono t-sm ${i.flagged ? 't-crit' : ''}` }, delta(i.countVarianceBase, i.baseUnit)),
            },
            {
              key: 'ratio', label: '% of expected',
              render: (i) => h('span', { class: 'mono t-sm' }, pct(i.varianceRatio === null ? null : i.varianceRatio * 100, 'n/a', 0)),
            },
            { key: 'status', label: 'Status', render: (i) => (i.flagged ? Pill('warn', 'review needed') : Pill('good', 'within range')) },
          ],
          rows: countedVariance,
          empty: EmptyState({ icon: '—', title: 'No counted ingredients', description: '' }),
        })),

    Card({},
      CardHeader({ title: 'Top 5 ingredients by recorded loss quantity', desc: 'Ranked by quantity, not value — no per-ingredient unit cost is available to this page.' }),
      topLossIngredients.length === 0
        ? EmptyState({ icon: '✓', title: 'No wastage or spoilage recorded', description: `Nothing logged in the last ${ANOMALY_LOOKBACK_DAYS} days.`, tight: true })
        : h('div', {}, ...topLossIngredients.map((t) => h('div', {
          style: { display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid var(--line-soft)' },
        },
        h('span', { class: 't-sm' }, t.name),
        h('span', { class: 'mono t-sm t-crit' }, qty(t.total, t.unit)))))));
}

function unusualUsageSection(ctx, { stockLoss, consumptionAnomalies }) {
  const cardsHost = h('div');

  function drawCards() {
    mount(cardsHost, stockLoss.length === 0
      ? EmptyState({
        icon: '✓',
        title: 'Nothing flagged for review',
        description: `Waste and stock-count entries are all within each ingredient's normal range for the last ${ANOMALY_LOOKBACK_DAYS} days.`,
      })
      : h('div', {}, ...stockLoss.map((a) => h('div', {
        class: 'card', style: { marginBottom: '10px' },
      },
      h('div', { class: 'card-head' },
        h('div', {},
          h('div', { class: 'card-title' }, a.ingredientName),
          h('div', { class: 't-xs t-faint' }, h('span', { class: 'mono' }, stamp(a.timestamp)))),
        h('div', { style: { display: 'flex', gap: '6px', flexShrink: '0' } },
          a.category ? Pill('flat', CATEGORY_LABEL[a.category] || humanise(a.category)) : null,
          Pill(a.severity, a.priority === 'review_soon' ? 'review soon' : 'worth a look'))),
      h('div', { class: 't-sm mono t-crit', style: { marginBottom: '6px' } }, delta(a.amount, a.baseUnit)),
      h('div', { class: 't-sm t-soft', style: { marginBottom: '8px' } }, a.flaggedBecause),
      h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' } },
        a.reviewStatus
          ? Pill(a.reviewStatus === 'confirmed_anomaly' ? 'crit' : a.reviewStatus === 'false_positive' ? 'flat' : 'info',
            a.reviewStatus === 'confirmed_anomaly' ? 'confirmed' : a.reviewStatus === 'false_positive' ? 'false positive' : humanise(a.reviewStatus))
          : Pill('flat', 'not yet reviewed'),
        Button({ size: 'sm', onClick: () => openInvestigateModal(a) }, 'Ask Admin to Investigate'))))));
  }
  drawCards();

  function openInvestigateModal(a) {
    const title = `Investigate unusual usage — ${a.ingredientName}`;
    const titleInput = h('input', { type: 'text', value: title });
    const bodyInput = h('textarea', { rows: 4 }, '');
    bodyInput.value = `Flagged stock-loss record (id ${a.id}) for ${a.ingredientName} on ${a.timestamp || 'unknown date'}: `
      + `${delta(a.amount, a.baseUnit).textContent || ''}. Reason: ${a.flaggedBecause || 'no detail given'}.`;
    const priorityInput = h('select', {},
      h('option', { value: 'normal' }, 'Normal'),
      h('option', { value: 'important', selected: true }, 'Important'),
      h('option', { value: 'urgent' }, 'Urgent'));
    priorityInput.value = 'important';
    const err = ModalError();
    const submit = Button({ variant: 'primary' }, 'Send to Admin');
    submit.addEventListener('click', async () => {
      const t = titleInput.value.trim();
      const b = bodyInput.value.trim();
      if (!t || !b) { err.textContent = 'Title and description are both required.'; err.style.display = ''; return; }
      submit.disabled = true;
      try {
        await createInstruction({ title: t, body: b, priority: priorityInput.value });
        modal.close();
        toast('Sent to Admin.', 'good');
      } catch (e) { err.textContent = e.message; err.style.display = ''; } finally { submit.disabled = false; }
    });
    const modal = Drawer({ title: 'Ask Admin to Investigate', onClose: () => {} },
      h('label', { class: 'field' }, 'Title', titleInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Description', bodyInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Priority', priorityInput),
      err,
      h('div', { style: { marginTop: '14px', display: 'flex', justifyContent: 'flex-end' } }, submit));
  }

  return h('div', { style: { marginBottom: '14px' } },
    Card({ style: { marginBottom: '14px' } },
      CardHeader({
        title: 'Unusual ingredient usage — stock loss',
        desc: 'Waste and count entries larger than usual for that ingredient, flagged against its own history. A flag worth a look, not proof anything went wrong. You can only escalate this to Admin from here — review/confirm is an Admin action.',
      }),
      cardsHost),
    Card({},
      CardHeader({
        title: 'Unusual ingredient usage — Unlimited Wings consumption',
        desc: 'A separate model over reconciled Unlimited Wings service-days. Kept apart from the stock-loss list above — the two answer different questions and are never combined into one ranking.',
      }),
      consumptionAnomalies.length === 0
        ? EmptyState({ icon: '✓', title: 'Nothing flagged for review', description: 'Reconciled days are all within each stream\'s normal pattern, or not enough real reconciled data exists yet to flag against.', tight: true })
        : Table({
          columns: [
            { key: 'date', label: 'Service date', render: (c) => h('span', { class: 'mono t-xs' }, c.service_date) },
            { key: 'stream', label: 'Stream', render: (c) => c.label || c.ingredient_name || '—' },
            { key: 'heads', label: 'Covers', render: (c) => count(c.heads) },
            { key: 'variance', label: 'Variance vs. provisional', render: (c) => h('span', { class: 'mono t-sm t-crit' }, pct(c.variance_pct, 'n/a', 1)) },
            { key: 'why', label: 'Why flagged', render: (c) => h('span', { class: 't-sm t-soft' }, c.why || '—') },
          ],
          rows: consumptionAnomalies,
          empty: EmptyState({ icon: '✓', title: 'Nothing flagged', description: '' }),
        })));
}

function unliSection(ctx, history) {
  const rows = Array.isArray(history) ? history : (history.items || []);
  return Card({ style: { marginBottom: '14px' } },
    CardHeader({
      title: 'Unlimited Wings',
      desc: 'Most recent reconciled sessions — estimated consumption vs. what the kitchen actually weighed out.',
      right: Button({ size: 'sm', onClick: () => ctx.navigate('unlisummary') }, 'View full history'),
    }),
    rows.length === 0
      ? EmptyState({ icon: '—', title: 'No reconciled sessions yet', description: 'Sessions appear here once Admin signs one off.', tight: true })
      : Table({
        columns: [
          { key: 'when', label: 'Date', render: (r) => h('span', { class: 'mono t-xs' }, stamp(r.serviceDate)) },
          { key: 'stream', label: 'Stream', render: (r) => r.stream || '—' },
          { key: 'covers', label: 'Covers Served', render: (r) => count(r.heads) },
          { key: 'estimated', label: 'Estimated', render: (r) => qty(r.theoreticalBase, r.baseUnit, '—') },
          { key: 'actual', label: 'Actual (Served)', render: (r) => qty(r.actualBase, r.baseUnit, '—') },
          { key: 'variance', label: 'Variance %', render: (r) => pct(r.variancePct, 'n/a', 1) },
          { key: 'status', label: 'Status', render: (r) => r.status || '—' },
        ],
        rows,
        empty: EmptyState({ icon: '—', title: 'No reconciled sessions yet', description: '' }),
      }));
}

function menuCostSection(margins, lowestMarginItems) {
  const warnItems = (margins.items || []).filter((m) => (m.unpricedIngredients || []).length
    || m.unverifiedCostLines || (m.flags || []).length);

  return h('div', {},
    Card({ style: { marginBottom: '14px' } },
      CardHeader({ title: 'Menu cost insights' }),
      h('div', { class: 'callout callout-info', style: { marginBottom: '14px' } },
        h('p', {}, h('strong', {}, 'Read this carefully. '),
          'These are estimated margins computed from recipe cost data of varying confidence (see each item\'s Cost confidence below) — NOT an audited or verified business profit figure. Items with unpriced ingredients or unverified cost lines are flagged explicitly rather than silently included at face value.',
          margins.note ? ` ${margins.note}` : '')),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)', marginBottom: '14px' } },
        Card({}, Stat({ value: count(margins.negativeMargin), label: 'Items with negative margin' })),
        Card({}, Stat({ value: count(margins.thinMargin), label: 'Items with thin margin' })),
        Card({}, Stat({ value: count(margins.confirmedCostItems), label: 'Items with confirmed cost' }))),

      CardHeader({ title: 'Lowest estimated margin', desc: 'Lowest marginPct first — estimated, not verified.' }),
      lowestMarginItems.length === 0
        ? EmptyState({ icon: '—', title: 'No menu margin data available', description: '', tight: true })
        : Table({
          columns: [
            { key: 'name', label: 'Item', render: (m) => m.name },
            { key: 'price', label: 'Price', render: (m) => money(m.price) },
            { key: 'cost', label: 'Est. cost', render: (m) => money(m.cost) },
            { key: 'margin', label: 'Est. margin', render: (m) => h('span', { class: `mono t-sm ${m.marginPct !== null && m.marginPct < 0 ? 't-crit' : ''}` }, pct(m.marginPct, 'n/a', 1)) },
            { key: 'confidence', label: 'Cost confidence', render: (m) => (m.costConfidence ? Pill(confidenceSeverity(m.costConfidence), humanise(m.costConfidence)) : Pill('flat', 'unknown')) },
          ],
          rows: lowestMarginItems,
          empty: EmptyState({ icon: '—', title: 'No menu margin data', description: '' }),
        })),

    Card({},
      CardHeader({ title: 'Missing cost data warnings', desc: 'Items with unpriced ingredients, unverified cost lines, or other flags on their recipe cost.' }),
      warnItems.length === 0
        ? EmptyState({ icon: '✓', title: 'No missing-cost warnings', description: 'Every menu item\'s recipe cost is fully priced and verified.', tight: true })
        : h('div', {}, ...warnItems.map((m) => h('div', {
          style: { padding: '9px 0', borderBottom: '1px solid var(--line-soft)' },
        },
        h('div', { class: 't-sm', style: { fontWeight: '600' } }, m.name),
        m.unpricedIngredients && m.unpricedIngredients.length
          ? h('div', { class: 't-xs t-soft' }, `Unpriced ingredients: ${m.unpricedIngredients.join(', ')}`)
          : null,
        m.unverifiedCostLines
          ? h('div', { class: 't-xs t-soft' }, `${count(m.unverifiedCostLines).textContent || m.unverifiedCostLines} unverified cost line(s)`)
          : null,
        (m.flags || []).length
          ? h('div', { class: 't-xs t-faint' }, m.flags.map((f) => humanise(f)).join(', '))
          : null)))));
}

function confidenceSeverity(confidence) {
  const c = String(confidence).toLowerCase();
  if (c === 'confirmed' || c === 'high') return 'good';
  if (c === 'low' || c === 'unverified' || c === 'benchmark') return 'warn';
  return 'info';
}
