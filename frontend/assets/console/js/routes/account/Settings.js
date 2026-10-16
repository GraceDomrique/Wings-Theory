import { h, icon } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { toast } from '../../components/ui/Feedback.js';
import { ICONS } from '../../lib/icons.js';
import { session } from '../../lib/api/client.js';
import { changePassword, MIN_PASSWORD_LENGTH } from '../../lib/api/account.js';

/**
 * Account Settings — same single destination for every role as before
 * (pinned at the bottom of the sidebar — see Sidebar.js / nav.js's
 * routeRoles('settings')), same password-change endpoint; a two-column
 * layout, identity on the left, security on the right, sized to match every
 * other full-width page in the console rather than floating as a narrow
 * centered column.
 *
 * The password-change logic is unchanged from the previous version of this
 * file (same changePassword() call, same validation, same "every other
 * device is logged out" behavior) — carried over, not rebuilt.
 */
export async function render() {
  const u = session.user || {};

  return h('div', { class: 'account-settings-grid' },
    identityPanel(u),
    h('div', {},
      securityPanel(),
      securityNotesPanel()));
}

function initials(name) {
  return (name || '?').trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase();
}

const ROLE_LABEL = { admin: 'Administrator', owner: 'Owner', cashier: 'Cashier', kitchen: 'Kitchen' };

// LEFT — Account Identity: a dark charcoal panel (spec: "charcoal profile
// panel with tasteful slate-blue and muted-orange accents") distinct from
// the warm-ivory security card beside it, so the two halves read as
// "who you are" vs. "what you do here" at a glance, not two copies of the
// same white card.
function identityPanel(u) {
  return h('div', { class: 'account-identity-panel' },
    h('div', { class: 'account-avatar' }, initials(u.displayName || u.username)),
    h('div', { class: 'account-identity-name' }, u.displayName || u.username || 'Unknown'),
    u.username && u.username !== u.displayName
      ? h('div', { class: 'account-identity-username' }, '@', u.username)
      : null,
    h('div', { style: { marginTop: '12px' } }, Pill('flat', ROLE_LABEL[u.role] || u.role || 'unknown')),
    h('div', { class: 'account-identity-divider' }),
    h('div', { class: 'account-identity-row' }, icon(ICONS.userRound, 14), h('span', {}, 'Signed in to Wings Theory Operations Console')),
    u.username
      ? h('div', { class: 'account-identity-row' }, icon(ICONS.userRound, 14), h('span', {}, 'Username: ', h('strong', {}, u.username)))
      : null);
}

// A short note below the password form so the right column doesn't read as
// a lone short card floating above empty space on a wide screen — plain,
// factual, no invented data.
function securityNotesPanel() {
  return Card({ className: 'account-security-panel' },
    CardHeader({ title: 'About your account' }),
    h('ul', { class: 't-sm t-soft', style: { margin: 0, paddingLeft: '18px', lineHeight: '1.7' } },
      h('li', {}, 'Changing your password signs out every other device currently signed in to this account.'),
      h('li', {}, 'Your username and display name can only be changed by an administrator, from Users & Access.'),
      h('li', {}, 'This console is reachable only on the restaurant’s local network.')));
}

// RIGHT — Account Security: the password form, unchanged behavior, redrawn
// with labeled icon rows and explicit loading/success/error states.
function securityPanel() {
  const current = h('input', { type: 'password', autocomplete: 'current-password', class: 'field-input' });
  const next = h('input', { type: 'password', autocomplete: 'new-password', class: 'field-input' });
  const confirm = h('input', { type: 'password', autocomplete: 'new-password', class: 'field-input' });
  const err = h('div', { class: 'login-err', style: { display: 'none' } });
  const submit = Button({ variant: 'primary', style: { width: '100%' } }, 'Save Changes');

  function setLoading(isLoading) {
    submit.disabled = isLoading;
    submit.textContent = isLoading ? 'Saving…' : 'Save Changes';
  }

  submit.addEventListener('click', async () => {
    err.style.display = 'none';
    if (!current.value) { err.textContent = 'Enter your current password.'; err.style.display = ''; return; }
    if (next.value.length < MIN_PASSWORD_LENGTH) {
      err.textContent = `New password must be at least ${MIN_PASSWORD_LENGTH} characters.`; err.style.display = ''; return;
    }
    if (next.value !== confirm.value) { err.textContent = 'New password and confirmation do not match.'; err.style.display = ''; return; }
    setLoading(true);
    try {
      await changePassword({ currentPassword: current.value, newPassword: next.value, confirmNewPassword: confirm.value });
      current.value = ''; next.value = ''; confirm.value = '';
      toast('Password changed. Every other signed-in device has been logged out.', 'good');
    } catch (e) {
      err.textContent = e.message; err.style.display = '';
    } finally { setLoading(false); }
  });

  function field(labelText, input) {
    return h('label', { class: 'field account-security-field' }, labelText, input);
  }

  return Card({ className: 'account-security-panel' },
    CardHeader({ title: 'Change Password', desc: `At least ${MIN_PASSWORD_LENGTH} characters, not the shared default` }),
    field('Current Password', current),
    field('New Password', next),
    field('Confirm Password', confirm),
    err,
    h('div', { style: { marginTop: '6px' } }, submit));
}
