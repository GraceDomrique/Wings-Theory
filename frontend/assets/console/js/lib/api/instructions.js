import { api } from './client.js';

/**
 * Owner -> Admin instructions — a shared task board, not a chat.
 *
 * ROUTE MAP:
 *   GET  /console/instructions                          list
 *   GET  /console/instructions/assignees                 admin accounts (owner, for the Assign to dropdown)
 *   POST /console/instructions                           create (owner)
 *   POST /console/instructions/{pk}/acknowledge           admin
 *   POST /console/instructions/{pk}/start                 admin
 *   POST /console/instructions/{pk}/complete               admin (note required)
 *   POST /console/instructions/{pk}/cancel                 owner (own, 'open' only) or admin
 */

function fromApi(r) {
  return {
    id: r.id,
    title: r.title,
    body: r.body,
    priority: r.priority,
    status: r.status,
    assignedTo: r.assigned_to,
    dueDate: r.due_date,
    createdBy: r.created_by,
    createdAt: r.created_at,
    updatedAt: r.updated_at,
    acknowledgedAt: r.acknowledged_at,
    acknowledgedBy: r.acknowledged_by,
    inProgressAt: r.in_progress_at,
    completedAt: r.completed_at,
    completedBy: r.completed_by,
    completionNote: r.completion_note,
    cancelledAt: r.cancelled_at,
    cancelledBy: r.cancelled_by,
    cancelReason: r.cancel_reason,
  };
}

export async function listInstructions(status) {
  const out = await api.get('/console/instructions', status ? { status } : undefined);
  return { items: (out.items || []).map(fromApi), count: out.count || 0 };
}

/** Active admin accounts, for the owner's "Assign to" dropdown — a
 * structured pick from real accounts, never a free-text username. */
export async function listAssignees() {
  const out = await api.get('/console/instructions/assignees');
  return (out.items || []).map((u) => ({ username: u.username, displayName: u.display_name }));
}

export async function createInstruction({ title, body, priority = 'normal', assignedTo, dueDate }) {
  const out = await api.post('/console/instructions', {
    title, body, priority, assigned_to: assignedTo || null, due_date: dueDate || null,
  });
  return fromApi(out);
}

export async function acknowledgeInstruction(pk) {
  return fromApi(await api.post(`/console/instructions/${pk}/acknowledge`));
}

export async function startInstruction(pk) {
  return fromApi(await api.post(`/console/instructions/${pk}/start`));
}

export async function completeInstruction(pk, completionNote) {
  return fromApi(await api.post(`/console/instructions/${pk}/complete`, { completion_note: completionNote }));
}

export async function cancelInstruction(pk, reason) {
  return fromApi(await api.post(`/console/instructions/${pk}/cancel`, { reason: reason || null }));
}
