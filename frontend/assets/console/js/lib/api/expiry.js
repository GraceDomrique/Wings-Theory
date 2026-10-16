import { api } from './client.js';
import { severityFromDays } from '../severity.js';

/**
 * Expiry Watch.
 *
 * ROUTE MAP:  GET /expiry?window=90d  ->  GET /console/expiry?within_days=90
 *
 * Days-to-expiry is computed server-side on every request (never stored, so
 * never stale). The three buckets below are display grouping only, derived
 * from the number the backend already decided on.
 *
 * `expirySource` matters and is preserved: an estimated date (received_at +
 * shelf_life_days for fresh produce) is not the same claim as a printed
 * manufacturer date, and the UI says which one it is rather than presenting
 * both as fact.
 */

/** @returns {Promise<{batches:any[], buckets:{expired:any[], soon:any[], later:any[]}}>} */
export async function getExpiry(withinDays = 90) {
  const out = await api.get('/console/expiry', { within_days: withinDays });
  const batches = (out.batches || []).map((b) => ({
    batchId: b.lot_no || `#${b.id}`,
    pk: b.id,
    ingredientPk: b.ingredient_id,
    ingredientName: b.ingredient_name,
    baseUnit: b.base_unit,
    qtyRemaining: b.qty_base ?? null,
    receivedDate: b.received_at || null,
    expiryDate: b.expires_at || null,
    expirySource: b.tracking === 'exact_date' ? 'exact'
      : b.tracking === 'estimated_shelf_life' ? 'estimated' : 'none',
    daysToExpiry: b.days_to_expiry ?? null,
    expired: !!b.expired,
    storageStatus: b.storage_status || null,
    severity: severityFromDays(b.days_to_expiry),
  }));

  return {
    batches,
    buckets: {
      expired: batches.filter((b) => b.daysToExpiry !== null && b.daysToExpiry < 0),
      soon: batches.filter((b) => b.daysToExpiry !== null && b.daysToExpiry >= 0 && b.daysToExpiry <= 7),
      later: batches.filter((b) => b.daysToExpiry !== null && b.daysToExpiry > 7),
    },
  };
}
