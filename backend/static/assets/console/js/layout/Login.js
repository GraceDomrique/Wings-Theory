import { h } from '../lib/dom.js';
import { login } from '../lib/api/client.js';

const EYE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 7 11 7a13.16 13.16 0 0 1-1.67 2.68M6.61 6.61A13.5 13.5 0 0 0 1 11s4 7 11 7a9.26 9.26 0 0 0 4.39-1.02M14.12 14.12a3 3 0 1 1-4.24-4.24"/><path d="M1 1l22 22"/></svg>';

/**
 * Sign-in gate — deliberately built to match POS/KDS's login exactly (same
 * dark brand.css/brand.js system, same logo, same wing-strike intro), by
 * explicit decision: this is the one screen where visual continuity with the
 * rest of the product matters more than the Console's own light identity.
 * Every screen past this point uses the Console's own tokens.css/app.css —
 * see styles/login-brand.css for why the two palettes are kept from touching.
 *
 * The intro replays once per cold arrival, exactly like POS/KDS: an
 * `?intro=done` query param (set by the landing page after it already played
 * the sequence once) skips it so the same three-second animation is never
 * shown twice in a row to someone who just watched it.
 */
export function Login({ onSuccess, expired = false }) {
  const username = h('input', { id: 'wt-luser', class: 'wt-field', type: 'text', autocomplete: 'username' });
  const password = h('input', { id: 'wt-lpass', class: 'wt-field', type: 'password', autocomplete: 'current-password' });
  const passToggle = h('button', {
    type: 'button', class: 'wt-pass-toggle', 'aria-label': 'Show password', 'aria-pressed': 'false',
    html: EYE,
  });
  const error = h('div', { class: 'wt-login-err' }, expired ? 'Your session timed out. Sign in again.' : '');
  const submit = h('button', { class: 'wt-btn', type: 'submit' }, 'Sign in');

  passToggle.addEventListener('click', () => {
    const showing = password.type === 'text';
    password.type = showing ? 'password' : 'text';
    passToggle.innerHTML = showing ? EYE : EYE_OFF;
    passToggle.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
    passToggle.setAttribute('aria-pressed', showing ? 'false' : 'true');
  });

  async function attempt(e) {
    e.preventDefault();
    error.textContent = '';
    submit.disabled = true;
    submit.textContent = 'Signing in…';
    try {
      const user = await login(username.value.trim(), password.value);
      onSuccess(user);
    } catch (err) {
      error.textContent = err.status === 401
        ? 'That username and password do not match an active account.'
        : err.message;
      password.value = '';
      password.focus();
    } finally {
      submit.disabled = false;
      submit.textContent = 'Sign in';
    }
  }

  const form = h('form', { onSubmit: attempt },
    h('label', { for: 'wt-luser' }, 'Username'), username,
    h('label', { for: 'wt-lpass' }, 'Password'),
    h('div', { class: 'wt-pass-wrap' }, password, passToggle),
    submit,
    error);

  const el = h('div', { class: 'wt-login' },
    h('div', { class: 'wt-login-box' },
      h('div', { class: 'wt-login-mark' }, h('img', { src: '/img/logo.png', alt: 'Wings Theory' })),
      h('h1', {}, 'Wings Theory'),
      h('p', { class: 'lead' }, 'Operations console — manager sign in'),
      form));

  requestAnimationFrame(() => username.focus());

  // Arriving cold plays the branded sequence, same rule POS/KDS use: the
  // landing page's own LOGIN button already ran it once and hands off with
  // ?intro=done, so it is never shown twice back to back.
  if (window.WingsIntro && typeof window.WingsIntro.play === 'function' &&
      new URLSearchParams(location.search).get('intro') !== 'done') {
    window.WingsIntro.play();
  }

  return el;
}
