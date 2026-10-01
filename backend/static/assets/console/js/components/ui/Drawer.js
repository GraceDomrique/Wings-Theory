import { h } from '../../lib/dom.js';

/**
 * Side drawer - the one "open a single record" surface for the redesigned
 * console (an ingredient, an order, an instruction, a recipe...). Slides in
 * from the right on desktop/tablet; becomes a full-screen sheet on phones
 * (see .drawer in components-v2.css). Header (title, subtitle, close), a
 * scrolling body, and an optional footer for the record's actions.
 *
 * Existing Modal-based workflows are untouched; pages adopt Drawer as they
 * are rebuilt. Escape and a backdrop click close it.
 *
 * @param {{title:string, subtitle?:string, onClose?:Function}} opts
 * @param {{body:Node|Node[], footer?:Node|Node[]}} content
 * @returns {{close:Function, setTitle:Function, setBody:Function, body:HTMLElement}}
 */
export function Drawer({ title, subtitle, onClose } = {}, { body, footer } = {}) {
  const prevFocus = document.activeElement;
  const titleEl = h('div', { class: 'drawer-title' }, title);
  const subEl = h('div', { class: 'drawer-sub', style: { display: subtitle ? '' : 'none' } }, subtitle || '');
  const bodyEl = h('div', { class: 'drawer-body' }, body);
  const footEl = footer ? h('div', { class: 'drawer-foot' }, footer) : null;

  const panel = h('aside', { class: 'drawer', role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('div', { class: 'drawer-head' },
      h('div', {}, titleEl, subEl),
      h('button', { class: 'icon-btn', title: 'Close', 'aria-label': 'Close', onClick: () => close() }, '✕')),
    bodyEl,
    footEl);

  const overlay = h('div', {
    class: 'drawer-overlay',
    onClick: (e) => { if (e.target === overlay) close(); },
  }, panel);

  function onKey(e) { if (e.key === 'Escape') close(); }
  document.addEventListener('keydown', onKey);

  function close() {
    document.removeEventListener('keydown', onKey);
    overlay.remove();
    if (prevFocus && prevFocus.focus) prevFocus.focus();
    if (onClose) onClose();
  }

  document.body.appendChild(overlay);
  requestAnimationFrame(() => {
    const first = panel.querySelector('input, select, textarea, button:not(.icon-btn)');
    (first || panel.querySelector('.icon-btn')).focus();
  });

  return {
    close,
    body: bodyEl,
    setTitle(t, s) {
      titleEl.textContent = t;
      if (s !== undefined) { subEl.textContent = s || ''; subEl.style.display = s ? '' : 'none'; }
    },
    setBody(...nodes) { bodyEl.replaceChildren(...nodes.flat(Infinity).filter(Boolean)); },
  };
}
