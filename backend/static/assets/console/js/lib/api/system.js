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
 * Capability matrix. This is a static description of wt/auth.py's CAPABILITIES
 * table, not a live read — there is no endpoint that enumerates the policy,
 * and inventing one client-side would risk showing a permission model that
 * differs from the one actually enforced. It is labelled as a reference in the
 * UI for that reason.
 *
 * TODO(backend): a GET /system/capabilities that dumps auth.CAPABILITIES would
 * let this table be live instead of transcribed.
 *
 * Transcribed directly from wt/auth.py's CAPABILITIES table — the console's
 * own write actions (receive/count/waste/use/edit ingredient, add ingredient,
 * add recipe, edit price, resolve the Ingredient Queue, confirm verification)
 * all require inventory.write / recipe.write / queue.resolve, which this
 * backend grants to manager and admin only — never cashier or kitchen. Update
 * this table if wt/auth.py's CAPABILITIES dict changes; there is no endpoint
 * that returns the policy live (see the TODO on getModelDetails' neighbor).
 */
export const CAPABILITY_REFERENCE = [
  { capability: 'Create orders (POS)', cashier: true, kitchen: false, manager: false, admin: true },
  { capability: 'Void an order', cashier: true, kitchen: true, manager: true, admin: true },
  { capability: 'Take an unlimited refill', cashier: true, kitchen: true, manager: true, admin: true },
  { capability: 'Weigh in a reconciliation', cashier: false, kitchen: true, manager: true, admin: true },
  { capability: 'Sign off / reject a reconciliation', cashier: false, kitchen: false, manager: true, admin: true },
  { capability: 'Set tolerance / recalibrate coefficients', cashier: false, kitchen: false, manager: true, admin: true },
  { capability: 'Receive, count, waste, use, edit, add ingredients', cashier: false, kitchen: false, manager: true, admin: true },
  { capability: 'Add or edit recipes, edit menu prices, edit flavors', cashier: false, kitchen: false, manager: true, admin: true },
  { capability: 'Resolve the Ingredient Queue', cashier: false, kitchen: false, manager: true, admin: true },
  { capability: 'Confirm / change verification items', cashier: false, kitchen: false, manager: true, admin: true },
  { capability: 'Reset another user’s password', cashier: false, kitchen: false, manager: false, admin: true },
];
