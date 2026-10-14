import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { toast } from '../../components/ui/Feedback.js';
import { session } from '../../lib/api/client.js';
import { changePassword, MIN_PASSWORD_LENGTH } from '../../lib/api/account.js';

/**
 * Account Settings — a real destination for "who am I signed in as, and how
 * do I change my own password", reached the same way for every role (pinned
 * at the bottom of the sidebar, below the role-specific tree — see
 * Sidebar.js and nav.js's routeRoles('settings')).
 *
 * The password-change logic here is the SAME call Users & Access used to
 * host (POST /auth/change_password via lib/api/account.js's changePassword)
 * — moved here, not duplicated. Users & Access keeps everything that is
 * actually administrator-controlled account management (reset someone
 * else's password, create an account, edit capabilities); this page is only
 * ever about the signed-in account's own login.
 */
export async function render() {
  const u = session.user || {};

  const whoAmI = Card({},
    CardHeader({ title: 'Signed in as' }),
    h('div', { style: { display: 'flex', alignItems: 'center', gap: '14px' } },
      h('div', {
        class: 'avatar', style: { width: '44px', height: '44px', fontSize: '15px' },
      }, initials(u.displayName || u.username)),
      h('div', {},
        h('div', { style: { fontWeight: '600', fontSize: '14.5px' } }, u.displayName || u.username),
        h('div', { class: 't-xs t-soft', style: { marginTop: '2px' } }, u.username),
        h('div', { style: { marginTop: '6px' } }, Pill('flat', u.role || 'unknown')))));

  return h('div', {},
    h('div', { style: { marginBottom: '14px' } }, whoAmI),
    myPasswordCard());
}

function initials(name) {
  return (name || '?').trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase();
}

/**
 * Self-service password change — identical to the one Users & Access hosted
 * before this page existed (same changePassword() call, same validation,
 * same "every other device is logged out" behavior). Exported for nothing
 * else — this is the one place it's used now.
 */
function myPasswordCard() {
  const current = h('input', { type: 'password', autocomplete: 'current-password' });
  const next = h('input', { type: 'password', autocomplete: 'new-password' });
  const confirm = h('input', { type: 'password', autocomplete: 'new-password' });
  const err = h('div', { class: 'login-err', style: { display: 'none' } });
  const submit = Button({ variant: 'primary', size: 'sm' }, 'Change password');
  submit.addEventListener('click', async () => {
    err.style.display = 'none';
    if (!current.value) { err.textContent = 'Enter your current password.'; err.style.display = ''; return; }
    if (next.value.length < MIN_PASSWORD_LENGTH) {
      err.textContent = `New password must be at least ${MIN_PASSWORD_LENGTH} characters.`; err.style.display = ''; return;
    }
    if (next.value !== confirm.value) { err.textContent = 'New password and confirmation do not match.'; err.style.display = ''; return; }
    submit.disabled = true;
    try {
      await changePassword({ currentPassword: current.value, newPassword: next.value, confirmNewPassword: confirm.value });
      current.value = ''; next.value = ''; confirm.value = '';
      toast('Password changed. Every other signed-in device has been logged out.', 'good');
    } catch (e) {
      err.textContent = e.message; err.style.display = '';
    } finally { submit.disabled = false; }
  });
  return Card({},
    CardHeader({ title: 'Change my password', desc: `At least ${MIN_PASSWORD_LENGTH} characters, not the shared default` }),
    h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Current password', current),
    h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'New password', next),
    h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Confirm new password', confirm),
    err, submit);
}
