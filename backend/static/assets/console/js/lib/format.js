/**
 * Value formatting, and the one place the "no data is not zero" rule is
 * enforced — spec section 6, point 4.
 *
 * This was the exact confusion that triggered the rebuild: a value nobody had
 * supplied rendered as a real number, and a paired void/return read as real
 * loss. So every formatter here returns a DOM Node, never a string:
 *
 *   - a value present  -> a text node with the formatted number
 *   - a value missing  -> <span class="nodata">needs a cost</span>
 *
 * Returning Nodes is deliberate. It makes `${cost}` impossible, so null can
 * never be silently coerced into "null", "0" or "". If you want a plain
 * string, you have to ask for it explicitly via the `*Text` helpers, and
 * those still refuse to invent a zero.
 */

/** True when the backend actually supplied a value. 0 counts as supplied. */
export function has(v) {
  return v !== null && v !== undefined && !(typeof v === 'number' && Number.isNaN(v));
}

/** The visual "we don't know" state. Never used for a real zero. */
export function nodata(label) {
  const span = document.createElement('span');
  span.className = 'nodata';
  span.textContent = label;
  return span;
}

function text(s) { return document.createTextNode(s); }

const PHP = new Intl.NumberFormat('en-PH', {
  style: 'currency', currency: 'PHP', minimumFractionDigits: 2, maximumFractionDigits: 2,
});

/**
 * Peso amount. A missing cost renders as copy, not as P0.00.
 * @param {number|null|undefined} v
 * @param {string} missing what to say when nobody has supplied this number
 */
export function money(v, missing = 'no cost set') {
  return has(v) ? text(PHP.format(v)) : nodata(missing);
}

/** Compact peso for KPI tiles: P64.5k. Same missing-value contract. */
export function moneyShort(v, missing = 'not valued') {
  if (!has(v)) return nodata(missing);
  const abs = Math.abs(v);
  if (abs >= 1000) return text('₱' + (v / 1000).toFixed(1).replace(/\.0$/, '') + 'k');
  return text('₱' + v.toFixed(0));
}

/**
 * Plain number with thousands separators.
 * @param {number|null|undefined} v
 * @param {{ decimals?: number, missing?: string, signed?: boolean }} [opts]
 */
export function num(v, opts = {}) {
  const { decimals = 2, missing = 'not measured', signed = false } = opts;
  if (!has(v)) return nodata(missing);
  const s = new Intl.NumberFormat('en-PH', {
    minimumFractionDigits: 0, maximumFractionDigits: decimals,
  }).format(v);
  return text(signed && v > 0 ? '+' + s : s);
}

/** An integer count. A real zero prints "0"; an absent count does not. */
export function count(v, missing = 'unknown') {
  return has(v) ? text(new Intl.NumberFormat('en-PH').format(Math.round(v))) : nodata(missing);
}

/** Quantity plus its base unit: "1,021.5 g". */
export function qty(v, unit, missing = 'not counted') {
  if (!has(v)) return nodata(missing);
  const n = new Intl.NumberFormat('en-PH', { maximumFractionDigits: 2 }).format(v);
  return text(unit ? `${n} ${unit}` : n);
}

/** Signed stock movement: "+0.70 g" / "-24.5 g". */
export function delta(v, unit, missing = 'unrecorded') {
  if (!has(v)) return nodata(missing);
  const n = new Intl.NumberFormat('en-PH', { maximumFractionDigits: 3 }).format(Math.abs(v));
  const sign = v > 0 ? '+' : v < 0 ? '−' : '';
  return text(`${sign}${n}${unit ? ' ' + unit : ''}`);
}

/**
 * Days of runway or days to expiry. Absent means the backend could not
 * compute a rate — "needs 7+ days" of history, not "0 days left".
 */
export function days(v, missing = 'no usage rate yet') {
  if (!has(v)) return nodata(missing);
  if (v < 0) return text(`${Math.abs(Math.round(v))}d ago`);
  return text(`${Number(v.toFixed(1)).toLocaleString('en-PH')}d`);
}

/** Short days label for the tight runway column: "110d", "expired". */
export function daysShort(v, missing = '——') {
  if (!has(v)) return nodata(missing);
  if (v < 0) return text('expired');
  if (v < 1) return text('today');
  return text(Math.round(v) + 'd');
}

export function pct(v, missing = 'not measured', decimals = 1) {
  return has(v) ? text(`${v.toFixed(decimals)}%`) : nodata(missing);
}

/** ISO date -> 2026-08-20. A batch with no expiry says so in words. */
export function date(iso, missing = 'no expiry date') {
  if (!has(iso) || iso === '') return nodata(missing);
  return text(String(iso).slice(0, 10));
}

/** ISO timestamp -> "08-16 03:42" (the ledger's compact form). */
export function stamp(iso, missing = 'no timestamp') {
  if (!has(iso) || iso === '') return nodata(missing);
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return text(String(iso).slice(5, 16).replace('T', ' '));
  const p = (n) => String(n).padStart(2, '0');
  return text(`${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`);
}

/**
 * String passthrough that still honours the rule — an empty or absent string
 * is "nothing recorded", not a blank cell the reader has to interpret.
 */
export function str(v, missing = 'not recorded') {
  return has(v) && String(v).trim() !== '' ? text(String(v)) : nodata(missing);
}

/**
 * Plain-string variants, for the few places that need a string (chart axis
 * labels, title attributes, aria-labels). They still never fabricate a zero —
 * they return the `missing` copy verbatim.
 */
export const asText = {
  money: (v, missing = 'no cost set') => (has(v) ? PHP.format(v) : missing),
  num: (v, missing = 'not measured', decimals = 2) =>
    (has(v) ? new Intl.NumberFormat('en-PH', { maximumFractionDigits: decimals }).format(v) : missing),
  days: (v, missing = 'no usage rate yet') => (has(v) ? `${v}d` : missing),
  date: (iso, missing = 'no date') => (has(iso) && iso !== '' ? String(iso).slice(0, 10) : missing),
};

/** Title-cases a backend enum for display: sale_deduction -> Sale deduction. */
export function humanise(key) {
  if (!has(key)) return '';
  const s = String(key).replace(/_/g, ' ').trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}
