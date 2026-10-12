import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Button } from '../../components/ui/Button.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { Loading } from '../../components/ui/Feedback.js';
import { stamp, delta, qty, str, asText } from '../../lib/format.js';
import {
  listMovements, listIngredients, LEDGER_REASON, LEDGER_REASONS,
} from '../../lib/api/inventory.js';

/**
 * Inventory > Movements — the append-only stock ledger.
 *
 * READ-ONLY, PERMANENTLY (spec section 6 point 2). There is no edit control,
 * no delete control, no inline editing, and no "correct this entry" affordance
 * anywhere in this file — and there must never be one. This mirrors the
 * database, which enforces it with triggers: stock_ledger has BEFORE UPDATE
 * and BEFORE DELETE triggers that RAISE(ABORT, 'stock_ledger is append-only').
 * A correction is a NEW entry, made from Receiving or a stock count.
 *
 * The other job of this screen is the labelling fix that prompted the rebuild.
 * `void_credit` is stock returning from a cancelled order. It was being shown
 * as if it were thrown away. It is not a loss — it is the second half of a
 * pair, and pairRows() below keeps the two halves adjacent and visually
 * joined so a cancelled order reads as one event (spec section 6 point 3).
 */
const PAGE_SIZE = 200;

export async function render(ctx) {
  const ingredients = await listIngredients({ limit: 500, withRunway: false });

  let reasonFilter = '';
  let ingredientPk = ctx.selection.ingredient ? ctx.selection.ingredient.pk : null;
  let offset = 0;
  let total = 0;
  let rows = [];

  const reasonPicker = h('select', {
    onChange: (e) => { reasonFilter = e.target.value; offset = 0; draw(); },
  },
  h('option', { value: '' }, 'All reasons'),
  ...LEDGER_REASONS.map((r) => h('option', { value: r }, LEDGER_REASON[r].label)));

  const ingredientPicker = h('select', {
    onChange: (e) => { ingredientPk = e.target.value ? Number(e.target.value) : null; offset = 0; draw(); },
  },
  h('option', { value: '' }, 'All ingredients'),
  ...ingredients.map((i) => h('option', {
    value: String(i.pk), selected: i.pk === ingredientPk ? 'selected' : null,
  }, i.name)));

  const host = h('div');
  const download = Button({ size: 'sm', onClick: () => exportCsv(rows) }, '↓ Download');

  async function draw() {
    mount(host, Loading('Loading movements…'));
    const out = await listMovements({ ingredientPk, reason: reasonFilter, limit: PAGE_SIZE, offset });
    rows = pairRows(out.entries);
    total = out.total;

    // Filtering by one reason can only ever return one half of a pair — the
    // other half is a different reason by definition. Say so, rather than
    // letting a filtered view quietly present a correction as a bare loss.
    // Pagination carries the same caveat: a pair can straddle a page boundary
    // exactly as it can straddle a reason filter — the note covers both.
    const splitsPairs = ['void_credit', 'wastage', 'spoilage', 'sale_commit'].includes(reasonFilter);

    const page = Math.floor(offset / PAGE_SIZE) + 1;
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

    mount(host, Card({},
      CardHeader({
        title: 'Every stock change',
        desc: `${total} records — a permanent, append-only ledger; nothing here can be edited or deleted`,
        right: download,
      }),
      splitsPairs
        ? h('div', { class: 'callout callout-info' },
          h('p', {},
            h('strong', {}, 'Filtered to one reason, so pairs are split. '),
            'A correction is written as two lines with different reasons. With this filter only one half is listed, and a line that is actually part of a stock-neutral correction will look like a standalone movement. Switch to “All reasons” to see them joined.'))
        : null,
      Table({
        columns: [
          {
            key: 'when',
            label: 'When',
            render: (m) => h('span', { class: 'mono t-xs' }, stamp(m.timestamp)),
          },
          { key: 'ingredient', label: 'Ingredient', render: (m) => m.ingredientName },
          { key: 'reason', label: 'Reason', render: reasonCell },
          {
            key: 'change',
            label: 'Change',
            render: (m) => h('span', {
              class: m.changeQty > 0 ? 't-good' : m.changeQty < 0 ? 't-crit' : '',
            }, delta(m.changeQty, m.baseUnit)),
          },
          {
            key: 'after',
            label: 'Stock after',
            render: (m) => h('span', { class: 'mono t-sm' }, qty(m.stockAfter, m.baseUnit)),
          },
          { key: 'link', label: 'Linked to', render: linkCell },
        ],
        rows,
        // Pair members carry a shared tinted band so the eye reads them as one
        // event rather than two unrelated movements.
        rowClass: (m) => m.__pair || '',
        empty: EmptyState({
          icon: '≡',
          title: 'No movements match this filter',
          description: 'Every sale, delivery, waste entry and count correction lands here. Widen the filter to see more.',
        }),
      }),
      h('p', { class: 't-xs t-soft', style: { marginTop: '10px' } },
        'Shaded pairs above are one event, not two. A correction returns the stock (“Order voided — return”) and immediately re-books it against the reason it actually went — so the two lines net out and no additional stock was lost. Read either line on its own and it looks like a loss that never happened.'),
      h('div', {
        style: {
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          marginTop: '14px', paddingTop: '12px', borderTop: '1px solid var(--line)',
        },
      },
      h('span', { class: 't-xs t-faint' }, `Page ${page} of ${pages}`),
      h('div', { style: { display: 'flex', gap: '8px' } },
        Button({ size: 'sm', disabled: offset <= 0, onClick: () => { offset = Math.max(0, offset - PAGE_SIZE); draw(); } }, 'Previous'),
        Button({ size: 'sm', disabled: offset + PAGE_SIZE >= total, onClick: () => { offset += PAGE_SIZE; draw(); } }, 'Next')))));
  }

  await draw();

  return h('div', {},
    h('div', { style: { display: 'flex', gap: '12px', marginBottom: '14px', flexWrap: 'wrap' } },
      h('label', { class: 'field', style: { minWidth: '220px' } }, 'Ingredient', ingredientPicker),
      h('label', { class: 'field', style: { minWidth: '200px' } }, 'Reason', reasonPicker)),
    host);
}

/**
 * Keep a void/return next to the line it reverses.
 *
 * THIS IS THE FIX FOR THE BUG THAT PROMPTED THE REBUILD, so it is worth being
 * precise about what the ledger actually contains.
 *
 * A corrected unlimited refill writes, per ingredient, two lines in the same
 * transaction against the same `unli_refill` reference:
 *
 *     void_credit  +24.0 g   "corrected: wrong flavour served"
 *     wastage      -24.0 g   "corrected: wrong flavour served"
 *
 * They net to zero. The stock was already deducted when the refill was served;
 * the correction only re-attributes it from "sold" to "wasted". Read on its
 * own, that `wastage` line looks like 24 g newly thrown away — which is
 * exactly how a paired correction came to be reported as real loss.
 *
 * A voided ORDER is the same idea with different partners: a `void_credit`
 * against an `order` ref reversing an earlier `sale_commit`.
 *
 * So a counterpart is looked for in that order — a same-transaction wastage
 * first, then the original sale_commit — always requiring the same ingredient,
 * the same source reference, and an equal and opposite quantity. Anything
 * without a genuine counterpart is left alone rather than force-paired.
 */
function pairRows(entries) {
  const byKey = new Map();
  for (const m of entries) {
    delete m.__pair;
    delete m.__pairNet;
    if (!m.pairKey) continue;
    if (!byKey.has(m.pairKey)) byKey.set(m.pairKey, []);
    byKey.get(m.pairKey).push(m);
  }

  const opposite = (a, b) => Math.abs((a.changeQty || 0) + (b.changeQty || 0)) < 0.0001;

  // PASS 1 — claim partners before emitting anything.
  //
  // This has to happen up front. The ledger arrives newest-first, so the
  // wastage half of a correction (written last, higher id) is reached BEFORE
  // its void_credit. Pairing greedily inside the emit loop would consume that
  // wastage as a standalone row and then pair the void against the original
  // sale_commit from three days earlier — technically an equal and opposite
  // line, but the wrong partner, and it would drag an unrelated older row out
  // of sequence to sit beside it.
  const partnerOf = new Map();
  const claimed = new Set();

  for (const m of entries) {
    if (m.reason !== 'void_credit' || !m.pairKey || claimed.has(m.id)) continue;
    const siblings = (byKey.get(m.pairKey) || [])
      .filter((s) => s.id !== m.id && !claimed.has(s.id) && opposite(m, s));

    // Preference order matters. A same-instant wastage/spoilage is the other
    // half of a correction written in one transaction. Only if there is no
    // such line does this fall back to the original sale_commit, which is the
    // plain voided-order case.
    const match = siblings.find((s) => (s.reason === 'wastage' || s.reason === 'spoilage')
        && s.timestamp === m.timestamp)
      || siblings.find((s) => s.reason === 'wastage' || s.reason === 'spoilage')
      || siblings.find((s) => s.reason === 'sale_commit');

    if (!match) continue;

    const net = (m.changeQty || 0) + (match.changeQty || 0);
    m.__pair = 'pair-top';
    match.__pair = 'pair-bottom';
    m.__pairNet = net;
    match.__pairNet = net;
    match.__pairUnit = match.baseUnit;
    partnerOf.set(m.id, match);
    claimed.add(m.id);
    claimed.add(match.id);
  }

  // PASS 2 — emit in ledger order, lifting each claimed partner up to sit
  // directly beneath its void_credit.
  const out = [];
  const emitted = new Set();
  for (const m of entries) {
    if (emitted.has(m.id)) continue;
    if (m.__pair === 'pair-bottom' && !emitted.has(m.id) && partnerOf.has(findTopId(partnerOf, m))) {
      // Its void_credit has not been reached yet; it will be emitted there.
      continue;
    }
    out.push(m);
    emitted.add(m.id);
    const partner = partnerOf.get(m.id);
    if (partner && !emitted.has(partner.id)) {
      out.push(partner);
      emitted.add(partner.id);
    }
  }

  // Anything whose void_credit never appeared in this page (a reason filter
  // can split a pair across the filter boundary) still has to be shown.
  for (const m of entries) {
    if (!emitted.has(m.id)) {
      delete m.__pair;
      delete m.__pairNet;
      out.push(m);
      emitted.add(m.id);
    }
  }

  return out;
}

/** The id of the void_credit that claimed this bottom row, if any. */
function findTopId(partnerOf, bottom) {
  for (const [topId, partner] of partnerOf) if (partner.id === bottom.id) return topId;
  return null;
}

function reasonCell(m) {
  const meta = LEDGER_REASON[m.reason] || { label: m.reason, severity: 'flat' };

  // A wastage line that is one half of a stock-neutral correction is not an
  // incremental loss, so it does not get the loud "real loss" treatment. The
  // label still says wastage — that is what it is — but the severity and the
  // tag below make clear it is paired.
  const severity = m.__pair && (m.reason === 'wastage' || m.reason === 'spoilage')
    ? 'warn'
    : meta.severity;

  let tag = null;
  if (m.__pair === 'pair-top') {
    tag = 'paired correction — 2 lines';
  } else if (m.__pair === 'pair-bottom') {
    const net = m.__pairNet ?? 0;
    tag = Math.abs(net) < 0.0001
      ? 'nets to zero with the line above'
      : `nets to ${net > 0 ? '+' : '−'}${Math.abs(net)} ${m.__pairUnit || ''} with the line above`;
  }

  return h('div', {},
    Pill(severity, meta.label),
    tag ? h('div', { class: 'pair-tag', style: { marginTop: '3px' } }, tag) : null,
    m.wastageReason
      ? h('div', { class: 't-xs t-faint', style: { marginTop: '3px' } }, m.wastageReason)
      : null);
}

/**
 * What this line is attached to. Real ref_types are order, unli_refill,
 * purchase, manual_count, manual_writeoff and unli_reconciliation — showing
 * only orders would hide the refill reference that joins a correction pair.
 */
const REF_LABEL = {
  order: 'order',
  unli_refill: 'refill',
  purchase: 'delivery',
  manual_count: 'stock count',
  manual_writeoff: 'write-off',
  unli_reconciliation: 'reconciliation',
};

function linkCell(m) {
  if (!m.refType) return str(null, 'no link');
  const label = REF_LABEL[m.refType] || m.refType;
  return h('span', { class: 'mono t-xs' }, `${label} #${m.refId ?? '—'}`);
}

/** Export is a read of what is on screen. It changes nothing. */
function exportCsv(rows) {
  const head = ['when', 'ingredient', 'reason', 'change', 'unit', 'stock_after', 'linked_to', 'note'];
  const lines = [head.join(',')];
  for (const m of rows) {
    lines.push([
      m.timestamp,
      m.ingredientName,
      (LEDGER_REASON[m.reason] || { label: m.reason }).label,
      asText.num(m.changeQty, '', 3),
      m.baseUnit,
      asText.num(m.stockAfter, '', 3),
      m.linkedOrderId ? `order #${m.linkedOrderId}` : '',
      m.note || '',
    ].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = h('a', {
    href: URL.createObjectURL(blob),
    download: `wings-theory-movements-${new Date().toISOString().slice(0, 10)}.csv`,
  });
  document.body.appendChild(a);
  a.click();
  a.remove();
}
