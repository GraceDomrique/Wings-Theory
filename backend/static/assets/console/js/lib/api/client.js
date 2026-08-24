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

export const session = {
  get token() { return localStorage.getItem(TOKEN_KEY); },
  get user() {
    const role = localStorage.getItem(ROLE_KEY);
    if (!role) return null;
    return { displayName: localStorage.getItem(NAME_KEY) || '', role };
  },
  start(token, user) {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(ROLE_KEY, user.role);
    localStorage.setItem(NAME_KEY, user.displayName || '');
  },
  end() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(ROLE_KEY);
    localStorage.removeItem(NAME_KEY);
  },
};

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
    const detail = payload && typeof payload === 'object' ? payload.detail : payload;
    throw new ApiError(res.status,
      typeof detail === 'string' ? detail : `Request failed (${res.status}).`, payload);
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
};

// The console is the manager/owner surface — matches POS's ['cashier','admin']
// and KDS's ['kitchen','admin'] guards (pos.html, kds.html), and the old
// console (console.html.bak) before this module's Phase 10 rebuild dropped
// it. Checked both here (refuse before a token is ever stored) and at
// startup in app.js (refuse a token stored by a role that no longer
// qualifies, e.g. after a role change).
export const ALLOWED_ROLES = ['manager', 'admin'];

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
    mustChangePassword: out.must_change_password,
  });
  return session.user;
}
