import { h, mount, icon } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading, toast } from '../../components/ui/Feedback.js';
import { money, qty, pct, count, nodata, str } from '../../lib/format.js';
import { ICONS } from '../../lib/icons.js';
import {
  listMenuItems, listRecipes, getRecipe, editMenuPrice,
} from '../../lib/api/menu.js';
import {
  listDraftCombos, listCombos, reviewDraftCombo,
} from '../../lib/api/comboPricing.js';
import { getCombinations } from '../../lib/api/reports.js';
import { openAddRecipeModal, openEditRecipeModal } from './Recipes.js';

/**
 * Menu Studio — 2026-10-15 integrated workspace replacing the Menu /
 * Recipes & Costing / Combos three-way split (spec: "Workspace 1 — Menu
 * Studio"). One searchable product library (left), the selected item's
 * price, recipe and costing (center), and its warnings + combo context
 * (right) — selecting a menu item answers "what is it, what's it made of,
 * what does it cost, what's wrong with it, what sells alongside it" without
 * a page change for any of those questions.
 *
 * Still backed by exactly the same endpoints the three old pages read
 * (listMenuItems/listRecipes/getRecipe/editMenuPrice/combo-pricing) — this
 * is a presentation and composition change, not a new data layer. Editing a
 * recipe reuses Recipes.js's own openEditRecipeModal/openAddRecipeModal
 * unchanged (same validation, same PATCH/POST calls), so there is exactly
 * one recipe editor in the codebase, not a second copy living here.
 *
 * The full Combo Builder (FP-Growth opportunities, draft review, publish-to-
 * POS image upload) stays its own route ('combos', reached via "Open Combo
 * Workspace →") rather than being inlined — a dedicated full-screen workflow
 * is the spec's own explicitly allowed exception for a workflow this complex.
 * Recipes & Costing's old standalone route ('recipes') remains a valid deep
 * link for the same reason (ADMIN_EXTRA_ROUTE_IDS in nav.js).
 */
export async function render(ctx) {
  const [menu, recipes, combosReport, drafts, published] = await Promise.all([
    listMenuItems(),
    listRecipes(),
    getCombinations(365).catch(() => ({ insufficientData: true, combos: [] })),
    listDraftCombos().catch(() => []),
    listCombos().catch(() => []),
  ]);

  // Keyed by the recipe's numeric row id (`pk`), not its string business
  // code (`id`, e.g. "REC-0038") — a menu item's `recipeId` (from
  // listMenuItems(), /pos/menu's `recipe_id` field) is that same numeric
  // id, not the string code listRecipes()/getRecipe() separately expose
  // under the confusingly identical-looking `id` field. See Recipes.js's
  // matching fix for the full story; this is the same mismatch.
  const recipeByPk = new Map(recipes.map((r) => [r.pk, r]));
  const linkedRecipePks = new Set(menu.items.filter((m) => m.recipeId).map((m) => m.recipeId));
  const standaloneRecipes = recipes.filter((r) => !linkedRecipePks.has(r.pk));

  let query = '';
  let statusFilter = 'all';
  let selected = menu.items.find((m) => m.sellable) || menu.items[0] || null;
  let selectedKind = selected ? 'menu' : null; // 'menu' | 'recipe'

  const listHost = h('div', { class: 'scrollpane', style: { maxHeight: '560px' } });
  const centerHost = h('div');
  const rightHost = h('div');
  const healthHost = h('div');

  function select(kind, ref) {
    selectedKind = kind;
    selected = ref;
    drawList();
    drawCenter();
    drawRight();
  }

  // ---------------------------------------------------------------------
  // Health strip — compact, one line, not a card bank (spec: "compact,
  // visually distinct menu-health indicators without wasting vertical
  // space"). Replaces the old page's full-width 3-KPI-card row.
  // ---------------------------------------------------------------------
  function drawHealth() {
    mount(healthHost, h('div', { class: 'menu-health-strip' },
      h('span', { class: 'mhs-item' }, h('b', {}, count(menu.items.length)), ' menu items'),
      h('span', { class: 'mhs-dot' }),
      h('span', { class: 'mhs-item mhs-good' }, icon(ICONS.checkCircle, 13), ' ', h('b', {}, count(menu.sellable)), ' sellable'),
      h('span', { class: 'mhs-dot' }),
      h('span', { class: `mhs-item ${menu.blocked > 0 ? 'mhs-crit' : 'mhs-good'}` }, icon(ICONS.alertTriangle, 13), ' ', h('b', {}, count(menu.blocked)), ' blocked'),
      h('span', { class: 'mhs-dot' }),
      h('span', { class: 'mhs-item' }, icon(ICONS.layers, 13), ' ', h('b', {}, count(standaloneRecipes.length)), ' standalone recipes')));
  }

  // ---------------------------------------------------------------------
  // Left — searchable product library: menu items (status-filterable) plus
  // standalone recipes (sub-recipes with no menu item pointing at them).
  // ---------------------------------------------------------------------
  function visibleMenuItems() {
    const q = query.trim().toLowerCase();
    let rows = q
      ? menu.items.filter((m) => m.name.toLowerCase().includes(q) || (m.category || '').toLowerCase().includes(q))
      : menu.items;
    if (statusFilter === 'available') rows = rows.filter((m) => m.sellable);
    if (statusFilter === 'blocked') rows = rows.filter((m) => !m.sellable);
    return rows.slice().sort((a, b) => (a.category || '').localeCompare(b.category || '') || a.name.localeCompare(b.name));
  }

  function drawList() {
    const rows = visibleMenuItems();
    let lastCategory = null;
    const menuRows = [];
    for (const m of rows) {
      if (m.category !== lastCategory) {
        lastCategory = m.category;
        menuRows.push(h('div', { class: 'studio-list-cat' }, str(m.category, 'Uncategorised')));
      }
      const isSel = selectedKind === 'menu' && selected && selected.pk === m.pk;
      menuRows.push(h('button', {
        class: `studio-list-row${isSel ? ' is-active' : ''}`,
        onClick: () => select('menu', m),
      },
      h('span', { class: `studio-dot ${m.sellable ? 'studio-dot-good' : 'studio-dot-crit'}` }),
      h('span', { style: { flex: '1', minWidth: '0' } },
        h('div', { class: 't-sm', style: { fontWeight: isSel ? '700' : '500' } }, m.name),
        h('div', { class: 't-xs t-faint' }, m.price !== null ? money(m.price).textContent : 'no price')),
      !m.sellable ? Pill('crit', 'blocked') : null));
    }

    const recipeRows = standaloneRecipes
      .filter((r) => !query.trim() || r.name.toLowerCase().includes(query.trim().toLowerCase()))
      .map((r) => {
        const isSel = selectedKind === 'recipe' && selected && selected.pk === r.pk;
        return h('button', {
          class: `studio-list-row${isSel ? ' is-active' : ''}`,
          onClick: () => select('recipe', r),
        },
        h('span', { class: 'studio-dot studio-dot-flat' }),
        h('span', { style: { flex: '1', minWidth: '0' } },
          h('div', { class: 't-sm', style: { fontWeight: isSel ? '700' : '500' } }, r.name),
          h('div', { class: 't-xs t-faint' }, `${r.lineCount} ingredient${r.lineCount === 1 ? '' : 's'} · not on the menu`)),
        null);
      });

    mount(listHost,
      menuRows.length === 0 && recipeRows.length === 0
        ? EmptyState({ icon: '⌕', title: 'Nothing matches', tight: true })
        : h('div', {}, ...menuRows),
      recipeRows.length
        ? h('div', {},
          h('div', { class: 'studio-list-cat', style: { marginTop: '10px' } }, 'Standalone recipes'),
          ...recipeRows)
        : null);
  }

  function drawFilters() {
    return h('div', { class: 'action-strip', style: { marginBottom: '10px' } },
      ...[['all', 'All'], ['available', 'Available'], ['blocked', 'Blocked']].map(([id, label]) => h('button', {
        class: `action-chip${statusFilter === id ? ' is-active' : ''}`,
        onClick: () => { statusFilter = id; drawList(); },
      }, label)));
  }

  const searchInput = h('input', {
    type: 'text', placeholder: 'Search menu items or recipes…',
    onInput: (e) => { query = e.target.value; drawList(); },
  });

  // ---------------------------------------------------------------------
  // Center — the selected product: price, recipe, cost, margin.
  // ---------------------------------------------------------------------
  async function drawCenter() {
    if (!selected) { mount(centerHost, Card({}, EmptyState({ icon: '☰', title: 'Nothing to show', description: 'Select a menu item or recipe from the list.' }))); return; }
    mount(centerHost, Loading('Loading…'));

    if (selectedKind === 'recipe') {
      await drawRecipeCenter(selected);
      return;
    }
    await drawMenuItemCenter(selected);
  }

  async function drawMenuItemCenter(m) {
    const recipe = m.recipeId ? recipeByPk.get(m.recipeId) : null;
    const detail = recipe ? await getRecipe(recipe.pk) : null;
    const cost = detail ? (detail.cost || {}) : {};
    const complete = !!cost.complete;
    const totalCost = cost.total_cost ?? null;
    const unpriced = cost.unpriced_ingredients || [];
    const margin = complete && m.price && totalCost !== null ? ((m.price - totalCost) / m.price) * 100 : null;
    const costByName = new Map((cost.lines || []).map((l) => [l.ingredient, l]));

    mount(centerHost, Card({},
      CardHeader({
        title: m.name,
        desc: str(m.category, 'uncategorised'),
        right: m.sellable ? Pill('good', 'available') : Pill('crit', 'unavailable'),
      }),
      priceRow(m),
      recipe
        ? h('div', {},
          h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)', gap: '10px', margin: '14px 0' } },
            h('div', { class: 'card card-sunk', style: { padding: '10px 12px' } },
              h('div', { class: 't-xs t-faint' }, complete ? 'Ingredient cost' : 'Ingredient cost (partial)'),
              h('div', { class: 't-strong', style: { marginTop: '3px', fontSize: '16px' } }, totalCost === null ? nodata('cannot be costed') : money(totalCost))),
            h('div', { class: 'card card-sunk', style: { padding: '10px 12px' } },
              h('div', { class: 't-xs t-faint' }, 'Gross margin'),
              h('div', { class: 't-strong', style: { marginTop: '3px', fontSize: '16px' } }, margin === null ? nodata('not calculable') : pct(margin, '', 0))),
            h('div', { class: 'card card-sunk', style: { padding: '10px 12px' } },
              h('div', { class: 't-xs t-faint' }, 'Ingredients'),
              h('div', { class: 't-strong', style: { marginTop: '3px', fontSize: '16px' } }, count(detail.lines.length)))),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' } },
            h('div', { class: 'eyebrow' }, 'Recipe'),
            Button({ size: 'sm', onClick: () => openEditRecipeModal(detail.recipe, detail.lines, () => drawCenter()) }, 'Edit recipe')),
          unpriced.length
            ? h('div', { class: 'callout callout-warn' }, h('p', {},
              h('strong', {}, `${unpriced.length} ingredient${unpriced.length === 1 ? '' : 's'} without a cost. `),
              'The total above excludes them — confirm under Setup & Verification before trusting the margin.'))
            : null,
          detail.problems && detail.problems.length
            ? h('div', { class: 'callout callout-crit' }, h('p', {}, h('strong', {}, 'This recipe has problems: '),
              detail.problems.map((p) => (typeof p === 'string' ? p : p.message || JSON.stringify(p))).join('; ')))
            : null,
          Table({
            columns: [
              { key: 'ingredient', label: 'Ingredient', render: (l) => h('div', {}, h('span', {}, l.name), l.label && l.label !== l.name ? h('div', { class: 't-xs t-faint' }, l.label) : null) },
              { key: 'amount', label: 'Amount', render: (l) => h('span', { class: 'mono t-sm' }, qty(l.amount, l.unit)) },
              { key: 'cost', label: 'Cost', render: (l) => { const priced = costByName.get(l.name); return h('span', { class: 'mono t-sm' }, priced ? money(priced.cost) : nodata('no cost set')); } },
              { key: 'confirmed', label: 'Confirmed', render: (l) => (l.qtyStatus === 'verified' ? Pill('good', 'confirmed') : Pill('warn', 'estimate')) },
            ],
            rows: detail.lines,
            empty: EmptyState({ icon: '☰', title: 'This recipe has no lines', tight: true }),
          }))
        : h('div', { class: 'callout callout-info', style: { marginTop: '14px' } },
          h('p', {}, h('strong', {}, 'No recipe mapped. '), 'This item has no ingredient composition on record — it cannot be costed or have its stock deducted correctly until one is linked.'))));
  }

  async function drawRecipeCenter(r) {
    const detail = await getRecipe(r.pk);
    const cost = detail.cost || {};
    const unpriced = cost.unpriced_ingredients || [];
    mount(centerHost, Card({},
      CardHeader({
        title: r.name,
        desc: `${detail.recipe.yieldQty} ${detail.recipe.yieldUnit} yield · not linked to a menu item`,
        right: Button({ size: 'sm', onClick: () => openEditRecipeModal(detail.recipe, detail.lines, () => drawCenter()) }, 'Edit recipe'),
      }),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(2,1fr)', gap: '10px', margin: '14px 0' } },
        h('div', { class: 'card card-sunk', style: { padding: '10px 12px' } },
          h('div', { class: 't-xs t-faint' }, cost.complete ? 'Ingredient cost' : 'Ingredient cost (partial)'),
          h('div', { class: 't-strong', style: { marginTop: '3px', fontSize: '16px' } }, cost.total_cost == null ? nodata('cannot be costed') : money(cost.total_cost))),
        h('div', { class: 'card card-sunk', style: { padding: '10px 12px' } },
          h('div', { class: 't-xs t-faint' }, 'Ingredients'),
          h('div', { class: 't-strong', style: { marginTop: '3px', fontSize: '16px' } }, count(detail.lines.length)))),
      unpriced.length
        ? h('div', { class: 'callout callout-warn' }, h('p', {}, h('strong', {}, `${unpriced.length} ingredient${unpriced.length === 1 ? '' : 's'} without a cost.`))) : null,
      Table({
        columns: [
          { key: 'ingredient', label: 'Ingredient', render: (l) => l.name },
          { key: 'amount', label: 'Amount', render: (l) => h('span', { class: 'mono t-sm' }, qty(l.amount, l.unit)) },
        ],
        rows: detail.lines,
        empty: EmptyState({ icon: '☰', title: 'This recipe has no lines', tight: true }),
      })));
  }

  function priceRow(m) {
    const display = h('span', {});
    const row = h('div', { class: 'studio-price-row' });

    function showDisplay() {
      mount(display, h('span', { style: { fontSize: '26px', fontWeight: '700' } }, m.price !== null ? money(m.price).textContent : 'no price set'));
      mount(row, display, Button({ size: 'sm', variant: 'ghost', onClick: showEditor }, 'Edit price'));
    }
    function showEditor() {
      const oldPrice = m.price ?? null;
      const input = h('input', { type: 'number', inputmode: 'decimal', min: '0.01', step: '0.01', value: m.price ?? '', class: 'no-spinner', style: { width: '110px' } });
      const diff = h('span', { class: 't-xs', style: { minWidth: '70px' } });
      function updateDiff() {
        const value = Number(input.value);
        if (!(value > 0) || oldPrice === null) { diff.textContent = ''; return; }
        const delta = value - oldPrice;
        diff.className = `t-xs ${delta > 0 ? 't-good' : delta < 0 ? 't-crit' : 't-faint'}`;
        diff.textContent = Math.abs(delta) < 0.001 ? 'no change' : `${delta > 0 ? '+' : '−'}₱${Math.abs(delta).toFixed(2)}`;
      }
      input.addEventListener('input', updateDiff);
      updateDiff();
      const err = h('span', { class: 't-crit t-xs' });
      const save = Button({ size: 'sm', variant: 'primary', onClick: async () => {
        const value = Number(input.value);
        if (!(value > 0)) { err.textContent = 'must be > 0'; return; }
        try {
          const r = await editMenuPrice(m.pk, value);
          m.price = r.price;
          toast(`${m.name} is now ₱${value.toFixed(2)} on the POS.`, 'good');
          showDisplay();
        } catch (e) { err.textContent = e.message; }
      } }, 'Save');
      const cancel = Button({ size: 'sm', variant: 'ghost', onClick: showDisplay }, 'Cancel');
      mount(row, input, diff, save, cancel, err);
    }
    showDisplay();
    return row;
  }

  // ---------------------------------------------------------------------
  // Right — warnings + combo context for the selected item.
  // ---------------------------------------------------------------------
  function drawRight() {
    const parts = [];

    if (selectedKind === 'menu' && selected && !selected.sellable) {
      parts.push(h('div', { class: 'callout callout-crit', style: { marginBottom: '0' } },
        h('p', {}, h('strong', {}, 'Blocked: '), str(selected.blockReason, 'reason not reported'))));
    }

    const comboOpportunities = combosReport.insufficientData ? 0 : combosReport.combos.length;
    const comboDrafts = drafts.filter((d) => d.status === 'draft').length;
    const relevantDrafts = selectedKind === 'menu' && selected
      ? drafts.filter((d) => d.itemAMenuItemId === selected.id || d.itemBMenuItemId === selected.id)
      : [];
    const relevantPublished = selectedKind === 'menu' && selected
      ? published.filter((c) => c.components.some((comp) => comp.menuItemId === selected.id))
      : [];

    parts.push(Card({},
      CardHeader({
        title: 'Discover Combos',
        desc: 'FP-Growth pairings, drafts and published combos',
        right: Button({ size: 'sm', variant: 'ghost', onClick: () => ctx.navigate('combos') }, 'Open workspace →'),
      }),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)', gap: '8px', marginBottom: relevantDrafts.length || relevantPublished.length ? '12px' : '0' } },
        comboStat(comboOpportunities, 'Opportunities'),
        comboStat(comboDrafts, 'Pending review'),
        comboStat(published.length, 'Published')),
      relevantDrafts.length ? h('div', { style: { marginTop: '10px' } },
        h('div', { class: 't-xs t-faint', style: { marginBottom: '6px' } }, 'Drafts involving this item'),
        ...relevantDrafts.map((d) => h('div', { class: 'mini-row' },
          h('span', {}, d.name), Pill(d.status === 'approved' ? 'good' : 'warn', d.status)))) : null,
      relevantPublished.length ? h('div', { style: { marginTop: '10px' } },
        h('div', { class: 't-xs t-faint', style: { marginBottom: '6px' } }, 'Published combos including this item'),
        ...relevantPublished.map((c) => h('div', { class: 'mini-row' },
          h('span', {}, c.name), Pill(c.posVisible ? 'good' : 'flat', c.posVisible ? 'on POS' : 'hidden')))) : null));

    mount(rightHost, ...parts);
  }

  function comboStat(value, label) {
    return h('div', { class: 'card card-sunk', style: { padding: '8px 10px', textAlign: 'center' } },
      h('div', { style: { fontSize: '18px', fontWeight: '700' } }, count(value)),
      h('div', { class: 't-xs t-faint' }, label));
  }

  drawHealth();
  drawList();
  await drawCenter();
  drawRight();
  ctx.setSearchHandler((q) => { query = q; drawList(); });

  const filterHost = h('div', {}, drawFilters());

  return h('div', { class: 'menu-studio' },
    healthHost,
    h('div', { class: 'studio-layout' },
      h('div', { class: 'studio-col studio-col-left' },
        h('div', { class: 'card', style: { padding: '12px' } },
          h('label', { class: 'field', style: { display: 'block', marginBottom: '10px' } }, searchInput),
          filterHost,
          listHost),
        Button({ size: 'sm', variant: 'ghost', style: { marginTop: '10px', width: '100%' }, onClick: () => openAddRecipeModal(() => {}) }, '+ Add standalone recipe')),
      h('div', { class: 'studio-col studio-col-center' }, centerHost),
      h('div', { class: 'studio-col studio-col-right' }, rightHost)));
}
