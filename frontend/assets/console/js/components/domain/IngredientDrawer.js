import { h, mount } from '../../lib/dom.js';
import { Modal, ModalError } from '../ui/Modal.js';
import { Button } from '../ui/Button.js';
import { Pill } from '../ui/Pill.js';
import { Tabbar } from '../ui/Tabbar.js';
import { toast } from '../ui/Feedback.js';
import {
  money, qty, date, stamp, delta, str,
} from '../../lib/format.js';
import {
  getIngredient, countStock, wasteStock, useStock, updateIngredient, receive,
} from '../../lib/api/inventory.js';
import { severityFromDays } from '../../lib/severity.js';

/**
 * The ingredient workflow hub — restored from the old console's
 * openIngredientDrawer(). This is where "inventory actions live inside the
 * relevant ingredient workflow" actually happens: Receive, Count, Waste, Use
 * and Edit all act on the ONE ingredient the manager is already looking at,
 * instead of being separate pages that make them re-find it.
 *
 * Every write here goes straight to the endpoint the old console called —
 * reservation.record_count / record_wastage / record_usage / receive_stock
 * on the backend, through wt/api.py's existing routes. Nothing here computes
 * a new on-hand quantity, a new ledger balance, or a new FEFO batch; the
 * response is re-fetched from the server and that is what's shown.
 *
 * @param {{pk:number, onChanged:Function, initialAction?:string, prefill?:object}} opts
 *        onChanged fires after any successful write, so the caller (OnHand's
 *        table) can refresh. initialAction/prefill let a caller land directly
 *        on a specific action pane with fields already filled in — e.g.
 *        Expiry's "Discard" link opens straight to Wastage with the expired
 *        batch's own quantity and reason='spoilage' already entered, instead
 *        of making the owner re-find the ingredient and re-type a number
 *        the expiry card already showed them.
 */
export function openIngredientDrawer({
  pk, onChanged, initialAction = null, prefill = null,
}) {
  let detail = null;
  let activeAction = initialAction; // 'receive' | 'count' | 'waste' | 'use' | 'edit' | null
  const body = h('div');

  const modal = Modal({ title: 'Loading…', wide: true, onClose: () => {} }, body);
  mount(body, h('div', { class: 'load-note' }, 'Loading ingredient…'));

  load();

  async function load() {
    try {
      detail = await getIngredient(pk);
      draw();
    } catch (err) {
      mount(body, ModalError(err.message));
    }
  }

  function draw() {
    const ing = detail.ingredient;
    modal.setTitle(ing.name, `${ing.id} · ${ing.category}`);
    mount(body, buildBody());
  }

  function buildBody() {
    const ing = detail.ingredient;
    const hasExpiredStock = (ing.expiredBaseQty || 0) > 0.001;

    const summary = h('div', {
      class: 'grid', style: { gridTemplateColumns: `repeat(${hasExpiredStock ? 5 : 4},1fr)`, gap: '10px', marginBottom: '14px' },
    },
    summaryTile('On hand', str(ing.onHandDisplay, 'not counted')),
    // Only shown when some of what's on hand is already expired — for
    // everything else, "On hand" already IS the usable figure, and a second
    // identical tile would just be noise.
    hasExpiredStock
      ? summaryTile('Usable', qty(ing.usableBaseQty, ing.baseUnit),
        h('div', { class: 't-xs t-crit', style: { marginTop: '2px' } },
          `${qty(ing.expiredBaseQty, ing.baseUnit).textContent} expired`))
      : null,
    summaryTile('Cost / unit', money(ing.costPerUnit)),
    summaryTile('Confidence',
      ing.confidence === 'confirmed' ? Pill('good', 'confirmed') : Pill('warn', 'unverified')),
    summaryTile('Sellable?',
      ing.isDeductable ? Pill('good', 'deductable') : Pill('crit', ing.blockReason || 'blocked')));

    const actionPane = h('div', { style: { marginBottom: '16px' } });

    const actions = Tabbar({
      items: [
        { id: 'receive', label: 'Receive' },
        { id: 'count', label: 'Stock count' },
        { id: 'waste', label: 'Wastage' },
        { id: 'use', label: 'Manual use' },
        { id: 'edit', label: 'Edit details' },
      ],
      activeId: activeAction || '__none__',
      onChange: (id) => { activeAction = id; drawActionPane(actionPane); },
    });

    drawActionPane(actionPane);

    const recent = h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '14px' } },
      recentBatches(), recentMovements());

    return h('div', {}, summary, actions, actionPane, recent);
  }

  function drawActionPane(host) {
    if (!activeAction) { mount(host, h('div')); return; }
    const panes = { receive: receivePane, count: countPane, waste: wastePane, use: usePane, edit: editPane };
    mount(host, panes[activeAction]());
  }

  function summaryTile(label, value, ...extra) {
    return h('div', { class: 'card card-sunk', style: { padding: '10px 12px' } },
      h('div', { class: 't-xs t-faint' }, label),
      h('div', { class: 't-strong', style: { marginTop: '3px' } }, value),
      ...extra);
  }

  function recentBatches() {
    const rows = (detail.batches || []).slice(0, 5);
    return h('div', {},
      h('div', { class: 'eyebrow' }, 'Open FEFO batches'),
      rows.length === 0
        ? h('div', { class: 't-sm t-faint' }, 'No open batches.')
        : rows.map((b) => h('div', { class: 'mini-row' },
          h('span', {}, b.batchId, h('div', { class: 'sub' }, qty(b.qtyRemaining, b.baseUnit))),
          Pill(severityFromDays(b.daysToExpiry), b.expirySource === 'none' ? 'not tracked' : date(b.expiryDate).textContent))));
  }

  function recentMovements() {
    const rows = (detail.movements || []).slice(0, 5);
    return h('div', {},
      h('div', { class: 'eyebrow' }, 'Recent ledger activity'),
      rows.length === 0
        ? h('div', { class: 't-sm t-faint' }, 'No movements yet.')
        : rows.map((m) => h('div', { class: 'mini-row' },
          h('span', {}, str(m.reason, 'movement').textContent, h('div', { class: 'sub' }, stamp(m.timestamp).textContent)),
          h('span', { class: `mono t-xs ${m.changeQty > 0 ? 't-good' : 't-crit'}` }, delta(m.changeQty, m.baseUnit)))));
  }

  // ---- Receive ----
  function receivePane() {
    const ing = detail.ingredient;
    const qtyInput = h('input', { type: 'number', step: '0.01', min: '0', placeholder: '0.00' });
    const lotInput = h('input', { type: 'text', placeholder: 'optional' });
    const expiryInput = h('input', { type: 'date' });
    const err = ModalError();
    const submit = Button({ variant: 'primary', size: 'sm' }, 'Confirm delivery');
    submit.addEventListener('click', () => runWrite({
      err, submit,
      validate: () => (Number(qtyInput.value) > 0 ? null : 'Enter how much was delivered.'),
      call: () => receive(pk, {
        purchaseQty: Number(qtyInput.value), lotNo: lotInput.value.trim() || null,
        expiresAt: expiryInput.value || null,
      }),
      successMsg: () => `Delivery recorded for ${ing.name}. A new FEFO batch is open.`,
    }));
    return actionForm(
      `Quantity received, in ${ing.onHandUnit}`, qtyInput,
      'Lot / batch no. (optional)', lotInput,
      'Expiry date (optional)', expiryInput,
      err, submit,
    );
  }

  // ---- Count ----
  function countPane() {
    const ing = detail.ingredient;
    const currentLabel = qty(ing.onHandBaseQty, ing.baseUnit).textContent;
    const actualInput = h('input', { type: 'number', step: '0.01', min: '0', placeholder: '0.00' });
    const noteInput = h('input', { type: 'text', placeholder: 'optional' });
    const err = ModalError();
    const submit = Button({ variant: 'primary', size: 'sm' }, 'Record count');
    // One key per open form, not per click — a double-click or a retried
    // request replays the same correction instead of posting it twice (see
    // reservation.record_count's client_request_id handling).
    const requestId = (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID() : `ct-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    submit.addEventListener('click', () => runWrite({
      err, submit,
      validate: () => (actualInput.value !== '' && Number(actualInput.value) >= 0
        ? null : 'Enter the physically counted quantity.'),
      call: () => countStock(pk, {
        actualBase: Number(actualInput.value), note: noteInput.value.trim() || null,
        clientRequestId: requestId,
      }),
      successMsg: (r) => `${ing.name} corrected to ${Number(actualInput.value).toFixed(2)} ${ing.baseUnit}.${r && r.variance_base != null
        ? ` Difference ${r.variance_base > 0 ? '+' : ''}${Number(r.variance_base).toFixed(2)} ${ing.baseUnit}.`
        : ''}`,
    }));
    return actionForm(
      `Physically counted quantity, in ${ing.baseUnit} (system currently expects ${currentLabel})`,
      actualInput, 'Note (optional)', noteInput, err, submit,
      h('div', { class: 'callout callout-info', style: { marginBottom: '10px' } },
        h('p', {}, 'This updates the expected quantity to match what you counted and keeps a permanent record of the difference. You don’t need to explain why — only enter a reason if you already know what happened (use Wastage for that instead).')),
    );
  }

  // ---- Waste ----
  function wastePane() {
    const ing = detail.ingredient;
    const pf = prefill || {};
    const qtyInput = h('input', {
      type: 'number', step: '0.01', min: '0.01', placeholder: '0.00',
      value: pf.qtyBase != null ? String(pf.qtyBase) : undefined,
    });
    const reasonSelect = h('select', {},
      h('option', { value: 'wastage' }, 'Wastage'),
      h('option', { value: 'spoilage' }, 'Spoilage'));
    if (pf.reason) reasonSelect.value = pf.reason;
    const noteInput = h('input', {
      type: 'text', placeholder: 'what happened (optional)',
      value: pf.note || undefined,
    });
    const err = ModalError();
    const submit = Button({ variant: 'primary', size: 'sm' }, 'Log wastage');
    submit.addEventListener('click', () => runWrite({
      err, submit,
      validate: () => (Number(qtyInput.value) > 0 ? null : 'Enter how much was lost.'),
      call: () => wasteStock(pk, {
        qtyBase: Number(qtyInput.value), reason: reasonSelect.value, note: noteInput.value.trim() || null,
        source: pf.source || 'manual_writeoff',
      }),
      successMsg: () => `${reasonSelect.value === 'spoilage' ? 'Spoilage' : 'Wastage'} logged for ${ing.name}.`,
    }));
    return actionForm(
      `Amount lost, in ${ing.baseUnit}`, qtyInput,
      'Reason', reasonSelect,
      'Note (optional)', noteInput,
      err, submit,
    );
  }

  // ---- Use ----
  function usePane() {
    const ing = detail.ingredient;
    const qtyInput = h('input', { type: 'number', step: '0.01', min: '0.01', placeholder: '0.00' });
    const noteInput = h('input', { type: 'text', placeholder: 'what it was used for (optional)' });
    const err = ModalError();
    const submit = Button({ variant: 'primary', size: 'sm' }, 'Record use');
    submit.addEventListener('click', () => runWrite({
      err, submit,
      validate: () => (Number(qtyInput.value) > 0 ? null : 'Enter how much was used.'),
      call: () => useStock(pk, { qtyBase: Number(qtyInput.value), note: noteInput.value.trim() || null }),
      successMsg: () => `Manual use recorded for ${ing.name}.`,
    }));
    return actionForm(
      `Amount used, in ${ing.baseUnit}`, qtyInput,
      'Note (optional)', noteInput,
      err, submit,
      h('div', { class: 'callout callout-info', style: { marginBottom: '10px' } },
        h('p', {}, 'For operational supplies that never pass through a sale (tissue, gloves, cleaning alcohol). Food ingredients sold on the menu should deduct through a POS order instead.')),
    );
  }

  // ---- Edit ----
  function editPane() {
    const ing = detail.ingredient;
    const supplierInput = h('input', { type: 'text', value: ing.supplier || '' });
    const leadInput = h('input', { type: 'number', min: '0', step: '1', value: ing.leadTimeDays ?? '' });
    const reorderInput = h('input', { type: 'number', min: '0', step: '0.01', placeholder: 'leave blank to keep as-is' });
    const minOrderInput = h('input', { type: 'number', min: '0.01', step: '0.01', value: ing.minimumOrderQty ?? '', placeholder: 'leave blank for no minimum' });
    const costInput = h('input', { type: 'number', min: '0', step: '0.01', value: ing.costPerUnit ?? '' });
    const err = ModalError();
    const submit = Button({ variant: 'primary', size: 'sm' }, 'Save changes');
    submit.addEventListener('click', () => runWrite({
      err, submit,
      validate: () => null,
      call: () => {
        const fields = {};
        if (supplierInput.value.trim() !== '') fields.supplier = supplierInput.value.trim();
        if (leadInput.value !== '') fields.lead_time_days = Number(leadInput.value);
        if (reorderInput.value !== '') fields.reorder_point_base = Number(reorderInput.value);
        if (minOrderInput.value !== '') fields.minimum_order_qty = Number(minOrderInput.value);
        if (costInput.value !== '') fields.unit_cost = Number(costInput.value);
        if (Object.keys(fields).length === 0) return Promise.reject(new Error('Change at least one field.'));
        return updateIngredient(pk, fields);
      },
      successMsg: () => 'Ingredient details updated.',
    }));
    return actionForm(
      'Supplier', supplierInput,
      'Lead time (days)', leadInput,
      `Reorder point, in ${ing.baseUnit}`, reorderInput,
      `Minimum order, in ${ing.purchaseUnit || 'purchase units'}`, minOrderInput,
      `Cost per ${ing.onHandUnit} (₱) — also marks cost as verified`, costInput,
      err, submit,
    );
  }

  function actionForm(...parts) {
    const nodes = [];
    let i = 0;
    while (i < parts.length) {
      const part = parts[i];
      if (typeof part === 'string') {
        nodes.push(h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, part, parts[i + 1]));
        i += 2;
      } else {
        nodes.push(part);
        i += 1;
      }
    }
    return h('div', { class: 'card card-sunk' }, ...nodes);
  }

  async function runWrite({ err, submit, validate, call, successMsg }) {
    err.textContent = '';
    err.style.display = 'none';
    const problem = validate();
    if (problem) { err.textContent = problem; err.style.display = ''; return; }
    submit.disabled = true;
    try {
      const result = await call();
      toast(successMsg(result), 'good');
      activeAction = null;
      await load();
      if (onChanged) onChanged();
    } catch (e) {
      err.textContent = e.message;
      err.style.display = '';
      submit.disabled = false;
    }
  }

  return modal;
}
