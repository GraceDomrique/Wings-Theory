import { h } from '../../lib/dom.js';
import { Pill } from '../ui/Pill.js';
import { Meter } from '../ui/Meter.js';
import { qty } from '../../lib/format.js';

/**
 * One batch in an Expiry Watch column.
 *
 * The label states the fact ("expired 3d ago", "4d", "past storage life") and
 * the sub-line says where the date came from. An estimated shelf-life date is
 * labelled as an estimate — treating it as a printed date would turn a
 * reasonable guess about lettuce into an instruction to throw food away.
 *
 * @param {{batch:any, horizonDays?:number}} opts
 */
export function ExpiryCard({ batch, horizonDays = 90 }) {
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
    Pill(batch.severity, label));
}
