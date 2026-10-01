import { api, session } from './api/client.js';

/**
 * UI-visibility view of the backend's capability policy.
 *
 * Source of truth is wt/auth.py's CAPABILITIES, already served live by
 * GET /console/capabilities (any signed-in role may read it). The caller's
 * role is already in `session.user.role`. So `can(cap)` is just "is my role
 * in the list the backend itself publishes" - no second copy of the policy,
 * no new endpoint.
 *
 * This only decides whether a control is SHOWN. Every endpoint still
 * enforces its capability server-side via needs(); a hidden button reached
 * through devtools gets a 403 regardless of what this returns. It also
 * cannot express the finer per-record rules the backend applies inside a
 * handler (e.g. Owner may cancel only their OWN draft order) - those still
 * need a per-row check by the caller, on top of `can()`.
 *
 * Fails closed: until the policy has loaded, or if it failed to load,
 * `can()` is false.
 */

let cache = null; // { role, policy: {cap: [roles]} }
let inflight = null;

function roleNow() { return (session.user || {}).role || null; }

/** Pure: is `role` allowed by `policy[cap]`. Exported for tests. */
export function allowed(policy, role, cap) {
  if (!policy || !role) return false;
  const roles = policy[cap];
  return Array.isArray(roles) && roles.includes(role);
}

/** Load (once per signed-in role) the published policy. Never throws. */
export function loadCapabilities() {
  const role = roleNow();
  if (!role) return Promise.resolve(null);
  if (cache && cache.role === role) return Promise.resolve(cache.policy);
  if (inflight && inflight.role === role) return inflight.promise;
  const promise = api.get('/console/capabilities')
    .then((out) => {
      cache = { role, policy: out.capabilities || {} };
      return cache.policy;
    })
    .catch(() => null)
    .finally(() => { if (inflight && inflight.role === role) inflight = null; });
  inflight = { role, promise };
  return promise;
}

/** Synchronous check against the loaded policy (false until loaded). */
export function can(cap) {
  const role = roleNow();
  return !!(cache && cache.role === role && allowed(cache.policy, role, cap));
}

/** True if the role holds at least one of the given capabilities. */
export function canAny(...caps) { return caps.some(can); }

/** Forget the cached policy (call on sign-out / role change). */
export function resetCapabilities() { cache = null; inflight = null; }
