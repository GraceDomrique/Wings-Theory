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
import { listRecipes, getRecipe, listMenuItems, createRecipe } from '../../lib/api/menu.js';
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
    const menuItem = priceByRecipe.get(d.recipe.id) || null;
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
        right: menuItem
          ? (menuItem.sellable ? Pill('good', 'available') : Pill('crit', 'unavailable'))
          : Pill('flat', 'not on the menu'),
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
 * POST /console/recipes — a menu recipe or a sub-recipe, with its lines, in
 * one call. Every ingredient_pk sent is a real FK the backend validates; an
 * unresolved ingredient belongs in the Ingredient Queue (Verification tab),
 * not typed here as free text — that is exactly the distinction that keeps
 * a recipe's cost and deduction path trustworthy.
 */
function openAddRecipeModal(onCreated) {
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

  function addLineRow() {
    const ingSelect = h('select', {},
      h('option', { value: '' }, 'Select ingredient…'),
      ...ingredientOptions.map((i) => h('option', { value: String(i.pk) }, i.name)));
    const labelInput = h('input', { type: 'text', placeholder: 'label (defaults to ingredient name)' });
    // No native spinner: the recipe app has no use for clicking +/- 0.01 a
    // hundred times to type a real quantity like "250" — a plain decimal
    // field is both simpler to build and faster to use.
    const qtyInput = h('input', { type: 'number', min: '0.01', step: '0.01', placeholder: 'qty', class: 'no-spinner' });
    const removeBtn = h('button', { class: 'icon-btn', title: 'Remove line' }, '✕');
    const num = h('span', { class: 'recipe-line-num' }, String(lineRows.length + 1));
    const row = h('div', { class: 'recipe-line-row' },
      num, ingSelect, labelInput, qtyInput, removeBtn);
    removeBtn.addEventListener('click', () => {
      row.remove();
      lineRows.splice(lineRows.indexOf(entry), 1);
      renumberRows();
    });
    const entry = { row, ingSelect, labelInput, qtyInput, num };
    lineRows.push(entry);
    linesHost.appendChild(row);
  }

  function renumberRows() {
    lineRows.forEach((r, i) => { r.num.textContent = String(i + 1); });
  }

  const addLineBtn = Button({ size: 'sm', onClick: addLineRow }, '+ Add ingredient line');

  submit.addEventListener('click', async () => {
    err.textContent = ''; err.style.display = 'none';
    if (!nameInput.value.trim()) { err.textContent = 'Enter a recipe name.'; err.style.display = ''; return; }
    const lines = [];
    for (const r of lineRows) {
      if (!r.ingSelect.value) continue;
      const qtyBase = Number(r.qtyInput.value);
      if (!(qtyBase > 0)) { err.textContent = 'Every line needs a quantity greater than zero.'; err.style.display = ''; return; }
      const ing = ingredientOptions.find((i) => i.pk === Number(r.ingSelect.value));
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
    addLineRow();
  });
}
