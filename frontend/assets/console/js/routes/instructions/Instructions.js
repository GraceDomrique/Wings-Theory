import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Pill } from '../../components/ui/Pill.js';
import { Tabbar } from '../../components/ui/Tabbar.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError, confirmModal } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { ageFromNow, stamp } from '../../lib/format.js';
import {
  listInstructions, listAssignees, createInstruction, acknowledgeInstruction,
  startInstruction, completeInstruction, cancelInstruction,
} from '../../lib/api/instructions.js';
import { session } from '../../lib/api/client.js';

/**
 * Instructions — the ONLY Owner<->Admin operational-communication screen.
 *
 * 2026-10: this replaces the old "Messages" concept, which paired this
 * fixed task board with wt/updates.py's free-text, bidirectional chat
 * thread (standalone heads-ups and inline replies). That chat half is
 * removed entirely, backend route and all — owner/admin communication is
 * now a structured instruction with a priority, an assignee, and a due
 * date, not a conversation. Where a choice is from a fixed, known set
 * (priority, who it's assigned to) it is a dropdown; title/body stay free
 * text because the instruction's content is inherently descriptive and
 * there is no fixed vocabulary to pick it from. A completion note is
 * still free text for the same reason, and is the one place admin writes
 * back — a structured close-out field on a specific task, not a chat
 * reply.
 *
 * One shared list, not a per-user inbox: `listInstructions()` (no status
 * filter, i.e. everything) is read identically by both roles.
 */
export async function render(ctx) {
  const me = session.user || {};
  const isOwner = me.role === 'owner';
  const isAdmin = me.role === 'admin';

  let items = (await listInstructions()).items;
  const listHost = h('div');
  const tabbarHost = h('div');

  function statusMeta(status) {
    switch (status) {
      case 'open': return ['flat', 'Open'];
      case 'acknowledged': return ['warn', 'Acknowledged'];
      case 'in_progress': return ['warn', 'In progress'];
      case 'completed': return ['good', 'Completed'];
      case 'cancelled': return ['flat', 'Cancelled'];
      default: return ['flat', status];
    }
  }

  function priorityMeta(priority) {
    switch (priority) {
      case 'urgent': return ['crit', 'Urgent'];
      case 'important': return ['warn', 'Important'];
      default: return ['flat', 'Normal'];
    }
  }

  const STATUS_TABS = ['open', 'acknowledged', 'in_progress', 'completed', 'cancelled'];
  let statusFilter = 'all';

  function tabItems() {
    return [
      { id: 'all', label: `All (${items.length})` },
      ...STATUS_TABS.map((s) => ({ id: s, label: `${statusMeta(s)[1]} (${items.filter((i) => i.status === s).length})` })),
    ];
  }

  function visibleItems() {
    return statusFilter === 'all' ? items : items.filter((i) => i.status === statusFilter);
  }

  function draw() {
    mount(tabbarHost, Tabbar({
      items: tabItems(), activeId: statusFilter,
      onChange: (id) => { statusFilter = id; draw(); },
    }));
    const shown = visibleItems();
    if (shown.length === 0) {
      mount(listHost, Card({}, EmptyState({
        icon: '✓',
        title: statusFilter === 'all' ? 'No instructions' : `No ${statusMeta(statusFilter)[1].toLowerCase()} instructions`,
        description: isOwner
          ? 'Send one to Admin using "+ New instruction" above.'
          : 'Nothing has been sent by the Owner yet.',
        tight: true,
      })));
      return;
    }
    mount(listHost, ...shown.map(instructionCard));
  }

  function instructionCard(ins) {
    const [statusSev, statusLabel] = statusMeta(ins.status);
    const [prioSev, prioLabel] = priorityMeta(ins.priority);
    const canCancel = isAdmin
      ? ['open', 'acknowledged', 'in_progress'].includes(ins.status)
      : (isOwner && ins.status === 'open' && ins.createdBy === me.username);

    return Card({ style: { marginBottom: '14px' } },
      h('div', { class: 'card-head' },
        h('div', {},
          h('div', { class: 'card-title' }, ins.title),
          h('div', { class: 'card-desc' }, ins.body)),
        h('div', { style: { display: 'flex', gap: '6px', flexShrink: '0' } }, Pill(prioSev, prioLabel), Pill(statusSev, statusLabel))),
      h('div', { class: 't-xs t-faint', style: { marginBottom: '10px' } },
        `From ${ins.createdBy} · `,
        h('span', { title: stamp(ins.createdAt) }, ageFromNow(ins.createdAt)),
        ins.assignedTo ? ` · assigned to ${ins.assignedTo}` : '',
        ins.dueDate ? ` · due ${ins.dueDate}` : ''),
      ins.status === 'completed' && ins.completionNote
        ? h('div', { class: 'callout callout-info', style: { marginBottom: '10px' } },
          h('p', {}, h('strong', {}, 'Completion note: '), ins.completionNote))
        : null,
      ins.status === 'cancelled' && ins.cancelReason
        ? h('div', { class: 'callout callout-info', style: { marginBottom: '10px' } },
          h('p', {}, h('strong', {}, 'Cancel reason: '), ins.cancelReason))
        : null,
      h('div', { style: { display: 'flex', gap: '8px' } },
        isAdmin && ins.status === 'open'
          ? Button({ size: 'sm', onClick: () => act(() => acknowledgeInstruction(ins.id)) }, 'Acknowledge')
          : null,
        isAdmin && ins.status === 'acknowledged'
          ? Button({ size: 'sm', onClick: () => act(() => startInstruction(ins.id)) }, 'Start')
          : null,
        isAdmin && ins.status === 'in_progress'
          ? Button({ size: 'sm', variant: 'primary', onClick: () => openCompleteModal(ins) }, 'Mark complete')
          : null,
        canCancel
          ? Button({ size: 'sm', variant: 'ghost', onClick: () => doCancel(ins) }, 'Cancel')
          : null));
  }

  async function act(fn) {
    try {
      await fn();
      items = (await listInstructions()).items;
      draw();
      ctx.refreshBadges();
    } catch (e) { toast(e.message, 'crit'); }
  }

  function doCancel(ins) {
    confirmModal({
      title: `Cancel “${ins.title}”?`,
      body: 'This cannot be undone.',
      confirmLabel: 'Cancel instruction',
      danger: true,
      onConfirm: () => act(() => cancelInstruction(ins.id, null)),
    });
  }

  function openCompleteModal(ins) {
    const noteInput = h('textarea', { rows: 3, placeholder: 'What was done…' });
    const err = ModalError();
    const submit = Button({ variant: 'primary' }, 'Mark complete');
    submit.addEventListener('click', async () => {
      const note = noteInput.value.trim();
      if (!note) { err.textContent = 'A completion note is required.'; err.style.display = ''; return; }
      submit.disabled = true;
      try {
        await completeInstruction(ins.id, note);
        modal.close();
        toast('Marked complete.', 'good');
        items = (await listInstructions()).items;
        draw();
        ctx.refreshBadges();
      } catch (e) { err.textContent = e.message; err.style.display = ''; } finally { submit.disabled = false; }
    });
    const modal = Modal({ title: `Complete “${ins.title}”`, onClose: () => {} },
      h('label', { class: 'field' }, 'Completion note', noteInput),
      err,
      h('div', { style: { marginTop: '10px', display: 'flex', justifyContent: 'flex-end' } }, submit));
  }

  async function openNewInstructionModal() {
    const titleInput = h('input', { type: 'text', placeholder: 'e.g. Verify Chicken Wings count before ordering' });
    const bodyInput = h('textarea', { rows: 3, placeholder: 'What needs doing, and any detail Admin needs to act on it.' });
    const priorityInput = h('select', {},
      h('option', { value: 'normal' }, 'Normal'),
      h('option', { value: 'important' }, 'Important'),
      h('option', { value: 'urgent' }, 'Urgent'));
    const assignedInput = h('select', {}, h('option', { value: '' }, 'Unassigned — any Admin'));
    try {
      const assignees = await listAssignees();
      for (const a of assignees) {
        assignedInput.appendChild(h('option', { value: a.username }, a.displayName || a.username));
      }
    } catch { /* dropdown still works unassigned-only if this fails */ }
    const dueInput = h('input', { type: 'date' });
    const err = ModalError();
    const submit = Button({ variant: 'primary' }, 'Send');

    submit.addEventListener('click', async () => {
      const title = titleInput.value.trim();
      const body = bodyInput.value.trim();
      if (!title || !body) { err.textContent = 'Title and description are both required.'; err.style.display = ''; return; }
      submit.disabled = true;
      try {
        await createInstruction({
          title, body, priority: priorityInput.value,
          assignedTo: assignedInput.value || null,
          dueDate: dueInput.value || null,
        });
        modal.close();
        toast('Sent to Admin.', 'good');
        items = (await listInstructions()).items;
        draw();
        ctx.refreshBadges();
      } catch (e) { err.textContent = e.message; err.style.display = ''; } finally { submit.disabled = false; }
    });

    const modal = Modal({ title: 'New instruction to Admin', onClose: () => {} },
      h('label', { class: 'field' }, 'Title', titleInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Description', bodyInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Priority', priorityInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Assign to', assignedInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Due date (optional)', dueInput),
      err,
      h('div', { style: { marginTop: '14px', display: 'flex', justifyContent: 'flex-end' } }, submit));
  }

  draw();

  return h('div', {},
    h('div', { style: { display: 'flex', justifyContent: 'flex-end', marginBottom: '12px' } },
      isOwner ? Button({ variant: 'primary', onClick: openNewInstructionModal }, '+ New instruction') : null),
    h('div', { style: { marginBottom: '12px' } }, tabbarHost),
    Card({},
      CardHeader({ title: 'Instructions', desc: 'Operational tasks from Owner to Admin — open through complete.' }),
      listHost));
}
