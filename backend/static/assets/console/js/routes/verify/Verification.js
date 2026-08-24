import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { RailCard } from '../../components/ui/RailCard.js';
import { Stat } from '../../components/ui/Stat.js';
import { Tabbar } from '../../components/ui/Tabbar.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError, confirmModal } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { toast } from '../../components/ui/Feedback.js';
import { VerificationItemCard } from '../../components/domain/VerificationItem.js';
import { count } from '../../lib/format.js';
import { getQueue } from '../../lib/api/verification.js';
import { listUnmapped, resolveMap, resolveDismiss } from '../../lib/api/queue.js';
import { listIngredients } from '../../lib/api/inventory.js';

/**
 * Verification — the setup numbers that still carry a seeded placeholder,
 * plus the Ingredient Queue: recipe lines a source import could not match to
 * a real ingredient. Both are "data readiness" problems in the same sense —
 * a value nobody has confirmed — and both were on one page in the old
 * console, so they stay together here as a fourth tab rather than a
 * separate, easy-to-forget top-level section.
 *
 * The queue can run to hundreds of items, so it is paged in place rather than
 * rendered all at once: several hundred cards is both slow and, more to the
 * point, unusable. The counts stay honest (the KPI cards show the true totals)
 * while the working area shows a manageable batch.
 */
const PAGE_SIZE = 25;

const PRIORITY_LABELS = {
  1: 'Priority 1 — affects stock deductions',
  2: 'Priority 2 — affects cost',
  3: 'Priority 3 — affects expiry only',
};

export async function render(ctx) {
  let queue = await getQueue();
  let unmapped = await listUnmapped();
  let active = ['1', '2', '3', 'unmapped'].includes(ctx.tab) ? ctx.tab : '1';
  let shown = PAGE_SIZE;

  const kpis = h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(4,1fr)', marginBottom: '14px' } });
  const listHost = h('div');

  function drawKpis() {
    mount(kpis,
      RailCard({ severity: 'crit' }, Stat({ value: count(queue.byPriority[1] || 0), label: PRIORITY_LABELS[1] })),
      RailCard({ severity: 'warn' }, Stat({ value: count(queue.byPriority[2] || 0), label: PRIORITY_LABELS[2] })),
      RailCard({ severity: 'info' }, Stat({ value: count(queue.byPriority[3] || 0), label: PRIORITY_LABELS[3] })),
      RailCard({ severity: unmapped.count > 0 ? 'crit' : 'good' },
        Stat({ value: count(unmapped.count), label: 'Ingredient Queue — blocks menu items' })));
  }

  function drawList() {
    if (active === 'unmapped') { drawUnmapped(); return; }
    const priority = Number(active);
    const items = queue.items.filter((i) => i.priority === priority);

    if (items.length === 0) {
      mount(listHost, Card({}, EmptyState({
        icon: '✓',
        title: `Nothing left at priority ${priority}`,
        description: priority === 1
          ? 'Every value that affects how much stock a sale deducts has been confirmed.'
          : priority === 2
            ? 'Every cost figure has been confirmed against what you actually pay.'
            : 'Every expiry estimate has been confirmed. These affect the Expiry Watch timeline only — never stock deductions.',
      })));
      return;
    }

    const visible = items.slice(0, shown);
    mount(listHost,
      h('div', { class: 't-sm t-soft', style: { marginBottom: '4px' } },
        `Showing ${visible.length} of ${items.length}. Each decision is recorded with who made it and when.`),
      ...visible.map((item) => VerificationItemCard({
        item,
        onResolved: (resolved) => {
          // Resolved items leave the queue locally so the list does not jump
          // under the reader's cursor while they work down it.
          queue.items = queue.items.filter((i) => i.id !== resolved.id);
          queue.byPriority[resolved.priority] = Math.max(0, (queue.byPriority[resolved.priority] || 1) - 1);
          queue.count = Math.max(0, queue.count - 1);
          drawKpis();
          drawList();
          ctx.refreshBadges();
        },
      })),
      visible.length < items.length
        ? h('div', { style: { marginTop: '14px', textAlign: 'center' } },
          Button({
            onClick: () => { shown += PAGE_SIZE; drawList(); },
          }, `Show ${Math.min(PAGE_SIZE, items.length - visible.length)} more`))
        : null);
  }

  function drawUnmapped() {
    if (unmapped.items.length === 0) {
      mount(listHost, Card({}, EmptyState({
        icon: '✓',
        title: 'No unresolved recipe lines',
        description: 'Every recipe line from the last import matched a real ingredient.',
      })));
      return;
    }
    mount(listHost, ...unmapped.items.map((item) => Card({ style: { marginTop: '10px' } },
      CardHeader({
        title: item.sourceIngredientLabel,
        desc: h('div', { class: 'card-desc' },
          `From “${item.sourceRecipeName}” · ${item.reasonLabel}`,
          item.sourceQtyText ? ` · read as "${item.sourceQtyText}"` : ''),
        right: [
          Button({ size: 'sm', onClick: () => dismissLine(item) }, 'Dismiss'),
          Button({ variant: 'primary', size: 'sm', onClick: () => openMapModal(item) }, 'Map to ingredient'),
        ],
      }),
      item.blocksMenuItems > 0
        ? h('div', { class: 'callout callout-crit', style: { marginBottom: '0' } },
          h('p', {}, h('strong', {}, `Blocks ${item.blocksMenuItems} menu item${item.blocksMenuItems === 1 ? '' : 's'} from being sellable. `),
            'Mapping this line to a real ingredient is what makes those items available on the POS again.'))
        : h('div', { class: 't-xs t-faint' }, 'Not currently attached to a live menu item.'))));
  }

  function dismissLine(item) {
    confirmModal({
      title: 'Dismiss without mapping?',
      body: `Use this only for lines that genuinely deduct nothing (e.g. "to taste"). "${item.sourceIngredientLabel}" from ${item.sourceRecipeName} will stop appearing in this queue, but the recipe line it came from stays unresolved.`,
      confirmLabel: 'Dismiss',
      onConfirm: async () => {
        await resolveDismiss(item.id);
        toast(`Dismissed — ${item.sourceIngredientLabel} will not be asked about again.`, 'info');
        unmapped = await listUnmapped();
        drawKpis();
        drawList();
        ctx.refreshBadges();
      },
    });
  }

  function openMapModal(item) {
    const search = h('input', { type: 'text', placeholder: 'Search ingredients…' });
    const listBox = h('div', { style: { maxHeight: '280px', overflowY: 'auto', border: '1px solid var(--line)', borderRadius: '8px', marginTop: '8px' } });
    const err = ModalError();
    let ingredients = [];
    let picked = null;
    const submit = Button({ variant: 'primary' }, 'Confirm mapping');
    submit.disabled = true;

    function drawOptions(query) {
      const q = (query || '').trim().toLowerCase();
      const rows = ingredients.filter((i) => !q || i.name.toLowerCase().includes(q)).slice(0, 60);
      mount(listBox, ...rows.map((i) => h('div', {
        class: 'row-click',
        style: {
          padding: '8px 12px', fontSize: '12.5px',
          background: picked && picked.pk === i.pk ? 'var(--amber-tint)' : undefined,
        },
        onClick: () => { picked = i; submit.disabled = false; drawOptions(query); },
      }, i.name, h('span', { class: 't-xs t-faint', style: { marginLeft: '6px' } }, `(${i.baseUnit})`))));
    }
    search.addEventListener('input', () => drawOptions(search.value));

    submit.addEventListener('click', async () => {
      if (!picked) return;
      submit.disabled = true;
      try {
        const r = await resolveMap(item.id, picked.pk);
        modal.close();
        toast(`Mapped "${item.sourceIngredientLabel}" to ${r.mapped_to}.${item.blocksMenuItems ? ' Menu availability will pick this up on next refresh.' : ''}`, 'good');
        unmapped = await listUnmapped();
        drawKpis();
        drawList();
        ctx.refreshBadges();
      } catch (e) {
        err.textContent = e.message; err.style.display = ''; submit.disabled = false;
      }
    });

    const modal = Modal({
      title: `Map “${item.sourceIngredientLabel}”`,
      desc: `From ${item.sourceRecipeName}. This also remembers the match for future imports of the same source label.`,
      onClose: () => {},
    }, search, listBox, err, h('div', { style: { marginTop: '10px' } }, submit));

    listIngredients({ limit: 500, withRunway: false }).then((list) => {
      ingredients = list;
      drawOptions('');
    });
  }

  drawKpis();
  drawList();

  return h('div', {},
    kpis,
    Tabbar({
      items: [
        { id: '1', label: 'Priority 1' },
        { id: '2', label: 'Priority 2' },
        { id: '3', label: 'Priority 3' },
        { id: 'unmapped', label: `Ingredient Queue${unmapped.count ? ` (${unmapped.count})` : ''}` },
      ],
      activeId: active,
      onChange: (id) => {
        active = id;
        shown = PAGE_SIZE;
        history.replaceState(null, '', `#/verify/${id}`);
        drawList();
      },
    }),
    listHost);
}
