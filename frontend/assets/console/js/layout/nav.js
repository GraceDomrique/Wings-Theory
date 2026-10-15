/**
 * The navigation model — a true hierarchical tree, not a flat list.
 *
 * 2026-10-01 redesign: Owner and Admin each get their OWN tree (not one
 * shared tree with items hidden/shown), matching the exact groupings given
 * in the approved IA spec. A tree is an array of top-level nodes, each
 * either:
 *   - a standalone link:  { id, label, icon, badge? }
 *   - an accordion group: { id, label, icon, children: [{ id, label, badge? }, ...] }
 *
 * A child's `id` is a real route (Sidebar.js renders it as its own clickable
 * item; AppShell.js mounts routes[id] for it directly) — there is no
 * in-page Tabbar standing in for primary navigation anymore. The group
 * itself only expands/collapses; it never mounts a page of its own.
 *
 * The SAME functional page can appear in both trees under different
 * groups/labels (e.g. 'expiry' sits under Admin's Inventory group and
 * Owner's Inventory & Losses group) — the route id is the single source of
 * truth for which component renders; the tree is presentation only. Where
 * the spec gives Owner a different label for the identical page (Purchase
 * Needs vs. What to Buy, Sales vs. Sales Overview, Orders vs. Order
 * History), that's a label on the child node, not a different id.
 *
 * `title`/`sub` shown in the topbar and breadcrumb come from this module's
 * lookups (labelOf/groupLabelOf/PAGE_SUB), not from a role-agnostic
 * PAGE_COPY pair — see the bottom of this file.
 */

import { ICONS } from '../lib/icons.js';

const G = (v) => v; // no-op marker for readability where an svg path is long

export const ADMIN_TREE = [
  {
    id: 'today',
    label: 'Today',
    icon: ICONS.layoutDashboard,
  },
  // 2026-10-14 rebuild: Inventory collapsed from a 7-item accordion into one
  // primary nav item that opens the consolidated workspace (OnHand.js)
  // directly — the old children (Receiving/Expiry/Physical Stock Count/
  // Losses & Variance/Unlimited Wings/Movements) are no longer their own
  // sidebar entries, but their routes, components and endpoints are
  // untouched: they're reached as contextual deep links FROM the Inventory
  // workspace (Quick Actions, "Needs attention" rows, "View full history"),
  // and remain valid direct-URL targets — see ADMIN_EXTRA_ROUTE_IDS below,
  // the same pattern OWNER_EXTRA_ROUTE_IDS already established.
  {
    id: 'onhand',
    label: 'Inventory',
    icon: ICONS.package,
  },
  // 2026-10-15 rebuild: collapsed from a 3-child accordion (Menu / Recipes &
  // Costing / Combos) into one primary nav item that opens Menu Studio — the
  // single integrated product-editor workspace (MenuStudio.js) — directly.
  // 'recipes' and 'combos' are no longer their own sidebar entries, but
  // their routes/components/endpoints are untouched and stay reachable as
  // deep links from within Menu Studio (its "Edit recipe"/"Open workspace →"
  // actions) and as direct-URL targets — see ADMIN_EXTRA_ROUTE_IDS below,
  // same pattern 'onhand' already established for Inventory.
  {
    id: 'menu',
    label: 'Menu & Recipes',
    icon: ICONS.bookOpen,
  },
  // 2026-10-15 rebuild: collapsed from a 3-child accordion (Demand
  // Forecasts / What to Buy / Purchase) into one primary nav item that
  // opens the Purchasing workspace (PurchasingHub.js) directly — same
  // consolidation as 'menu' above. 'forecasts', 'whattobuy' and 'purchasing'
  // are no longer their own sidebar entries; their routes/components stay
  // reachable as deep links from Purchasing's own actions ("View demand
  // forecast evidence", "Full purchase order history") and as direct-URL
  // targets — see ADMIN_EXTRA_ROUTE_IDS below.
  {
    id: 'purchasinghub',
    label: 'Purchasing',
    icon: ICONS.shoppingCart,
  },
  // 2026-10-15 rebuild: collapsed from a 3-child accordion (Overview /
  // Orders / Reports) into one primary nav item — AdminSales.js now
  // includes a "Generate Report" section inline (period picker, CSV
  // exports, inventory valuation), and Recent Orders already links to full
  // order search. 'orderhistory' and 'reports' are no longer their own
  // sidebar entries but stay reachable as deep links (full historical
  // search with server-side pagination, and the dedicated printable report
  // center) — same consolidation as 'menu' and 'purchasinghub' above.
  {
    id: 'salesoverview',
    label: 'Sales & Orders',
    icon: ICONS.chartCombined,
  },
  {
    id: 'grp-admin',
    label: 'Management',
    icon: ICONS.settings2,
    children: [
      { id: 'verify', label: 'Setup & Verification', badge: 'verification' },
      { id: 'users', label: 'Users & Access' },
      { id: 'audit', label: 'Audit Trail' },
    ],
  },
  {
    id: 'instructions',
    label: 'Instructions',
    icon: ICONS.clipboardList,
    badge: 'instructions',
  },
];

/**
 * 2026-10-13 rebuild: Owner's tree is now exactly SEVEN flat top-level
 * pages — the approved "Business Command Center" IA — with no accordion
 * groups. Each page composes what used to be several separate sidebar
 * leaves as internal tabs instead (see each page's own route module).
 * Account Settings ('settings') is rendered separately at the bottom of
 * the sidebar, outside this tree, same as before.
 */
export const OWNER_TREE = [
  {
    id: 'today',
    label: 'Overview',
    icon: ICONS.layoutDashboard,
  },
  {
    id: 'salesoverview',
    label: 'Sales & Performance',
    icon: ICONS.chartCombined,
  },
  {
    id: 'stockreplenishment',
    label: 'Stock & Replenishment',
    icon: ICONS.package,
    badge: 'stockreplenishment',
  },
  {
    id: 'purchasing',
    label: 'Purchase Orders',
    icon: ICONS.shoppingCart,
  },
  {
    id: 'costslosses',
    label: 'Costs & Losses',
    icon: G('<circle cx="12" cy="12" r="9"/><path d="M12 7v10M9.5 9.5a2.5 2.5 0 012.5-1h.5a2 2 0 010 4h-1a2 2 0 000 4h.5a2.5 2.5 0 002.5-1"/>'),
  },
  {
    id: 'instructions',
    label: 'Instructions',
    icon: ICONS.clipboardList,
    badge: 'instructions',
  },
  {
    id: 'reportshistory',
    label: 'Reports & History',
    icon: ICONS.fileChart,
  },
];

/**
 * Route ids that must stay reachable for Owner (internal links, deep
 * links from a composite page's "View all"/drawer, etc.) but are no
 * longer their own sidebar entries — their content now lives inside one
 * of the seven pages above, either merged as a tab or linked to directly.
 * Kept out of OWNER_TREE (so Sidebar.js never renders them) but still
 * valid for AppShell's per-navigation role guard — see routeRoles() below.
 */
const OWNER_EXTRA_ROUTE_IDS = [
  'forecasts', 'whattobuy', 'orderhistory', 'bestsellers',
  'inventoryoverview', 'expiry', 'losses', 'unlisummary',
  'reports', 'historicalrecords', 'downloads', 'audit',
];

/** Admin's equivalent of OWNER_EXTRA_ROUTE_IDS: the six ex-children of the
 * old Inventory accordion. Not in ADMIN_TREE (so Sidebar.js never renders
 * them as their own entries) but still valid direct-URL / deep-link targets
 * — reached from Inventory's Quick Actions and "Needs attention" rows. */
const ADMIN_EXTRA_ROUTE_IDS = [
  'receiving', 'expiry', 'stockcounts', 'losses', 'unli', 'movements', 'bestsellers',
  'recipes', 'combos', 'forecasts', 'whattobuy', 'purchasing',
  'orderhistory', 'reports',
];

/** Display label for a route id that isn't a leaf in either tree anymore
 * (an *_EXTRA_ROUTE_IDS id) — breadcrumbFor's last resort before falling
 * back to the bare id. Movements is relabeled "Stock Activity" here (the
 * route id and the underlying append-only ledger are unchanged — display
 * name only, per the terminology pass). */
const EXTRA_LABELS = {
  receiving: { group: 'Inventory', label: 'Receiving' },
  expiry: { group: 'Inventory', label: 'Expiry' },
  stockcounts: { group: 'Inventory', label: 'Physical Stock Count' },
  losses: { group: 'Inventory', label: 'Stock Differences' },
  unli: { group: 'Inventory', label: 'Unlimited Wings' },
  movements: { group: 'Inventory', label: 'Stock Activity' },
  bestsellers: { group: 'Sales & Orders', label: 'Best Sellers' },
  recipes: { group: 'Menu & Recipes', label: 'Recipes & Costing' },
  combos: { group: 'Menu & Recipes', label: 'Combos' },
  forecasts: { group: 'Purchasing', label: 'Demand Forecasts' },
  whattobuy: { group: 'Purchasing', label: 'What to Buy' },
  orderhistory: { group: 'Sales & Orders', label: 'Orders' },
  reports: { group: 'Sales & Orders', label: 'Reports' },
};

export function treeForRole(role) {
  return role === 'owner' ? OWNER_TREE : ADMIN_TREE;
}

/** Every leaf id in a tree (a standalone link counts as its own leaf). */
function leafIds(tree) {
  return tree.flatMap((node) => (node.children ? node.children.map((c) => c.id) : [node.id]));
}

/** Which roles may ever reach a given route id — AppShell's guard on a
 * direct hash visit (typed URL, bookmark, reload) that bypasses the
 * sidebar entirely. A route reachable from only one role's tree (e.g.
 * 'onhand', admin-only) is guarded accordingly; one present in both (e.g.
 * 'expiry') is open to both. Each role additionally reaches its own
 * *_EXTRA_ROUTE_IDS — pages folded into a primary workspace but still valid
 * navigation targets, not shown in the sidebar. */
export function routeRoles(id) {
  // Account Settings is rendered outside both trees (Sidebar.js pins it at
  // the bottom, separately) but every signed-in account — Admin or Owner —
  // reaches the same page to manage their own login.
  if (id === 'settings') return ['admin', 'owner'];
  const roles = [];
  if (leafIds(ADMIN_TREE).includes(id) || ADMIN_EXTRA_ROUTE_IDS.includes(id)) roles.push('admin');
  if (leafIds(OWNER_TREE).includes(id) || OWNER_EXTRA_ROUTE_IDS.includes(id)) roles.push('owner');
  return roles;
}

export const ROUTE_IDS = [...new Set([
  ...leafIds(ADMIN_TREE), ...leafIds(OWNER_TREE),
  ...ADMIN_EXTRA_ROUTE_IDS, ...OWNER_EXTRA_ROUTE_IDS,
  'settings',
])];

/** {groupLabel, leafLabel} for the breadcrumb and topbar title, resolved
 * against the CURRENT role's tree (the same id can carry a different label
 * — or a different parent group entirely — depending on who's looking). A
 * standalone link (Today/Instructions) has no group, so groupLabel is null. */
export function breadcrumbFor(role, id) {
  if (id === 'settings') return { groupLabel: null, leafLabel: 'Account Settings' };
  const tree = treeForRole(role);
  for (const node of tree) {
    if (node.id === id) return { groupLabel: null, leafLabel: node.label };
    if (node.children) {
      const child = node.children.find((c) => c.id === id);
      if (child) return { groupLabel: node.label, leafLabel: child.label };
    }
  }
  // Not a leaf of this role's own tree (one of OWNER_EXTRA_ROUTE_IDS, folded
  // into a composite page but still a valid navigation target) — fall back
  // to the other role's tree so the topbar still shows a real label instead
  // of the bare route id.
  const other = treeForRole(role === 'owner' ? 'admin' : 'owner');
  for (const node of other) {
    if (node.id === id) return { groupLabel: null, leafLabel: node.label };
    if (node.children) {
      const child = node.children.find((c) => c.id === id);
      if (child) return { groupLabel: node.label, leafLabel: child.label };
    }
  }
  // Last resort: one of the *_EXTRA_ROUTE_IDS ids (an ex-sidebar-child,
  // reached only as a deep link now) — labelled and grouped, rather than
  // falling through to the bare route id.
  if (EXTRA_LABELS[id]) return { groupLabel: EXTRA_LABELS[id].group, leafLabel: EXTRA_LABELS[id].label };
  return { groupLabel: null, leafLabel: id };
}

/** Topbar subtitle per route id — one sentence, why this page exists. */
export const PAGE_SUB = {
  today: 'Everything that needs your attention right now — nothing you have to go looking for.',
  onhand: 'What you have, what needs attention, and what to do about it — one workspace.',
  receiving: 'Record a delivery and see what has recently come in.',
  expiry: 'Oldest stock first, so it gets used before it turns.',
  stockcounts: 'Compare what the system expects on hand against a physical count.',
  losses: 'Stock loss and consumption that looked different from normal — a flag to look into, never an accusation.',
  unli: 'Weigh-ins, sign-off, and the portion rate they’re measured against.',
  unlisummary: 'How Unlimited Wings consumption has tracked against the estimate.',
  movements: 'Every stock change, in order — received, used, wasted, counted, corrected.',
  menu: 'What we’re selling, for how much, and whether it’s available right now.',
  recipes: 'What each menu item is made of, and what it costs to make.',
  combos: 'What sells together (from real order history), combo ideas worth a real price, and what to publish to the POS.',
  forecasts: 'Predicted demand for each menu item, and how well that prediction has held up.',
  purchasinghub: 'What needs buying, why, and what to do about it — plus the orders already in motion.',
  whattobuy: 'What needs buying, why, and how much.',
  purchasing: 'Purchase orders being processed, and every order ever placed, in one place.',
  salesoverview: 'How the business is performing, and how it compares to before.',
  orderhistory: 'Every past order, searchable and ready to inspect.',
  bestsellers: 'What sells the most, and how that’s shifted over time.',
  inventoryoverview: 'Current stock, its value, and what needs a closer look.',
  reports: 'Sales summary, best sellers and inventory valuation — generate and export a report for any period.',
  historicalrecords: 'A jumping-off point into every historical record the business keeps.',
  downloads: 'Printable and exportable records.',
  stockreplenishment: 'Inventory visibility and purchase recommendations — what to buy, what is expiring, and why.',
  costslosses: 'Inventory value, recorded losses, unusual consumption, and unlimited-wings reconciliation — all read-only.',
  reportshistory: 'Historical business records, year over year, with downloadable reports.',
  instructions: 'Operational tasks from Owner to Admin, tracked from open to complete.',
  verify: 'Setup numbers that still need a real, confirmed value.',
  users: 'Who has access, and what they can do.',
  audit: 'Who changed what, and when.',
  settings: 'Your account — who you are signed in as, and your password.',
};
