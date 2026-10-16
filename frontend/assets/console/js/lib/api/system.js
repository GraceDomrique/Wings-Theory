import { api } from './client.js';

/**
 * System & Settings.
 *
 * ROUTE MAP:
 *   GET /system/checks        -> GET /console/readiness
 *   GET /system/model-details -> GET /console/model-details
 */

/**
 * The end-to-end checks. Each check is turned into a pass/fail line here, but
 * the counts behind them are the backend's — nothing is recomputed.
 */
export async function getChecks() {
  const r = await api.get('/console/readiness');
  const checks = [
    {
      label: 'Every menu item can be sold',
      pass: r.menu_items_blocked === 0,
      detail: `${r.menu_items_sellable} of ${r.menu_items_total}`,
      severity: r.menu_items_blocked === 0 ? 'good' : 'warn',
    },
    {
      label: 'No ingredient is blocking a menu item',
      pass: r.ingredients_blocked === 0,
      detail: r.ingredients_blocked === 0 ? 'good' : `${r.ingredients_blocked} blocking`,
      severity: r.ingredients_blocked === 0 ? 'good' : 'crit',
    },
    {
      label: 'Every recipe ingredient is matched',
      pass: r.unmapped_open === 0,
      detail: r.unmapped_open === 0 ? 'good' : `${r.unmapped_open} unmapped`,
      severity: r.unmapped_open === 0 ? 'good' : 'crit',
    },
    {
      label: 'No placeholder numbers left',
      pass: r.needs_verification === 0,
      detail: r.needs_verification === 0 ? 'good' : `${r.needs_verification} open`,
      severity: r.needs_verification === 0 ? 'good' : 'crit',
    },
  ];
  return {
    checks,
    passing: checks.filter((c) => c.pass).length,
    total: checks.length,
    productionReady: !!r.production_ready,
    raw: r,
  };
}

/**
 * Model details — the evidence-of-work view. Every model reports its own
 * availability; when Prophet has no holdout test it says why, and the UI
 * repeats that reason instead of showing an accuracy of zero.
 */
export function getModelDetails() {
  return api.get('/console/model-details');
}

/**
 * Replenishment early-warning backtest (wt/depletion.replenishment_backtest)
 * and the ML-related data-quality report (wt/data_quality.data_quality_report)
 * — both already computed by the backend for the technical panel, fetched
 * here the same way every other model-details call is: nothing recomputed
 * or relabelled on this side.
 */
export function getReplenishmentBacktest() {
  return api.get('/console/replenishment/backtest');
}

/**
 * Structural data-integrity audit (wt/data_quality.py) — a set of
 * read-only, deterministic SQL checks (duplicate orders, orphaned rows,
 * missing links) over the raw tables the ML layer reads. Each check's own
 * `examples` rows are left exactly as the backend shaped them (their
 * columns differ check to check — id/order_id for one, name/pack_size for
 * another — so there is no single field set to rename here).
 */
export async function getDataQuality() {
  const out = await api.get('/console/data-quality');
  return {
    generatedAt: out.generated_at,
    lookbackDays: out.lookback_days,
    checksRun: out.checks_run,
    checksWithFindings: out.checks_with_findings,
    checks: out.checks || [],
  };
}

/**
 * ML model registry (migration 0050) — current mode, lock state, the
 * active training run (trained_at, dataset_version, real/synthetic
 * counts, metrics, artifact_path), and real-data-readiness per model.
 * Nothing here is recomputed on this side; every field is exactly what
 * wt/model_registry.py persisted.
 */
export function getMlModels() {
  return api.get('/console/ml/models');
}

export function getMlModelHistory(modelKey) {
  return api.get(`/console/ml/models/${modelKey}/history`);
}

/** Manual "train now" — retrains under the model's CURRENT mode. */
export function trainMlModel(modelKey) {
  return api.post(`/console/ml/models/${modelKey}/train`);
}

/** Manual mode promotion — retrains under the NEXT mode, only actually
 * moves if that run passes validation. 403s for a mode_locked model. */
export function promoteMlModel(modelKey) {
  return api.post(`/console/ml/models/${modelKey}/promote`);
}

/** Runs the full daily train+promote cycle immediately, for every model. */
export function runMlCycleNow() {
  return api.post('/console/ml/cycle');
}

/**
 * Recent login attempts (login_event, migration 0017) and recent
 * attributable actions (admin_audit_log, migration 0016) — readable by
 * owner and admin (audit.read), both append-only on the backend. "Who did
 * what, where, and when" for the account holder who actually needs to
 * answer that question.
 */
export async function getLoginAudit(limit = 50) {
  const out = await api.get('/console/audit/logins', { limit });
  return (out.events || []).map((e) => ({
    username: e.username,
    role: e.role,
    success: !!e.success,
    reason: e.reason,
    at: e.created_at,
  }));
}

/** `entityType` optionally narrows to one kind of row — e.g. 'page' alone
 * for navigation history, or omitted to mix data edits with page opens. */
export async function getActionAudit(limit = 50, entityType) {
  const out = await api.get('/console/audit/actions', entityType ? { limit, entity_type: entityType } : { limit });
  return (out.events || []).map((e) => ({
    entityType: e.entity_type,
    entityId: e.entity_id,
    action: e.action,
    field: e.field,
    previousValue: e.previous_value,
    newValue: e.new_value,
    userId: e.user_id,
    note: e.note,
    at: e.created_at,
  }));
}

/**
 * POS billing configuration — service charge on/off + rate, and which of
 * the Senior/PWD/Staff discount cards a cashier may select. Admin-only to
 * write (GET /console/pos-config needs recipe.read, PATCH needs the new
 * pos_config.write capability — see wt/auth.py). The till itself never
 * calls this: it reads the effective result off GET /pos/menu instead, so
 * this screen and the checkout math can never drift apart.
 */
export function getPosConfig() {
  return api.get('/console/pos-config');
}

export function setPosConfig(body) {
  return api.patch('/console/pos-config', body);
}

/**
 * The capability matrix used to be transcribed here as CAPABILITY_REFERENCE —
 * a static, hand-maintained copy of wt/auth.py's CAPABILITIES dict, with a
 * comment on it recording that a previous transcription had already drifted
 * out of step with the policy it described, and a TODO asking for a live
 * endpoint.
 *
 * That endpoint now exists (GET /console/capabilities), so the transcription
 * is gone rather than being re-transcribed and left to drift again. See
 * getCapabilityMatrix() in lib/api/permissions.js.
 */
