// Run with: node --test phaseA.test.mjs
// Pins the pure logic behind the Phase A redesign primitives.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { allowed } from './capabilities.js';
import { togetherInOrders } from './glossary.js';
import { daysOfStockText } from '../components/domain/DaysOfStock.js';
import { stockConditions } from '../components/domain/StockConditions.js';
import { workflowLabel, workflowStep, FLOWS } from '../components/ui/WorkflowState.js';

// Mirrors the real auth.py rows these checks matter for.
const POLICY = {
  'purchase_order.create': ['owner'],
  'purchase_order.manage': ['admin'],
  'instruction.create': ['owner'],
  'inventory.write': ['admin'],
};

test('allowed(): owner may create but not manage purchase orders', () => {
  assert.equal(allowed(POLICY, 'owner', 'purchase_order.create'), true);
  assert.equal(allowed(POLICY, 'owner', 'purchase_order.manage'), false);
  assert.equal(allowed(POLICY, 'admin', 'purchase_order.manage'), true);
  assert.equal(allowed(POLICY, 'owner', 'inventory.write'), false);
});

test('allowed(): fails closed on missing policy, role, or unknown capability', () => {
  assert.equal(allowed(null, 'admin', 'inventory.write'), false);
  assert.equal(allowed(POLICY, null, 'inventory.write'), false);
  assert.equal(allowed(POLICY, 'admin', 'no.such.capability'), false);
});

test('daysOfStockText(): never fabricates a runway without evidence', () => {
  assert.deepEqual(daysOfStockText({ daysLeft: null, risk: 'no_data' }),
    { text: 'Not enough history', known: false });
  assert.deepEqual(daysOfStockText({ daysLeft: null, risk: 'idle' }),
    { text: 'Not being used lately', known: false });
  assert.deepEqual(daysOfStockText({}), { text: 'Not enough history', known: false });
  assert.deepEqual(daysOfStockText({ daysLeft: undefined, risk: 'ok' }),
    { text: 'Not enough history', known: false });
});

test('daysOfStockText(): shows backend runway and out-of-stock', () => {
  assert.equal(daysOfStockText({ daysLeft: 6.4, risk: 'warning' }).text, 'About 6 days');
  assert.equal(daysOfStockText({ daysLeft: 1, risk: 'critical' }).text, 'About 1 day');
  assert.equal(daysOfStockText({ daysLeft: 0, risk: 'out' }).text, 'Out of stock');
  assert.equal(daysOfStockText({ daysLeft: 0 }).text, 'Out of stock');
});

test('stockConditions(): low AND expiring are independent, normal shows nothing', () => {
  const both = stockConditions({
    low: true, lowSeverity: 'warn', expiresOn: 'Oct 4', expirySeverity: 'warn',
  });
  assert.deepEqual(both.map((c) => c.key), ['low', 'expiring']);
  assert.equal(both[1].label, 'Expires Oct 4');
  assert.deepEqual(stockConditions({}), []);
  // out of stock supersedes "low" but not expiry/expired
  assert.deepEqual(stockConditions({ outOfStock: true, low: true }).map((c) => c.key), ['out']);
  assert.deepEqual(stockConditions({ low: true, expiredQty: true }).map((c) => c.key), ['low', 'expired']);
});

test('workflow states are labelled per flow and are not severities', () => {
  assert.equal(workflowLabel('purchaseOrder', 'draft'), 'Draft');
  assert.equal(workflowLabel('purchaseOrder', 'partially_received'), 'Partly received');
  assert.equal(workflowLabel('instruction', 'open'), 'New');
  assert.equal(workflowLabel('check', 'reviewing'), 'Reviewing');
  assert.equal(workflowLabel('investigation', 'resolved'), 'Resolved');
  assert.equal(workflowLabel('purchaseOrder', 'weird_state'), 'weird state'); // never guessed
  assert.deepEqual(workflowStep('purchaseOrder', 'ordered'), { step: 2, of: 3 });
  assert.equal(workflowStep('purchaseOrder', 'cancelled'), null);
  // every listed state of every flow has a label and (if linear) a position
  for (const [name, f] of Object.entries(FLOWS)) {
    for (const s of f.order) {
      assert.ok(f.labels[s], `${name}.${s} label`);
      assert.ok(f.position[s], `${name}.${s} position`);
    }
  }
});

test('togetherInOrders() states only the count', () => {
  assert.equal(togetherInOrders(1), 'Appeared together in 1 order');
  assert.equal(togetherInOrders(42), 'Appeared together in 42 orders');
  assert.equal(togetherInOrders(null), 'Appeared together in 0 orders');
});
