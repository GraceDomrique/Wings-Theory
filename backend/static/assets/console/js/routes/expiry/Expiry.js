import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { RailCard } from '../../components/ui/RailCard.js';
import { Stat } from '../../components/ui/Stat.js';
import { Pill } from '../../components/ui/Pill.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { ExpiryCard } from '../../components/domain/ExpiryCard.js';
import { count } from '../../lib/format.js';
import { getExpiry } from '../../lib/api/expiry.js';
import { listIngredients } from '../../lib/api/inventory.js';

const WINDOW_DAYS = 90;

/**
 * Expiry Watch — oldest stock first, so it gets used before it turns.
 *
 * The last KPI is the important one: how many ingredients carry NO expiry
 * signal at all. Those are not "fine" and they are not "expiring" — they are
 * unknown, and the console says so rather than letting an absence read as
 * safety. Some of them are unknown by design (eggs are configured not to
 * track), which is why the number links to the explanation rather than
 * demanding action.
 */
export async function render(ctx) {
  const [{ batches, buckets }, ingredients] = await Promise.all([
    getExpiry(WINDOW_DAYS),
    listIngredients({ limit: 500, withRunway: false }),
  ]);

  const tracked = new Set(batches.map((b) => b.ingredientPk));
  const untracked = ingredients.filter((i) => !tracked.has(i.pk)).length;

  const useToday = buckets.soon.filter((b) => b.daysToExpiry <= 0).length;
  const soon = buckets.soon.length;

  return h('div', {},
    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(5,1fr)', marginBottom: '14px' } },
      RailCard({ severity: 'crit' }, Stat({ value: count(buckets.expired.length), label: 'Expired' })),
      RailCard({ severity: 'crit' }, Stat({ value: count(useToday), label: 'Use today' })),
      RailCard({ severity: 'warn' }, Stat({ value: count(soon), label: 'Expiring soon' })),
      RailCard({ severity: 'info' }, Stat({ value: count(buckets.later.length), label: 'Later' })),
      Card({}, Stat({ value: count(untracked), label: 'No expiry data' }))),

    h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(3,1fr)' } },
      column('Expired', 'crit', buckets.expired, {
        icon: '✓',
        title: 'Nothing has expired',
        description: 'No open batch is past its date.',
      }),
      column('Expiring soon', 'warn', buckets.soon, {
        icon: '✓',
        title: 'Nothing turning this week',
        description: 'No batch falls inside the next seven days.',
      }),
      column('Later', 'info', buckets.later, {
        icon: '◷',
        title: 'Nothing further out',
        description: `No batch is dated inside the ${WINDOW_DAYS}-day window.`,
      })),

    untracked > 0
      ? h('div', { class: 'callout callout-info', style: { marginTop: '14px' } },
        h('p', {},
          h('strong', {}, `${untracked} ingredients carry no expiry signal. `),
          'Some are that way deliberately — items configured with no expiry mode are left out rather than given an invented date. The rest are batches whose date nobody has recorded, and they show up in Verification as Priority 3.'))
      : null);
}

function column(title, severity, rows, empty) {
  return Card({},
    CardHeader({ right: Pill(severity, `${title} · ${rows.length}`) }),
    rows.length === 0
      ? EmptyState({ ...empty, tight: true })
      : h('div', {}, ...rows.map((b) => ExpiryCard({ batch: b, horizonDays: WINDOW_DAYS }))));
}
