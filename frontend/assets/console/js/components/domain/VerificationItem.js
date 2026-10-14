import { h, mount, icon } from '../../lib/dom.js';
import { Drawer } from '../ui/Drawer.js';
import { Button } from '../ui/Button.js';
import { Pill } from '../ui/Pill.js';
import { toast, Loading } from '../ui/Feedback.js';
import { stamp, str } from '../../lib/format.js';
import { ICONS } from '../../lib/icons.js';

import * as verification from '../../lib/api/verification.js';

/**
 * One verification record's row (for the compact queue table) and its
 * detail drawer (Confirm / Change / Keep estimate + history).
 *
 * 2026-10-14 redesign: this used to be a full-width, always-expanded Card
 * per item — with potentially dozens on screen at once, that repeated the
 * same question/button layout down the page ("enormous repetitive
 * verification cards"). The underlying actions are unchanged (same three
 * additive, non-destructive verification.* calls — nothing here rewrites
 * history, the backend's verification_event stays append-only); only the
 * shell moved from one-card-per-item to one-row-per-item with the actual
 * workflow living in a drawer, the same pattern the ingredient drawer uses.
 *
 * @param {{item:import('../../lib/types.js').VerificationItem}} opts
 */
export function verificationRowCells(item) {
  return {
    record: h('div', {},
      h('div', { class: 't-sm t-strong' }, item.entityName),
      h('div', { class: 'sub' }, item.question)),
    current: item.currentValue === null
      ? h('span', { class: 'nodata' }, 'nothing set')
      : h('span', { class: 'mono t-sm' }, String(item.currentValue)),
    reason: h('span', { class: 't-xs t-soft' }, item.context || '—'),
    status: item.alreadyExpired ? Pill('crit', 'already past this date') : Pill('flat', 'needs review'),
  };
}

/**
 * The focused review drawer — Source and current value, History, and the
 * three actions. Replaces the old inline expand-on-card "Change" row and
 * the separate History modal with one place everything about this one
 * record lives, the same "one drawer per record" shape as the ingredient
 * drawer.
 *
 * @param {{item:object, onResolved:Function}} opts
 */
export function openVerificationDrawer({ item, onResolved }) {
  const body = h('div');
  const drawer = Drawer({ title: item.entityName, desc: item.fieldLabel || item.field, onClose: () => {} }, body);

  let changeOpen = false;

  function draw() {
    const changeInput = h('input', {
      type: 'text', value: item.currentValue ?? '', placeholder: 'new value',
    });

    function setBusy(busy) {
      for (const b of body.querySelectorAll('button')) b.disabled = busy;
    }

    async function act(action) {
      setBusy(true);
      try {
        if (action === 'confirm') {
          await verification.confirm(item.field, item.rowId);
          toast(`Confirmed — ${item.entityName} is now a verified value.`, 'good');
        } else if (action === 'keep') {
          await verification.keepEstimate(item.field, item.rowId);
          toast('Kept as an estimate — recorded as a deliberate choice.', 'info');
        } else {
          const raw = changeInput.value.trim();
          if (raw === '') { toast('Enter the new value first.', 'warn'); setBusy(false); return; }
          const asNumber = Number(raw);
          await verification.change(item.field, item.rowId, Number.isNaN(asNumber) ? raw : asNumber);
          toast(`Updated — future deductions for ${item.entityName} use the new value.`, 'good');
        }
        drawer.close();
        onResolved(item);
      } catch (err) {
        toast(err.message, 'crit');
        setBusy(false);
      }
    }

    const summary = h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '10px', marginBottom: '14px' } },
      h('div', { class: 'card card-sunk', style: { padding: '10px 12px' } },
        h('div', { class: 't-xs t-faint' }, 'Currently'),
        h('div', { class: 't-strong', style: { marginTop: '3px' } },
          item.currentValue === null ? h('span', { class: 'nodata' }, 'nothing set') : h('span', { class: 'mono' }, String(item.currentValue)))),
      h('div', { class: 'card card-sunk', style: { padding: '10px 12px' } },
        h('div', { class: 't-xs t-faint' }, 'Status'),
        h('div', { style: { marginTop: '3px' } }, item.alreadyExpired ? Pill('crit', 'already past this date') : Pill('flat', 'needs review'))));

    const notes = h('div', { style: { marginBottom: '14px' } },
      item.whyItMatters ? h('div', { class: 't-xs t-soft' }, item.whyItMatters) : null,
      item.whatToCheck ? h('div', { class: 't-xs t-faint', style: { marginTop: '3px' } }, `What to check: ${item.whatToCheck}`) : null);

    const actionTiles = h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)', gap: '10px', marginBottom: '10px' } },
      h('button', { class: 'quick-action', style: { flexDirection: 'column', textAlign: 'center' }, onClick: () => act('confirm') },
        h('span', { class: 'kpi-icon kpi-icon-good' }, icon(ICONS.checkCircle, 16)), h('span', {}, 'Confirm')),
      h('button', { class: 'quick-action', style: { flexDirection: 'column', textAlign: 'center' }, onClick: () => act('keep') },
        h('span', { class: 'kpi-icon kpi-icon-flat' }, icon(ICONS.clipboardList, 16)), h('span', {}, 'Keep estimate')),
      h('button', {
        class: `quick-action${changeOpen ? ' active' : ''}`, style: { flexDirection: 'column', textAlign: 'center' },
        onClick: () => { changeOpen = !changeOpen; draw(); },
      }, h('span', { class: 'kpi-icon kpi-icon-warn' }, icon(ICONS.layers, 16)), h('span', {}, 'Change')));

    const changeForm = changeOpen
      ? h('div', { class: 'card card-sunk', style: { marginBottom: '14px' } },
        h('label', { class: 'field', style: { display: 'block', marginBottom: '10px' } }, 'New value', changeInput),
        h('div', { class: 't-xs t-faint', style: { marginBottom: '10px' } }, 'Applies to future deductions only. Past orders are never restated.'),
        Button({ variant: 'primary', size: 'sm', onClick: () => act('change') }, 'Save new value'))
      : null;

    const historyHost = h('div');
    loadHistory(historyHost);

    mount(body, summary, notes, actionTiles, changeForm,
      h('div', { class: 'eyebrow', style: { marginTop: '10px' } }, 'History'),
      historyHost);
  }

  function loadHistory(host) {
    mount(host, Loading('Loading history…'));
    verification.getHistory(item.field, item.rowId).then((events) => {
      if (events.length === 0) {
        mount(host, h('div', { class: 't-sm t-faint' }, 'No decisions recorded yet — still on its original seeded estimate.'));
        return;
      }
      mount(host, ...events.map((e) => h('div', { class: 'mini-row' },
        h('span', {},
          h('div', { class: 't-sm' }, actionLabel(e.action)),
          h('div', { class: 'sub' }, `by ${e.userId || 'unknown'}${e.note ? ` · ${e.note}` : ''}`)),
        h('span', { class: 't-xs t-faint' },
          e.previousValue !== null ? `${str(e.previousValue).textContent} → ` : '',
          h('span', { class: 'mono' }, str(e.newValue, 'kept as estimate').textContent),
          h('div', { class: 'sub', style: { textAlign: 'right' } }, stamp(e.timestamp))))));
    }).catch((err) => {
      mount(host, h('div', { class: 'login-err' }, err.message));
    });
  }

  draw();
  return drawer;
}

function actionLabel(action) {
  return { confirmed: 'Confirmed', changed: 'Changed', kept_estimate: 'Kept as estimate' }[action] || action;
}
