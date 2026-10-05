import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading, toast } from '../../components/ui/Feedback.js';
import { money, pct, nodata } from '../../lib/format.js';
import { getCombinations } from '../../lib/api/reports.js';
import {
  getComboPricingPreview, createDraftCombo, listDraftCombos, reviewDraftCombo,
  getComboPricingConfig, setComboPricingConfig,
} from '../../lib/api/comboPricing.js';

/**
 * Menu & Recipes > Combos — spec section 11: "Which products are commonly
 * purchased together, and can we create a sensible combo?" This is the
 * CREATE/manage side of FP-Growth's pairing output (wt/combo_pricing.py's
 * draft -> approve/reject workflow); Sales & History > Product Combinations
 * shows the same underlying pairs as a read-only business report — same
 * data, different job (spec section 43: "one metric, one home" does not
 * mean one screen when the two screens do different things).
 */
export async function render(ctx) {
  const host = h('div');

  async function draw() {
    mount(host, Loading('Loading…'));
    // 2026-10 audit: was getCombinations(90). A 90-day window uses barely a
    // quarter of this store's real order history (5.2k of ~21.7k real
    // transactions) and, checked against a holdout split, found zero pairs
    // that beat their own base rate out-of-sample. The full real year finds
    // more candidate pairs AND several with genuine out-of-sample lift >1 --
    // not every pair, but real signal the shorter window was discarding
    // along with the noise. ProductCombinations.js (the read-only report)
    // already offers 90/180/365; this, the page that actually drives
    // combo-creation decisions, had no picker and was stuck on the window
    // that uses the least of the real data.
    const [combos, drafts, comboConfig] = await Promise.all([
      getCombinations(365), listDraftCombos(), getComboPricingConfig().catch(() => null),
    ]);
    mount(host, combosSection(combos), draftsCard(drafts), comboConfig ? comboPricingConfigCard(comboConfig) : null);
  }

  function combosSection(combos) {
    if (combos.insufficientData) {
      return Card({},
        CardHeader({ title: 'Frequently bought together', desc: 'FP-Growth over real order history' }),
        EmptyState({ icon: '✦', title: 'Not enough order history yet', description: `Needs at least ${combos.required ?? 30} orders to find a reliable pattern — has ${combos.transactionCount ?? 0} so far.` }));
    }
    if (combos.combos.length === 0) {
      return Card({},
        CardHeader({ title: 'Frequently bought together', desc: 'FP-Growth over real order history' }),
        EmptyState({ icon: '✦', title: 'No reliable pairings found', description: `Enough orders were analysed (${combos.transactionCount}), but no pair cleared the lift and support thresholds. That is a result, not a gap.` }));
    }
    return Card({},
      CardHeader({ title: 'Frequently bought together', desc: 'Customers who order one of these often order the other too' }),
      Table({
        columns: [
          { key: 'pair', label: 'Pair', render: (c) => `${c.itemA} + ${c.itemB}` },
          { key: 'observed', label: 'Observed together', render: (c) => h('span', { class: 'mono t-sm' }, pct(c.confidence === null ? null : c.confidence * 100, 'n/a', 0)) },
          {
            key: 'action', label: '',
            render: (c) => c.itemAMenuItemId && c.itemBMenuItemId
              ? Button({ size: 'sm', onClick: () => openComboModal(c) }, 'Suggest a combo')
              : h('span', { class: 't-xs t-faint' }, 'not available'),
          },
        ],
        rows: combos.combos,
      }),
      h('div', { class: 'hint', style: { marginTop: '8px' } }, 'Statistical strength (lift/support/confidence) is available on Product Combinations for anyone who wants the detail.'));
  }

  function draftsCard(drafts) {
    return Card({ style: { marginTop: '14px' } },
      CardHeader({ title: 'Combo ideas', desc: 'Saved for review — never published to the live menu or POS automatically' }),
      drafts.length === 0
        ? EmptyState({ icon: '✦', title: 'No combo ideas yet', description: 'Suggest one from a pairing above.', tight: true })
        : h('div', {}, ...drafts.map((d) => h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '9px 0', borderBottom: '1px solid var(--line-soft)' } },
          h('div', {},
            h('div', { class: 't-sm', style: { fontWeight: '600' } }, d.name),
            h('div', { class: 't-xs t-soft' }, d.chosenPrice !== null ? money(d.chosenPrice) : nodata('no price chosen'))),
          h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center' } },
            Pill(d.status === 'approved' ? 'good' : d.status === 'rejected' ? 'flat' : 'warn', d.status),
            d.status === 'draft'
              ? h('div', { style: { display: 'flex', gap: '6px' } },
                Button({ size: 'sm', onClick: () => act(() => reviewDraftCombo(d.id, 'approved')) }, 'Approve'),
                Button({ size: 'sm', onClick: () => act(() => reviewDraftCombo(d.id, 'rejected')) }, 'Reject'))
              : null)))));
  }

  async function act(fn) {
    try { await fn(); draw(); } catch (e) { toast(e.message, 'crit'); }
  }

  function openComboModal(c) {
    const err = ModalError();
    const body = h('div', {}, Loading('Working out cost & price…'));
    const nameInput = h('input', { type: 'text', value: `${c.itemA} + ${c.itemB}` });
    const priceInput = h('input', { type: 'number', step: '0.01', min: '0' });
    const noteInput = h('input', { type: 'text', placeholder: 'optional' });
    const submit = Button({ variant: 'primary' }, 'Save as draft idea');
    submit.disabled = true;

    getComboPricingPreview({ itemAId: c.itemAMenuItemId, itemBId: c.itemBMenuItemId }).then((p) => {
      priceInput.value = p.price.costBasedPrice !== null ? String(p.price.costBasedPrice) : '';
      mount(body,
        h('div', { style: { marginBottom: '12px' } },
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
            : null),
        h('label', { class: 'field' }, 'Name', nameInput),
        h('label', { class: 'field', style: { marginTop: '10px' } }, 'Chosen price (₱)', priceInput),
        h('label', { class: 'field', style: { marginTop: '10px' } }, 'Note', noteInput));
      submit.disabled = false;
    }).catch((e) => { err.textContent = e.message; err.style.display = ''; });

    submit.addEventListener('click', async () => {
      submit.disabled = true;
      try {
        await createDraftCombo({
          itemAId: c.itemAMenuItemId, itemBId: c.itemBMenuItemId,
          name: nameInput.value.trim() || `${c.itemA} + ${c.itemB}`,
          chosenPrice: priceInput.value ? Number(priceInput.value) : null,
          note: noteInput.value.trim() || null,
          pairStats: { lift: c.lift, confidence: c.confidence, support: c.support },
          clientRequestId: `combo-draft-${c.itemAMenuItemId}-${c.itemBMenuItemId}-${Date.now()}`,
        });
        modal.close();
        toast('Saved as a combo idea.', 'good');
        draw();
      } catch (e) { err.textContent = e.message; err.style.display = ''; submit.disabled = false; }
    });

    const modal = Modal({
      title: 'Suggest a combo',
      desc: 'A cost-based estimate — never a guess. Saving does not change the live menu or POS.',
      onClose: () => {},
    }, body, err, h('div', { style: { marginTop: '14px', display: 'flex', justifyContent: 'flex-end' } }, submit));
  }

  /**
   * Combo pricing settings — PATCH /console/combo-pricing-config
   * (wt/combo_pricing.py), admin-only (inventory.write). Moved here from
   * Administration > Settings (2026-10 cleanup): it configures what THIS
   * page does (turns a cost estimate into a suggested price range above),
   * so it belongs next to its effect, not filed under generic account
   * settings. Every field is None ("not configured") until set here. The
   * form always submits its full current state; since the backend only
   * writes the fields present in the body, a full resend is idempotent.
   */
  function comboPricingConfigCard(config) {
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
        await setComboPricingConfig(body);
        toast('Combo pricing settings saved.', 'good');
      } catch (e) {
        err.textContent = e.message; err.style.display = '';
      } finally {
        submit.disabled = false;
      }
    });

    return Card({ style: { marginTop: '14px' } },
      CardHeader({ title: 'Combo pricing settings', desc: 'A target food-cost % is required before "Suggest a combo" above can price a range' }),
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
      h('div', { style: { marginTop: '12px' } }, submit));
  }

  await draw();
  return host;
}
