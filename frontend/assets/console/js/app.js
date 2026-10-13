import { mount } from './lib/dom.js';
import { AppShell } from './layout/AppShell.js';
import { Login } from './layout/Login.js';
import { session, ALLOWED_ROLES } from './lib/api/client.js';
import { destroyAllCharts } from './components/charts/base.js';

import * as today from './routes/today/Today.js';

import * as onhand from './routes/inventory/OnHand.js';
import * as receiving from './routes/inventory/Receiving.js';
import * as expiry from './routes/expiry/Expiry.js';
import * as stockcounts from './routes/inventory/StockCounts.js';
import * as losses from './routes/losses/Losses.js';
import * as unli from './routes/intelligence/Reconciliation.js';
import * as movements from './routes/inventory/Movements.js';

import * as menu from './routes/menu/MenuAvailability.js';
import * as recipes from './routes/menu/Recipes.js';
import * as combos from './routes/menu/Combos.js';

import * as forecasts from './routes/intelligence/Forecasting.js';
import * as whattobuy from './routes/purchasing/WhatToBuy.js';
import * as purchasing from './routes/purchasing/Purchasing.js';

import * as salesoverview from './routes/reports/SalesOverview.js';
import * as orderhistory from './routes/reports/OrderHistory.js';
import * as bestsellers from './routes/reports/BestSellers.js';
import * as reports from './routes/reports/Reports.js';

import * as inventoryoverview from './routes/owner/InventoryOverview.js';
import * as historicalrecords from './routes/owner/HistoricalRecords.js';
import * as downloads from './routes/owner/Downloads.js';
import * as unlisummary from './routes/owner/UnliSummary.js';
import * as stockreplenishment from './routes/owner/StockReplenishment.js';
import * as costslosses from './routes/owner/CostsLosses.js';
import * as reportshistory from './routes/owner/ReportsHistory.js';

import * as instructions from './routes/instructions/Instructions.js';
import * as verify from './routes/verify/Verification.js';
import * as users from './routes/admin/UsersAccess.js';
import * as audit from './routes/admin/AuditTrail.js';

/**
 * Entry point. Decides between the login gate and the shell, and owns nothing
 * else — every screen lives in its own route module, one module per sidebar
 * leaf (2026-10-01: no more composite tab-shell modules like the old
 * Inventory.js/Menu.js/Purchasing.js/System.js — see nav.js for the tree
 * each of these ids belongs to per role).
 */
const routes = {
  today,
  onhand, receiving, expiry, stockcounts, losses, unli, movements,
  menu, recipes, combos,
  forecasts, whattobuy, purchasing,
  salesoverview, orderhistory, bestsellers, reports,
  inventoryoverview, historicalrecords, downloads, unlisummary,
  stockreplenishment, costslosses, reportshistory,
  instructions, verify, users, audit,
};

const root = document.getElementById('root');

function showShell() {
  const shell = AppShell({
    routes,
    onSignOut: (expired = false) => {
      session.end();
      destroyAllCharts();
      showLogin(expired === true);
    },
  });
  mount(root, shell);
  shell.start();
}

function showLogin(expired = false) {
  mount(root, Login({ onSuccess: showShell, expired }));
}

// A stored token from a role the console no longer serves (e.g. a role
// change on the account, or a stray wt_console_* key from some other
// cause) is treated the same as no session at all, not shown the shell.
if (session.token && ALLOWED_ROLES.includes((session.user || {}).role)) {
  showShell();
} else if (session.token) {
  session.end();
  showLogin();
} else {
  showLogin();
}
