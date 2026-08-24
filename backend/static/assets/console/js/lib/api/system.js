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
 * Recent login attempts (login_event, migration 0017) and recent sensitive
 * mutations (admin_audit_log, migration 0016) — both admin-only
 * (user.manage), both append-only on the backend. "Who did what, when" for
 * the account holder who actually needs to answer that question.
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

export async function getActionAudit(limit = 50) {
  const out = await api.get('/console/audit/actions', { limit });
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
 * Capability matrix. This is a static description of wt/auth.py's CAPABILITIES
 * table, not a live read — there is no endpoint that enumerates the policy,
 * and inventing one client-side would risk showing a permission model that
 * differs from the one actually enforced. It is labelled as a reference in the
 * UI for that reason.
 *
 * TODO(backend): a GET /system/capabilities that dumps auth.CAPABILITIES would
 * let this table be live instead of transcribed.
 *
 * 2026-08 RBAC audit: this table was found stale — it still showed admin as
 * holding every capability manager has, which stopped being true the moment
 * auth.py's CAPABILITIES was redesigned for separation of duties (admin/
 * owner keeps read access, queue.resolve, unli.approve and user.manage;
 * every capability that directly writes a floor/operational record now
 * requires the operational role that actually does the work). A reference
 * table that silently drifts from the policy it describes is worse than no
 * table — re-transcribed below to match auth.py's CAPABILITIES exactly as
 * of that redesign. Update this table if CAPABILITIES changes; there is no
 * endpoint that returns the policy live (see the TODO above).
 */
export const CAPABILITY_REFERENCE = [
  { capability: 'Create orders (POS)', cashier: true, kitchen: false, manager: false, admin: false },
  { capability: 'Void an order', cashier: true, kitchen: true, manager: true, admin: false },
  { capability: 'Take an unlimited refill', cashier: true, kitchen: true, manager: true, admin: false },
  { capability: 'Weigh in a reconciliation', cashier: false, kitchen: true, manager: true, admin: false },
  { capability: 'Sign off / reject a reconciliation', cashier: false, kitchen: false, manager: true, admin: true },
  { capability: 'Set tolerance / recalibrate coefficients', cashier: false, kitchen: false, manager: true, admin: true },
  { capability: 'Receive, count, waste, use, edit, add ingredients', cashier: false, kitchen: false, manager: true, admin: false },
  { capability: 'Add or edit recipes, edit menu prices, edit flavors', cashier: false, kitchen: false, manager: true, admin: false },
  { capability: 'Resolve the Ingredient Queue', cashier: false, kitchen: false, manager: true, admin: true },
  { capability: 'Confirm / change verification items', cashier: false, kitchen: false, manager: true, admin: true },
  { capability: 'Reset another user’s password', cashier: false, kitchen: false, manager: false, admin: true },
];
