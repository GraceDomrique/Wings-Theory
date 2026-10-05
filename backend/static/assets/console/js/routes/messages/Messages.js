import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError, confirmModal } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { ageFromNow, stamp } from '../../lib/format.js';
import {
  listInstructions, createInstruction, acknowledgeInstruction, startInstruction,
  completeInstruction, cancelInstruction,
} from '../../lib/api/instructions.js';
import { listUpdates, createUpdate, acknowledgeUpdate } from '../../lib/api/updates.js';
import { session } from '../../lib/api/client.js';

/**
 * Messages — the Owner<->Admin communication the console never had a
 * screen for. wt/instructions.py's fixed task board (open -> acknowledged
 * -> in_progress -> completed/cancelled) and wt/updates.py's free-text,
 * bidirectional thread both already existed, fully audit-logged, with a
 * complete frontend API client each and zero route consuming either.
 *
 * One list, shared: `listInstructions()` and `listUpdates()` (no
 * `owner_instruction_id`, i.e. everything) are the SAME shared board both
 * roles read — never a per-user inbox. Updates thread onto an instruction
 * (ownerInstructionId set) or stand alone (a heads-up with no instruction).
 */
export async function render(ctx) {
  const me = session.user || {};
  const isOwner = me.role === 'owner';
  const isAdmin = me.role === 'admin';

  let instructions = (await listInstructions()).items;
  const allUpdates = (await listUpdates()).items;
  const updatesByInstruction = new Map();
  const standalone = [];
  for (const u of allUpdates) {
    if (u.ownerInstructionId) {
      if (!updatesByInstruction.has(u.ownerInstructionId)) updatesByInstruction.set(u.ownerInstructionId, []);
      updatesByInstruction.get(u.ownerInstructionId).push(u);
    } else {
      standalone.push(u);
    }
  }

  const threadHost = h('div');
  const standaloneHost = h('div');

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

  function drawThreads() {
    if (instructions.length === 0) {
      mount(threadHost, Card({}, EmptyState({
        icon: '✉',
        title: 'No instructions yet',
        description: isOwner
          ? 'Send one to Admin using "+ New instruction" above.'
          : 'Nothing has been sent by the Owner yet.',
        tight: true,
      })));
      return;
    }
    mount(threadHost, ...instructions.map(instructionCard));
  }

  function instructionCard(ins) {
    const [statusSev, statusLabel] = statusMeta(ins.status);
    const [prioSev, prioLabel] = priorityMeta(ins.priority);
    const thread = updatesByInstruction.get(ins.id) || [];
    const canCancel = isAdmin
      ? ['open', 'acknowledged', 'in_progress'].includes(ins.status)
      : (isOwner && ins.status === 'open' && ins.createdBy === me.username);

    const replyInput = h('textarea', {
      class: 'textarea-plain', rows: 2, placeholder: 'Add an update on this…',
      style: { width: '100%', resize: 'vertical' },
    });
    const replyBtn = Button({ size: 'sm', onClick: async () => {
      const body = replyInput.value.trim();
      if (!body) return;
      replyBtn.disabled = true;
      try {
        await createUpdate({ body, ownerInstructionId: ins.id });
        replyInput.value = '';
        const fresh = (await listUpdates(ins.id)).items;
        updatesByInstruction.set(ins.id, fresh);
        drawThreads();
      } catch (e) { toast(e.message, 'crit'); } finally { replyBtn.disabled = false; }
    } }, 'Post');

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
      thread.length
        ? h('div', { style: { marginBottom: '10px', paddingLeft: '12px', borderLeft: '2px solid var(--line)' } },
          ...thread.map((u) => h('div', { style: { marginBottom: '8px' } },
            h('div', { class: 't-sm' }, u.body),
            h('div', { class: 't-xs t-faint' },
              `${u.createdByRole === 'owner' ? 'Owner' : 'Admin'} · `,
              h('span', { title: stamp(u.createdAt) }, ageFromNow(u.createdAt)),
              u.acknowledgedAt ? ' · acknowledged' : '',
              !u.acknowledgedAt && u.createdByRole !== me.role
                ? h('a', {
                  href: '#', class: 't-xs', style: { marginLeft: '6px' },
                  onClick: async (e) => {
                    e.preventDefault();
                    await acknowledgeUpdate(u.id);
                    const fresh = (await listUpdates(ins.id)).items;
                    updatesByInstruction.set(ins.id, fresh);
                    drawThreads();
                  },
                }, ' · acknowledge')
                : null))))
        : null,
      h('div', { style: { display: 'flex', gap: '8px', alignItems: 'flex-start', marginBottom: '10px' } }, replyInput, replyBtn),
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
          ? h('a', {
            href: '#', class: 't-xs',
            onClick: (e) => { e.preventDefault(); doCancel(ins); },
          }, 'Cancel')
          : null));
  }

  async function act(fn) {
    try {
      await fn();
      instructions = (await listInstructions()).items;
      drawThreads();
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
        instructions = (await listInstructions()).items;
        drawThreads();
      } catch (e) { err.textContent = e.message; err.style.display = ''; } finally { submit.disabled = false; }
    });
    const modal = Modal({ title: `Complete “${ins.title}”`, onClose: () => {} },
      h('label', { class: 'field' }, 'Completion note', noteInput),
      err,
      h('div', { style: { marginTop: '10px', display: 'flex', justifyContent: 'flex-end' } }, submit));
  }

  function drawStandalone() {
    if (standalone.length === 0) {
      mount(standaloneHost, h('div', { class: 't-sm t-faint' }, 'No standalone updates.'));
      return;
    }
    mount(standaloneHost, ...standalone.map((u) => h('div', { style: { padding: '10px 0', borderBottom: '1px solid var(--line-soft)' } },
      h('div', { class: 't-sm' }, u.body),
      h('div', { class: 't-xs t-faint' },
        `${u.createdByRole === 'owner' ? 'Owner' : 'Admin'} · `,
        h('span', { title: stamp(u.createdAt) }, ageFromNow(u.createdAt)),
        u.acknowledgedAt ? ' · acknowledged' : '',
        !u.acknowledgedAt && u.createdByRole !== me.role
          ? h('a', {
            href: '#', class: 't-xs', style: { marginLeft: '6px' },
            onClick: async (e) => { e.preventDefault(); await acknowledgeUpdate(u.id); location.reload(); },
          }, ' · acknowledge')
          : null))));
  }

  function openNewInstructionModal() {
    const titleInput = h('input', { type: 'text', placeholder: 'e.g. Verify Chicken Wings count before ordering' });
    const bodyInput = h('textarea', { rows: 3 });
    const priorityInput = h('select', {},
      h('option', { value: 'normal' }, 'Normal'),
      h('option', { value: 'important' }, 'Important'),
      h('option', { value: 'urgent' }, 'Urgent'));
    const assignedInput = h('input', { type: 'text', placeholder: 'optional' });
    const dueInput = h('input', { type: 'date' });
    const err = ModalError();
    const submit = Button({ variant: 'primary' }, 'Send');

    submit.addEventListener('click', async () => {
      const title = titleInput.value.trim();
      const body = bodyInput.value.trim();
      if (!title || !body) { err.textContent = 'Title and message are both required.'; err.style.display = ''; return; }
      submit.disabled = true;
      try {
        await createInstruction({
          title, body, priority: priorityInput.value,
          assignedTo: assignedInput.value.trim() || null,
          dueDate: dueInput.value || null,
        });
        modal.close();
        toast('Sent to Admin.', 'good');
        instructions = (await listInstructions()).items;
        drawThreads();
      } catch (e) { err.textContent = e.message; err.style.display = ''; } finally { submit.disabled = false; }
    });

    const modal = Modal({ title: 'New instruction to Admin', onClose: () => {} },
      h('label', { class: 'field' }, 'Title', titleInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Message', bodyInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Priority', priorityInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Assign to (optional)', assignedInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Due date (optional)', dueInput),
      err,
      h('div', { style: { marginTop: '14px', display: 'flex', justifyContent: 'flex-end' } }, submit));
  }

  const postInput = h('textarea', {
    class: 'textarea-plain', rows: 2, placeholder: isOwner ? 'Post a heads-up to Admin…' : 'Post a heads-up to Owner…',
    style: { width: '100%', resize: 'vertical' },
  });
  const postBtn = Button({ onClick: async () => {
    const body = postInput.value.trim();
    if (!body) return;
    postBtn.disabled = true;
    try {
      await createUpdate({ body, ownerInstructionId: null });
      postInput.value = '';
      const fresh = (await listUpdates()).items;
      const newStandalone = fresh.filter((u) => !u.ownerInstructionId);
      standalone.length = 0; standalone.push(...newStandalone);
      drawStandalone();
      toast('Posted.', 'good');
    } catch (e) { toast(e.message, 'crit'); } finally { postBtn.disabled = false; }
  } }, 'Post');

  drawThreads();
  drawStandalone();

  return h('div', { style: { display: 'flex', gap: '24px' } },
    h('div', { style: { flex: '1.6', minWidth: '0' } },
      h('div', { style: { display: 'flex', justifyContent: 'flex-end', marginBottom: '12px' } },
        isOwner ? Button({ variant: 'primary', onClick: openNewInstructionModal }, '+ New instruction') : null),
      threadHost),
    h('div', { style: { flex: '1', minWidth: '0' } },
      Card({},
        CardHeader({ title: 'Standalone updates', desc: 'Not tied to an instruction — a quick heads-up either way.' }),
        h('div', { style: { display: 'flex', gap: '8px', marginBottom: '12px' } }, postInput, postBtn),
        standaloneHost)));
}
