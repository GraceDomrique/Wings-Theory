import { h } from '../../lib/dom.js';
import { Pill } from '../ui/Pill.js';
import { Meter } from '../ui/Meter.js';
import { Button } from '../ui/Button.js';
import { qty } from '../../lib/format.js';

/**
 * One batch in an Expiry Watch column.
 *
 * The label states the fact ("expired 3d ago", "4d", "past storage life") and
 * the sub-line says where the date came from. An estimated shelf-life date is
 * labelled as an estimate — treating it as a printed date would turn a
 * reasonable guess about lettuce into an instruction to throw food away.
 *
 * Expired stock is never auto-discarded — the date means "do not use", not
 * "staff already threw this out at midnight". `onDiscard`, when supplied
 * (only by Expiry.js's Expired column), opens the ingredient's Wastage pane
 * pre-filled with this batch's own quantity and reason='spoilage', so
 * recording the disposal is one click instead of re-finding the ingredient
 * and re-typing a number this card already shows.
 *
 * @param {{batch:any, horizonDays?:number, onDiscard?:Function}} opts
 */
export function ExpiryCard({ batch, horizonDays = 90, onDiscard }) {
  const d = batch.daysToExpiry;
  const label = d === null ? 'no date'
    : d < 0 ? `${Math.abs(d)}d ago`
      : d === 0 ? 'today'
        : `${d}d`;

  return h('div', { class: 'mini-row' },
    h('span', {},
      batch.ingredientName,
      h('div', { class: 'sub' },
        qty(batch.qtyRemaining, batch.baseUnit),
        ' · ',
        batch.expirySource === 'estimated' ? 'estimated from shelf life'
          : batch.expirySource === 'exact' ? 'printed date'
            : 'not tracked'),
      h('div', { style: { marginTop: '5px', maxWidth: '150px' } },
        Meter({
          // Past its date the bar is empty and red — there is no runway left
          // to represent, which is different from having no measurement.
          value: d === null ? null : Math.max(0, d),
          max: horizonDays,
          severity: batch.severity,
        }))),
    h('div', { style: { display: 'flex', flexDirection: 'column', gap: '6px', alignItems: 'flex-end' } },
      Pill(batch.severity, label),
      onDiscard ? Button({ variant: 'ghost', size: 'sm', onClick: onDiscard }, 'Discard') : null));
}
