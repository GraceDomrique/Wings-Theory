import { h, mount, icon } from '../../lib/dom.js';
import { Button } from './Button.js';
import { ICONS } from '../../lib/icons.js';

/**
 * A styled image upload control — replaces the raw browser "Choose File /
 * No file chosen" presentation (2026-10-15). A real, keyboard-accessible
 * <input type="file"> is still there (native file pickers cannot be
 * recreated from scratch and still be accessible), it's just visually
 * hidden and triggered by the styled "Choose Image" button/drop zone
 * instead — the same "style the trigger, keep the real control" pattern
 * the console's <select> chevron already uses.
 *
 * This component does NOT upload anything itself — it validates type/size
 * client-side (a courtesy; the backend re-validates) and hands the caller
 * either a File (to upload however that caller already does it) or, for
 * convenience, reads it to a data URL first if `asDataUrl` is set. Combos.js
 * already has a working upload call (uploadComboImage); this only replaces
 * the surrounding UI, not that call.
 *
 * @param {{
 *   currentUrl?: string|null,
 *   accept?: string,
 *   maxSizeMB?: number,
 *   asDataUrl?: boolean,
 *   onSelect: (fileOrDataUrl: File|string) => void|Promise<void>,
 *   onRemove?: () => void,
 * }} opts
 */
export function FileUpload({
  currentUrl = null, accept = 'image/png,image/jpeg,image/webp', maxSizeMB = 5, asDataUrl = false, onSelect, onRemove,
} = {}) {
  let imageUrl = currentUrl;
  const root = h('div', {});
  const input = h('input', {
    type: 'file', accept, style: { position: 'absolute', width: '1px', height: '1px', opacity: '0', overflow: 'hidden', pointerEvents: 'none' },
  });
  const status = h('div', { class: 't-xs', style: { marginTop: '6px' } });

  function setStatus(text, severity) {
    status.textContent = text;
    status.className = `t-xs ${severity ? `t-${severity}` : 't-faint'}`;
    status.style.marginTop = '6px';
  }

  async function handleFile(file) {
    if (!file) return;
    const okType = accept.split(',').some((t) => file.type === t.trim());
    if (!okType) { setStatus(`Unsupported file type (${file.type || 'unknown'}). Use ${accept.replace(/image\//g, '').toUpperCase()}.`, 'crit'); return; }
    if (file.size > maxSizeMB * 1024 * 1024) { setStatus(`File is too large (max ${maxSizeMB} MB).`, 'crit'); return; }
    setStatus('Uploading…');
    try {
      if (asDataUrl) {
        const dataUrl = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = reject;
          reader.readAsDataURL(file);
        });
        await onSelect(dataUrl);
      } else {
        await onSelect(file);
      }
      setStatus('Image uploaded.', 'good');
    } catch (e) {
      setStatus(e.message || 'Upload failed.', 'crit');
    }
  }

  /** Called by the caller after a successful upload to show the real
   * stored URL (the component itself doesn't know what the backend
   * returned — Combos.js's uploadComboImage() does). */
  function setImage(url) {
    imageUrl = url;
    draw();
  }

  input.addEventListener('change', () => handleFile(input.files[0]));

  function draw() {
    if (imageUrl) {
      mount(root,
        h('div', { style: { display: 'flex', gap: '12px', alignItems: 'flex-start' } },
          h('div', {
            style: {
              width: '96px', height: '96px', borderRadius: 'var(--radius-sm)', overflow: 'hidden', flexShrink: '0',
              background: 'var(--surface-sunk)', border: '1px solid var(--line)',
            },
          }, h('img', { src: imageUrl, style: { width: '100%', height: '100%', objectFit: 'cover' } })),
          h('div', {},
            h('div', { style: { display: 'flex', gap: '8px' } },
              Button({ size: 'sm', onClick: () => input.click() }, 'Replace image'),
              Button({
                size: 'sm', variant: 'ghost',
                onClick: () => { imageUrl = null; setStatus(''); draw(); if (onRemove) onRemove(); },
              }, icon(ICONS.trash, 13), ' Remove image')),
            status)),
        input);
      return;
    }

    const dropZone = h('div', {
      class: 'file-upload-zone',
      tabIndex: '0',
      role: 'button',
      'aria-label': 'Choose or drop an image',
      onClick: () => input.click(),
      onKeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } },
    },
    icon(ICONS.imagePlus, 28),
    h('div', { class: 't-sm t-strong', style: { marginTop: '8px' } }, 'Choose or drop an image'),
    h('div', { class: 't-xs t-faint', style: { marginTop: '2px' } },
      `${accept.replace(/image\//g, '').toUpperCase()} · up to ${maxSizeMB} MB`),
    Button({ size: 'sm', style: { marginTop: '10px' }, onClick: (e) => { e.stopPropagation(); input.click(); } }, 'Choose Image'));

    dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('drag-over'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
    dropZone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropZone.classList.remove('drag-over');
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) handleFile(file);
    });

    mount(root, dropZone, status, input);
  }

  draw();
  root.setImage = setImage;
  return root;
}
