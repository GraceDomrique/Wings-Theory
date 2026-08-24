// Plain node:test — no framework/build step, matching this project's own
// no-bundler convention for the console. Run with: node --test severity.test.mjs
//
// 2026-08 audit regression: severityFromRisk's switch was written against a
// risk vocabulary ('out_of_stock', 'low', 'healthy', 'unknown', 'no_usage')
// wt/forecasting.py has never actually emitted. Its real vocabulary is
// RISK_ORDER = {"out":0, "critical":1, "warning":2, "idle":3, "ok":4,
// "no_data":5}. Because 'out' (an ingredient with zero runway left) had no
// matching case, it silently fell through to 'info' instead of 'crit' --
// confirmed against the live dev database, where "Rice" was at risk='out'
// and rendered as a calm gray "no usage rate" pill on the Inventory page,
// and was silently excluded from the Attention Needed report's crit/warn
// filter. This test pins the real six-value vocabulary so a future rename
// on either side (wt/forecasting.py's RISK_ORDER or this switch) fails loud
// instead of silently downgrading the single most urgent inventory state.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { severityFromRisk } from './severity.js';

test('every real backend risk value maps to the right severity', () => {
  assert.equal(severityFromRisk('out'), 'crit', '"out" (zero runway) must be crit, not info');
  assert.equal(severityFromRisk('critical'), 'crit');
  assert.equal(severityFromRisk('warning'), 'warn');
  assert.equal(severityFromRisk('ok'), 'good');
  assert.equal(severityFromRisk('idle'), 'flat');
  assert.equal(severityFromRisk('no_data'), 'flat');
});

test('an unrecognized non-empty string is info, not silently crit or good', () => {
  assert.equal(severityFromRisk('some_future_risk_value'), 'info');
});

test('null/undefined is flat, not a guessed severity', () => {
  assert.equal(severityFromRisk(null), 'flat');
  assert.equal(severityFromRisk(undefined), 'flat');
});
