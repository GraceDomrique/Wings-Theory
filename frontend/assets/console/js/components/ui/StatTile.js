import { h, icon } from '../../lib/dom.js';
import { Card } from './Card.js';
import { Stat } from './Stat.js';

/**
 * A `.card` wrapping a colored `.kpi-icon` badge + `Stat`'s value/label +
 * an optional small sub-line (a delta pill, a short note) underneath —
 * the reference mockup's recurring top-row stat tile (Today's Revenue,
 * Need Purchasing, etc.), built on the existing light-background
 * `.kpi-icon-{severity}` treatment (app.css, already used by AdminToday's
 * heroStat on a dark photo) rather than introducing new color rules.
 *
 * `value`/`missing` pass straight through to `Stat`, so a null value still
 * renders as the honest `.nodata` state, never a fabricated number.
 *
 * @param {{value:Node|string|number|null, label:string, missing?:string,
 *   severity?:'crit'|'warn'|'good'|'info'|'flat', iconPath?:string,
 *   sub?:Node|string, onClick?:Function}} opts
 */
export function StatTile({
  value, label, missing, severity = 'flat', iconPath, sub, onClick,
}) {
  const stat = Stat({ value, label, missing });
  // A long unbroken figure (a 7-digit peso total, say) has no space to
  // wrap at and would otherwise overflow this tile's narrow column —
  // shrink-to-fit beats a number silently cut off at the card edge.
  const numberNode = stat.querySelector('.n');
  if (numberNode) Object.assign(numberNode.style, { overflowWrap: 'anywhere', fontSize: 'clamp(15px, 1.6vw, 22px)' });

  return Card({
    className: 'stat-tile-card',
    style: onClick ? { cursor: 'pointer' } : undefined,
  },
  h('div', {
    style: { display: 'flex', alignItems: 'flex-start', gap: '10px' },
    onClick,
  },
  iconPath ? h('span', { class: `kpi-icon kpi-icon-${severity}` }, icon(iconPath, 15)) : null,
  h('div', { style: { flex: '1', minWidth: '0' } },
    stat,
    sub ? h('div', { class: 't-xs', style: { marginTop: '4px' } }, sub) : null)));
}
