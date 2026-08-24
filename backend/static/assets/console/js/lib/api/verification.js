import { api } from './client.js';

/**
 * Verification queue.
 *
 * ROUTE MAP:
 *   GET  /verification-queue?priority= -> GET /console/queues/verification
 *                                          (returns all; filtered by priority
 *                                           in the route — one call, no
 *                                           refetch per tab)
 *   POST /verification/:id/confirm     -> POST /console/queues/verification/resolve
 *                                          { action: 'confirm' }
 *   POST /verification/:id/change      -> same endpoint, { action: 'change',
 *                                          new_value }
 *   history                            -> GET /console/queues/verification/history
 *
 * Spec section 6 point 6: a change is additive. The backend records the
 * decision and only future deductions use the new value — no historical POS
 * transaction is rewritten. Nothing in the UI offers to "correct the past",
 * because the backend has no such operation.
 */

/** @returns {Promise<{items:import('../types.js').VerificationItem[], byPriority:object, count:number}>} */
export async function getQueue() {
  const out = await api.get('/console/queues/verification');
  return {
    items: (out.items || []).map((r) => ({
      id: `${r.field}:${r.row_id}`,
      field: r.field,
      rowId: r.row_id,
      entityId: r.entity_id ?? null,
      entityName: r.entity_name,
      priority: r.priority,
      priorityLabel: r.priority_label,
      groupLabel: r.group_label,
      fieldLabel: r.field_label,
      question: r.question,
      // current_value may legitimately be absent — that is precisely why the
      // item is in this queue. It renders as "nothing set yet", not as 0.
      // The view writes the literal string '(none)' for a batch with no
      // expiry recorded; that is an absence, so it becomes null here rather
      // than being displayed as if it were a value.
      currentValue: r.current_value === '(none)' ? null : (r.current_value ?? null),
      // 'packs -> g', 'per bottle', 'received 2026-05-23' — the view's own
      // context string for what the number means.
      context: r.context ?? null,
      whyItMatters: r.why_it_matters || null,
      whatToCheck: r.what_to_check || null,
      // Only meaningful for field==='fefo_batch.expires_at': the recorded
      // date needing confirmation is ALSO already in the past. Two
      // different questions ("is this date right" vs. "this batch is
      // already expired") the queue used to conflate into one row.
      alreadyExpired: !!r.already_expired,
    })),
    byPriority: out.by_priority || {},
    byField: out.by_field || {},
    count: out.count || 0,
  };
}

/** @returns {Promise<VerificationHistoryEvent[]>} */
export async function getHistory(field, rowId) {
  const out = await api.get('/console/queues/verification/history', { field, row_id: rowId });
  return (out.events || []).map((e) => ({
    action: e.action,
    previousValue: e.previous_value,
    newValue: e.new_value,
    note: e.note || null,
    userId: e.user_id || null,
    timestamp: e.created_at,
  }));
}

/** Confirm the current estimate as correct. */
export function confirm(field, rowId, note) {
  return api.post('/console/queues/verification/resolve', {
    field, row_id: rowId, action: 'confirm', note: note || null,
  });
}

/** Replace the value. Appends to history; never overwrites the trail. */
export function change(field, rowId, newValue, note) {
  return api.post('/console/queues/verification/resolve', {
    field, row_id: rowId, action: 'change', new_value: newValue, note: note || null,
  });
}

/** Leave it as an unconfirmed estimate, on the record as a deliberate choice. */
export function keepEstimate(field, rowId, note) {
  return api.post('/console/queues/verification/resolve', {
    field, row_id: rowId, action: 'keep_estimate', note: note || null,
  });
}
