/**
 * Display-only unit formatting for the console frontend.
 *
 * wt/units.py remains the sole authority for actual unit conversion
 * (base/purchase/consumption unit math, pack size, yield factor) — nothing
 * here converts a quantity between units, and nothing here should. This
 * file exists only for the handful of places the console needs to present
 * an ingredient's own free-text `purchase_unit` (e.g. "700ml bottle",
 * "sack") as a readable label, without duplicating a `|| 'unit'`-style
 * fallback at every call site.
 */

/**
 * `ingredient.purchase_unit` verbatim, trimmed, or `null` if it is missing
 * or whitespace-only — the caller decides the fallback copy (e.g.
 * `purchaseUnitLabel(i.purchaseUnit) || 'unit'`).
 *
 * @param {string|null|undefined} purchaseUnit
 * @returns {string|null}
 */
export function purchaseUnitLabel(purchaseUnit) {
  const trimmed = (purchaseUnit || '').trim();
  return trimmed || null;
}
