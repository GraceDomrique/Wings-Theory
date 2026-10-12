import { h } from '../../lib/dom.js';

/**
 * Sub-tab switcher — spec section 5. Inventory, Menu, Intelligence and
 * Verification all use this one component; the route owns the `items` array
 * and swaps its own subview in `onChange`.
 *
 * @param {{items:{id:string,label:string}[], activeId:string, onChange:(id:string)=>void}} opts
 */
export function Tabbar({ items, activeId, onChange }) {
  const bar = h('div', { class: 'tabbar' });
  for (const item of items) {
    bar.appendChild(h('button', {
      class: item.id === activeId ? 'active' : '',
      onClick: () => {
        if (item.id === activeId) return;
        for (const b of bar.children) b.classList.remove('active');
        const idx = items.indexOf(item);
        bar.children[idx].classList.add('active');
        onChange(item.id);
      },
    }, item.label));
  }
  return bar;
}
