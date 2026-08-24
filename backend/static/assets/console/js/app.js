import { mount } from './lib/dom.js';
import { AppShell } from './layout/AppShell.js';
import { Login } from './layout/Login.js';
import { session } from './lib/api/client.js';
import { destroyAllCharts } from './components/charts/base.js';

import * as today from './routes/today/Today.js';
import * as inventory from './routes/inventory/Inventory.js';
import * as menu from './routes/menu/Menu.js';
import * as intelligence from './routes/intelligence/Intelligence.js';
import * as expiry from './routes/expiry/Expiry.js';
import * as verify from './routes/verify/Verification.js';
import * as reports from './routes/reports/Reports.js';
import * as system from './routes/system/System.js';

/**
 * Entry point. Decides between the login gate and the shell, and owns nothing
 * else — every screen lives in its own route module.
 */

const routes = {
  today, inventory, menu, intel: intelligence, expiry, verify, reports, system,
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

if (session.token) showShell();
else showLogin();
