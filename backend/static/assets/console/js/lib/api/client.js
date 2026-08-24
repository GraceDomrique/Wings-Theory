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

const TOKEN_KEY = 'wt.console.token';
const USER_KEY = 'wt.console.user';

export const session = {
  get token() { return localStorage.getItem(TOKEN_KEY); },
  get user() {
    try { return JSON.parse(localStorage.getItem(USER_KEY) || 'null'); } catch { return null; }
  },
  start(token, user) {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY, JSON.stringify(user));
  },
  end() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
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

/**
 * Sign in. Returns the principal the sidebar shows.
 * POST /auth/login -> { token, role, display_name, must_change_password }
 */
export async function login(username, password) {
  const out = await request('POST', '/auth/login', { username, password });
  session.start(out.token, {
    username,
    displayName: out.display_name || username,
    role: out.role,
    mustChangePassword: out.must_change_password,
  });
  return session.user;
}
