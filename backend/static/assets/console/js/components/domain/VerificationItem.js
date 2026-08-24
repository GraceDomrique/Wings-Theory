import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../ui/Card.js';
import { Button } from '../ui/Button.js';
import { Modal } from '../ui/Modal.js';
import { toast, Loading } from '../ui/Feedback.js';
import { stamp, str } from '../../lib/format.js';

import * as verification from '../../lib/api/verification.js';

/**
 * One Confirm / Change / Keep row.
 *
 * All three actions are additive (spec section 6 point 6). Confirming records
 * that the current estimate is right; changing records a new value from now
 * on; keeping records a deliberate decision to leave it as an estimate. None
 * of them rewrite history — the backend appends to verification_event, which
 * has append-only triggers of its own, and no historical POS transaction is
 * restated. Only future deductions use a changed value, and the UI says so.
 *
 * @param {{item:import('../../lib/types.js').VerificationItem, onResolved:Function}} opts
 */
export function VerificationItemCard({ item, onResolved }) {
  const card = Card({ style: { marginTop: '10px' } });

  const changeInput = h('input', {
    type: 'text',
    value: item.currentValue ?? '',
    placeholder: 'new value',
    style: { maxWidth: '180px' },
  });

  const changeRow = h('div', {
    style: { display: 'none', marginTop: '12px', paddingTop: '12px', borderTop: '1px solid var(--line-soft)' },
  },
  h('div', { class: 'grid', style: { gridTemplateColumns: 'auto auto 1fr', gap: '10px', alignItems: 'end' } },
    h('label', { class: 'field' }, 'New value', changeInput),
    Button({ variant: 'primary', size: 'sm', onClick: () => act('change') }, 'Save new value'),
    h('div', { class: 't-xs t-faint' },
      'Applies to future deductions only. Past orders are never restated.')));

  async function act(action) {
    setBusy(true);
    try {
      if (action === 'confirm') {
        await verification.confirm(item.field, item.rowId);
        toast(`Confirmed — ${item.entityName} is now a verified value.`, 'good');
      } else if (action === 'keep') {
        await verification.keepEstimate(item.field, item.rowId);
        toast(`Kept as an estimate — recorded as a deliberate choice.`, 'info');
      } else {
        const raw = changeInput.value.trim();
        if (raw === '') { toast('Enter the new value first.', 'warn'); setBusy(false); return; }
        const asNumber = Number(raw);
        await verification.change(item.field, item.rowId, Number.isNaN(asNumber) ? raw : asNumber);
        toast(`Updated — future deductions for ${item.entityName} use the new value.`, 'good');
      }
      onResolved(item);
    } catch (err) {
      toast(err.message, 'crit');
      setBusy(false);
    }
  }

  function setBusy(busy) {
    for (const b of card.querySelectorAll('button')) b.disabled = busy;
  }

  mount(card,
    CardHeader({
      title: item.question,
      desc: h('div', { class: 'card-desc' },
        item.entityName,
        ' · currently ',
        // "currently nothing set" is the honest description of an item that is
        // in this queue precisely because no one has supplied a value.
        item.currentValue === null
          ? h('span', { class: 'nodata' }, 'nothing set')
          : h('span', { class: 'mono' }, item.currentValue),
        item.context ? ` (${item.context})` : ''),
      right: [
        Button({ size: 'sm', onClick: () => openHistoryModal(item) }, 'History'),
        Button({ size: 'sm', onClick: () => act('keep') }, 'Keep estimate'),
        Button({
          size: 'sm',
          onClick: () => {
            changeRow.style.display = changeRow.style.display === 'none' ? '' : 'none';
            if (changeRow.style.display === '') changeInput.focus();
          },
        }, 'Change'),
        Button({ variant: 'primary', size: 'sm', onClick: () => act('confirm') }, 'Confirm'),
      ],
    }),
    item.whyItMatters
      ? h('div', { class: 't-xs t-soft' }, item.whyItMatters)
      : null,
    item.whatToCheck
      ? h('div', { class: 't-xs t-faint', style: { marginTop: '3px' } }, `What to check: ${item.whatToCheck}`)
      : null,
    changeRow);

  return card;
}

/**
 * Every decision ever recorded for this exact field/row — restored from the
 * old console. GET /console/queues/verification/history reads
 * verification_event, which the DB makes append-only the same way stock_ledger
 * is: nothing here can be a rewrite of the past, only a trail leading to now.
 */
function openHistoryModal(item) {
  const host = h('div');
  const modal = Modal({
    title: `History — ${item.entityName}`,
    desc: item.fieldLabel || item.field,
    onClose: () => {},
  }, host);
  mount(host, Loading('Loading history…'));

  verification.getHistory(item.field, item.rowId).then((events) => {
    if (events.length === 0) {
      mount(host, h('div', { class: 't-sm t-soft' }, 'No decisions recorded yet for this value — it is still on its original seeded estimate.'));
      return;
    }
    mount(host, ...events.map((e) => h('div', {
      style: { padding: '10px 0', borderBottom: '1px solid var(--line-soft)' },
    },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: '10px' } },
      h('span', { class: 't-strong t-sm' }, actionLabel(e.action)),
      h('span', { class: 'mono t-xs t-faint' }, stamp(e.timestamp).textContent)),
    h('div', { class: 't-sm', style: { marginTop: '3px' } },
      e.previousValue !== null ? `${str(e.previousValue).textContent} → ` : '',
      h('span', { class: 'mono' }, str(e.newValue, 'kept as estimate').textContent)),
    h('div', { class: 't-xs t-faint', style: { marginTop: '2px' } },
      `by ${e.userId || 'unknown'}`, e.note ? ` · ${e.note}` : ''))));
  }).catch((err) => {
    mount(host, h('div', { class: 'login-err' }, err.message));
  });
}

function actionLabel(action) {
  return { confirmed: 'Confirmed', changed: 'Changed', kept_estimate: 'Kept as estimate' }[action] || action;
}
