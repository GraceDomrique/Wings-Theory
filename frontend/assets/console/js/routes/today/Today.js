import { session } from '../../lib/api/client.js';
import * as AdminToday from './AdminToday.js';
import * as OwnerOverview from './OwnerOverview.js';

/**
 * Route id 'today' is shared by both roles (nav.js: labeled "Today" for
 * Admin, "Overview" for Owner — see that file's own docstring on why the
 * same id can carry two labels). The two roles see genuinely different
 * pages now (2026-10 reference-design rebuild): Admin gets the operational
 * landing screen (AdminToday.js), Owner gets the business-at-a-glance
 * screen (OwnerOverview.js) — not a shared component with role-conditional
 * panels, because the two pages answer different questions ("what do I do
 * right now" vs. "how is the business doing").
 */
export async function render(ctx) {
  const role = (session.user || {}).role;
  return role === 'owner' ? OwnerOverview.render(ctx) : AdminToday.render(ctx);
}
