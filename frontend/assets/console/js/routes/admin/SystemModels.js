import { h } from '../../lib/dom.js';
import { Card } from '../../components/ui/Card.js';
import { Pill } from '../../components/ui/Pill.js';
import { getChecks, getModelDetails } from '../../lib/api/system.js';

/**
 * Administration > System & Models — spec section 20: forecasting/model
 * diagnostics moved here so normal restaurant operations never have to
 * interact with them, while a technical user still can. Plain-language
 * summary first (the pill: live / not available), the model's own stated
 * reason or live parameters second — never a filled-in zero standing in
 * for "not available."
 */
export async function render(ctx) {
  const [state, models] = await Promise.all([getChecks(), getModelDetails().catch(() => null)]);

  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: '2fr 1fr', marginBottom: '14px' } },
      Card({},
        h('div', { class: 'card-title', style: { marginBottom: '12px' } }, 'End-to-end readiness checks'),
        ...state.checks.map((c) => h('div', { class: 'mini-row' }, h('span', {}, c.label), Pill(c.severity, c.detail)))),
      Card({ style: { display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' } },
        h('div', { style: { fontFamily: 'var(--font-display)', fontSize: '34px', fontWeight: '700' } }, `${state.passing}/${state.total}`),
        h('div', { class: 't-sm t-soft', style: { textAlign: 'center', marginTop: '4px' } },
          state.passing === state.total ? 'checks passing — ready for service' : 'checks passing — setup still in progress'))),

    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)' } },
      modelCard('Demand forecasting', 'Prophet', models && models.demand_forecast,
        'Not available — needs more real sales history for a holdout test.'),
      modelCard('Anomaly detection', 'Isolation Forest', models && models.stock_loss_detection,
        'Retrains on every request once enough comparable entries exist.'),
      modelCard('Product combinations', 'FP-Growth', models && models.product_combinations,
        'Waiting on enough order history to find reliable pairings.')),

    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(2,1fr)', marginTop: '14px' } },
      modelCard('Unlimited Wings consumption', 'Isolation Forest', models && models.anomaly_detection,
        'Retrains on every request once enough reconciled streams exist.'),
      flavorReadinessCard(models && models.flavor_anomaly_detection)),

    combinedValidationCard(models && models.product_combinations));
}

/**
 * Two SEPARATE checks exist for a candidate pairing, and they answer
 * different questions: "holdout" is the one chronological 80/20 split
 * that actually gates what the Combos/Product-Combinations pages show
 * live; "temporal stability" is a later, independent check across three
 * rolling time windows that a pairing was never required to pass to be
 * shown live. Rendering them as one merged "validated" pill would let a
 * pair look more proven than it is — this table always shows both,
 * explicitly, never collapses them into a single verdict.
 */
function combinedValidationCard(details) {
  if (!details || !Array.isArray(details.combined_validation) || details.combined_validation.length === 0) {
    return null;
  }
  const stabilityLabel = {
    validated_stable: 'Stable across time',
    unstable_failed: 'Failed a later window',
    insufficient_evidence: 'Not enough evidence yet',
    not_evaluated: 'Not evaluated',
  };
  const stabilityTone = {
    validated_stable: 'good',
    unstable_failed: 'crit',
    insufficient_evidence: 'flat',
    not_evaluated: 'flat',
  };
  const rows = details.combined_validation.slice(0, 15);
  return h('div', { style: { marginTop: '14px' } },
    Card({},
      h('div', { class: 'card-title', style: { marginBottom: '4px' } }, 'Product combinations — two separate checks'),
      h('div', { class: 't-sm t-soft', style: { marginBottom: '10px' } },
        'Live recommendation = passed the chronological 80/20 holdout (the only check that gates what shows on Combos/Product Combinations). Temporal stability is a separate, additional check across three rolling time windows — a pairing can be live without it, or stable without being live.'),
      ...rows.map((r) => h('div', { class: 'mini-row' },
        h('span', {}, `${r.item_a} + ${r.item_b}`),
        h('span', { style: { display: 'flex', gap: '6px' } },
          Pill(r.is_live_recommendation ? 'good' : 'flat', r.is_live_recommendation ? 'live recommendation' : 'not shown live'),
          Pill(stabilityTone[r.temporal_stability_verdict] || 'flat', stabilityLabel[r.temporal_stability_verdict] || r.temporal_stability_verdict))))));
}

/**
 * Flavour-level anomaly pass. Its readiness state is the ONLY place this
 * page claims real-world accuracy for it — SIMULATION_ONLY/
 * INSUFFICIENT_REAL_DATA/READY_WITH_LIMITATIONS never render a precision/
 * recall number at all, and even READY_REAL_DATA shows the scientific
 * validation metrics, never the fixed-seed simulation F1/precision/recall
 * from the test suite. Those simulation numbers belong only in engineering
 * reports, never on a page an Owner or Admin reads as "how well does this
 * work on our restaurant".
 */
function flavorReadinessCard(detail) {
  const readinessLabel = {
    SIMULATION_ONLY: 'Simulation only — no real data yet',
    INSUFFICIENT_REAL_DATA: 'Insufficient real data',
    READY_WITH_LIMITATIONS: 'Ready — limited human review so far',
    READY_REAL_DATA: 'Ready — validated on real data',
  };
  const readinessTone = {
    SIMULATION_ONLY: 'flat',
    INSUFFICIENT_REAL_DATA: 'flat',
    READY_WITH_LIMITATIONS: 'warn',
    READY_REAL_DATA: 'good',
  };
  const readiness = detail && detail.readiness;
  const pill = readiness
    ? Pill(readinessTone[readiness.state] || 'flat', readinessLabel[readiness.state] || readiness.state)
    : Pill('flat', 'not available');

  const note = detail
    ? (detail.available === false
      ? (detail.reason || 'Not available.')
      : (readiness ? readiness.reason : 'Per-flavour consumption anomaly detection.'))
    : 'Not available — no flavour reconciliation data yet.';

  return Card({},
    h('div', { class: 'eyebrow' }, 'Flavour anomaly detection'),
    h('div', { style: { fontWeight: '600', marginBottom: '6px', display: 'flex', gap: '8px', alignItems: 'center' } },
      'Isolation Forest (per flavour)', pill),
    h('div', { class: 't-sm t-soft' }, note),
    h('div', { class: 't-xs t-soft', style: { marginTop: '6px' } },
      'Real-world accuracy awaits genuine restaurant review — this never shows the fixed-seed simulation precision/recall from the engineering validation suite.'));
}

/** One model panel. `detail` is whatever the backend reported — its live
 * parameters when it is working, its own stated reason when it is not. */
function modelCard(eyebrow, name, detail, fallbackNote) {
  let note = fallbackNote;
  let pill = null;

  if (detail && typeof detail === 'object') {
    if (detail.available === false) {
      note = detail.reason || fallbackNote;
      pill = Pill('flat', 'not available');
    } else {
      pill = Pill('good', 'live');
      const bits = [];
      if (detail.n_estimators) bits.push(`${detail.n_estimators} estimators`);
      if (detail.min_support) bits.push(`min support ${detail.min_support}`);
      if (detail.min_lift) bits.push(`min lift ${detail.min_lift}×`);
      if (detail.records_evaluated != null) bits.push(`${detail.records_evaluated} records evaluated`);
      if (detail.transactions_evaluated != null) bits.push(`${detail.transactions_evaluated} transactions analysed`);
      if (detail.min_pooled_samples != null) bits.push(`needs ${detail.min_pooled_samples} pooled samples`);
      if (detail.mae != null) bits.push(`MAE ${detail.mae}`);
      if (detail.items_evaluated != null) {
        bits.push(`${detail.items_evaluated} of ${detail.items_considered} items backtested`);
        const prophetWape = (detail.fleet_summary || {}).prophet ? detail.fleet_summary.prophet.mean_wape : null;
        if (prophetWape != null) bits.push(`Prophet mean WAPE ${prophetWape}%`);
        const pvb = detail.prophet_vs_baseline;
        if (pvb && pvb.prophet_actually_better != null) {
          bits.push(pvb.prophet_actually_better
            ? `beats best baseline by ${pvb.absolute_improvement_wape_pts} pts`
            : 'not measurably better than best baseline');
        }
      }
      if (bits.length) note = bits.join(' · ');
    }
  }

  return Card({},
    h('div', { class: 'eyebrow' }, eyebrow),
    h('div', { style: { fontWeight: '600', marginBottom: '6px', display: 'flex', gap: '8px', alignItems: 'center' } }, name, pill),
    h('div', { class: 't-sm t-soft' }, note));
}
