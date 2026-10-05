import { h } from '../../lib/dom.js';

/**
 * @param {{variant?:'default'|'primary'|'ghost', size?:'md'|'sm',
 *          onClick?:Function, disabled?:boolean, title?:string, type?:string,
 *          style?:object}} opts
 * @param {...(string|Node)} children
 */
export function Button({ variant = 'default', size = 'md', onClick, disabled, title, type, style } = {}, ...children) {
  const cls = ['btn',
    variant === 'primary' && 'btn-primary',
    variant === 'ghost' && 'btn-ghost',
    size === 'sm' && 'btn-sm'].filter(Boolean).join(' ');
  return h('button', { class: cls, onClick, disabled, title, type: type || 'button', style }, ...children);
}
