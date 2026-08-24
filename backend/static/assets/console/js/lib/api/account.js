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

export { MIN_PASSWORD_LENGTH };
