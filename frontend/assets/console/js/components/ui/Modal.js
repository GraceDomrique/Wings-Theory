import { h } from '../../lib/dom.js';

/**
 * Modal — the one write-workflow surface the original component set (spec
 * §2/§5) did not include, because the frontend spec's route map only covered
 * read screens plus three inline forms (Receiving, Confirm/Change/Keep,
 * reconciliation submit). Restoring the console's write parity (add
 * ingredient, edit fields, bulk count, add recipe, map an unmapped line,
 * configure a flavor, reset a password, …) means every one of those needs a
 * host that isn't a full page. This is that host — one implementation, so
 * every write workflow opens, closes, and errors the same way.
 *
 * Built from the same tokens as everything else (Card's surface/radius/shadow,
 * Button, the .field input styling) — no new visual language, just a new
 * place to put the existing one.
 *
 * @param {{title:string, desc?:string, wide?:boolean, onClose:Function}} opts
 * @param {...(string|Node)} children
 */
export function Modal({ title, desc, wide = false, onClose }, ...children) {
  const overlay = h('div', {
    style: {
      position: 'fixed', inset: '0', background: 'rgba(31,28,23,0.45)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      padding: '20px', zIndex: '100',
    },
    onClick: (e) => { if (e.target === overlay) close(); },
  });

  const closeBtn = h('button', {
    class: 'icon-btn', title: 'Close', style: { flexShrink: '0' }, onClick: () => close(),
  }, '✕');

  const titleEl = h('div', { class: 'card-title' }, title);
  const descEl = h('div', { class: 'card-desc', style: { display: desc ? '' : 'none' } }, desc || '');

  const box = h('div', {
    class: 'card',
    style: {
      width: '100%', maxWidth: wide ? '760px' : '460px', maxHeight: '86vh',
      overflowY: 'auto', boxShadow: '0 8px 40px rgba(31,28,23,0.22)',
    },
  },
  h('div', { class: 'card-head' },
    h('div', {}, titleEl, descEl),
    closeBtn),
  ...children);

  overlay.appendChild(box);

  function onKey(e) { if (e.key === 'Escape') close(); }
  document.addEventListener('keydown', onKey);

  function close() {
    document.removeEventListener('keydown', onKey);
    overlay.remove();
    if (onClose) onClose();
  }

  document.body.appendChild(overlay);
  const focusable = box.querySelector('input, select, textarea, button');
  if (focusable) requestAnimationFrame(() => focusable.focus());

  return {
    close,
    setTitle(newTitle, newDesc) {
      titleEl.textContent = newTitle;
      if (newDesc !== undefined) {
        descEl.textContent = newDesc || '';
        descEl.style.display = newDesc ? '' : 'none';
      }
    },
  };
}

/**
 * A footer's worth of validation error, matching the `.login-err` styling
 * already used at the sign-in gate — never a raw server exception string
 * shown unstyled.
 */
export function ModalError(message) {
  return h('div', { class: 'login-err', style: { display: message ? '' : 'none' } }, message || '');
}

/**
 * A destructive/financially-significant confirmation step — spec's own
 * instruction that such actions "must require confirmation" applied as one
 * shared implementation rather than an ad hoc `window.confirm`.
 * @param {{title:string, body:string, confirmLabel?:string, danger?:boolean, onConfirm:Function}} opts
 */
export function confirmModal({ title, body, confirmLabel = 'Confirm', danger = false, onConfirm }) {
  let modal;
  const btn = h('button', {
    class: `btn ${danger ? '' : 'btn-primary'} btn-sm`,
    style: danger ? { background: 'var(--crit)', borderColor: 'var(--crit)', color: '#fff' } : undefined,
    onClick: async () => {
      btn.disabled = true;
      try { await onConfirm(); modal.close(); }
      finally { btn.disabled = false; }
    },
  }, confirmLabel);

  modal = Modal({ title, onClose: () => {} },
    h('p', { class: 't-sm t-soft' }, body),
    h('div', { style: { display: 'flex', gap: '8px', justifyContent: 'flex-end', marginTop: '16px' } },
      h('button', { class: 'btn btn-sm', onClick: () => modal.close() }, 'Cancel'),
      btn));
}
