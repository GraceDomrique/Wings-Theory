/**
 * The navigation model — spec's eight grouped sections, and nothing else.
 *
 * This replaces the old flat 19-page list. Adding a ninth top-level entry is a
 * design change, not a routing change: if something new needs a home it goes
 * inside one of these eight as a sub-tab (see Tabbar), which is why Inventory,
 * Menu, Intelligence and Verification each own their own `tabs` array.
 *
 * `title` / `sub` are the topbar copy, taken verbatim from the prototype.
 */

export const NAV_GROUPS = [
  {
    label: null,
    items: [{
      id: 'today',
      label: 'Today',
      icon: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    }],
  },
  {
    label: 'Manage',
    items: [
      {
        id: 'inventory',
        label: 'Inventory',
        icon: '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
      },
      {
        id: 'menu',
        label: 'Recipes & Menu',
        icon: '<path d="M4 19.5A2.5 2.5 0 016.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z"/>',
      },
    ],
  },
  {
    label: 'Intelligence',
    items: [
      {
        id: 'intel',
        label: 'Forecast & Insights',
        icon: '<path d="M3 3v18h18"/><path d="M7 15l4-6 4 3 5-8"/>',
      },
      {
        id: 'expiry',
        label: 'Expiry Watch',
        icon: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/><path d="M12 14v3l2 1"/>',
      },
    ],
  },
  {
    label: 'Administer',
    items: [
      {
        id: 'verify',
        label: 'Verification',
        icon: '<path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z"/>',
        badge: 'verification', // count supplied at runtime
      },
      {
        id: 'reports',
        label: 'Reports',
        icon: '<path d="M4 19V5a2 2 0 012-2h9l5 5v11a2 2 0 01-2 2H6a2 2 0 01-2-2z"/><path d="M9 13h6M9 17h6M9 9h2"/>',
      },
      {
        id: 'system',
        label: 'System & Settings',
        icon: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.34 1.87l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.7 1.7 0 00-1.87-.34 1.7 1.7 0 00-1.04 1.56V21a2 2 0 01-4 0v-.09A1.7 1.7 0 009 19.35a1.7 1.7 0 00-1.87.34l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.7 1.7 0 004.65 15a1.7 1.7 0 00-1.56-1.04H3a2 2 0 010-4h.09A1.7 1.7 0 004.65 9a1.7 1.7 0 00-.34-1.87l-.06-.06a2 2 0 112.83-2.83l.06.06A1.7 1.7 0 009 4.65a1.7 1.7 0 001.04-1.56V3a2 2 0 014 0v.09A1.7 1.7 0 0015 4.65a1.7 1.7 0 001.87-.34l.06-.06a2 2 0 112.83 2.83l-.06.06A1.7 1.7 0 0019.35 9a1.7 1.7 0 001.56 1.04H21a2 2 0 010 4h-.09A1.7 1.7 0 0019.4 15z"/>',
      },
    ],
  },
];

/** Topbar copy per route — verbatim from the prototype. */
export const PAGE_COPY = {
  today: ['Today', 'Everything that needs your attention right now — nothing you have to go looking for.'],
  inventory: ['Inventory', 'Every ingredient, how much is on hand, and how long it will last.'],
  menu: ['Recipes & Menu', 'What goes into each menu item, and whether it can be sold right now.'],
  intel: ['Forecast & Insights', 'Prophet, Isolation Forest and FP-Growth — what they predict and what they flag.'],
  expiry: ['Expiry Watch', 'Oldest stock first, so it gets used before it turns.'],
  verify: ['Verification', 'Setup numbers that still need a real, confirmed value.'],
  reports: ['Reports', 'Sales, product combinations and inventory value.'],
  system: ['System & Settings', 'Readiness checks, model details, access and account.'],
};

export const ROUTE_IDS = Object.keys(PAGE_COPY);
