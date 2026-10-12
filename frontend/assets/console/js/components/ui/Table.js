import { h, mount } from '../../lib/dom.js';

/**
 * Thin table wrapper — sortable, empty-state aware (spec section 2).
 *
 * Deliberately thin: it does not filter, page, or derive anything. Ordering
 * that carries business meaning (FEFO draw order, ledger sequence, depletion
 * risk) is server-computed and must be passed through untouched — see spec
 * section 4 on Batches, and section 6 point 3 on paired ledger lines. Columns
 * are only made sortable where re-sorting is harmless.
 *
 * @param {Object} opts
 * @param {{key:string, label:string, render:(row:any)=>Node|string,
 *          sortValue?:(row:any)=>any, align?:'left'|'right', width?:string}[]} opts.columns
 * @param {any[]} opts.rows
 * @param {Node} [opts.empty]        shown instead of <table> when rows is empty
 * @param {(row:any)=>void} [opts.onRowClick]
 * @param {(row:any, i:number)=>string} [opts.rowClass]
 */
export function Table({ columns, rows, empty, onRowClick, rowClass }) {
  if (!rows || rows.length === 0) {
    return empty || h('div', { class: 'load-note' }, 'Nothing to show.');
  }

  let sortKey = null;
  let sortDir = 1;
  const tbody = h('tbody');

  const thead = h('thead', {}, h('tr', {}, ...columns.map((col) => {
    const sortable = typeof col.sortValue === 'function';
    const caret = h('span', { class: 'sort-caret' });
    const th = h('th', {
      class: [sortable && 'sortable', col.align === 'right' && 'num'].filter(Boolean).join(' '),
      style: col.width ? { width: col.width } : undefined,
      onClick: sortable ? () => {
        sortDir = sortKey === col.key ? -sortDir : 1;
        sortKey = col.key;
        for (const other of thead.querySelectorAll('.sort-caret')) other.textContent = '';
        caret.textContent = sortDir === 1 ? '▲' : '▼';
        draw();
      } : undefined,
    }, col.label, sortable ? caret : null);
    return th;
  })));

  function draw() {
    const data = rows.slice();
    if (sortKey) {
      const col = columns.find((c) => c.key === sortKey);
      data.sort((a, b) => {
        const va = col.sortValue(a);
        const vb = col.sortValue(b);
        // Unknown values sort last in both directions — a null runway is not
        // "the most urgent", it is "we don't know yet".
        if (va === null || va === undefined) return 1;
        if (vb === null || vb === undefined) return -1;
        if (va < vb) return -sortDir;
        if (va > vb) return sortDir;
        return 0;
      });
    }
    mount(tbody, data.map((row, i) => h('tr', {
      class: [onRowClick && 'row-click', rowClass && rowClass(row, i)].filter(Boolean).join(' '),
      onClick: onRowClick ? () => onRowClick(row) : undefined,
    }, ...columns.map((col) => h('td', {
      class: col.align === 'right' ? 'num' : undefined,
    }, col.render(row))))));
  }

  draw();
  return h('div', { class: 'table-wrap' }, h('table', {}, thead, tbody));
}
