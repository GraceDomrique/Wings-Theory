import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Pill } from '../../components/ui/Pill.js';
import { Tabbar } from '../../components/ui/Tabbar.js';
import { Table } from '../../components/ui/Table.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError, confirmModal } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { ageFromNow, stamp, date } from '../../lib/format.js';
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
  const detailHost = h('div');
  let selectedId = null;

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

  // 2026-10 relayout: a stack of cards (one per instruction, full body text
  // and action buttons repeated on every one) used to be the whole page —
  // scanning six open instructions meant six cards' worth of scrolling.
  // Matches the approved reference mockup instead: a compact table up top
  // (title/priority/status/assignee/due/created), a row click selects one,
  // and that row's full detail — body, completion/cancel note, and a real
  // activity timeline built from its own actual stage timestamps, never
  // invented — renders once, below, with its actions right there.
  function draw() {
    mount(tabbarHost, Tabbar({
      items: tabItems(), activeId: statusFilter,
      onChange: (id) => { statusFilter = id; draw(); },
    }));
    const shown = visibleItems();
    if (!shown.some((i) => i.id === selectedId)) selectedId = shown.length ? shown[0].id : null;
    if (shown.length === 0) {
      mount(listHost, Card({}, EmptyState({
        icon: '✓',
        title: statusFilter === 'all' ? 'No instructions' : `No ${statusMeta(statusFilter)[1].toLowerCase()} instructions`,
        description: isOwner
          ? 'Send one to Admin using "+ New instruction" above.'
          : 'Nothing has been sent by the Owner yet.',
        tight: true,
      })));
      mount(detailHost, h('div'));
      return;
    }
    mount(listHost, Card({},
      CardHeader({ title: 'Instructions', desc: 'Operational tasks from Owner to Admin — open through complete.' }),
      Table({
        columns: [
          { key: 'title', label: 'Title', render: (i) => i.title },
          { key: 'priority', label: 'Priority', render: (i) => { const [sev, label] = priorityMeta(i.priority); return Pill(sev, label); } },
          { key: 'status', label: 'Status', render: (i) => { const [sev, label] = statusMeta(i.status); return Pill(sev, label); } },
          { key: 'assignee', label: 'Assignee', render: (i) => i.assignedTo || h('span', { class: 't-xs t-faint' }, 'Unassigned') },
          { key: 'due', label: 'Due Date', render: (i) => (i.dueDate ? date(i.dueDate) : h('span', { class: 't-xs t-faint' }, '—')) },
          { key: 'created', label: 'Created', render: (i) => h('span', { title: stamp(i.createdAt) }, date(i.createdAt)) },
        ],
        rows: shown,
        rowClass: (i) => (i.id === selectedId ? 'row-selected' : ''),
        onRowClick: (i) => { selectedId = i.id; drawDetail(); },
      })));
    drawDetail();
  }

  function timelineFor(ins) {
    const steps = [
      { at: ins.createdAt, label: `Created by ${ins.createdBy}` },
      { at: ins.acknowledgedAt, label: `Acknowledged${ins.acknowledgedBy ? ` by ${ins.acknowledgedBy}` : ''}` },
      { at: ins.inProgressAt, label: 'Marked in progress' },
      { at: ins.completedAt, label: `Completed${ins.completedBy ? ` by ${ins.completedBy}` : ''}` },
      { at: ins.cancelledAt, label: `Cancelled${ins.cancelledBy ? ` by ${ins.cancelledBy}` : ''}` },
    ].filter((s) => s.at);
    steps.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
    return steps;
  }

  function drawDetail() {
    const ins = items.find((i) => i.id === selectedId);
    if (!ins) { mount(detailHost, h('div')); return; }
    const [statusSev, statusLabel] = statusMeta(ins.status);
    const [prioSev, prioLabel] = priorityMeta(ins.priority);
    const canCancel = isAdmin
      ? ['open', 'acknowledged', 'in_progress'].includes(ins.status)
      : (isOwner && ins.status === 'open' && ins.createdBy === me.username);
    const steps = timelineFor(ins);

    mount(detailHost, Card({},
      CardHeader({
        title: ins.title,
        desc: `From ${ins.createdBy} · ${ins.assignedTo ? `assigned to ${ins.assignedTo}` : 'unassigned'}${ins.dueDate ? ` · due ${date(ins.dueDate)}` : ''}`,
        right: h('div', { style: { display: 'flex', gap: '6px' } }, Pill(prioSev, prioLabel), Pill(statusSev, statusLabel)),
      }),
      h('div', { class: 'grid', style: { gridTemplateColumns: '1.6fr 1fr', gap: '18px' } },
        h('div', {},
          h('div', { class: 'eyebrow' }, 'Instructions'),
          h('p', { class: 't-sm', style: { marginBottom: '14px' } }, ins.body),
          ins.status === 'completed' && ins.completionNote
            ? h('div', { class: 'callout callout-info', style: { marginBottom: '10px' } },
              h('p', {}, h('strong', {}, 'Completion note: '), ins.completionNote))
            : null,
          ins.status === 'cancelled' && ins.cancelReason
            ? h('div', { class: 'callout callout-info', style: { marginBottom: '10px' } },
              h('p', {}, h('strong', {}, 'Cancel reason: '), ins.cancelReason))
            : null,
          h('div', { style: { display: 'flex', gap: '8px', marginTop: '14px' } },
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
              : null)),
        h('div', {},
          h('div', { class: 'eyebrow' }, 'Activity Timeline'),
          h('div', { class: 'timeline' }, ...steps.map((s) => h('div', { class: 'timeline-step' },
            h('div', { class: 'timeline-dot' }),
            h('div', {},
              h('div', { class: 't-sm', style: { fontWeight: '600' } }, s.label),
              h('div', { class: 't-xs t-faint' }, stamp(s.at)))))))));
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
    h('div', { style: { marginBottom: '14px' } }, listHost),
    detailHost);
}
