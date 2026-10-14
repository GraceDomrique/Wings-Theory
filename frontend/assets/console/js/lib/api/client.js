/**
 * The one place the console talks to the network.
 *
 * The API is bearer-token gated (wt/auth.py) and every /console route requires
 * a capability, so a token is attached to every request and a 401 drops the
 * session rather than leaving pages half-rendered against a dead token.
 *
 * Nothing here interprets business data. Adapters live in the sibling domain
 * files; this only moves JSON.
 */

// 'wt_console_' + token/role/name, not a single combined-JSON key: this is
// the same three-key scheme landing.html writes for admin/manager
// (DEST.ns) and POS/KDS use for their own roles (wt_pos_*, wt_kds_*) — see
// landing.html's "Signed in as X · Continue" resume flow, which reads
// wt_console_token/role/name directly. A prior version of this module used
// 'wt.console.token'/'wt.console.user' instead, which landing.html never
// wrote to, so signing in on the landing page and clicking Continue into
// the console silently failed to be recognized and re-showed the login
// screen. mustChangePassword is intentionally not persisted here (no
// caller reads it back from storage — it only matters in the instant
// right after login, from the /auth/login response itself).
const NS = 'wt_console_';
const TOKEN_KEY = NS + 'token';
const ROLE_KEY = NS + 'role';
const NAME_KEY = NS + 'name';
// 2026-09-11: added for Owner's "cancel my own instruction/purchase order"
// UI check (the backend is still the real authority — this only decides
// whether to show the button). landing.html's resume flow reads only
// token/role/name, so this additional key is additive and does not affect it.
const USERNAME_KEY = NS + 'username';

// Page-visibility grants (wt/permissions.py) — in-memory only, never
// localStorage. Unlike token/role/name these must never survive as stale
// data across a session: an admin can revoke a grant at any time, and the
// only source of truth is a fresh /console/permissions/pages read at boot.
let _grants = null;

export const session = {
  get token() { return localStorage.getItem(TOKEN_KEY); },
  get user() {
    const role = localStorage.getItem(ROLE_KEY);
    if (!role) return null;
    return {
      displayName: localStorage.getItem(NAME_KEY) || '',
      username: localStorage.getItem(USERNAME_KEY) || '',
      role,
    };
  },
  get grants() { return _grants; },
  setGrants(g) { _grants = g; },
  start(token, user) {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(ROLE_KEY, user.role);
    localStorage.setItem(NAME_KEY, user.displayName || '');
    localStorage.setItem(USERNAME_KEY, user.username || '');
  },
  end() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(ROLE_KEY);
    localStorage.removeItem(NAME_KEY);
    localStorage.removeItem(USERNAME_KEY);
    _grants = null;
  },
};

/**
 * Explicit sign-out. Clears local state immediately — the UI must never
 * wait on the network to show the login screen again — then revokes the
 * session server-side in the background (wt/sessions.py, POST
 * /auth/logout) so the bearer token this tab was using stops working too,
 * including a captured copy replayed directly against the API. Best-effort:
 * an offline/unreachable server, or a token that was already dead (the
 * automatic session.end() a 401 triggers in request() below has already
 * cleared it by the time this runs), must never throw past this function.
 */
export function logout() {
  const token = session.token;
  session.end();
  if (token) {
    fetch('/auth/logout', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    }).catch(() => {});
  }
}

// Frontend mirror of the backend's role split (wt/auth.py CAPABILITIES,
// 2026-09 revision): admin holds every write capability, manager is
// read/monitoring-only. This is UI convenience ONLY — hiding/disabling a
// control a manager isn't meant to see — never the actual authorization
// boundary. Every mutating endpoint still checks the caller's role
// server-side via needs(); a manager who bypasses the UI (devtools, a
// crafted request) is refused there with a 403 regardless of what this
// returns. See routes/* for where this gates Add/Edit/Delete/Save controls.
export function canWrite() {
  return (session.user || {}).role === 'admin';
}

/** Admin-configurable page visibility (wt/permissions.py) — same UI-
 * convenience-only caveat as canWrite() above: the real boundary is the
 * backend's needs_page() check, added alongside needs() on the handful of
 * routes each gated page actually reads. Admin is always true. A manager
 * whose grants haven't loaded yet (still fetching, or the fetch failed)
 * reads false — fail closed, never show a page speculatively. */
export function canView(pageKey) {
  if ((session.user || {}).role === 'admin') return true;
  return !!(session.grants && session.grants[pageKey]);
}

/** Raised for any non-2xx response. `status` lets callers treat 403 (not
 *  permitted for this role) differently from 500 (broken). */
export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

let onUnauthorized = () => {};
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

async function request(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  const token = session.token;
  if (token) headers.Authorization = `Bearer ${token}`;

  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError(0, 'The server did not respond. Is it running?', null);
  }

  // A 401 from the login call itself is "wrong password", not "session
  // expired" — it must not tear down a session or bounce the user, or the
  // login form would clear itself on every typo.
  if (res.status === 401 && path !== '/auth/login') {
    session.end();
    onUnauthorized();
    throw new ApiError(401, 'Session expired — please sign in again.', null);
  }

  const text = await res.text();
  let payload = null;
  if (text) { try { payload = JSON.parse(text); } catch { payload = text; } }

  if (!res.ok) {
    // Two error shapes reach here: this project's own WTError handler
    // (wt/errors.py) emits {code, message, detail: {...kwargs}} — the
    // human-readable string is `message`, `detail` is a kwargs object, not
    // text. Plain FastAPI HTTPException (still used for a handful of
    // routes) emits {detail: "..."} instead, with no `message` key at all.
    // Try message first, then a string detail, before giving up.
    let human = null;
    if (payload && typeof payload === 'object') {
      if (typeof payload.message === 'string') human = payload.message;
      else if (typeof payload.detail === 'string') human = payload.detail;
    } else if (typeof payload === 'string') {
      human = payload;
    }
    throw new ApiError(res.status,
      human || `Request failed (${res.status}).`, payload);
  }
  return payload;
}

export const api = {
  get: (path, params) => {
    const qs = params
      ? '?' + new URLSearchParams(
        Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
      ).toString()
      : '';
    return request('GET', path + qs);
  },
  post: (path, body) => request('POST', path, body),
  patch: (path, body) => request('PATCH', path, body),
  put: (path, body) => request('PUT', path, body),
};

// The console is the manager/admin/owner surface — matches POS's
// ['cashier','admin'] and KDS's ['kitchen','admin'] guards (pos.html,
// kds.html), and the old console (console.html.bak) before this module's
// Phase 10 rebuild dropped it. Checked both here (refuse before a token is
// ever stored) and at startup in app.js (refuse a token stored by a role
// that no longer qualifies, e.g. after a role change).
//
// 2026-09-11: 'owner' added — a genuinely separate, read-only executive
// role (see wt/auth.py's CAPABILITIES comment). It reuses this same console
// shell/session, but sees its own nav group and route set (layout/nav.js)
// rather than manager's/admin's operational pages.
//
// 2026-09-16: 'manager' removed. The role itself still exists (its account
// keeps its historical data/grants and shows up as an inactive row in the
// System & Settings roster), but the one seeded manager account was
// deactivated (migration 0034) and no new one can be created (api.py's
// create_user already refuses role='manager'). Login for that account
// already fails server-side (app_user is queried WHERE is_active=1), so
// this is defense-in-depth: even a still-valid manager JWT issued before
// deactivation is refused entry to the console shell itself.
export const ALLOWED_ROLES = ['admin', 'owner'];

/**
 * Sign in. Returns the principal the sidebar shows.
 * POST /auth/login -> { token, role, display_name, must_change_password }
 */
export async function login(username, password) {
  const out = await request('POST', '/auth/login', { username, password });
  if (!ALLOWED_ROLES.includes(out.role)) {
    throw new ApiError(403,
      `The ${out.role} role does not have access to the console.`, null);
  }
  session.start(out.token, {
    username,
    displayName: out.display_name || username,
    role: out.role,
  });
  // mustChangePassword is folded in here, not stored by session.start (see
  // the note above NS) -- it only matters for the instant right after login,
  // to decide whether Login.js shows the forced change-password step before
  // ever handing the caller into the shell.
  return { ...session.user, mustChangePassword: !!out.must_change_password };
}
