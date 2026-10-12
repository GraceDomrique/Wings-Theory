import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError, confirmModal } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading, toast } from '../../components/ui/Feedback.js';
import { money, pct, nodata } from '../../lib/format.js';
import { getCombinations } from '../../lib/api/reports.js';
import { listMenuItems } from '../../lib/api/menu.js';
import { session } from '../../lib/api/client.js';
import {
  createDraftCombo, listDraftCombos, reviewDraftCombo,
  getComboPricingConfig, setComboPricingConfig,
  getComboPreview, uploadComboImage, listCombos, publishCombo, updateCombo, setComboVisibility,
} from '../../lib/api/comboPricing.js';

/**
 * Menu & Recipes > Combos — spec section 11: "Which products are commonly
 * purchased together, and can we create a sensible combo?"
 *
 * Three stages live on this one page, in the order an admin actually walks
 * through them:
 *   1. "Frequently bought together" — the FP-growth pairing output (read).
 *   2. "Combo ideas" — a saved, cost-based draft an admin can approve/reject
 *      without touching the live menu at all (wt/combo_pricing.py).
 *   3. "Published combos" — the step migration 0036 deliberately left out:
 *      turning an approved idea into a real POS-sellable menu_item
 *      (wt/combos.py). The SAME builder modal (comboBuilderModal) handles
 *      "suggest from a pair", "publish an approved idea" and "edit an
 *      already-published combo" — one place that composes items, previews
 *      cost, uploads an image, and either saves a draft or publishes.
 *
 * Deliberately never shows support/confidence/lift as the primary number
 * anywhere on this page (spec: owner/admin business view says "Observed
 * together X% of the time", not FP-growth terminology) — the technical
 * figures stay tucked behind this page's own "Technical details" accordion
 * (the separate read-only Product Combinations report page was removed as a
 * duplicate of this same section; nothing it showed is lost).
 */
// money() returns a Text node (made for use as an h() child), not a string —
// interpolating it into a template literal stringifies the node itself
// ("[object Text]"). This is the plain-string peso formatter for the few
// spots below that build a string (an <option> label, a joined summary
// line) rather than passing a child node to h().
function pesoStr(v) {
  return '₱' + Number(v ?? 0).toFixed(2);
}

export async function render(ctx) {
  const host = h('div');
  let menuItemsCache = null;
  // 2026-10 fix: this page is only reachable from Admin's own nav tree
  // (Owner never has a link to it — see nav.js), and combo.publish is
  // already admin-only server-side (wt/auth.py), so this was never
  // exploitable in practice. It was still the one write-capable page in
  // the console with no in-component role check of its own (every other
  // page with admin-only actions, e.g. PurchaseOrders.js, gates its
  // buttons the same way) — added here for the same defense-in-depth the
  // backend already provides, not because a real gap was reachable.
  const isAdmin = (session.user || {}).role === 'admin';

  async function getMenuItems() {
    if (!menuItemsCache) menuItemsCache = (await listMenuItems()).items;
    return menuItemsCache;
  }

  let comboConfig = null;
  let lookbackDays = 365;

  async function draw() {
    mount(host, Loading('Loading…'));
    const [combosReport, drafts, config, published] = await Promise.all([
      getCombinations(lookbackDays), listDraftCombos(), getComboPricingConfig().catch(() => null),
      listCombos().catch(() => []),
    ]);
    comboConfig = config;
    mount(host,
      combosSection(combosReport),
      draftsCard(drafts),
      publishedCard(published));
  }

  async function act(fn) {
    try { await fn(); await draw(); } catch (e) { toast(e.message, 'crit'); }
  }

  // -------------------------------------------------------------------
  // 1. Frequently bought together — folds in what used to be the separate
  // "Product Combinations" report page (removed: it was the same
  // FP-Growth data as this section, just read-only and one click further
  // away). The lookback picker and the technical-detail accordion it had
  // live here now instead.
  // -------------------------------------------------------------------
  const LOOKBACK_OPTIONS = [
    { value: '90', label: 'Last 90 days' },
    { value: '180', label: 'Last 6 months' },
    { value: '365', label: 'Last 12 months' },
  ];

  function lookbackPicker() {
    const select = h('select', {
      onChange: (e) => { lookbackDays = Number(e.target.value); draw(); },
    }, ...LOOKBACK_OPTIONS.map((o) => h('option', {
      value: o.value, selected: Number(o.value) === lookbackDays ? 'selected' : undefined,
    }, o.label)));
    return h('label', { class: 'field' }, select);
  }

  function combosSection(combos) {
    if (combos.insufficientData) {
      return Card({},
        CardHeader({ title: 'Customers often buy together', desc: 'FP-Growth over real order history', right: lookbackPicker() }),
        EmptyState({ icon: '✦', title: 'Not enough order history yet', description: `Needs at least ${combos.required ?? 30} orders to find a reliable pattern — has ${combos.transactionCount ?? 0} so far.` }));
    }
    if (combos.combos.length === 0) {
      return Card({},
        CardHeader({ title: 'Customers often buy together', desc: 'FP-Growth over real order history', right: lookbackPicker() }),
        EmptyState({ icon: '✦', title: 'No reliable pairings found', description: `Enough orders were analysed (${combos.transactionCount}), but no pair cleared the lift and support thresholds. That is a result, not a gap.` }));
    }
    return Card({},
      CardHeader({ title: 'Customers often buy together', desc: 'Frequently appear in the same order, across your full order history', right: lookbackPicker() }),
      Table({
        columns: [
          { key: 'pair', label: 'Pair', render: (c) => `${c.itemA} + ${c.itemB}` },
          { key: 'observed', label: 'Observed together', render: (c) => h('span', { class: 'mono t-sm' }, pct(c.confidence === null ? null : c.confidence * 100, 'n/a', 0)) },
          {
            key: 'action', label: '',
            render: (c) => c.itemAMenuItemId && c.itemBMenuItemId
              ? Button({ size: 'sm', onClick: () => openBuilder({ mode: 'from-pair', pair: c }) }, 'View opportunity')
              : h('span', { class: 't-xs t-faint' }, 'not available'),
          },
        ],
        rows: combos.combos,
      }),
      h('div', { class: 'accordion', style: { marginTop: '10px' } },
        h('details', {},
          h('summary', { class: 't-xs t-soft' }, 'Technical details'),
          h('div', { class: 't-xs t-faint', style: { marginTop: '8px' } },
            `FP-Growth over ${combos.transactionCount} real orders (last ${lookbackDays} days). `,
            combos.combos.map((c) => `${c.itemA} + ${c.itemB}: lift ${c.lift === null ? 'n/a' : c.lift.toFixed(2) + '×'}, support ${c.support === null ? 'n/a' : c.support}`).join(' · ')))));
  }

  // -------------------------------------------------------------------
  // 2. Combo ideas (drafts)
  // -------------------------------------------------------------------
  function draftsCard(drafts) {
    return Card({ style: { marginTop: '14px' } },
      CardHeader({ title: 'Combo ideas', desc: 'Saved for review — publishing to the POS is a separate, explicit step' }),
      drafts.length === 0
        ? EmptyState({ icon: '✦', title: 'No combo ideas yet', description: 'Open "View opportunity" above to start one.', tight: true })
        : h('div', {}, ...drafts.map((d) => h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '9px 0', borderBottom: '1px solid var(--line-soft)' } },
          h('div', {},
            h('div', { class: 't-sm', style: { fontWeight: '600' } }, d.name),
            h('div', { class: 't-xs t-soft' }, d.chosenPrice !== null ? money(d.chosenPrice) : nodata('no price chosen'))),
          h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center' } },
            Pill(d.status === 'approved' ? 'good' : d.status === 'rejected' ? 'flat' : 'warn', d.status),
            !isAdmin ? null
              : d.status === 'draft'
              ? h('div', { style: { display: 'flex', gap: '6px' } },
                Button({ size: 'sm', onClick: () => act(() => reviewDraftCombo(d.id, 'approved')) }, 'Approve'),
                Button({ size: 'sm', onClick: () => act(() => reviewDraftCombo(d.id, 'rejected')) }, 'Reject'))
              : d.status === 'approved'
                ? Button({ size: 'sm', variant: 'primary', onClick: () => openBuilder({ mode: 'from-draft', draft: d }) }, 'Build & publish to POS')
                : null)))));
  }

  // -------------------------------------------------------------------
  // 3. Published combos
  // -------------------------------------------------------------------
  function publishedCard(combos) {
    return Card({ style: { marginTop: '14px' } },
      CardHeader({ title: 'Published combos', desc: 'Live on the POS — behaves exactly like any other menu item' }),
      isAdmin ? h('div', { style: { marginBottom: '10px' } },
        Button({ onClick: () => openBuilder({ mode: 'create' }) }, '+ Build a new combo')) : null,
      combos.length === 0
        ? EmptyState({ icon: '✦', title: 'No combos published yet', tight: true })
        : h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: '10px' } },
          ...combos.map((c) => comboTile(c))));
  }

  function comboTile(c) {
    return h('div', { class: 'card', style: { padding: '10px' } },
      h('div', {
        style: {
          width: '100%', height: '90px', borderRadius: '8px', marginBottom: '8px',
          background: 'var(--surface-2, #f1ece3)', display: 'flex', alignItems: 'center',
          justifyContent: 'center', overflow: 'hidden',
        },
      }, c.imagePath
        ? h('img', { src: c.imagePath, alt: c.name, style: { width: '100%', height: '100%', objectFit: 'cover' } })
        : h('b', { style: { fontSize: '28px', color: 'var(--t-faint)' } }, c.name.charAt(0))),
      h('div', { class: 't-sm', style: { fontWeight: '600' } }, c.name),
      h('div', { class: 't-xs t-soft' }, c.components.map((x) => `${x.quantity}× ${x.name}`).join(' + ')),
      h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '6px' } },
        h('span', { class: 'mono t-sm', style: { fontWeight: '600' } }, money(c.price)),
        Pill(c.posVisible ? 'good' : 'flat', c.posVisible ? 'On POS' : 'Hidden')),
      isAdmin ? h('div', { style: { display: 'flex', gap: '6px', marginTop: '8px' } },
        Button({ size: 'sm', onClick: () => openBuilder({ mode: 'edit', combo: c }) }, 'Edit'),
        Button({ size: 'sm', onClick: () => act(() => setComboVisibility(c.menuItemId, !c.posVisible)) }, c.posVisible ? 'Hide' : 'Show')) : null);
  }

  // -------------------------------------------------------------------
  // Combo builder — the one modal for all four entry points.
  // -------------------------------------------------------------------
  async function openBuilder({ mode, pair, draft, combo }) {
    const items = await getMenuItems();
    const pickable = items.filter((i) => !i.isUnli);

    const state = { rows: [] };
    if (mode === 'from-pair') {
      state.name = `${pair.itemA} + ${pair.itemB}`;
      state.category = 'Combos';
      state.price = null;
      state.imagePath = null;
      state.draftComboId = null;
      state.rows = [
        { menuItemId: pair.itemAMenuItemId, quantity: 1 },
        { menuItemId: pair.itemBMenuItemId, quantity: 1 },
      ];
    } else if (mode === 'from-draft') {
      state.name = draft.name;
      state.category = 'Combos';
      state.price = draft.chosenPrice;
      state.imagePath = null;
      state.draftComboId = draft.id;
      state.rows = [
        { menuItemId: draft.itemAMenuItemId, quantity: 1 },
        { menuItemId: draft.itemBMenuItemId, quantity: 1 },
      ];
    } else if (mode === 'edit') {
      state.name = combo.name;
      state.category = combo.category;
      state.price = combo.price;
      state.imagePath = combo.imagePath;
      state.draftComboId = null;
      state.menuItemPk = combo.menuItemId;
      state.rows = combo.components.map((c) => ({ menuItemId: c.menuItemId, quantity: c.quantity }));
    } else {
      state.name = '';
      state.category = 'Combos';
      state.price = null;
      state.imagePath = null;
      state.draftComboId = null;
      state.rows = [{ menuItemId: pickable[0]?.pk ?? null, quantity: 1 }];
    }

    const err = ModalError();
    const nameInput = h('input', { type: 'text', value: state.name });
    const categoryInput = h('input', { type: 'text', value: state.category });
    const priceInput = h('input', { type: 'number', step: '0.01', min: '0', value: state.price !== null ? String(state.price) : '' });

    const rowsHost = h('div', {});
    const previewHost = h('div', { style: { marginTop: '12px' } });
    const imagePreview = h('div', {
      style: {
        width: '96px', height: '96px', borderRadius: '8px', background: 'var(--surface-2, #f1ece3)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden', flexShrink: '0',
      },
    }, state.imagePath ? h('img', { src: state.imagePath, style: { width: '100%', height: '100%', objectFit: 'cover' } }) : '🖼');
    const fileInput = h('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp' });
    const imageStatus = h('div', { class: 't-xs t-soft', style: { marginTop: '4px' } }, state.imagePath ? 'Image set.' : 'No image yet.');

    fileInput.addEventListener('change', async () => {
      const file = fileInput.files[0];
      if (!file) return;
      if (file.size > 5 * 1024 * 1024) { imageStatus.textContent = 'Image is too large (max 5 MB).'; return; }
      imageStatus.textContent = 'Uploading…';
      try {
        const base64 = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = reject;
          reader.readAsDataURL(file);
        });
        state.imagePath = await uploadComboImage(base64);
        mount(imagePreview, h('img', { src: state.imagePath, style: { width: '100%', height: '100%', objectFit: 'cover' } }));
        imageStatus.textContent = 'Image uploaded.';
      } catch (e) { imageStatus.textContent = e.message; }
    });

    function removeImage() {
      state.imagePath = null;
      mount(imagePreview, '🖼');
      imageStatus.textContent = 'No image yet.';
    }

    function itemOptions(selectedId) {
      return pickable.map((i) => h('option', {
        value: String(i.pk), selected: i.pk === selectedId ? 'selected' : undefined,
      }, `${i.name} (${pesoStr(i.price)})`));
    }

    function renderRows() {
      mount(rowsHost, ...state.rows.map((row, idx) => {
        const select = h('select', {}, ...itemOptions(row.menuItemId));
        select.value = String(row.menuItemId ?? '');
        select.addEventListener('change', () => { row.menuItemId = Number(select.value); schedulePreview(); });
        const qtyInput = h('input', { type: 'number', min: '1', max: '20', step: '1', value: String(row.quantity), style: { width: '70px' } });
        qtyInput.addEventListener('change', () => { row.quantity = Math.max(1, Number(qtyInput.value) || 1); schedulePreview(); });
        const removeBtn = Button({ size: 'sm', variant: 'ghost', onClick: () => { state.rows.splice(idx, 1); renderRows(); schedulePreview(); } }, '✕');
        removeBtn.disabled = state.rows.length <= 2;
        return h('div', { class: 'combo-item-row' },
          h('span', { class: 'combo-item-row-num' }, String(idx + 1)),
          select,
          h('span', { class: 'combo-item-row-x' }, '×'), qtyInput,
          removeBtn);
      }),
      Button({ size: 'sm', variant: 'ghost', onClick: () => { state.rows.push({ menuItemId: pickable[0]?.pk ?? null, quantity: 1 }); renderRows(); schedulePreview(); } }, '+ Add item'));
    }
    renderRows();

    let previewTimer = null;
    function schedulePreview() {
      clearTimeout(previewTimer);
      previewTimer = setTimeout(runPreview, 250);
    }

    async function runPreview() {
      const valid = state.rows.filter((r) => r.menuItemId);
      if (valid.length < 2) {
        mount(previewHost, h('div', { class: 't-xs t-soft' }, 'Add at least 2 items to see a preview.'));
        return;
      }
      mount(previewHost, Loading('Working out cost & price…'));
      try {
        const p = await getComboPreview(valid);
        if (priceInput.value === '' && p.price.costBasedPrice !== null) {
          priceInput.value = String(p.price.costBasedPrice);
        }
        const nameForPreview = nameInput.value.trim() || 'Combo';
        mount(previewHost,
          h('div', { style: { display: 'flex', gap: '16px', flexWrap: 'wrap' } },
            // POS card mockup — same information a real POS tile shows,
            // never a different layout the live tile doesn't actually use.
            h('div', { class: 'card', style: { padding: '10px', width: '160px' } },
              h('div', {
                style: {
                  width: '100%', height: '70px', borderRadius: '8px', marginBottom: '6px',
                  background: 'var(--surface-2, #f1ece3)', display: 'flex', alignItems: 'center',
                  justifyContent: 'center', overflow: 'hidden',
                },
              }, state.imagePath
                ? h('img', { src: state.imagePath, style: { width: '100%', height: '100%', objectFit: 'cover' } })
                : h('b', { style: { fontSize: '22px', color: 'var(--t-faint)' } }, nameForPreview.charAt(0))),
              h('div', { class: 't-sm', style: { fontWeight: '600' } }, nameForPreview),
              h('div', { class: 'mono t-sm' }, money(priceInput.value ? Number(priceInput.value) : (p.price.costBasedPrice ?? 0))),
              h('div', { class: 't-xs t-faint', style: { marginTop: '2px' } }, 'POS preview')),
            h('div', { style: { flex: '1', minWidth: '200px' } },
              h('div', { class: 't-sm t-soft' }, 'Estimated ingredient + packaging cost'),
              h('div', { style: { fontSize: '18px', fontWeight: '600' } },
                p.cost.totalCost !== null ? money(p.cost.totalCost) : nodata('missing cost data')),
              p.price.range
                ? h('div', { class: 't-xs t-soft', style: { marginTop: '4px' } },
                  'Suggested price range: ', money(p.price.range[0]), ' – ', money(p.price.range[1]))
                : p.price.issue
                  ? h('div', { class: 't-xs t-soft', style: { marginTop: '4px' } }, p.price.issue)
                  : null,
              p.cost.unpricedIngredients && p.cost.unpricedIngredients.length
                ? h('div', { class: 'callout callout-warn', style: { marginTop: '8px' } },
                  h('p', {}, 'Some ingredients have no confirmed cost yet: ', p.cost.unpricedIngredients.join(', '), '. This estimate is a floor, not the full cost.'))
                : null,
              h('details', { style: { marginTop: '8px' } },
                h('summary', { class: 't-xs t-soft' }, 'Technical detail'),
                h('div', { class: 't-xs t-faint', style: { marginTop: '4px' } },
                  p.cost.items.map((i) => `${i.quantity}× ${i.name}: ${i.ingredientCost !== null ? pesoStr(i.ingredientCost) : 'n/a'}`).join(' · '))))));
      } catch (e) {
        mount(previewHost, h('div', { class: 't-xs', style: { color: 'var(--crit)' } }, e.message));
      }
    }
    runPreview();

    function collectComponents() {
      const valid = state.rows.filter((r) => r.menuItemId);
      if (valid.length < 2) throw new Error('A combo needs at least 2 items.');
      return valid;
    }

    const saveDraftBtn = mode === 'from-pair' || mode === 'from-draft'
      ? Button({}, 'Save as draft idea only') : null;
    if (saveDraftBtn) {
      saveDraftBtn.addEventListener('click', async () => {
        err.textContent = ''; err.style.display = 'none';
        try {
          const valid = collectComponents();
          await createDraftCombo({
            itemAId: valid[0].menuItemId, itemBId: valid[1].menuItemId,
            name: nameInput.value.trim() || state.name,
            chosenPrice: priceInput.value ? Number(priceInput.value) : null,
            pairStats: pair ? { lift: pair.lift, confidence: pair.confidence, support: pair.support } : null,
            clientRequestId: `combo-draft-${valid[0].menuItemId}-${valid[1].menuItemId}-${Date.now()}`,
          });
          modal.close();
          toast('Saved as a combo idea.', 'good');
          draw();
        } catch (e) { err.textContent = e.message; err.style.display = ''; }
      });
    }

    async function doSave() {
      err.textContent = ''; err.style.display = 'none';
      primaryBtn.disabled = true;
      try {
        const valid = collectComponents();
        const payload = {
          name: nameInput.value.trim(),
          category: categoryInput.value.trim(),
          price: Number(priceInput.value || 0),
          components: valid,
          imagePath: state.imagePath,
        };
        if (!payload.name) throw new Error('Combo name is required.');
        if (!payload.category) throw new Error('Combo category is required.');

        if (mode === 'edit') {
          await updateCombo(state.menuItemPk, payload);
          toast('Combo updated.', 'good');
        } else {
          await publishCombo({ ...payload, draftComboId: state.draftComboId });
          toast('Combo published to the POS.', 'good');
        }
        modal.close();
        draw();
      } catch (e) {
        err.textContent = e.message; err.style.display = '';
        primaryBtn.disabled = false;
        throw e;
      }
    }

    const primaryBtn = Button({ variant: 'primary' }, mode === 'edit' ? 'Save changes' : 'Publish to POS');
    primaryBtn.addEventListener('click', () => {
      err.textContent = ''; err.style.display = 'none';
      if (mode === 'edit') { doSave().catch(() => {}); return; }
      confirmModal({
        title: 'Publish to POS',
        body: `"${nameInput.value.trim() || state.name}" will immediately appear as a sellable item on the POS at ₱${priceInput.value || '0'}. Continue?`,
        confirmLabel: 'Publish',
        onConfirm: doSave,
      });
    });

    const pricingRules = pricingRulesSection(comboConfig || {}, (saved) => { comboConfig = saved; runPreview(); });

    const modal = Modal({
      title: mode === 'edit' ? 'Edit combo' : mode === 'create' ? 'Build a combo' : 'View opportunity',
      desc: 'Choose the items, set a price and image, preview it, then publish — it will appear on the POS exactly like any other menu item.',
      wide: true,
      onClose: () => {},
    },
    h('div', { class: 'combo-builder-grid' },
      h('div', { class: 'combo-builder-col' },
        h('div', { class: 'eyebrow' }, 'Details'),
        h('label', { class: 'field' }, 'Name', nameInput),
        h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '10px', marginTop: '10px' } },
          h('label', { class: 'field' }, 'Category', categoryInput),
          h('label', { class: 'field' }, 'Price (₱)', priceInput)),

        h('div', { class: 'eyebrow', style: { marginTop: '18px' } }, 'Items in this combo'),
        rowsHost,

        h('div', { class: 'eyebrow', style: { marginTop: '18px' } }, 'Image'),
        h('div', { style: { display: 'flex', gap: '12px', alignItems: 'flex-start' } },
          imagePreview,
          h('div', {},
            h('div', {}, fileInput),
            imageStatus,
            state.imagePath ? Button({ size: 'sm', variant: 'ghost', onClick: removeImage }, 'Remove image') : null)),

        pricingRules),
      h('div', { class: 'combo-builder-col combo-builder-preview' },
        h('div', { class: 'eyebrow' }, 'Live preview'),
        previewHost)),
    err,
    h('div', { style: { marginTop: '14px', display: 'flex', justifyContent: 'flex-end', gap: '8px' } },
      saveDraftBtn, primaryBtn));

    priceInput.addEventListener('input', () => { if (previewHost.firstChild) runPreview(); });
    nameInput.addEventListener('input', () => { if (previewHost.firstChild) runPreview(); });
  }

  /**
   * Combo pricing settings — PATCH /console/combo-pricing-config
   * (wt/combo_pricing.py), admin-only (inventory.write). Lives INSIDE the
   * builder modal as a collapsible "Pricing rules" section rather than its
   * own standalone card: it only ever affects the price-range suggestion
   * the same modal's preview computes, so the admin sets it right where it
   * takes effect instead of hunting for a disconnected settings page. Every
   * field is None ("not configured") until set here. The form always
   * submits its full current state; since the backend only writes the
   * fields present in the body, a full resend is idempotent.
   */
  function pricingRulesSection(config, onSaved) {
    const num = (v) => (v === null || v === undefined ? '' : String(v));

    const targetInput = h('input', { type: 'number', step: '0.1', min: '1', max: '100', value: num(config.targetFoodCostPct), placeholder: 'e.g. 30' });
    const laborMethodSelect = h('select', {},
      h('option', { value: '' }, 'Not set'),
      h('option', { value: 'fixed_per_serving', selected: config.laborMethod === 'fixed_per_serving' ? 'selected' : undefined }, 'Fixed amount per serving'),
      h('option', { value: 'pct_of_ingredient_cost', selected: config.laborMethod === 'pct_of_ingredient_cost' ? 'selected' : undefined }, '% of ingredient cost'));
    laborMethodSelect.value = config.laborMethod || '';
    const laborFixedInput = h('input', { type: 'number', step: '0.01', min: '0', value: num(config.laborFixedAmount), placeholder: '0.00' });
    const laborPctInput = h('input', { type: 'number', step: '0.1', min: '0', max: '100', value: num(config.laborPct), placeholder: 'e.g. 15' });
    const overheadInput = h('input', { type: 'number', step: '0.1', min: '0', max: '100', value: num(config.overheadPct), placeholder: 'e.g. 10' });
    const minPriceInput = h('input', { type: 'number', step: '0.01', min: '0', value: num(config.preferredMinPrice), placeholder: 'optional' });
    const maxPriceInput = h('input', { type: 'number', step: '0.01', min: '0', value: num(config.preferredMaxPrice), placeholder: 'optional' });
    const maxDiscountInput = h('input', { type: 'number', step: '0.1', min: '0', max: '100', value: num(config.maxDiscountPct), placeholder: 'optional' });

    const laborFixedRow = h('label', { class: 'field', style: { marginTop: '10px' } }, 'Labor cost per serving (₱)', laborFixedInput);
    const laborPctRow = h('label', { class: 'field', style: { marginTop: '10px' } }, 'Labor cost (% of ingredient cost)', laborPctInput);
    const syncLaborRows = () => {
      laborFixedRow.style.display = laborMethodSelect.value === 'fixed_per_serving' ? '' : 'none';
      laborPctRow.style.display = laborMethodSelect.value === 'pct_of_ingredient_cost' ? '' : 'none';
    };
    syncLaborRows();
    laborMethodSelect.addEventListener('change', syncLaborRows);

    const err = h('div', { class: 'login-err', style: { display: 'none' } });
    const submit = Button({ variant: 'primary', size: 'sm' }, 'Save pricing settings');

    submit.addEventListener('click', async () => {
      err.style.display = 'none';
      const toFloat = (input) => (input.value.trim() === '' ? null : Number(input.value));
      const method = laborMethodSelect.value || null;
      const body = {
        target_food_cost_pct: toFloat(targetInput),
        labor_method: method,
        labor_fixed_amount: method === 'fixed_per_serving' ? toFloat(laborFixedInput) : null,
        labor_pct: method === 'pct_of_ingredient_cost' ? toFloat(laborPctInput) : null,
        overhead_pct: toFloat(overheadInput),
        preferred_min_price: toFloat(minPriceInput),
        preferred_max_price: toFloat(maxPriceInput),
        max_discount_pct: toFloat(maxDiscountInput),
      };
      if (body.preferred_min_price !== null && body.preferred_max_price !== null && body.preferred_min_price > body.preferred_max_price) {
        err.textContent = 'Preferred minimum price cannot be above the preferred maximum.';
        err.style.display = '';
        return;
      }
      submit.disabled = true;
      try {
        const saved = await setComboPricingConfig(body);
        toast('Pricing rules saved.', 'good');
        if (onSaved) onSaved(saved || config);
      } catch (e) {
        err.textContent = e.message; err.style.display = '';
      } finally {
        submit.disabled = false;
      }
    });

    return h('details', { class: 'combo-pricing-rules' },
      h('summary', {}, '⚙ Pricing rules', h('span', { class: 't-xs t-faint' }, ' — target food-cost %, overhead, labor and price bounds')),
      h('div', { class: 'combo-pricing-rules-body' },
        h('div', { class: 'grid', style: { gridTemplateColumns: '1fr 1fr', gap: '10px 16px' } },
          h('label', { class: 'field' }, 'Target food-cost (%)', targetInput),
          h('label', { class: 'field' }, 'Overhead (% of ingredient cost)', overheadInput),
          h('label', { class: 'field' }, 'Labor method', laborMethodSelect),
          h('div'),
          laborFixedRow, laborPctRow,
          h('label', { class: 'field' }, 'Preferred minimum price (₱)', minPriceInput),
          h('label', { class: 'field' }, 'Preferred maximum price (₱)', maxPriceInput),
          h('label', { class: 'field' }, 'Max discount off individual total (%)', maxDiscountInput)),
        err,
        h('div', { style: { marginTop: '12px' } }, submit)));
  }

  await draw();
  return host;
}
