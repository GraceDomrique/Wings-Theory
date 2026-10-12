import { api } from './client.js';
import { severityFromDepletionStatus, severityFromAnomaly } from '../severity.js';

/**
 * Forecast & Insights domain — forecasting, replenishment, anomalies,
 * reconciliation.
 *
 * ROUTE MAP:
 *   GET /forecast?menuItemId=   -> GET /console/forecasts/demand?menu_item_pk=
 *   GET /replenishment          -> GET /console/forecasts/ingredients
 *   GET /anomalies?window=90d   -> GET /console/anomalies?lookback_days=90
 *   GET /reconciliation/:date   -> GET /unli/reconciliation/pending?service_date=
 *                                  + GET /unli/coefficients
 *   POST /reconciliation/:date/submit -> POST /unli/reconciliation
 *
 * Nothing in this file computes a forecast, a runway or a flag. Prophet,
 * the depletion arithmetic and the Isolation Forest all run server-side; these
 * adapters only rename fields.
 */

/** Menu items with enough history to forecast, plus those that don't. */
export async function listDemandSummaries() {
  const out = await api.get('/console/forecasts/demand', { horizon_days: 7 });
  return (out.items || []).map(toForecastSeries);
}

/** @returns {Promise<import('../types.js').ForecastSeries>}
 * lookbackDays is optional (server default 60, single-item requests are
 * always widened server-side to at least 90 — see /console/forecasts/demand)
 * — the caller passes it explicitly to show more than the default window,
 * up to the server's 365-day ceiling (a full year, since that's as much
 * real order history as this system tracks in one place). */
export async function getForecast(menuItemPk, horizonDays = 7, lookbackDays) {
  const params = { menu_item_pk: menuItemPk, horizon_days: horizonDays };
  if (lookbackDays) params.lookback_days = lookbackDays;
  const out = await api.get('/console/forecasts/demand', params);
  return toForecastSeries(out);
}

function toForecastSeries(r) {
  return {
    menuItemId: r.menu_item_id,
    name: r.name,
    // active_days is real days of sales on file. Below required_days the
    // backend refuses to forecast, and so does the chart — spec section 4.
    daysOfHistory: r.active_days ?? 0,
    requiredDays: r.required_days ?? 7,
    insufficientData: !!r.insufficient_data,
    method: r.method,
    prophetUnavailable: !!r.prophet_unavailable,
    history: (r.series || []).map((p) => ({ date: p.date, qty: p.qty })),
    series: (r.forecast || []).map((p) => ({
      date: p.date,
      predictedUnits: p.qty ?? null,
      low: p.low ?? null,
      high: p.high ?? null,
    })),
    trailingAvgDaily: r.trailing_avg_daily ?? null,
    totalQtyInWindow: r.total_qty_in_window ?? null,
    accuracy: null, // holdout metrics live on /console/model-details
  };
}

/**
 * Replenishment — the actual purchase-recommendation table. Reads
 * GET /console/forecasts/ingredients, wt/depletion.py's forward chain
 * (menu demand -> recipe -> projected consumption -> stock trajectory ->
 * depletion date -> suggested order quantity), NOT
 * wt/forecasting.ingredient_depletion()'s trailing-ledger-rate runway.
 *
 * That backward function used to carry its own independent purchase-quantity
 * calculation and this adapter read it — a second reorder engine, reachable
 * from the one screen an owner actually uses to decide what to buy, that
 * could disagree with wt/depletion.py's (it ignored yield_factor in the pack
 * conversion and had no seasonality or today's-pace signal). It no longer
 * computes one; this is the only source of suggestedOrderQty now, which is
 * also what the Today dashboard's "below reorder point" KPI counts against —
 * see wt/api.py's dashboard_summary(), which reads this exact function with
 * the same cache key so the two screens cannot disagree.
 *
 * `reorderPoint` stays null when the manager has not set one — the backend
 * is explicit that an unset reorder point is never treated as zero, and
 * neither is this. `suggestedReorderPoint` is advice, shown as such.
 *
 * `demandModel`/`prophetDrivenIngredients` are passed through from the same
 * raw response (getIngredientForecast() below reads them too, from the same
 * endpoint) so a caller — e.g. the manager Forecast Status KPI — can tell
 * whether the forecast behind this table is Prophet-driven or still
 * falling back to a trend average, without a second network call.
 * @returns {Promise<{rows:import('../types.js').ReplenishmentRow[], belowReorderPoint:number, configured:number, demandModel:string, prophetDrivenIngredients:number}>}
 */
export async function getReplenishment() {
  const out = await api.get('/console/forecasts/ingredients');
  return {
    rows: (out.ingredients || []).map((d) => ({
      ingredientId: d.ingredientId,
      name: d.name,
      // Real passthrough -- wt/depletion.py's ingredient_forecast() row
      // always carries this; just not previously surfaced here. null when
      // the ingredient genuinely has no category recorded, never guessed.
      category: d.category || null,
      onHand: d.available ?? null,
      baseUnit: d.baseUnit,
      usedPerDay: d.forecastDailyConsumption > 0 ? d.forecastDailyConsumption : null,
      daysLeft: d.daysOfSupply ?? null,
      leadTimeDays: d.leadTimeDays ?? null,
      reorderPoint: d.reorderPoint ?? null,
      suggestedReorderPoint: d.suggestedReorderPoint ?? null,
      suggestedOrderQty: d.suggestedOrderQty ?? null,
      suggestedOrderUnit: d.suggestedOrderUnit ?? null,
      // Additive for the Purchasing page (2026-09-30): the same
      // ingredient_forecast() row already carries this -- REVIEW_CYCLE_DAYS
      // of forecast demand held as a buffer beyond lead time -- it just
      // wasn't surfaced to any caller yet. Not recomputed here.
      safetyStockBase: d.safetyStockBase ?? null,
      belowReorderPoint: !!d.belowReorderPoint,
      status: severityFromDepletionStatus(d.status),
      risk: d.status,
      supplier: d.supplier || null,
      purchaseUnit: d.purchaseUnit || null,
      packSize: d.packSize ?? null,
      // Supplier-imposed floor on a single reorder, in suggestedOrderUnit --
      // see wt/depletion.py's ingredient_forecast(). suggestedOrderQty is
      // already raised to this floor server-side; shown here only so the
      // owner can see why a recommendation is larger than the raw
      // pack-rounded shortfall.
      minimumOrderQty: d.minimumOrderQty ?? null,
      // Already on a confirmed (placed) purchase order and not yet fully
      // received -- see wt/depletion.py's _incoming_ordered(). Already netted
      // out of suggestedOrderQty server-side; shown here only so the owner
      // can see WHY a recommendation is smaller than the raw shortfall.
      incomingOrderedBase: d.incomingOrderedBase ?? 0,
      // Plain-language depletion/days-of-supply warning computed server-side
      // (wt/depletion.py's ingredient_forecast) from the same fields above --
      // not a separate frontend estimate.
      replenishmentMessage: d.replenishmentMessage ?? null,
    })),
    belowReorderPoint: out.belowReorderPoint || 0,
    configured: out.reorderPointsConfigured || 0,
    demandModel: out.demandModel || null,
    prophetDrivenIngredients: out.prophetDrivenIngredients || 0,
  };
}

/**
 * Anomalies. Two independent passes, reported separately — merging them would
 * imply a single ranking that neither model produces.
 *
 * `flaggedBecause` is the backend's own sentence (wt/anomaly.py builds it as
 * "N.Nx this ingredient's typical wastage"). It is displayed verbatim and
 * never rewritten into something accusatory — spec section 6 point 5.
 */
export async function getAnomalies(lookbackDays = 90, ingredientPk = null) {
  // `ingredient_id` narrows the result server-side. It is applied AFTER
  // detection (see api.py), so the models still score against the whole
  // pooled population — the `why` sentence is a comparison against every
  // recent loss, and refitting on one ingredient's handful of rows would
  // silently change what that sentence means. Without it, the ingredient
  // drawer downloaded all 183 flags in the system to display the handful
  // belonging to the ingredient on screen.
  const out = await api.get('/console/anomalies', {
    lookback_days: lookbackDays, ingredient_id: ingredientPk,
  });
  return {
    stockLoss: (out.stock_loss_anomalies || []).map((a) => ({
      id: String(a.ledger_id),
      ingredientId: a.ingredient_id,
      ingredientName: a.ingredient_name,
      timestamp: a.created_at,
      amount: a.delta_base ?? null,
      baseUnit: a.base_unit,
      reasonCode: a.wastage_reason || a.reason,
      // Coarse ledger reason (wastage / spoilage / count_adjustment) kept
      // separate from reasonCode above -- reasonCode prefers the free-text
      // sub-reason when one exists, which is too granular to group/filter
      // by. `category` is always one of the three ledger reasons, so the
      // Losses & Stock Issues screen can offer a stable category filter.
      category: a.reason,
      flaggedBecause: a.why,
      priority: a.severity === 'high' ? 'review_soon' : 'worth_a_look',
      severity: severityFromAnomaly(a.severity),
      score: a.score ?? null,
      note: a.note || null,
      reviewStatus: a.review_status || null,
    })),
    consumption: out.consumption_anomalies || [],
    // The raw lists too: the ingredient drawer leads with the model's own
    // `why` sentence and needs the untouched rows, not the remapped ones.
    stock_loss_anomalies: out.stock_loss_anomalies || [],
    consumption_anomalies: out.consumption_anomalies || [],
  };
}

/**
 * Record a human reviewer's classification of one flagged stock-loss
 * ledger row (POST /console/anomalies/{ledger_id}/review, `inventory.write`
 * -- admin only). This is the ground truth wt/anomaly.py's
 * validation_metrics() computes Precision/Recall/F1 from; a flag alone is
 * never itself confirmed_anomaly. `status` is one of 'confirmed_anomaly'
 * or 'false_positive' from this screen (the fuller flagged/under_review/
 * resolved lifecycle exists server-side but isn't exposed as separate
 * buttons here -- a two-way call is what a reviewer actually makes).
 */
export function reviewAnomaly(ledgerId, status, note) {
  return api.post(`/console/anomalies/${ledgerId}/review`, {
    status, note: note || null, client_request_id: `anomaly-review-${ledgerId}-${Date.now()}`,
  });
}

// ===========================================================================
// Review-sampling / scientific validation (2026-10) — see wt/anomaly.py's
// sample_for_review()/validation_queue()/submit_validation_label()/
// scientific_validation_metrics(). Separate from reviewAnomaly() above,
// which only ever reviews a row the model's LIVE output currently flags —
// this is what makes False Negatives and True Negatives measurable.
// ===========================================================================

/** POST /console/anomalies/sample — pull new candidates into the blind
 * review queue. Never writes to the ledger or any inventory quantity. */
export function sampleAnomaliesForReview({ nFlagged, nNearThreshold = 5, nRandom = 5, lookbackDays = 90 } = {}) {
  return api.post('/console/anomalies/sample', {
    n_flagged: nFlagged ?? null, n_near_threshold: nNearThreshold,
    n_random: nRandom, lookback_days: lookbackDays,
  });
}

/**
 * GET /console/anomalies/validation-queue — the blind labeling queue.
 * blind is always true here on purpose: this adapter backs the labeling
 * screen, which must never see sample_source/model_prediction/score/
 * threshold/version. A separate technical/audit read (not wired to any UI
 * yet) would pass blind=false server-side if one is ever built.
 */
export async function getValidationQueue(status = 'pending') {
  const out = await api.get('/console/anomalies/validation-queue', { status, blind: true });
  return (out.items || []).map((r) => ({
    sampleId: r.sample_id,
    ledgerId: r.ledger_id,
    ingredientName: r.ingredient_name,
    baseUnit: r.base_unit,
    reason: r.reason,
    wastageReason: r.wastage_reason,
    deltaBase: r.delta_base,
    balanceBeforeBase: r.balance_before_base,
    balanceAfterBase: r.balance_after_base,
    note: r.note,
    eventAt: r.event_at,
    humanLabel: r.human_label,
  }));
}

/** POST /console/anomalies/validation-queue/{id}/label — one human verdict,
 * asked as an operational question, never as "is this a false positive". */
export function submitValidationLabel(sampleId, humanLabel, note) {
  return api.post(`/console/anomalies/validation-queue/${sampleId}/label`, {
    human_label: humanLabel, note: note || null,
  });
}

/** GET /console/anomalies/validation-metrics — version-scoped Precision/
 * Recall/Specificity/F1, or `available:false` below minimum reviewed
 * evidence. See wt/anomaly.py's scientific_validation_metrics(). */
export function getScientificValidationMetrics() {
  return api.get('/console/anomalies/validation-metrics');
}

// ===========================================================================
// Same review-sampling / scientific validation shape, for the Unlimited-
// Wings consumption pass — a separate, parallel mechanism over reconciled
// service-date x stream days. See wt/anomaly.py's consumption-pass
// functions; the four adapters below mirror the stock-loss ones exactly,
// just against the /consumption/ endpoint family and the pass's own human
// label ('expected_or_explained', not 'expected_or_normal').
// ===========================================================================

export function sampleConsumptionAnomaliesForReview({ nFlagged, nNearThreshold = 5, nRandom = 5, lookbackDays = 90 } = {}) {
  return api.post('/console/anomalies/consumption/sample', {
    n_flagged: nFlagged ?? null, n_near_threshold: nNearThreshold,
    n_random: nRandom, lookback_days: lookbackDays,
  });
}

export async function getConsumptionValidationQueue(status = 'pending') {
  const out = await api.get('/console/anomalies/consumption/validation-queue', { status, blind: true });
  return (out.items || []).map((r) => ({
    sampleId: r.sample_id,
    reconciliationId: r.reconciliation_id,
    serviceDate: r.service_date,
    label: r.label,
    ingredientName: r.ingredient_name,
    baseUnit: r.base_unit,
    heads: r.heads,
    theoreticalBase: r.theoretical_base,
    actualBase: r.actual_base,
    actualPerHead: r.actual_per_head,
    varianceBase: r.variance_base,
    variancePct: r.variance_pct,
    humanLabel: r.human_label,
  }));
}

export function submitConsumptionValidationLabel(sampleId, humanLabel, note) {
  return api.post(`/console/anomalies/consumption/validation-queue/${sampleId}/label`, {
    human_label: humanLabel, note: note || null,
  });
}

export function getScientificConsumptionValidationMetrics() {
  return api.get('/console/anomalies/consumption/validation-metrics');
}

/**
 * 2026-10 fix: the restaurant-friendly, grouped view of the Unlimited Wings
 * FLAVOR-LEVEL consumption pass — GET /console/anomalies/flavor/events. A
 * separate, parallel mechanism from both the stock-loss pass and the
 * stream-level consumption pass above (see wt/anomaly.py's
 * flavor_anomaly_events()): one entry per service-date event, naming the
 * primary (likely root-cause) flavour(s) and which others look like
 * secondary/compositional effects. Never merge this list with
 * consumption_anomalies above — they answer different questions over
 * different reconciliation tables.
 */
export async function getFlavorAnomalyEvents(lookbackDays = 90) {
  const out = await api.get('/console/anomalies/flavor/events', { lookback_days: lookbackDays });
  return (out.events || []).map((e) => ({
    serviceDate: e.service_date,
    primaryFlavors: e.primary_flavors || [],
    secondaryFlavors: e.secondary_flavors || [],
    totalFlagged: e.total_flagged,
    why: e.why,
    rows: e.rows || [],
  }));
}

/** Reconciliation: today's streams plus the coefficients that size them. */
export async function getReconciliation(serviceDate) {
  const [pending, coeff] = await Promise.all([
    api.get('/unli/reconciliation/pending', { service_date: serviceDate }),
    api.get('/unli/coefficients'),
  ]);
  return {
    pending,
    /** @type {import('../types.js').ReconciliationStream[]} */
    streams: (coeff.streams || []).map((s) => {
      const rolling = s.rolling_lambda ?? null;
      const baseline = s.baseline_lambda ?? null;
      return {
        stream: s.label,
        pk: s.id,
        itemKey: s.item_key,
        deductsFrom: s.ingredient_name,
        baseUnit: s.base_unit,
        coefficient: rolling,
        baselineCoefficient: baseline,
        // Drift is only meaningful when both numbers exist. No baseline means
        // no drift to report — not 0% drift.
        driftPct: rolling !== null && baseline ? ((rolling - baseline) / baseline) * 100 : null,
        windowDays: s.window_days ?? null,
        revisions: s.rolling_revisions ?? 0,
      };
    }),
  };
}

/** POST /unli/reconciliation — stage 2+3, posts only the difference to stock. */
export function submitReconciliation({ serviceDate, itemKey, openingBase, closingBase, addedBase, note }) {
  return api.post('/unli/reconciliation', {
    service_date: serviceDate,
    item_key: itemKey,
    opening_base: openingBase,
    closing_base: closingBase,
    added_base: addedBase || 0,
    note: note || null,
  });
}

/**
 * GET /unli/reconciliation — full history, most recent first. This is what
 * the "Waiting for sign-off" list and the daily-history table are built from;
 * `reconciliation_id` (not `id`) is the view's own PK column and is the value
 * `reviewReconciliation` below expects.
 */
export async function getReconciliationHistory({ limit = 120, itemKey, days } = {}) {
  const out = await api.get('/unli/reconciliation', { limit, item_key: itemKey, days });
  return (out.reconciliations || []).map((h) => ({
    id: h.reconciliation_id,
    serviceDate: h.service_date,
    stream: h.label,
    itemKey: h.item_key,
    baseUnit: h.base_unit,
    heads: h.heads,
    openingBase: h.opening_base,
    addedBase: h.added_base,
    closingBase: h.closing_base,
    actualBase: h.actual_base,
    theoreticalBase: h.theoretical_base,
    varianceBase: h.variance_base,
    variancePct: h.variance_pct,
    actualPerHead: h.actual_per_head ?? null,
    status: h.status,
    correctionBase: h.correction_base,
    validationState: h.validation_state,
    note: h.note || null,
  }));
}

/**
 * POST /unli/reconciliation/{pk}/review — manager sign-off (or rejection) of
 * a day that landed outside tolerance. Only governs whether the day may
 * influence a future coefficient — the inventory correction was already
 * posted when the measurement was recorded, so rejecting does NOT undo any
 * stock change. Requires unli.approve (manager or admin).
 */
export function reviewReconciliation(pk, approve, note) {
  return api.post(`/unli/reconciliation/${pk}/review`, { approve, note: note || null });
}

/**
 * 2026-10-14 fix: POST /unli/reconciliation/{pk}/revise/preview — a
 * read-only dry run, so staff can see the freshly recomputed head count
 * (late orders become visible here, before anything is written) and the
 * exact incremental correction a submission would post, before deciding to
 * submit one. Writes nothing. Same capability as the real submission
 * (unli.reconcile.write) since it's part of the same workflow. See
 * wt/unli.preview_revision().
 */
export async function previewReconciliationRevision(pk, { openingBase, closingBase, addedBase = 0 } = {}) {
  const out = await api.post(`/unli/reconciliation/${pk}/revise/preview`, {
    opening_base: openingBase, closing_base: closingBase, added_base: addedBase,
  });
  return {
    reconciliationId: out.reconciliationId,
    serviceDate: out.serviceDate,
    label: out.label,
    baseUnit: out.baseUnit,
    headsAtOriginal: out.headsAtOriginal,
    heads: out.heads,
    lateOrdersDetected: !!out.lateOrdersDetected,
    theoreticalBase: out.theoreticalBase,
    actualBase: out.actualBase,
    varianceBase: out.varianceBase,
    variancePct: out.variancePct,
    status: out.status,
    alreadyAppliedBase: out.alreadyAppliedBase,
    incrementalCorrectionBase: out.incrementalCorrectionBase,
    cumulativeCorrectionBase: out.cumulativeCorrectionBase,
    correctionClamped: !!out.correctionClamped,
  };
}

/**
 * 2026-10 fix: POST /unli/reconciliation/{pk}/revise — brings an
 * already-reconciled service-date/stream up to date when an order for that
 * day committed after the original reconciliation ran. The original row is
 * never touched; this posts only the incremental correction beyond what
 * was already applied. Requires unli.reconcile.write (same capability as
 * submitReconciliation). See wt/unli.revise_reconciliation().
 */
export function reviseReconciliation(pk, { openingBase, closingBase, addedBase = 0, note } = {}) {
  return api.post(`/unli/reconciliation/${pk}/revise`, {
    opening_base: openingBase, closing_base: closingBase, added_base: addedBase,
    note: note || null, client_request_id: `unli-revise-${pk}-${Date.now()}`,
  });
}

/** GET /unli/reconciliation/{pk}/revisions — the append-only trail of every
 * revision recorded against one original reconciliation, oldest first. */
export async function getReconciliationRevisions(pk) {
  const out = await api.get(`/unli/reconciliation/${pk}/revisions`);
  return (out.revisions || []).map((r) => ({
    id: r.id,
    heads: r.heads,
    theoreticalBase: r.theoretical_base,
    actualBase: r.actual_base,
    varianceBase: r.variance_base,
    status: r.status,
    validationState: r.validation_state,
    correctionBase: r.correction_base,
    cumulativeCorrectionBase: r.cumulative_correction_base,
    recordedBy: r.recorded_by,
    note: r.note,
    createdAt: r.created_at,
  }));
}

/**
 * PATCH /unli/tolerance — the variance band before a day needs manager
 * sign-off. Requires unli.approve (manager or admin). Already-reconciled
 * days keep the tolerance they were judged against.
 */
export function setTolerance(toleranceRatio) {
  return api.patch('/unli/tolerance', { tolerance_ratio: toleranceRatio });
}

/**
 * POST /unli/coefficients/recalibrate — re-estimate each stream's rolling
 * lambda from its own validated, sufficiently-covered reconciliation history.
 * dry_run=true computes and returns the result without writing; the frozen
 * baseline is never touched either way. Requires unli.approve.
 */
export function recalibrate({ itemKey, dryRun = false } = {}) {
  return api.post('/unli/coefficients/recalibrate', { item_key: itemKey || null, dry_run: dryRun });
}

/**
 * GET /unli/flavors?all_flavors=true — every selectable flavor, included and
 * special. Prices are resolved server-side; the frontend never invents one.
 */
export async function listFlavors() {
  const out = await api.get('/unli/flavors', { all_flavors: true });
  return (out.flavors || []).map((f) => ({
    pk: f.id,
    name: f.display_name,
    flavorType: f.flavor_type,
    addonPrice: f.flavor_type === 'special' ? f.addon_price : null,
    addonMenuItemName: f.addon_menu_item_name || null,
    isDeductable: !!f.is_deductable,
    isActive: !!f.is_active,
  }));
}

/**
 * PATCH /unli/flavors/{pk} — make a flavor premium/included, price it, or
 * withdraw it from the POS. Requires recipe.write (manager or admin) — the
 * same capability that edits a menu price, because a special flavor's
 * add-on price is exactly that.
 */
export function setFlavor(pk, { flavorType, addonPrice, isActive }) {
  const body = { flavor_type: flavorType, is_active: isActive };
  if (flavorType === 'special') body.addon_price = addonPrice;
  return api.patch(`/unli/flavors/${pk}`, body);
}

/**
 * GET /console/reconciliation/ingredients — the general-ingredient
 * counterpart to the unli reconciliation above: recipe/wastage-driven
 * consumption vs. what physical counts actually found, per ingredient,
 * over the window. A flagged item is a variance worth a look, same
 * "review, not accusation" framing as getAnomalies — nothing here names a
 * cause.
 */
export async function getIngredientVariance(days = 30) {
  const out = await api.get('/console/reconciliation/ingredients', { days });
  return {
    days: out.days,
    since: out.since,
    flagRatio: out.flag_ratio,
    minIndependentCountDates: out.min_independent_count_dates,
    items: (out.items || []).map((i) => ({
      ingredientId: i.ingredient_id,
      name: i.name,
      baseUnit: i.base_unit,
      consumedBase: i.consumed_base,
      receivedBase: i.received_base,
      voidCreditBase: i.void_credit_base,
      countVarianceBase: i.count_variance_base,
      countEvents: i.count_events,
      hasCountData: i.has_count_data,
      varianceRatio: i.variance_ratio,
      flagged: i.flagged,
      // Repeated-direction evidence across independent physical-count
      // dates (reservation._repeated_variance_signals) — a SEPARATE fact
      // from the window-aggregate fields above. null when there isn't
      // enough independent evidence yet (fewer than minIndependentCountDates
      // distinct count dates in the window) — never fabricated as "no
      // signal = fine".
      repeatedVarianceSignal: i.repeated_variance_signal ? {
        active: !!i.repeated_variance_signal.active,
        observationCount: i.repeated_variance_signal.observation_count,
        dates: i.repeated_variance_signal.dates || [],
        // 'below' | 'above' | null (null when not active — mixed
        // directions or not enough of the recent dates individually
        // qualified). Never a cause, only a direction.
        direction: i.repeated_variance_signal.direction,
        averageVarianceRatio: i.repeated_variance_signal.average_variance_ratio,
        thresholdRatio: i.repeated_variance_signal.threshold_ratio,
        minObservations: i.repeated_variance_signal.min_observations,
      } : null,
    })),
  };
}

/**
 * GET /console/forecasts/ingredients — the ingredient-level depletion
 * forecast: menu demand run through recipes into projected daily ingredient
 * consumption and a day-by-day stock trajectory.
 *
 * Deliberately separate from getReplenishment() above, which reads
 * /console/forecasts/depletion — that endpoint is the BACKWARD runway (a
 * trailing ledger rate) and this one is the FORWARD projection. They are two
 * different questions and the console shows both; neither is recomputed here.
 *
 * The backend already returns camelCase for this route, so this adapter only
 * passes it through. Nothing below invents a number: a null depletion date
 * means the line does not cross zero inside the horizon, and it stays null.
 */
export function getIngredientForecast({ horizonDays = 21, lookbackDays = 56 } = {}) {
  return api.get('/console/forecasts/ingredients', {
    horizon_days: horizonDays, lookback_days: lookbackDays,
  });
}

/** GET /console/forecasts/ingredients/{pk} — trajectory, history, drivers, evidence. */
export function getIngredientForecastDetail(ingredientPk, { horizonDays = 21, historyDays = 30 } = {}) {
  return api.get(`/console/forecasts/ingredients/${ingredientPk}`, {
    horizon_days: horizonDays, history_days: historyDays,
  });
}

/**
 * GET /console/menu/margins — every menu price against its recipe's
 * estimated ingredient cost, computed server-side (wt/api.py's
 * menu_margins(), from the same recipe_cost() Recipes.js's own detail view
 * reads) — one margin calculation, read here, not a second one recomputed
 * in the frontend from price and cost. `costConfidence` is carried through
 * untouched: almost every ingredient cost in this database is a benchmark
 * pending supplier confirmation, and a margin computed from one is an
 * estimate, not a fact.
 *
 * The raw response mixes snake_case (menu_item table columns, spread as-is)
 * with the camelCase fields the endpoint computes itself (marginPct,
 * costConfidence, ...) — normalised to one casing here rather than leaking
 * that inconsistency to callers.
 */
export async function getMenuMargins() {
  const out = await api.get('/console/menu/margins');
  return {
    items: (out.items || []).map((r) => ({
      menuItemId: r.id,
      recipeId: r.recipe_id,
      name: r.name,
      category: r.category,
      price: r.price,
      isUnli: !!r.is_unli,
      cost: r.cost,
      marginPct: r.marginPct,
      costConfidence: r.costConfidence,
      unpricedIngredients: r.unpricedIngredients || [],
      unverifiedCostLines: r.unverifiedCostLines || 0,
      flags: r.flags || [],
    })),
    negativeMargin: out.negativeMargin || 0,
    thinMargin: out.thinMargin || 0,
    confirmedCostItems: out.confirmedCostItems || 0,
    note: out.note || null,
  };
}
