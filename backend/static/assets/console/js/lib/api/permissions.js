import { api } from './client.js';

/**
 * Admin-configurable page visibility (wt/permissions.py).
 *
 * ROUTE MAP:
 *   GET /permissions/mine       -> GET /console/permissions/pages
 *   GET /permissions/all        -> GET /console/admin/permissions
 *   PUT /permissions/:role/:key -> PUT /console/admin/permissions
 *
 * Distinct from the write-capability matrix on System & Settings
 * ("Who can do what", getCapabilityMatrix below) — this is a
 * coarser, actually-editable layer: which console PAGES a role can reach at
 * all, not which individual actions it can take once there.
 */

/** What the CALLING role can currently see. Every signed-in user may ask
 * this about themselves; used to build the sidebar at boot. */
export async function getMyPageGrants() {
  const out = await api.get('/console/permissions/pages');
  return out.grants;
}

/** The full editable matrix — admin only (permissions.manage). */
export async function getAllPageGrants() {
  const out = await api.get('/console/admin/permissions');
  return { pageKeys: out.pageKeys, grants: out.grants };
}

/** Grant or revoke one role's access to one page — admin only. Returns that
 * role's new effective grants. */
export async function setPageGrant(role, pageKey, allowed) {
  const out = await api.put('/console/admin/permissions', {
    role, page_key: pageKey, allowed,
  });
  return out.grants;
}

/**
 * The LIVE capability matrix — `GET /console/capabilities`.
 *
 * Replaces the hand-transcribed CAPABILITY_REFERENCE table that used to live
 * in system.js. That table carried a comment recording that a previous
 * transcription had already drifted out of step with wt/auth.py's real
 * CAPABILITIES dict, and a TODO asking for exactly this endpoint — because a
 * permission table that can silently disagree with the one being enforced is
 * worse than showing no table at all.
 *
 * Shaped into one row per raw capability key (e.g. "order.create") with a
 * boolean per role, matching the table System.js already renders — labels
 * are the raw policy strings, not hand-picked prose, on purpose: inventing a
 * grouped human label here would recreate the exact second copy of the
 * policy this endpoint exists to stop maintaining.
 *
 * @returns {Promise<{capability:string, cashier:boolean, kitchen:boolean,
 *   manager:boolean, admin:boolean}[]>}
 */
export async function getCapabilityMatrix() {
  const out = await api.get('/console/capabilities');
  const roles = out.roles || ['cashier', 'kitchen', 'manager', 'admin'];
  return Object.entries(out.capabilities || {}).map(([capability, allowed]) => {
    const row = { capability };
    for (const role of roles) row[role] = allowed.includes(role);
    return row;
  });
}
