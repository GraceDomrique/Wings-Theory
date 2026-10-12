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

const G = (v) => v; // no-op marker for readability where an svg path is long

export const ADMIN_TREE = [
  {
    id: 'today',
    label: 'Today',
    icon: G('<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>'),
  },
  {
    id: 'grp-inventory',
    label: 'Inventory',
    icon: G('<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>'),
    children: [
      { id: 'onhand', label: 'On Hand' },
      { id: 'receiving', label: 'Receiving' },
      { id: 'expiry', label: 'Expiry' },
      { id: 'stockcounts', label: 'Stock Counts' },
      { id: 'losses', label: 'Losses & Variance' },
      { id: 'unli', label: 'Unlimited Wings' },
      { id: 'movements', label: 'Movements' },
    ],
  },
  {
    id: 'grp-menu',
    label: 'Menu & Recipes',
    icon: G('<path d="M4 19.5A2.5 2.5 0 016.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z"/>'),
    children: [
      { id: 'menu', label: 'Menu' },
      { id: 'recipes', label: 'Recipes & Costing' },
      { id: 'combos', label: 'Combos' },
    ],
  },
  {
    id: 'grp-purchasing',
    label: 'Purchasing',
    icon: G('<path d="M6 2l1.5 5h9L18 2"/><path d="M3 7h18l-1.7 10.2A2 2 0 0117.3 19H6.7a2 2 0 01-2-1.8L3 7z"/><circle cx="9" cy="22" r="1"/><circle cx="16" cy="22" r="1"/>'),
    children: [
      { id: 'forecasts', label: 'Demand Forecasts' },
      { id: 'whattobuy', label: 'What to Buy' },
      { id: 'purchaseorders', label: 'Purchase Orders' },
      { id: 'purchasehistory', label: 'Purchase History' },
    ],
  },
  {
    id: 'grp-sales',
    label: 'Sales & History',
    icon: G('<path d="M3 3v18h18"/><path d="M7 15l4-6 4 3 5-8"/>'),
    children: [
      { id: 'salesoverview', label: 'Sales Overview' },
      { id: 'orderhistory', label: 'Order History' },
      { id: 'bestsellers', label: 'Best Sellers' },
      { id: 'combinations', label: 'Product Combinations' },
      // Not in the spec's literal Admin tree, but valuation and the
      // printable Attention Needed list (spec section 27's "Business
      // Reports") pre-existed here and have no other home on Admin's side
      // — dropping them would violate "preserve current functionality"
      // (spec section 45). Owner reaches the identical page under Reports
      // & History.
      { id: 'reports', label: 'Business Reports' },
    ],
  },
  {
    id: 'grp-admin',
    label: 'Administration',
    icon: G('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.34 1.87l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.7 1.7 0 00-1.87-.34 1.7 1.7 0 00-1.04 1.56V21a2 2 0 01-4 0v-.09A1.7 1.7 0 009 19.35a1.7 1.7 0 00-1.87.34l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.7 1.7 0 004.65 15a1.7 1.7 0 00-1.56-1.04H3a2 2 0 010-4h.09A1.7 1.7 0 004.65 9a1.7 1.7 0 00-.34-1.87l-.06-.06a2 2 0 112.83-2.83l.06.06A1.7 1.7 0 009 4.65a1.7 1.7 0 001.04-1.56V3a2 2 0 014 0v.09A1.7 1.7 0 0015 4.65a1.7 1.7 0 001.87-.34l.06-.06a2 2 0 112.83 2.83l-.06.06A1.7 1.7 0 0019.35 9a1.7 1.7 0 001.56 1.04H21a2 2 0 010 4h-.09A1.7 1.7 0 0019.4 15z"/>'),
    children: [
      { id: 'verify', label: 'Setup & Verification', badge: 'verification' },
      { id: 'users', label: 'Users & Access' },
      { id: 'models', label: 'System & Models' },
      { id: 'dataintegrity', label: 'Data Integrity' },
      { id: 'audit', label: 'Audit Trail' },
      { id: 'settings', label: 'Settings' },
    ],
  },
  {
    id: 'instructions',
    label: 'Instructions',
    icon: G('<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>'),
    badge: 'instructions',
  },
];

export const OWNER_TREE = [
  {
    id: 'today',
    label: 'Overview',
    icon: G('<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>'),
  },
  {
    id: 'grp-purchasing',
    label: 'Purchasing',
    icon: G('<path d="M6 2l1.5 5h9L18 2"/><path d="M3 7h18l-1.7 10.2A2 2 0 0117.3 19H6.7a2 2 0 01-2-1.8L3 7z"/><circle cx="9" cy="22" r="1"/><circle cx="16" cy="22" r="1"/>'),
    children: [
      { id: 'forecasts', label: 'Demand Forecasts' },
      { id: 'whattobuy', label: 'Purchase Needs' },
      { id: 'purchaseorders', label: 'Purchase Orders' },
      { id: 'purchasehistory', label: 'Purchase History' },
    ],
  },
  {
    id: 'grp-business',
    label: 'Business Performance',
    icon: G('<path d="M3 3v18h18"/><path d="M7 15l4-6 4 3 5-8"/>'),
    children: [
      { id: 'salesoverview', label: 'Sales' },
      { id: 'orderhistory', label: 'Orders' },
      { id: 'bestsellers', label: 'Best Sellers' },
      { id: 'combinations', label: 'Product Combinations' },
    ],
  },
  {
    id: 'grp-invloss',
    label: 'Inventory & Losses',
    icon: G('<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>'),
    children: [
      { id: 'inventoryoverview', label: 'Inventory Overview' },
      { id: 'expiry', label: 'Expiry' },
      { id: 'losses', label: 'Losses' },
      { id: 'unlisummary', label: 'Unlimited Wings Summary' },
    ],
  },
  {
    id: 'grp-reports',
    label: 'Reports & History',
    icon: G('<path d="M4 19V5a2 2 0 012-2h9l5 5v11a2 2 0 01-2 2H6a2 2 0 01-2-2z"/><path d="M9 13h6M9 17h6M9 9h2"/>'),
    children: [
      { id: 'reports', label: 'Business Reports' },
      { id: 'historicalrecords', label: 'Historical Records' },
      { id: 'downloads', label: 'Downloads' },
      // 2026-10: owner gained read-only audit.read (see wt/auth.py) so the
      // accountability record — who changed what, where, and when, across
      // both roles — isn't admin-only. Same route id/component as Admin's
      // Audit Trail; this grants no new capability, just a sidebar entry
      // pointing at a page owner can now actually reach.
      { id: 'audit', label: 'Audit Trail' },
    ],
  },
  {
    id: 'instructions',
    label: 'Instructions',
    icon: G('<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>'),
    badge: 'instructions',
  },
];

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
 * 'expiry') is open to both. */
export function routeRoles(id) {
  const roles = [];
  if (leafIds(ADMIN_TREE).includes(id)) roles.push('admin');
  if (leafIds(OWNER_TREE).includes(id)) roles.push('owner');
  return roles;
}

export const ROUTE_IDS = [...new Set([...leafIds(ADMIN_TREE), ...leafIds(OWNER_TREE)])];

/** {groupLabel, leafLabel} for the breadcrumb and topbar title, resolved
 * against the CURRENT role's tree (the same id can carry a different label
 * — or a different parent group entirely — depending on who's looking). A
 * standalone link (Today/Instructions) has no group, so groupLabel is null. */
export function breadcrumbFor(role, id) {
  const tree = treeForRole(role);
  for (const node of tree) {
    if (node.id === id) return { groupLabel: null, leafLabel: node.label };
    if (node.children) {
      const child = node.children.find((c) => c.id === id);
      if (child) return { groupLabel: node.label, leafLabel: child.label };
    }
  }
  return { groupLabel: null, leafLabel: id };
}

/** Topbar subtitle per route id — one sentence, why this page exists. */
export const PAGE_SUB = {
  today: 'Everything that needs your attention right now — nothing you have to go looking for.',
  onhand: 'Every ingredient, how much is on hand, and how long it will last.',
  receiving: 'Record a delivery and see what has recently come in.',
  expiry: 'Oldest stock first, so it gets used before it turns.',
  stockcounts: 'Compare what the system expects on hand against a physical count.',
  losses: 'Stock loss and consumption that looked different from normal — a flag to look into, never an accusation.',
  unli: 'Weigh-ins, sign-off, and the portion rate they’re measured against.',
  unlisummary: 'How Unlimited Wings consumption has tracked against the estimate.',
  movements: 'Every stock change, in order — received, used, wasted, counted, corrected.',
  menu: 'What we’re selling, for how much, and whether it’s available right now.',
  recipes: 'What each menu item is made of, and what it costs to make.',
  combos: 'What sells together, combo ideas worth a real price, and what to publish to the POS.',
  forecasts: 'Predicted demand for each menu item, and how well that prediction has held up.',
  whattobuy: 'What needs buying, why, and how much.',
  purchaseorders: 'Purchases currently being processed, from draft to received.',
  purchasehistory: 'Every purchase order ever placed, searchable by date and supplier.',
  salesoverview: 'How the business is performing, and how it compares to before.',
  orderhistory: 'Every past order, searchable and ready to inspect.',
  bestsellers: 'What sells the most, and how that’s shifted over time.',
  combinations: 'What customers commonly order together.',
  inventoryoverview: 'Current stock, its value, and what needs a closer look.',
  reports: 'Inventory value and a printable list of what needs attention.',
  historicalrecords: 'A jumping-off point into every historical record the business keeps.',
  downloads: 'Printable and exportable records.',
  instructions: 'Operational tasks from Owner to Admin, tracked from open to complete.',
  verify: 'Setup numbers that still need a real, confirmed value.',
  users: 'Who has access, and what they can do.',
  models: 'How the forecasting, anomaly and combination models are performing.',
  dataintegrity: 'Automatic structural checks over the stored data itself.',
  audit: 'Who changed what, and when.',
  settings: 'Account security.',
};
