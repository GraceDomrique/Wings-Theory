import { h, icon } from '../../lib/dom.js';

/**
 * Sub-tab switcher — spec section 5. Inventory, Menu, Intelligence and
 * Verification all use this one component; the route owns the `items` array
 * and swaps its own subview in `onChange`.
 *
 * `icon` (an SVG path string, same convention as every other icon prop in
 * this codebase) is optional per item — Owner call sites pass one so the
 * pill-tab skin (app.css, body.role-owner .tabbar) gets a small icon chip
 * per tab, matching Admin's own Quick Actions row the brief asked Owner's
 * tabs to read like. Admin's existing Tabbar call sites pass no icon, so
 * nothing renders differently for them: the `.tab-icon` span below simply
 * doesn't exist on their buttons.
 *
 * @param {{items:{id:string,label:string,icon?:string}[], activeId:string, onChange:(id:string)=>void}} opts
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
    }, item.icon ? h('span', { class: 'tab-icon' }, icon(item.icon, 11)) : null, item.label));
  }
  return bar;
}
