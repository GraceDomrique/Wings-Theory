import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Stat } from '../../components/ui/Stat.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading } from '../../components/ui/Feedback.js';
import { toast } from '../../components/ui/Feedback.js';
import { BarChart } from '../../components/charts/BarChart.js';
import { palette, destroyAllCharts } from '../../components/charts/base.js';
import { money, count, pct, nodata } from '../../lib/format.js';
import { getSales, getCombinations } from '../../lib/api/reports.js';
import { listMenuItems } from '../../lib/api/menu.js';
import {
  getComboPricingPreview, createDraftCombo, listDraftCombos, reviewDraftCombo,
} from '../../lib/api/comboPricing.js';

/**
 * Sales & Menu — the Owner-facing business-reporting page (2026-09-30
 * UX/IA redesign): "how is the business doing, what's frequently bought
 * together, what can't be sold right now." Reports (admin-only) keeps the
 * full toolkit (valuation, CSV export, the printable Attention Needed
 * list) — this page deliberately does NOT duplicate those, only the sales
 * KPIs/best-sellers/combinations numbers both pages would otherwise show
 * independently, per the redesign's "one metric, one home" rule. Visible
 * to both roles: combo drafting (`combo.draft`) is a shared capability,
 * and Admin benefits from the simpler view too.
 */
export async function render(ctx) {
  let rangeDays = 30;
  const host = h('div');

  const rangePicker = h('select', {
    onChange: (e) => { rangeDays = Number(e.target.value); draw(); },
  },
  h('option', { value: '7' }, 'Last 7 days'),
  h('option', { value: '30', selected: 'selected' }, 'Last 30 days'),
  h('option', { value: '90' }, 'Last 90 days'));

  async function draw() {
    destroyAllCharts();
    mount(host, Loading('Loading…'));
    const [sales, combos, menu, drafts] = await Promise.all([
      getSales(rangeDays),
      getCombinations(90),
      listMenuItems(),
      listDraftCombos(),
    ]);
    const unavailable = menu.items.filter((m) => !m.sellable);

    mount(host,
      h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', marginBottom: '14px' } },
        Card({}, Stat({ value: money(sales.revenue, 'no sales yet'), label: `Revenue · ${rangeDays}d` })),
        Card({}, Stat({ value: count(sales.orders), label: 'Orders' })),
        Card({}, Stat({ value: count(sales.covers), label: 'Covers served' })),
        Card({}, Stat({
          value: sales.averageOrder === null ? nodata('no orders yet') : money(sales.averageOrder),
          label: 'Average order',
        }))),

      Card({ style: { marginBottom: '14px' } },
        CardHeader({
          title: 'Sales trend',
          desc: 'Real completed orders only — voids excluded',
          right: h('label', { class: 'field' }, rangePicker),
        }),
        BarChart({
          labels: sales.byDay.map((d) => String(d.day).slice(5)),
          data: sales.byDay.map((d) => d.revenue),
          color: palette.info,
          height: 160,
          empty: {
            icon: '▤',
            title: 'No completed orders in this window',
            description: 'Widen the range, or take an order — voided orders are deliberately not counted here.',
            tight: true,
          },
        })),

      h('div', { class: 'grid', style: { gridTemplateColumns: '1.2fr 1fr' } },
        Card({},
          CardHeader({ title: 'Best sellers' }),
          Table({
            columns: [
              { key: 'item', label: 'Item', render: (i) => i.name },
              { key: 'qty', label: 'Qty sold', render: (i) => h('span', { class: 'mono t-sm' }, count(i.qtySold)), sortValue: (i) => i.qtySold },
              { key: 'revenue', label: 'Revenue', render: (i) => h('span', { class: 'mono t-sm' }, money(i.revenue, 'not priced')), sortValue: (i) => i.revenue },
              { key: 'share', label: 'Share', render: (i) => h('span', { class: 'mono t-sm' }, pct(i.sharePct, 'n/a')) },
            ],
            rows: sales.byItem,
            empty: EmptyState({ icon: '▤', title: 'No sales in this window', description: 'Best sellers rank themselves once completed orders exist.' }),
          })),

        Card({},
          CardHeader({ title: 'Unavailable right now', desc: 'Verbatim reason from the system — never guessed from stock numbers' }),
          unavailable.length === 0
            ? EmptyState({ icon: '✓', title: 'Everything on the menu can be sold', description: `All ${menu.items.length} items are available.`, tight: true })
            : h('div', {}, ...unavailable.map((m) => h('div', { style: { padding: '9px 0', borderBottom: '1px solid var(--line-soft)' } },
              h('div', { class: 't-sm', style: { fontWeight: '600' } }, m.name),
              h('div', { class: 't-xs t-soft' }, m.blockReason || 'no reason given')))))),

      combosSection(combos),

      draftsCard(drafts));
  }

  function combosSection(combos) {
    if (combos.insufficientData) {
      return Card({ style: { marginTop: '14px' } },
        CardHeader({ title: 'Frequently bought together', desc: 'FP-Growth over real order history' }),
        EmptyState({ icon: '✦', title: 'Not enough order history yet', description: `Needs at least ${combos.required ?? 30} orders to find a reliable pattern — has ${combos.transactionCount ?? 0} so far.` }));
    }
    if (combos.combos.length === 0) {
      return Card({ style: { marginTop: '14px' } },
        CardHeader({ title: 'Frequently bought together', desc: 'FP-Growth over real order history' }),
        EmptyState({ icon: '✦', title: 'No reliable pairings found', description: `Enough orders were analysed (${combos.transactionCount}), but no pair cleared the lift and support thresholds. That is a result, not a gap.` }));
    }
    return Card({ style: { marginTop: '14px' } },
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
      h('div', { class: 'hint', style: { marginTop: '8px' } }, 'Statistical strength (lift/support/confidence) is available on Reports for anyone who wants the detail.'));
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
    let preview = null;
    const body = h('div', {}, Loading('Working out cost & price…'));
    const nameInput = h('input', { type: 'text', value: `${c.itemA} + ${c.itemB}` });
    const priceInput = h('input', { type: 'number', step: '0.01', min: '0' });
    const noteInput = h('input', { type: 'text', placeholder: 'optional' });
    const submit = Button({ variant: 'primary' }, 'Save as draft idea');
    submit.disabled = true;

    getComboPricingPreview({ itemAId: c.itemAMenuItemId, itemBId: c.itemBMenuItemId }).then((p) => {
      preview = p;
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

  await draw();
  return host;
}
