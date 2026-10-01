import { h } from '../../lib/dom.js';

/**
 * A record's position in a workflow - NOT a warning. Deliberately a
 * different shape (outlined square tag, neutral ink, no dot) from Pill,
 * which carries warning severity. A "Draft" order is not a problem; a
 * "Needs attention" stock item is. They must never be confused.
 *
 * Each flow lists its states in order; the tag's tooltip shows the step
 * ("Step 2 of 3") for linear flows. Unknown states render as plain text,
 * never guessed.
 */
export const FLOWS = {
  // wt/purchasing.py
  purchaseOrder: {
    order: ['draft', 'ordered', 'received'],
    labels: {
      draft: 'Draft', ordered: 'Ordered', partially_received: 'Partly received',
      received: 'Received', cancelled: 'Cancelled',
    },
    // partially_received sits between ordered and received
    position: { draft: 1, ordered: 2, partially_received: 2, received: 3 },
  },
  // wt/instructions.py (open -> acknowledged -> in_progress -> completed)
  instruction: {
    order: ['open', 'acknowledged', 'in_progress', 'completed'],
    labels: {
      open: 'New', acknowledged: 'Seen', in_progress: 'In progress',
      completed: 'Done', cancelled: 'Cancelled',
    },
    position: { open: 1, acknowledged: 2, in_progress: 3, completed: 4 },
  },
  // Stock Checks & Losses items: New -> Reviewing -> Explained
  check: {
    order: ['new', 'reviewing', 'explained'],
    labels: { new: 'New', reviewing: 'Reviewing', explained: 'Explained' },
    position: { new: 1, reviewing: 2, explained: 3 },
  },
  // Investigating -> Resolved
  investigation: {
    order: ['investigating', 'resolved'],
    labels: { investigating: 'Investigating', resolved: 'Resolved' },
    position: { investigating: 1, resolved: 2 },
  },
};

/** Pure label lookup (exported for tests). */
export function workflowLabel(flow, state) {
  const f = FLOWS[flow];
  return (f && f.labels[state]) || (state ? String(state).replace(/_/g, ' ') : '');
}

/** Pure "step N of M" (null when not a linear step, e.g. cancelled). */
export function workflowStep(flow, state) {
  const f = FLOWS[flow];
  if (!f || !f.position[state]) return null;
  return { step: f.position[state], of: f.order.length };
}

/**
 * @param {keyof FLOWS} flow
 * @param {string} state backend status string
 */
export function WorkflowState(flow, state) {
  const step = workflowStep(flow, state);
  return h('span', {
    class: `wf wf-${state === 'cancelled' ? 'ended' : 'step'}`,
    title: step ? `Step ${step.step} of ${step.of}` : undefined,
  }, workflowLabel(flow, state));
}
