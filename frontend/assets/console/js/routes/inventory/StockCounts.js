import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { count, qty } from '../../lib/format.js';
import { listIngredients, bulkCount, getCountToday } from '../../lib/api/inventory.js';

/**
 * Inventory > Stock Counts — spec section 8: "Does the system's quantity
 * match what is physically in storage?" This wraps the same POST
 * /console/ingredients/bulk-count the old On Hand "Bulk update" modal
 * called (still there, unchanged) but as its own full page: a manager doing
 * a real stock take needs to see System quantity / Physical count /
 * Difference side by side for everything at once, not one ingredient at a
 * time inside a small dialog.
 *
 * Nothing here computes a variance itself — entering a physical count just
 * previews `physical - system` client-side so the difference is visible
 * before submitting; the real count_adjustment ledger entry (and the real
 * variance figure Losses & Variance reads back) is posted server-side by
 * bulkCount(), exactly as record_count already does one ingredient at a
 * time.
 */
export async function render(ctx) {
  let ingredients = [];
  let lastQuery = '';
  let priorityOnly = false;
  // ingredientId (depletion's integer pk, same value as toIngredient's .pk)
  // -> { tier: 'needs'|'worth', reasons: string[] }. Built from
  // GET /console/count-today — see wt/depletion.py's count_today() for the
  // deterministic rule set behind `reasons`. This page never recomputes the
  // priority itself; it only filters/annotates the same full ingredient
  // list bulk-count already works from.
  let priority = new Map();
  const tableHost = h('div');
  const statsHost = h('div');
  const priorityHost = h('div');
  const rowState = new Map(); // pk -> { input }

  async function reload() {
    const [ingList, ct] = await Promise.all([
      listIngredients({ limit: 500, withRunway: false }),
      getCountToday().catch(() => null), // a failed fetch here must not block the full count-everything page
    ]);
    ingredients = ingList;
    priority = new Map();
    if (ct) {
      for (const item of ct.needsChecking) priority.set(item.pk, { tier: 'needs', reasons: item.reasons });
      for (const item of ct.worthChecking) priority.set(item.pk, { tier: 'worth', reasons: item.reasons });
    }
    rowState.clear();
    for (const ing of ingredients) {
      rowState.set(ing.pk, {
        input: h('input', {
          type: 'number', step: '0.01', min: '0', placeholder: 'not counted',
          style: { width: '110px' },
          onInput: () => drawDiff(ing.pk),
        }),
      });
    }
    drawStats();
    drawPriority();
    drawTable(lastQuery);
  }

  function drawPriority() {
    const needs = [...priority.values()].filter((p) => p.tier === 'needs').length;
    const worth = [...priority.values()].filter((p) => p.tier === 'worth').length;
    if (needs + worth === 0) {
      mount(priorityHost, Card({}, h('div', { class: 't-sm t-faint', style: { padding: '2px 0' } },
        'Nothing is flagged for a physical check right now — it’s safe to only count what you want to spot-check below.')));
      return;
    }
    mount(priorityHost, Card({},
      CardHeader({
        title: 'Count Today',
        desc: `${needs} need checking, ${worth} worth checking, out of ${ingredients.length} tracked — a risk-based shortlist, not everything.`,
        right: Button({ variant: priorityOnly ? 'primary' : 'default', size: 'sm', onClick: togglePriorityOnly },
          priorityOnly ? 'Showing shortlist only' : 'Show shortlist only'),
      })));
  }

  function togglePriorityOnly() {
    priorityOnly = !priorityOnly;
    drawPriority();
    drawTable(lastQuery);
  }

  function drawStats() {
    const counted = [...rowState.values()].filter((r) => r.input.value !== '').length;
    mount(statsHost,
      Card({}, Stat({ value: count(ingredients.length), label: 'Ingredients' })),
      Card({}, Stat({ value: count(counted), label: 'Entered this session' })));
  }

  function diffCell(ing) {
    const state = rowState.get(ing.pk);
    const el = h('span', { class: 'mono t-sm' }, '—');
    state.diffEl = el;
    return el;
  }

  function drawDiff(pk) {
    const ing = ingredients.find((i) => i.pk === pk);
    const state = rowState.get(pk);
    if (!state || !state.diffEl) return;
    if (state.input.value === '') { state.diffEl.textContent = '—'; state.diffEl.className = 'mono t-sm'; return; }
    const physical = Number(state.input.value);
    const diffBase = physical - (ing.onHandBaseQty ?? 0);
    const sign = diffBase > 0 ? '+' : '';
    state.diffEl.textContent = `${sign}${diffBase.toFixed(2)} ${ing.baseUnit}`;
    state.diffEl.className = `mono t-sm ${diffBase === 0 ? '' : diffBase < 0 ? 't-crit' : 't-warn'}`;
    drawStats();
  }

  function drawTable(query) {
    lastQuery = query || '';
    const q = lastQuery.trim().toLowerCase();
    let rows = q ? ingredients.filter((i) => i.name.toLowerCase().includes(q)) : ingredients;
    if (priorityOnly) rows = rows.filter((i) => priority.has(i.pk));
    // Flagged rows first, needs-checking ahead of worth-checking, so the
    // handful that matter are not buried at the bottom of an alphabetical
    // 131-row table.
    const tierRank = { needs: 0, worth: 1 };
    rows = [...rows].sort((a, b) => {
      const pa = priority.get(a.pk); const pb = priority.get(b.pk);
      const ra = pa ? tierRank[pa.tier] : 2; const rb = pb ? tierRank[pb.tier] : 2;
      return ra - rb || a.name.localeCompare(b.name);
    });

    mount(tableHost, Table({
      columns: [
        {
          key: 'name',
          label: 'Ingredient',
          render: (i) => {
            const flag = priority.get(i.pk);
            if (!flag) return i.name;
            return h('div', {},
              h('div', {}, i.name),
              h('div', { class: `t-xs ${flag.tier === 'needs' ? 't-crit' : 't-warn'}` }, flag.reasons[0]));
          },
        },
        {
          key: 'system', label: 'System quantity',
          render: (i) => h('span', { class: 'mono t-sm' }, qty(i.onHandBaseQty, i.baseUnit, 'not tracked')),
        },
        {
          key: 'physical', label: 'Physical count',
          render: (i) => rowState.get(i.pk).input,
        },
        { key: 'diff', label: 'Difference', render: (i) => diffCell(i) },
      ],
      rows,
      empty: EmptyState({
        icon: '⌕',
        title: priorityOnly && !q ? 'Nothing needs checking right now' : q ? `Nothing matches "${query}"` : 'No ingredients yet',
        description: priorityOnly && !q
          ? 'The shortlist is empty — show every ingredient instead to count something specific.'
          : q ? 'Try part of the ingredient name.' : 'Ingredients appear here once the inventory list has been imported.',
      }),
    }));
    for (const i of rows) drawDiff(i.pk);
  }

  const noteInput = h('input', { type: 'text', placeholder: 'e.g. weekly stock take (optional)', style: { minWidth: '220px' } });
  const submit = Button({ variant: 'primary', size: 'sm', onClick: submitCounts }, 'Submit counts');

  async function submitCounts() {
    const items = [...rowState.entries()]
      .filter(([, s]) => s.input.value !== '')
      .map(([pk, s]) => ({ pk, actualBase: Number(s.input.value) }));
    if (items.length === 0) { toast('Enter at least one physical count.', 'crit'); return; }
    submit.disabled = true;
    try {
      const r = await bulkCount(items, noteInput.value.trim() || null);
      toast(`Stock count submitted — ${r.updated} updated${r.failed ? `, ${r.failed} failed` : ''}.`, r.failed ? 'warn' : 'good');
      noteInput.value = '';
      await reload();
    } catch (e) {
      toast(e.message, 'crit');
    } finally {
      submit.disabled = false;
    }
  }

  await reload();
  ctx.setSearchHandler(drawTable);

  return h('div', {},
    h('div', { style: { marginBottom: '14px' } }, priorityHost),
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(2,1fr)', marginBottom: '14px' } }, statsHost),
    Card({},
      CardHeader({
        title: 'Physical stock take',
        desc: 'Enter a physical count only for what you actually counted — everything left blank is untouched',
        right: h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center' } }, noteInput, submit),
      }),
      tableHost));
}
