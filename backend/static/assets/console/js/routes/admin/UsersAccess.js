import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError } from '../../components/ui/Modal.js';
import { toast } from '../../components/ui/Feedback.js';
import { ageFromNow } from '../../lib/format.js';
import { session } from '../../lib/api/client.js';
import { listUsers, createUser, resetPassword } from '../../lib/api/account.js';
import { getCapabilityMatrix } from '../../lib/api/permissions.js';

/**
 * Administration > Users & Access — spec section 21ish (the "who has
 * access" half of the old System & Settings page).
 *
 * 2026-10 redesign: accounts moved from a dense row-table (username,
 * display name, role, status and "Reset password" all crammed into one
 * thin row) to scannable cards — status, lockout and last-sign-in are now
 * visible without reading across a wide table. The capability matrix moved
 * from one flat 30-row table to grouped, collapsible sections with a text
 * filter, since that table only grows. getCapabilityMatrix() already
 * derives its role columns from the server's real `roles` list
 * (auth.ROLES, which includes 'owner') — the OLD version of this page just
 * hardcoded 4 columns and silently dropped Owner; this version renders
 * whatever roles the server actually returns, so the display can't drift
 * out of step with the policy again the way the old hand-picked column
 * list eventually would have.
 */

const ROLE_COLOR = {
  admin: { bg: '#FFE7C2', fg: '#A65C00' },
  owner: { bg: '#E5EEF6', fg: '#3D6A96' },
  manager: { bg: '#F0EDE4', fg: '#6E6656' },
  cashier: { bg: '#F0EDE4', fg: '#6E6656' },
  kitchen: { bg: '#F0EDE4', fg: '#6E6656' },
};

const ROLE_ORDER = ['cashier', 'kitchen', 'manager', 'admin', 'owner'];

// Capability key prefix -> group label, in display order. A prefix not
// listed here falls into "Other" rather than being silently dropped, so a
// future capability always shows up somewhere even before this map is
// updated for it.
const GROUPS = [
  ['order', 'Orders'],
  ['menu', 'Menu'],
  ['inventory', 'Inventory'],
  ['recipe', 'Recipes'],
  ['pos_config', 'POS configuration'],
  ['queue', 'Verification queue'],
  ['unli', 'Unlimited Wings'],
  ['instruction', 'Messages / Instructions'],
  ['purchase_order', 'Purchasing'],
  ['update', 'Standalone updates'],
  ['combo', 'Combos'],
  ['user', 'Administration'],
  ['permissions', 'Administration'],
];

function groupFor(capability) {
  const prefix = capability.split('.')[0];
  const hit = GROUPS.find(([p]) => p === prefix);
  return hit ? hit[1] : 'Other';
}

function initials(name) {
  return (name || '?').trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase();
}

export async function render(ctx) {
  const isAdmin = (session.user || {}).role === 'admin';
  const [users, capabilities] = await Promise.all([
    isAdmin ? listUsers().catch(() => null) : Promise.resolve(null),
    getCapabilityMatrix().catch(() => []),
  ]);

  const roles = ROLE_ORDER.filter((r) => capabilities.length && r in capabilities[0]);
  const usersHost = h('div');

  function accountCard(u) {
    const color = ROLE_COLOR[u.role] || ROLE_COLOR.cashier;
    const locked = !!u.lockedUntil && new Date(u.lockedUntil) > new Date();
    const statusPill = !u.isActive ? Pill('flat', 'inactive')
      : locked ? Pill('crit', 'locked')
        : Pill('good', 'active');
    const resetBtn = Button({ size: 'sm', onClick: () => openResetModal(u.username) }, 'Reset password');

    return Card({ style: { display: 'flex', gap: '14px', alignItems: 'flex-start' } },
      h('div', {
        style: {
          width: '40px', height: '40px', borderRadius: '99px', background: color.bg, color: color.fg,
          fontWeight: '700', fontSize: '13px', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: '0',
        },
      }, initials(u.displayName || u.username)),
      h('div', { style: { flexGrow: '1', minWidth: '0' } },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' } },
          h('div', { style: { fontWeight: '600', fontSize: '14px' } }, u.displayName || u.username),
          Pill('flat', u.role),
          statusPill),
        h('div', { class: 't-xs t-soft', style: { marginTop: '2px' } },
          u.username,
          ' · last signed in ', ageFromNow(u.lastLoginAt, 'never'),
          locked ? ` · locked until ${u.lockedUntil}` : '',
          u.mustChangePassword ? ' · must change password' : ''),
        h('div', { style: { marginTop: '10px' } }, resetBtn)));
  }

  function drawUsers(list) {
    mount(usersHost, h('div', { class: 'grid', style: { gridTemplateColumns: 'repeat(2, minmax(0,1fr))', gap: '12px' } },
      ...list.map(accountCard)));
  }
  if (users) drawUsers(users);

  function openResetModal(username) {
    const err = ModalError();
    const resultHost = h('div', { style: { marginTop: '10px' } });
    const submit = Button({ variant: 'primary' }, 'Reset password');
    submit.addEventListener('click', async () => {
      submit.disabled = true;
      try {
        const r = await resetPassword(username);
        mount(resultHost, h('div', { class: 'callout callout-warn' },
          h('p', {}, h('strong', {}, `New password for ${r.username}: `), h('span', { class: 'mono' }, r.temporary_password)),
          h('p', { class: 't-xs', style: { marginTop: '6px' } }, r.note)));
        const fresh = await listUsers().catch(() => null);
        if (fresh) drawUsers(fresh);
      } catch (e) { err.textContent = e.message; err.style.display = ''; } finally { submit.disabled = false; }
    });
    Modal({
      title: `Reset password — ${username}`,
      desc: 'Generates a one-time temporary password, shown once, right here. Also clears any login lockout on this account.',
      onClose: () => {},
    }, err, submit, resultHost);
  }

  function openAddUserModal() {
    const usernameInput = h('input', { type: 'text', placeholder: 'e.g. cashier2' });
    const nameInput = h('input', { type: 'text', placeholder: 'optional' });
    const roleSelect = h('select', {},
      h('option', { value: 'cashier' }, 'Cashier'),
      h('option', { value: 'kitchen' }, 'Kitchen'),
      h('option', { value: 'admin' }, 'Admin'));
    const err = ModalError();
    const resultHost = h('div', { style: { marginTop: '10px' } });
    const submit = Button({ variant: 'primary' }, 'Create account');
    submit.addEventListener('click', async () => {
      err.textContent = ''; err.style.display = 'none';
      const username = usernameInput.value.trim();
      if (!username) { err.textContent = 'Enter a username.'; err.style.display = ''; return; }
      submit.disabled = true;
      try {
        const r = await createUser({ username, displayName: nameInput.value.trim() || null, role: roleSelect.value });
        mount(resultHost, h('div', { class: 'callout callout-warn' },
          h('p', {}, h('strong', {}, `Temporary password for ${r.username}: `), h('span', { class: 'mono' }, r.temporaryPassword)),
          h('p', { class: 't-xs', style: { marginTop: '6px' } }, 'Shown once, right here — hand it to the account holder directly.')));
        usernameInput.value = ''; nameInput.value = '';
        toast(`${r.username} created.`, 'good');
        const fresh = await listUsers().catch(() => null);
        if (fresh) drawUsers(fresh);
      } catch (e) { err.textContent = e.message; err.style.display = ''; } finally { submit.disabled = false; }
    });
    Modal({
      title: 'Add a user',
      desc: 'A temporary password is generated and shown once — never stored or logged. Owner and legacy Manager accounts cannot be created here; Owner is provisioned once at deployment.',
      onClose: () => {},
    },
      h('label', { class: 'field' }, 'Username', usernameInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Display name', nameInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Role', roleSelect),
      err, submit, resultHost);
  }

  // --- capability matrix: grouped, collapsible, filterable ---
  const matrixHost = h('div');
  let filterQuery = '';

  function tick(allowed) {
    return allowed ? h('span', { class: 't-good t-strong' }, '✓') : h('span', { class: 't-faint' }, '—');
  }

  function drawMatrix() {
    const q = filterQuery.trim().toLowerCase();
    const byGroup = new Map();
    for (const row of capabilities) {
      if (q && !row.capability.toLowerCase().includes(q)) continue;
      const g = groupFor(row.capability);
      if (!byGroup.has(g)) byGroup.set(g, []);
      byGroup.get(g).push(row);
    }
    const groupOrder = [...new Set(GROUPS.map(([, label]) => label))].concat(['Other']);
    const sections = groupOrder
      .filter((g) => byGroup.has(g))
      .map((g) => h('details', { open: q ? 'open' : undefined, style: { marginTop: '10px' } },
        h('summary', { style: { cursor: 'pointer', fontWeight: '600', fontSize: '12.5px', padding: '4px 0' } }, g),
        Table({
          columns: [
            { key: 'cap', label: 'Capability', render: (r) => h('span', { class: 'mono t-xs' }, r.capability) },
            ...roles.map((role) => ({ key: role, label: role.charAt(0).toUpperCase() + role.slice(1), render: (r) => tick(r[role]) })),
          ],
          rows: byGroup.get(g),
        })));

    mount(matrixHost, sections.length
      ? h('div', {}, ...sections)
      : h('div', { class: 't-sm t-faint', style: { padding: '10px 0' } }, 'No capability matches that filter.'));
  }

  const filterInput = h('input', { type: 'text', placeholder: 'Filter capabilities…', onInput: (e) => { filterQuery = e.target.value; drawMatrix(); } });
  drawMatrix();

  return h('div', {},
    isAdmin
      ? h('div', { style: { marginBottom: '14px' } },
        Card({},
          CardHeader({
            title: 'Accounts',
            desc: 'Every user account, its role, and when it last signed in',
            right: Button({ variant: 'primary', size: 'sm', onClick: openAddUserModal }, '+ Add user'),
          }),
          usersHost))
      : null,
    Card({},
      CardHeader({
        title: 'Who can do what',
        desc: 'Grouped by area, every real role included — the policy itself still lives in wt/auth.py',
        right: h('label', { class: 'field' }, filterInput),
      }),
      matrixHost,
      h('div', { class: 't-xs t-faint', style: { marginTop: '10px' } },
        `Signed in as ${(session.user || {}).displayName || 'unknown'} · role: ${(session.user || {}).role || 'unknown'}`)));
}
