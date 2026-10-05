import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Button } from '../../components/ui/Button.js';
import { toast } from '../../components/ui/Feedback.js';
import { session } from '../../lib/api/client.js';
import { changePassword, MIN_PASSWORD_LENGTH } from '../../lib/api/account.js';

/**
 * Administration > Settings — account security only. Combo pricing config
 * moved to Menu & Recipes > Combos (2026-10 cleanup) — it configures what
 * that one page does and nobody looking for "change my password" expected
 * food-cost math to be on the same screen. User provisioning/password
 * resets for OTHER accounts live on Users & Access.
 */
export async function render(ctx) {
  return h('div', {}, changePasswordCard());
}

function changePasswordCard() {
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
    } finally {
      submit.disabled = false;
    }
  });

  return Card({},
    CardHeader({ title: 'Change my password', desc: `At least ${MIN_PASSWORD_LENGTH} characters, not the shared default` }),
    h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Current password', current),
    h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'New password', next),
    h('label', { class: 'field', style: { marginBottom: '10px', display: 'block' } }, 'Confirm new password', confirm),
    err, submit);
}
