import { api, session } from './client.js';

/**
 * Account and admin user management — restored from the old console, which
 * had these under Settings.
 *
 * ROUTE MAP:
 *   POST /auth/change_password                    — self-service
 *   POST /console/users/{username}/reset_password  — admin-only (user.manage)
 */

const MIN_PASSWORD_LENGTH = 10; // mirrors wt/auth.py's MIN_PASSWORD_LENGTH, display-only

/**
 * Self-service password change. Requires the caller's own current password —
 * a valid session token is not enough on its own. On success the backend
 * advances password_changed_at, which invalidates every other token already
 * issued for this account (every other signed-in device is logged out), and
 * returns a fresh token for THIS session so the caller is not logged out by
 * their own change.
 */
export async function changePassword({ currentPassword, newPassword, confirmNewPassword }) {
  const out = await api.post('/auth/change_password', {
    current_password: currentPassword,
    new_password: newPassword,
    confirm_new_password: confirmNewPassword,
  });
  if (out.token) {
    const user = session.user;
    session.start(out.token, user);
  }
  return out;
}

/**
 * Admin-only emergency reset. Generates a random password server-side and
 * returns it exactly once, in this response only — it is never logged or
 * stored anywhere else, so losing this response means resetting again.
 * Flags must_change_password on the account.
 */
export function resetPassword(username) {
  return api.post(`/console/users/${encodeURIComponent(username)}/reset_password`);
}

/**
 * Every account and its role — never password fields. Admin-only
 * (user.manage), same as resetPassword above.
 */
export async function listUsers() {
  const out = await api.get('/console/users');
  return (out.users || []).map((u) => ({
    username: u.username,
    displayName: u.display_name,
    role: u.role,
    mustChangePassword: !!u.must_change_password,
    lockedUntil: u.locked_until || null,
    isActive: !!u.is_active,
    lastLoginAt: u.last_login_at || null,
  }));
}

/**
 * Provision a new account (2026-09-11 — there was no way to do this before).
 * Same one-time-secret contract as resetPassword: the temp password is
 * returned exactly once, in this response, and never stored or logged
 * anywhere else.
 */
export async function createUser({ username, displayName, role }) {
  const out = await api.post('/console/users', {
    username, display_name: displayName, role,
  });
  return { username: out.username, role: out.role, temporaryPassword: out.temporary_password };
}

/**
 * Admin-only rename of an account's display name (never the username used
 * to sign in). Same capability as resetPassword/createUser above.
 */
export function updateDisplayName(username, displayName) {
  return api.patch(`/console/users/${encodeURIComponent(username)}/display_name`, {
    display_name: displayName,
  });
}

/**
 * Admin-only rename of the username used to sign in. Signs that account out
 * everywhere (same mechanism as a password change) — the account must sign
 * in again with the new username afterward.
 */
export function updateUsername(username, newUsername) {
  return api.patch(`/console/users/${encodeURIComponent(username)}/username`, {
    new_username: newUsername,
  });
}

/**
 * POS/Kitchen station lock (migration 0056) — admin-only (user.manage).
 * Closing a station refuses sign-in for that role at /auth/login, even with
 * the correct password, until an admin reopens it.
 */
export async function getSystemLock() {
  return api.get('/console/system-lock');
}

export async function setSystemLock(station, closed, reason) {
  return api.post(`/console/system-lock/${encodeURIComponent(station)}`, { closed, reason });
}

export { MIN_PASSWORD_LENGTH };
