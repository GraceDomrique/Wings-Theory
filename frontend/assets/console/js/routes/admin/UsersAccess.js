import { h, mount } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Button } from '../../components/ui/Button.js';
import { Modal, ModalError } from '../../components/ui/Modal.js';
import { toast } from '../../components/ui/Feedback.js';
import { ageFromNow } from '../../lib/format.js';
import { session } from '../../lib/api/client.js';
import {
  listUsers, createUser, resetPassword, updateDisplayName, updateUsername,
} from '../../lib/api/account.js';
import { getCapabilityMatrix, getUserCapabilities, setUserCapability } from '../../lib/api/permissions.js';

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
 *
 * 2026-10 addition: "Who can do what" is where the actual per-user editing
 * happens now (migration 0052's user_capability_override) — a "Checking
 * permissions for" picker right on that card switches its role columns out
 * for one real checkbox column for the picked account, so admin can grant
 * or revoke an individual capability for one account, right there, without
 * touching the role default every other account of that role still
 * follows. Not a separate modal off the account card — this IS the "Who
 * can do what" list, just made editable per account instead of read-only
 * per role. New accounts may also be provisioned as Owner here; every
 * Owner account shares the exact same capability set (auth.CAPABILITIES
 * has no separate "main owner" tier).
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

// Capability suffix -> plain-English verb, for the summary a non-technical
// manager reads first. A suffix not listed here falls back to itself,
// title-cased — so a new capability is never silently dropped from the
// summary, it just reads a little more literally until this map catches up.
const ACTION_LABELS = {
  read: 'View', write: 'Edit', create: 'Create', delete: 'Delete',
  approve: 'Approve', void: 'Void', receive: 'Receive', count: 'Count',
  waste: 'Record waste for', reset: 'Reset', export: 'Export', override: 'Override',
};

function humanCapability(capability) {
  const [prefix, ...rest] = capability.split('.');
  const suffix = rest.join('.');
  const verb = ACTION_LABELS[suffix] || (suffix.charAt(0).toUpperCase() + suffix.slice(1).replace(/_/g, ' '));
  return `${verb} ${groupFor(capability).toLowerCase()}`;
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
    const renameBtn = Button({ size: 'sm', variant: 'ghost', onClick: () => openRenameModal(u.username, u.displayName) }, 'Edit');

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
        h('div', { style: { marginTop: '10px', display: 'flex', gap: '8px' } }, resetBtn, renameBtn)));
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

  function openRenameModal(username, currentDisplayName) {
    const nameInput = h('input', { type: 'text', value: currentDisplayName || '' });
    const usernameInput = h('input', { type: 'text', value: username });
    const err = ModalError();
    const submit = Button({ variant: 'primary' }, 'Save');
    submit.addEventListener('click', async () => {
      err.textContent = ''; err.style.display = 'none';
      const displayName = nameInput.value.trim();
      const newUsername = usernameInput.value.trim();
      if (!displayName) { err.textContent = 'Enter a display name.'; err.style.display = ''; return; }
      if (!newUsername) { err.textContent = 'Enter a username.'; err.style.display = ''; return; }
      submit.disabled = true;
      try {
        // The sign-in username and the display name are changed with two
        // separate calls (separate endpoints, separate audit entries) —
        // display name first since it always succeeds; a username clash is
        // then the only way this can partially apply.
        if (displayName !== currentDisplayName) await updateDisplayName(username, displayName);
        let effectiveUsername = username;
        if (newUsername !== username) {
          await updateUsername(username, newUsername);
          effectiveUsername = newUsername;
          toast(`Signed in as ${username} will need to sign in again as ${newUsername}.`, 'warn');
        }
        toast(`${effectiveUsername}'s account details updated.`, 'good');
        modal.close();
        const fresh = await listUsers().catch(() => null);
        if (fresh) drawUsers(fresh);
      } catch (e) { err.textContent = e.message; err.style.display = ''; } finally { submit.disabled = false; }
    });
    const modal = Modal({
      title: `Edit account — ${username}`,
      desc: 'Changing the username signs that account out everywhere — it must sign in again with the new username.',
      onClose: () => {},
    },
      h('label', { class: 'field' }, 'Display name', nameInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Username', usernameInput),
      err, submit);
  }

  function openAddUserModal() {
    const usernameInput = h('input', { type: 'text', placeholder: 'e.g. cashier2' });
    const nameInput = h('input', { type: 'text', placeholder: 'optional' });
    const roleSelect = h('select', {},
      h('option', { value: 'cashier' }, 'Cashier'),
      h('option', { value: 'kitchen' }, 'Kitchen'),
      h('option', { value: 'admin' }, 'Admin'),
      h('option', { value: 'owner' }, 'Owner'));
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
      desc: 'A temporary password is generated and shown once — never stored or logged. A new Owner account holds the exact same capabilities as every other Owner account — there is no separate "main owner" privilege level.',
      onClose: () => {},
    },
      h('label', { class: 'field' }, 'Username', usernameInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Display name', nameInput),
      h('label', { class: 'field', style: { marginTop: '10px' } }, 'Role', roleSelect),
      err, submit, resultHost);
  }

  // Self-service "change my own password" moved to Account Settings
  // (routes/account/Settings.js, 2026-10-14) — reached from every role's
  // sidebar now, not buried in an admin-only page. This page keeps only
  // administrator-controlled account management (reset someone ELSE's
  // password, create an account, edit capabilities).

  // --- "Who can do what": grouped, collapsible, filterable — and, once
  // admin picks an account in the "Checking permissions for" selector,
  // editable for that one account right here, in place of the read-only
  // role columns. No separate modal: this list IS the editor.
  const matrixHost = h('div');
  const summaryHost = h('div');
  let filterQuery = '';
  let editingUsername = ''; // '' = reference mode (role columns, read-only)
  let editingCapabilities = null; // this account's getUserCapabilities() rows, while editing

  /**
   * Plain-English capability summary (2026-10-15) — sits above the raw
   * capability-ID matrix, which moved into a collapsed "Advanced
   * Permissions" section below it. Reference mode summarizes each role by
   * area ("full" = every capability in that group is allowed, "partial" =
   * some are); editing mode summarizes one account's overrides against its
   * role default, since that's the only thing that differs per-account.
   */
  function drawSummary() {
    if (editingUsername) {
      const overrides = editingCapabilities.filter((r) => r.override !== null);
      if (overrides.length === 0) {
        mount(summaryHost, h('div', { class: 't-sm t-faint' },
          `${editingUsername} follows the ${(() => { const u = (users || []).find((x) => x.username === editingUsername); return u ? u.role : 'role'; })()} default for every capability — no individual overrides.`));
        return;
      }
      const granted = overrides.filter((r) => r.override === true);
      const revoked = overrides.filter((r) => r.override === false);
      mount(summaryHost, h('div', { class: 'grid', style: { gap: '4px' } },
        granted.length ? h('div', { class: 't-sm' },
          h('span', { class: 't-good t-strong' }, `+${granted.length} granted `),
          'beyond the role default: ', granted.map((r) => humanCapability(r.capability)).join(', ')) : null,
        revoked.length ? h('div', { class: 't-sm' },
          h('span', { class: 't-crit t-strong' }, `−${revoked.length} revoked `),
          'from the role default: ', revoked.map((r) => humanCapability(r.capability)).join(', ')) : null));
      return;
    }
    // Reference mode: one line per role, grouped by area, "full" vs "partial".
    const byGroupAll = new Map();
    for (const row of capabilities) {
      const g = groupFor(row.capability);
      if (!byGroupAll.has(g)) byGroupAll.set(g, []);
      byGroupAll.get(g).push(row);
    }
    mount(summaryHost, h('div', { class: 'grid', style: { gap: '8px' } },
      ...roles.map((role) => {
        const full = []; const partial = [];
        for (const [g, rows] of byGroupAll) {
          const allowedCount = rows.filter((r) => r[role]).length;
          if (allowedCount === 0) continue;
          if (allowedCount === rows.length) full.push(g); else partial.push(`${g} (${allowedCount}/${rows.length})`);
        }
        return h('div', { class: 't-sm' },
          h('strong', {}, role.charAt(0).toUpperCase() + role.slice(1)), ' — ',
          full.length ? `full access to ${full.join(', ')}` : null,
          full.length && partial.length ? '; ' : null,
          partial.length ? `partial access to ${partial.join(', ')}` : null,
          !full.length && !partial.length ? 'no access configured' : null);
      })));
  }

  function tick(allowed) {
    return allowed ? h('span', { class: 't-good t-strong' }, '✓') : h('span', { class: 't-faint' }, '—');
  }

  async function toggleForEditingUser(row, checkbox, resetLink) {
    checkbox.disabled = true;
    try {
      const out = await setUserCapability(editingUsername, row.capability, checkbox.checked);
      editingCapabilities = out.capabilities;
      const fresh = out.capabilities.find((r) => r.capability === row.capability);
      resetLink.style.display = fresh.override === null ? 'none' : '';
      drawSummary();
    } catch (e) {
      checkbox.checked = !checkbox.checked;
      toast(e.message, 'bad');
    } finally { checkbox.disabled = false; }
  }

  function drawMatrix() {
    const q = filterQuery.trim().toLowerCase();
    const sourceRows = editingUsername
      ? editingCapabilities.map((r) => ({ capability: r.capability, _override: r }))
      : capabilities;
    const byGroup = new Map();
    for (const row of sourceRows) {
      if (q && !row.capability.toLowerCase().includes(q)) continue;
      const g = groupFor(row.capability);
      if (!byGroup.has(g)) byGroup.set(g, []);
      byGroup.get(g).push(row);
    }
    const groupOrder = [...new Set(GROUPS.map(([, label]) => label))].concat(['Other']);

    const columns = editingUsername
      ? [
        { key: 'cap', label: 'Capability', render: (r) => h('span', { class: 'mono t-xs' }, r.capability) },
        {
          key: 'allowed', label: `Allowed for ${editingUsername}`,
          render: (r) => {
            const fresh = r._override;
            const checkbox = h('input', { type: 'checkbox', checked: fresh.effective || undefined });
            const resetLink = h('a', {
              href: '#', class: 't-xs', style: { marginLeft: '8px', display: fresh.override === null ? 'none' : '' },
              onClick: async (e) => {
                e.preventDefault();
                checkbox.disabled = true;
                try {
                  const out = await setUserCapability(editingUsername, r.capability, null);
                  editingCapabilities = out.capabilities;
                  const after = out.capabilities.find((x) => x.capability === r.capability);
                  checkbox.checked = after.effective;
                  resetLink.style.display = 'none';
                  drawSummary();
                } catch (e2) { toast(e2.message, 'bad'); } finally { checkbox.disabled = false; }
              },
            }, 'Reset to role default');
            checkbox.addEventListener('change', () => toggleForEditingUser(r, checkbox, resetLink));
            return h('span', { style: { display: 'flex', alignItems: 'center' } }, checkbox, resetLink);
          },
        },
      ]
      : [
        { key: 'cap', label: 'Capability', render: (r) => h('span', { class: 'mono t-xs' }, r.capability) },
        ...roles.map((role) => ({ key: role, label: role.charAt(0).toUpperCase() + role.slice(1), render: (r) => tick(r[role]) })),
      ];

    const sections = groupOrder
      .filter((g) => byGroup.has(g))
      .map((g) => h('details', { open: q || editingUsername ? 'open' : undefined, style: { marginTop: '10px' } },
        h('summary', { style: { cursor: 'pointer', fontWeight: '600', fontSize: '12.5px', padding: '4px 0' } }, g),
        Table({ columns, rows: byGroup.get(g) })));

    mount(matrixHost, sections.length
      ? h('div', {}, ...sections)
      : h('div', { class: 't-sm t-faint', style: { padding: '10px 0' } }, 'No capability matches that filter.'));
  }

  async function setEditingUser(username) {
    editingUsername = username;
    if (!username) { editingCapabilities = null; drawMatrix(); drawSummary(); return; }
    mount(matrixHost, h('div', { class: 't-sm t-faint' }, 'Loading…'));
    try {
      const data = await getUserCapabilities(username);
      editingCapabilities = data.capabilities;
    } catch (e) {
      toast(e.message, 'bad');
      editingUsername = ''; editingCapabilities = null;
      userSelect.value = '';
    }
    drawMatrix();
    drawSummary();
  }

  const filterInput = h('input', { type: 'text', placeholder: 'Filter capabilities…', onInput: (e) => { filterQuery = e.target.value; drawMatrix(); } });
  const advancedDetails = h('details', { style: { marginTop: '14px', borderTop: '1px solid var(--line)', paddingTop: '10px' } },
    h('summary', { style: { cursor: 'pointer', fontWeight: '600', fontSize: '12.5px' } }, 'Advanced Permissions — raw capability IDs and overrides'),
    matrixHost);
  const userSelect = h('select', {
    onChange: (e) => { setEditingUser(e.target.value); if (e.target.value) advancedDetails.open = true; },
  },
    h('option', { value: '' }, 'All roles (reference)'),
    ...(users || []).map((u) => h('option', { value: u.username }, `${u.displayName || u.username} (${u.role})`)));
  drawMatrix();
  drawSummary();

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
        desc: editingUsername
          ? `Editing ${editingUsername}'s own capabilities — a change here affects only this account, not its role`
          : 'Grouped by area, every real role included — the policy itself still lives in wt/auth.py',
        right: h('div', { style: { display: 'flex', gap: '10px', flexWrap: 'wrap' } },
          isAdmin ? h('label', { class: 'field' }, h('div', { class: 't-xs t-faint' }, 'Checking permissions for'), userSelect) : null,
          h('label', { class: 'field' }, h('div', { class: 't-xs t-faint', style: { visibility: 'hidden' } }, '.'), filterInput)),
      }),
      summaryHost,
      advancedDetails,
      h('div', { class: 't-xs t-faint', style: { marginTop: '10px' } },
        `Signed in as ${(session.user || {}).displayName || 'unknown'} · role: ${(session.user || {}).role || 'unknown'}`)));
}
