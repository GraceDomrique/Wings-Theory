/**
 * Tiny element builder. Everything in this console is built from `h()` rather
 * than innerHTML strings, so ingredient names, notes and backend-authored copy
 * can never be parsed as markup — they go in as text nodes.
 *
 * Usage:
 *   h('div', { class: 'card' }, h('span', {}, 'hello'))
 *   h('button', { class: 'btn', onClick: fn }, 'Save')
 *
 * Props:
 *   class / className  -> class attribute
 *   style              -> object of camelCase CSS properties
 *   onX                -> addEventListener('x', ...)
 *   dataFoo            -> data-foo
 *   anything else      -> setAttribute
 *
 * Children: elements, strings, numbers, or arrays thereof. null / undefined /
 * false are skipped, so `cond && h(...)` works inline.
 */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class' || k === 'className') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'html') el.innerHTML = v; // only ever for inlined trusted SVG
    else if (k.startsWith('on') && typeof v === 'function') {
      el.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k.startsWith('data') && k.length > 4) {
      el.setAttribute('data-' + k.slice(4).toLowerCase(), v);
    } else el.setAttribute(k, v === true ? '' : v);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false || c === true) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** Replace an element's entire contents. */
export function mount(host, ...children) {
  host.replaceChildren();
  append(host, children);
  return host;
}

/** Inline SVG icon from a raw path string, at the nav/topbar sizes. */
export function icon(paths, size) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  if (size) { svg.setAttribute('width', size); svg.setAttribute('height', size); }
  svg.innerHTML = paths; // static icon geometry defined in code, never user data
  return svg;
}
