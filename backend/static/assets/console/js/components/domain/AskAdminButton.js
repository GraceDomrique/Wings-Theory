import { h } from '../../lib/dom.js';
import { Button } from '../ui/Button.js';
import { Can } from '../ui/Can.js';
import { Modal, ModalError } from '../ui/Modal.js';
import { toast } from '../ui/Feedback.js';
import { createInstruction } from '../../lib/api/instructions.js';

/**
 * Owner -> Admin request, over the existing instruction workflow
 * (POST /console/instructions, capability instruction.create). The label is
 * contextual ("Ask Admin to check", "...investigate", "...update",
 * "...handle"); the mechanism underneath is always the same instruction.
 *
 * Renders only for roles holding instruction.create (Owner today). The
 * backend remains the authority.
 *
 * @param {{label:string, title:string, body?:string, priority?:string,
 *          onSent?:Function}} p
 *   title/body prefill the request so the Owner just confirms and sends.
 */
export function AskAdminButton({ label, title, body = '', priority = 'normal', onSent }) {
  return Can('instruction.create', () => Button({ size: 'sm', onClick: openForm }, label));

  function openForm() {
    const titleIn = h('input', { type: 'text', value: title, maxlength: '140' });
    const bodyIn = h('textarea', { rows: '4' }, body);
    const err = ModalError('');
    let modal;
    const send = Button({
      variant: 'primary',
      size: 'sm',
      onClick: async () => {
        const t = titleIn.value.trim();
        if (!t) { err.textContent = 'Please add a short title.'; err.style.display = ''; return; }
        send.disabled = true;
        try {
          await createInstruction({ title: t, body: bodyIn.value.trim(), priority });
          toast('Request sent to Admin');
          modal.close();
          if (onSent) onSent();
        } catch (e) {
          err.textContent = e.message;
          err.style.display = '';
        } finally { send.disabled = false; }
      },
    }, 'Send request');
    modal = Modal(
      { title: label, desc: 'This goes to Admin as a task they can see and update.', onClose: () => {} },
      h('label', { class: 'field' }, 'Title', titleIn),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Details', bodyIn),
      err,
      h('div', { style: { display: 'flex', gap: '8px', justifyContent: 'flex-end', marginTop: '14px' } },
        Button({ size: 'sm', onClick: () => modal.close() }, 'Cancel'), send),
    );
  }
}
