import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading, toast } from '../../components/ui/Feedback.js';
import { money, qty, pct, count, nodata } from '../../lib/format.js';
import {
  listRecipes, getRecipe, listMenuItems, createRecipe, editRecipe,
} from '../../lib/api/menu.js';
import { listIngredients } from '../../lib/api/inventory.js';

/**
 * Recipes & Menu > Recipes. Master list on the left, costing on the right.
 *
 * Costing honesty is the point of this screen. recipe_cost() reports which
 * ingredients it could not price, and this view refuses to present a total as
 * if it were complete when some lines have no cost — an under-counted cost
 * per serving produces a flattering margin, which is worse than no margin at
 * all. When lines are unpriced the total is shown as a partial with the
 * missing items named.
 */
export async function render(ctx) {
  let recipes = [];
  let menu = { items: [] };
  let priceByRecipe = new Map();
  let selected = null;
  const detailHost = h('div');
  const listHost = h('div', { class: 'scrollpane', style: { maxHeight: '520px' } });
  const countHost = h('div', { class: 'card-desc' });

  async function reload(selectPk) {
    [recipes, menu] = await Promise.all([listRecipes(), listMenuItems()]);
    priceByRecipe = new Map();
    for (const m of menu.items) if (m.recipeId) priceByRecipe.set(m.recipeId, m);
    selected = (selectPk && recipes.find((r) => r.pk === selectPk)) || recipes[0] || null;
    countHost.textContent = `${recipes.length} active`;
    drawList();
    if (selected) await drawDetail();
    else mount(detailHost, Card({}, EmptyState({
      icon: '☰',
      title: 'No recipes yet',
      description: 'Add the first one with the button above.',
    })));
  }

  function drawList() {
    mount(listHost, h('table', {}, h('tbody', {}, ...recipes.map((r) => h('tr', {
      class: 'row-click',
      onClick: () => { selected = r; drawList(); drawDetail(); },
    }, h('td', {
      style: r.pk === selected.pk
        ? { background: 'var(--amber-tint)', fontWeight: '600' }
        : {},
    },
    h('div', {}, r.name),
    h('div', { class: 't-xs t-faint' },
      `${r.lineCount} ingredient${r.lineCount === 1 ? '' : 's'}`,
      r.unmappedCount ? ` · ${r.unmappedCount} unmapped` : '')))))));
  }

  async function drawDetail() {
    mount(detailHost, Loading('Loading recipe…'));
    const d = await getRecipe(selected.pk);
    const cost = d.cost || {};
    // 2026-10-15 fix: was `priceByRecipe.get(d.recipe.id)` — d.recipe.id is
    // the string business code ("REC-0038") getRecipe() maps from the
    // recipe's own recipe_id field, but priceByRecipe (built from
    // listMenuItems()) is keyed by m.recipeId, the NUMERIC row id
    // /pos/menu's recipe_id field actually holds. Those are two different
    // values that happen to share a JSON key name across two endpoints —
    // every lookup here silently missed, so this panel never showed a menu
    // item's real price or availability next to its recipe, for any recipe,
    // ever (found while building Menu Studio, which depends on the same
    // linkage actually working). d.recipe.pk is the numeric id that matches.
    const menuItem = priceByRecipe.get(d.recipe.pk) || null;
    const price = menuItem ? menuItem.price : null;

    const complete = !!cost.complete;
    const totalCost = cost.total_cost ?? null;
    const unpriced = cost.unpriced_ingredients || [];

    // A margin computed from an incomplete cost would be flattering and wrong,
    // so it is only shown when every line is priced and there is a price to
    // compare against.
    const margin = complete && price && totalCost !== null
      ? ((price - totalCost) / price) * 100
      : null;

    const costByName = new Map((cost.lines || []).map((l) => [l.ingredient, l]));

    mount(detailHost, Card({},
      CardHeader({
        title: h('div', { class: 'card-title' },
          d.recipe.name,
          price !== null
            ? h('span', { class: 't-soft', style: { fontWeight: '400', fontSize: '13px', marginLeft: '8px' } },
              money(price).textContent)
            : null),
        desc: 'Cost per serving vs. menu price',
        right: [
          Button({
            size: 'sm',
            onClick: () => openEditRecipeModal(d.recipe, d.lines, () => reload(selected.pk)),
          }, 'Edit recipe'),
          menuItem
            ? (menuItem.sellable ? Pill('good', 'available') : Pill('crit', 'unavailable'))
            : Pill('flat', 'not on the menu'),
        ],
      }),

      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)', marginBottom: '14px' } },
        Card({ sunk: true }, Stat({
          value: totalCost === null ? nodata('cannot be costed') : money(totalCost),
          label: complete ? 'Ingredient cost' : 'Ingredient cost (partial)',
        })),
        Card({ sunk: true }, Stat({
          value: margin === null ? nodata('not calculable') : pct(margin, '', 0),
          label: 'Gross margin',
        })),
        Card({ sunk: true }, Stat({
          value: count(d.lines.length),
          label: 'Ingredients used',
        }))),

      unpriced.length
        ? h('div', { class: 'callout callout-warn' },
          h('p', {},
            h('strong', {}, `${unpriced.length} ingredient${unpriced.length === 1 ? '' : 's'} without a cost. `),
            'The total above excludes ',
            unpriced.map((u) => (typeof u === 'string' ? u : u.name || u.ingredient)).join(', '),
            ' — confirm their costs under Verification before trusting the margin.'))
        : null,

      d.problems && d.problems.length
        ? h('div', { class: 'callout callout-crit' },
          h('p', {}, h('strong', {}, 'This recipe has problems: '),
            d.problems.map((p) => (typeof p === 'string' ? p : p.message || JSON.stringify(p))).join('; ')))
        : null,

      Table({
        columns: [
          {
            key: 'ingredient',
            label: 'Ingredient',
            render: (l) => h('div', {},
              h('span', {}, l.name),
              l.label && l.label !== l.name
                ? h('div', { class: 't-xs t-faint' }, l.label)
                : null),
          },
          {
            key: 'amount',
            label: 'Amount',
            render: (l) => h('span', { class: 'mono t-sm' }, qty(l.amount, l.unit)),
          },
          {
            key: 'cost',
            label: 'Cost',
            render: (l) => {
              const priced = costByName.get(l.name);
              return h('span', { class: 'mono t-sm' },
                priced ? money(priced.cost) : nodata('no cost set'));
            },
          },
          {
            key: 'confirmed',
            label: 'Amount confirmed',
            render: (l) => (l.qtyStatus === 'verified'
              ? Pill('good', 'confirmed')
              : Pill('warn', 'estimate')),
          },
        ],
        rows: d.lines,
        empty: EmptyState({
          icon: '☰',
          title: 'This recipe has no lines',
          description: 'Add ingredients to it before it can be costed or sold.',
        }),
      })));
  }

  await reload();

  return h('div', { class: 'grid', style: { gridTemplateColumns: '340px 1fr' } },
    Card({ flush: true },
      h('div', {
        style: {
          padding: '14px 16px', borderBottom: '1px solid var(--line)',
          display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '8px',
        },
      },
      h('div', {},
        h('div', { class: 'card-title', style: { fontSize: '14px' } }, 'Recipes'),
        countHost),
      Button({ size: 'sm', onClick: () => openAddRecipeModal((newPk) => reload(newPk)) }, '+ Add')),
      listHost),
    detailHost);
}

/**
 * A searchable ingredient field — replaces a plain <select> for a list that
 * can run past 100 items. Text input shows the current selection; typing
 * filters a dropdown of matches; picking one or clicking away closes it.
 * Same "type to filter, click a row" pattern already proven in
 * Verification.js's ingredient-mapping modal, pulled out here so the recipe
 * line-row builder isn't a second copy of it.
 *
 * @returns {{el:Node, getPk:()=>number|null}}
 */
function ingredientSearchField(ingredientOptions, initialPk) {
  let selectedPk = initialPk ?? null;
  const initial = ingredientOptions.find((i) => i.pk === initialPk);
  const input = h('input', { type: 'text', placeholder: 'Search ingredient…', value: initial ? initial.name : '', autocomplete: 'off' });
  const menu = h('div', {
    style: {
      position: 'absolute', zIndex: '20', top: '100%', left: '0', right: '0',
      maxHeight: '220px', overflowY: 'auto', background: 'var(--surface)', border: '1px solid var(--line)',
      borderRadius: 'var(--radius-sm)', boxShadow: 'var(--shadow)', display: 'none', marginTop: '2px',
    },
  });
  const wrap = h('div', { style: { position: 'relative' } }, input, menu);

  function drawMenu(query) {
    const q = (query || '').trim().toLowerCase();
    const rows = (q ? ingredientOptions.filter((i) => i.name.toLowerCase().includes(q)) : ingredientOptions).slice(0, 50);
    if (rows.length === 0) {
      mount(menu, h('div', { class: 't-xs t-faint', style: { padding: '8px 12px' } }, 'No match'));
    } else {
      mount(menu, ...rows.map((i) => h('div', {
        class: 'row-click', style: { padding: '7px 12px', fontSize: '12.5px' },
        onMousedown: (e) => {
          e.preventDefault();
          selectedPk = i.pk;
          input.value = i.name;
          menu.style.display = 'none';
        },
      }, i.name)));
    }
    menu.style.display = '';
  }

  input.addEventListener('focus', () => drawMenu(input.value));
  input.addEventListener('input', () => { selectedPk = null; drawMenu(input.value); });
  input.addEventListener('blur', () => { menu.style.display = 'none'; });

  return { el: wrap, getPk: () => selectedPk };
}

/**
 * One ingredient line row — shared by Add recipe and Edit recipe, which
 * used to each carry their own near-identical copy of this. `initial`, when
 * given, pre-fills from an existing recipe line (Edit); omitted, it's a
 * blank row (Add, or "+ Add ingredient line" in either modal).
 */
function buildLineRow(ingredientOptions, lineRows, linesHost, initial) {
  const ingField = ingredientSearchField(ingredientOptions, initial ? initial.ingredientId : null);
  const labelInput = h('input', { type: 'text', placeholder: 'label (defaults to ingredient name)', value: initial ? initial.label : '' });
  const qtyInput = h('input', {
    type: 'number', inputmode: 'decimal', min: '0.01', step: '0.01', placeholder: 'qty', class: 'no-spinner',
    value: initial && initial.amount != null ? String(initial.amount) : '',
  });
  const removeBtn = h('button', { class: 'icon-btn', title: 'Remove line' }, '✕');
  const num = h('span', { class: 'recipe-line-num' }, String(lineRows.length + 1));
  const row = h('div', { class: 'recipe-line-row' },
    num, ingField.el, labelInput, qtyInput, removeBtn);
  removeBtn.addEventListener('click', () => {
    row.remove();
    lineRows.splice(lineRows.indexOf(entry), 1);
    lineRows.forEach((r, i) => { r.num.textContent = String(i + 1); });
  });
  const entry = {
    row, labelInput, qtyInput, num, getIngredientPk: ingField.getPk,
  };
  lineRows.push(entry);
  linesHost.appendChild(row);
}

/**
 * POST /console/recipes — a menu recipe or a sub-recipe, with its lines, in
 * one call. Every ingredient_pk sent is a real FK the backend validates; an
 * unresolved ingredient belongs in the Ingredient Queue (Verification tab),
 * not typed here as free text — that is exactly the distinction that keeps
 * a recipe's cost and deduction path trustworthy.
 */
export function openAddRecipeModal(onCreated) {
  let ingredientOptions = [];
  const nameInput = h('input', { type: 'text', placeholder: 'e.g. Chicken Salpicao' });
  const kindSelect = h('select', {},
    h('option', { value: 'menu' }, 'Menu item recipe'),
    h('option', { value: 'sub_recipe' }, 'Sub-recipe (house sauce / prep)'));
  const yieldQtyInput = h('input', { type: 'number', min: '0.01', step: '0.01', value: '1', class: 'no-spinner' });
  const yieldUnitInput = h('input', { type: 'text', value: 'serving' });
  const linesHost = h('div', { style: { display: 'flex', flexDirection: 'column', gap: '8px' } });
  const err = ModalError();
  const submit = Button({ variant: 'primary' }, 'Create recipe');
  const lineRows = [];

  const addLineBtn = Button({ size: 'sm', onClick: () => buildLineRow(ingredientOptions, lineRows, linesHost) }, '+ Add ingredient line');

  submit.addEventListener('click', async () => {
    err.textContent = ''; err.style.display = 'none';
    if (!nameInput.value.trim()) { err.textContent = 'Enter a recipe name.'; err.style.display = ''; return; }
    const lines = [];
    for (const r of lineRows) {
      const pk = r.getIngredientPk();
      if (!pk) continue;
      const qtyBase = Number(r.qtyInput.value);
      if (!(qtyBase > 0)) { err.textContent = 'Every line needs a quantity greater than zero.'; err.style.display = ''; return; }
      const ing = ingredientOptions.find((i) => i.pk === pk);
      lines.push({
        ingredientPk: ing.pk,
        displayLabel: r.labelInput.value.trim() || ing.name,
        qtyBase,
      });
    }
    if (lines.length === 0) { err.textContent = 'Add at least one ingredient line.'; err.style.display = ''; return; }

    submit.disabled = true;
    try {
      const r = await createRecipe({
        name: nameInput.value.trim(),
        kind: kindSelect.value,
        yieldQty: Number(yieldQtyInput.value) || 1,
        yieldUnit: yieldUnitInput.value.trim() || 'serving',
        lines,
      });
      modal.close();
      const problems = r.problems || [];
      toast(problems.length
        ? `Recipe created, but has ${problems.length} problem(s) to resolve.`
        : 'Recipe created.', problems.length ? 'warn' : 'good');
      onCreated(r.id);
    } catch (e) {
      err.textContent = e.message; err.style.display = ''; submit.disabled = false;
    }
  });

  const modal = Modal({
    title: 'Add recipe',
    desc: 'Every line must point at a real, already-existing ingredient.',
    wide: true,
    onClose: () => {},
  },
  h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Name', nameInput),
  h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr 1fr', gap: '10px', marginBottom: '10px' } },
    h('label', { class: 'field' }, 'Kind', kindSelect),
    h('label', { class: 'field' }, 'Yields', yieldQtyInput),
    h('label', { class: 'field' }, 'Yield unit', yieldUnitInput)),
  h('div', { class: 'eyebrow', style: { marginTop: '6px' } }, 'Ingredient lines'),
  h('div', { class: 'recipe-line-row recipe-line-head' },
    h('span', {}), h('span', {}, 'Ingredient'), h('span', {}, 'Label (optional)'), h('span', {}, 'Qty'), h('span', {})),
  linesHost,
  h('div', { style: { margin: '8px 0 14px' } }, addLineBtn),
  err,
  submit);

  listIngredients({ limit: 500, withRunway: false }).then((list) => {
    ingredientOptions = list;
    buildLineRow(ingredientOptions, lineRows, linesHost);
  });
}

/**
 * PATCH /console/recipes/{pk} — edit an existing recipe's name, yield and
 * full ingredient line set. Same line-row editor as Add recipe, pre-filled
 * from the recipe's current lines. Replacing the whole line set (rather
 * than diffing adds/removes/changes) matches exactly what the backend
 * endpoint does — see its own docstring for why that's safe against an
 * order already reserved against the old lines.
 */
export function openEditRecipeModal(recipe, currentLines, onSaved) {
  let ingredientOptions = [];
  const nameInput = h('input', { type: 'text', value: recipe.name });
  const yieldQtyInput = h('input', { type: 'number', min: '0.01', step: '0.01', value: String(recipe.yieldQty ?? 1), class: 'no-spinner' });
  const yieldUnitInput = h('input', { type: 'text', value: recipe.yieldUnit || 'serving' });
  const linesHost = h('div', { style: { display: 'flex', flexDirection: 'column', gap: '8px' } });
  const err = ModalError();
  const submit = Button({ variant: 'primary' }, 'Save changes');
  const lineRows = [];

  const addLineBtn = Button({ size: 'sm', onClick: () => buildLineRow(ingredientOptions, lineRows, linesHost) }, '+ Add ingredient line');

  submit.addEventListener('click', async () => {
    err.textContent = ''; err.style.display = 'none';
    if (!nameInput.value.trim()) { err.textContent = 'Enter a recipe name.'; err.style.display = ''; return; }
    const lines = [];
    for (const r of lineRows) {
      const pk = r.getIngredientPk();
      if (!pk) continue;
      const qtyBase = Number(r.qtyInput.value);
      if (!(qtyBase > 0)) { err.textContent = 'Every line needs a quantity greater than zero.'; err.style.display = ''; return; }
      const ing = ingredientOptions.find((i) => i.pk === pk);
      lines.push({
        ingredientPk: ing.pk,
        displayLabel: r.labelInput.value.trim() || ing.name,
        qtyBase,
      });
    }
    if (lines.length === 0) { err.textContent = 'Add at least one ingredient line.'; err.style.display = ''; return; }

    submit.disabled = true;
    try {
      const r = await editRecipe(recipe.pk, {
        name: nameInput.value.trim(),
        yieldQty: Number(yieldQtyInput.value) || 1,
        yieldUnit: yieldUnitInput.value.trim() || 'serving',
        lines,
      });
      modal.close();
      const problems = r.problems || [];
      toast(problems.length
        ? `Recipe updated, but has ${problems.length} problem(s) to resolve.`
        : 'Recipe updated — future orders use the new lines; orders already reserved are unaffected.', problems.length ? 'warn' : 'good');
      onSaved();
    } catch (e) {
      err.textContent = e.message; err.style.display = ''; submit.disabled = false;
    }
  });

  const modal = Modal({
    title: `Edit — ${recipe.name}`,
    desc: 'Changes apply to orders reserved from now on. Orders already punched keep the quantities they were reserved with.',
    wide: true,
    onClose: () => {},
  },
  h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Name', nameInput),
  h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '10px', marginBottom: '10px' } },
    h('label', { class: 'field' }, 'Yields', yieldQtyInput),
    h('label', { class: 'field' }, 'Yield unit', yieldUnitInput)),
  h('div', { class: 'eyebrow', style: { marginTop: '6px' } }, 'Ingredient lines'),
  h('div', { class: 'recipe-line-row recipe-line-head' },
    h('span', {}), h('span', {}, 'Ingredient'), h('span', {}, 'Label (optional)'), h('span', {}, 'Qty'), h('span', {})),
  linesHost,
  h('div', { style: { margin: '8px 0 14px' } }, addLineBtn),
  err,
  submit);

  listIngredients({ limit: 500, withRunway: false }).then((list) => {
    ingredientOptions = list;
    if (currentLines.length === 0) buildLineRow(ingredientOptions, lineRows, linesHost);
    else for (const l of currentLines) buildLineRow(ingredientOptions, lineRows, linesHost, l);
  });
}
