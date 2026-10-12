/**
 * Shared plain-language rendering for a repeated-variance signal
 * (wt/reservation.py's _repeated_variance_signals, surfaced on
 * GET /console/reconciliation/ingredients as `repeatedVarianceSignal`).
 *
 * One place for this copy because it appears in three call sites
 * (Ingredient Drawer, Losses & Variance > Ingredient Variance, Purchasing) —
 * duplicating the wording risks one of them silently drifting into
 * language the backend doesn't actually support (e.g. calling a positive
 * variance "over-consumption", which the backend deliberately never says).
 *
 * This module computes NOTHING — every number it reads was already
 * computed server-side. It only turns an existing fact into a sentence.
 */

/**
 * @param {{active:boolean, observationCount:number, dates:string[],
 *          direction:'below'|'above'|null, averageVarianceRatio:number|null,
 *          thresholdRatio:number, minObservations:number}|null} signal
 * @returns {{headline:string, detail:string, causes:string[], action:string}|null}
 *   null when there is nothing to show (no signal object, or not active).
 */
export function describeVarianceSignal(signal) {
  if (!signal || !signal.active) return null;

  const pct = signal.averageVarianceRatio !== null
    ? `${Math.round(Math.abs(signal.averageVarianceRatio) * 100)}%`
    : null;

  if (signal.direction === 'below') {
    return {
      headline: 'Physical stock is repeatedly lower than expected',
      detail: `Based on ${signal.observationCount} separate physical checks`
        + (pct ? `, averaging about ${pct} less than the system expected each time` : '')
        + '. The cause is not known — this only means the same thing kept happening, not why.',
      causes: [
        'Larger portions than the recipe assumes',
        'Preparation or trimming loss',
        'The recipe quantity may be set too low',
        'The usable-yield estimate may be set too high',
        'Stock loss that was not logged as wastage',
      ],
      action: 'Review recent physical counts and the recipe/yield assumptions for this ingredient before changing anything.',
    };
  }

  if (signal.direction === 'above') {
    return {
      headline: 'Physical stock is repeatedly higher than expected',
      detail: `Based on ${signal.observationCount} separate physical checks`
        + (pct ? `, averaging about ${pct} more than the system expected each time` : '')
        + '. This is not automatically "using less than expected" — it can have several explanations.',
      causes: [
        'A delivery that was not logged as received',
        'Usage lower than the recipe assumes',
        'The usable-yield estimate may be set too low',
        'A counting difference',
        'Some other stock movement that was not recorded',
      ],
      action: 'Review recent physical counts and receiving records for this ingredient before changing anything.',
    };
  }

  return null;
}
