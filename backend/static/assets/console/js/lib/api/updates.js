import { api } from './client.js';

/**
 * Operational updates — the free-text, bidirectional half of Owner/Admin
 * communication (instructions.js's task board only covers the other half:
 * a fixed acknowledge/start/complete state machine with a single note at
 * the end). An update is either threaded onto an instruction
 * (ownerInstructionId set) or a standalone heads-up (null).
 *
 * ROUTE MAP:
 *   GET  /console/updates[?owner_instruction_id=]   list (all, or one thread)
 *   POST /console/updates                            create (either role)
 *   POST /console/updates/{pk}/acknowledge            the OTHER role only
 */

function fromApi(r) {
  return {
    id: r.id,
    ownerInstructionId: r.owner_instruction_id,
    body: r.body,
    createdBy: r.created_by,
    createdByRole: r.created_by_role,
    createdAt: r.created_at,
    acknowledgedAt: r.acknowledged_at,
    acknowledgedBy: r.acknowledged_by,
  };
}

export async function listUpdates(ownerInstructionId) {
  const out = await api.get(
    '/console/updates',
    ownerInstructionId != null ? { owner_instruction_id: ownerInstructionId } : undefined,
  );
  return { items: (out.items || []).map(fromApi), count: out.count || 0 };
}

export async function createUpdate({ body, ownerInstructionId }) {
  return fromApi(await api.post('/console/updates', {
    body, owner_instruction_id: ownerInstructionId ?? null,
  }));
}

export async function acknowledgeUpdate(pk) {
  return fromApi(await api.post(`/console/updates/${pk}/acknowledge`));
}
