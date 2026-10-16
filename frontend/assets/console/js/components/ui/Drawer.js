import { h, icon } from '../../lib/dom.js';
import { ICONS } from '../../lib/icons.js';

/**
 * Drawer — a right-side slide-out panel, the sibling primitive to Modal.js
 * for workflows that want to keep the page behind them in view (receiving,
 * an ingredient's detail, a batch's history) rather than interrupting with a
 * centered dialog.
 *
 * Same contract as Modal: click-outside-to-close, Escape-to-close, and the
 * same `{close(), setTitle(title, desc?)}` return shape, so call sites can
 * swap Modal({wide:true}) for Drawer({}) without changing how they drive it.
 *
 * Width is ~40% of viewport on desktop (clamped so it never gets
 * uselessly narrow or needlessly wide), full-screen below tablet width —
 * matching the design spec's "35-45% desktop, full-screen on tablet/phone".
 *
 * @param {{title:string, desc?:string, onClose:Function}} opts
 * @param {...(string|Node)} children
 */
export function Drawer({ title, desc, onClose }, ...children) {
  const overlay = h('div', {
    style: {
      position: 'fixed', inset: '0', background: 'rgba(31,28,23,0.45)',
      zIndex: '100',
    },
    onClick: (e) => { if (e.target === overlay) close(); },
  });

  const closeBtn = h('button', {
    class: 'icon-btn', title: 'Close', style: { flexShrink: '0' }, onClick: () => close(),
  }, icon(ICONS.close, 16));

  const titleEl = h('div', { class: 'card-title' }, title);
  const descEl = h('div', { class: 'card-desc', style: { display: desc ? '' : 'none' } }, desc || '');

  const panel = h('div', {
    class: 'card drawer-panel',
    style: {
      position: 'fixed', top: '0', right: '0', bottom: '0',
      width: 'clamp(380px, 42vw, 620px)', maxWidth: '100vw',
      height: '100vh', maxHeight: '100vh', overflowY: 'auto',
      borderRadius: '0', boxShadow: '-8px 0 40px rgba(31,28,23,0.22)',
      animation: 'wt-drawer-in .18s ease',
    },
  },
  h('div', { class: 'card-head', style: { position: 'sticky', top: '0', background: 'var(--surface)', zIndex: '1' } },
    h('div', { style: { flex: '1', minWidth: '0' } }, titleEl, descEl),
    closeBtn),
  ...children);

  // Keyframes injected once — Modal.js has no equivalent because a centered
  // dialog doesn't need a directional entrance animation the same way.
  if (!document.getElementById('wt-drawer-keyframes')) {
    const style = h('style', { id: 'wt-drawer-keyframes' });
    style.textContent = '@keyframes wt-drawer-in{from{transform:translateX(24px);opacity:.6}to{transform:translateX(0);opacity:1}}'
      + '@media (max-width: 760px){.drawer-panel{width:100vw !important;}}';
    document.head.appendChild(style);
  }

  overlay.appendChild(panel);

  function onKey(e) { if (e.key === 'Escape') close(); }
  document.addEventListener('keydown', onKey);

  function close() {
    document.removeEventListener('keydown', onKey);
    overlay.remove();
    if (onClose) onClose();
  }

  document.body.appendChild(overlay);
  const focusable = panel.querySelector('input, select, textarea, button');
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
