# Wings Theory — go-live checklist

This is the list of things a human (the owner or an admin acting for them)
must actually do before this system is production-ready. Passing tests
proves the *code* is safe; it does not perform any of the actions below,
and none of them can be automated on the owner's behalf — see Phase 09A's
instructions: "DO NOT invent passwords for the restaurant owner."

Sections are added to this file as each phase closes an item. As of Phase
09A: credentials (P0-2) and backup/restore (P1-1).

---

## 1. Rotate the shared seed password (P0-2)

Every account `seed.py` creates starts with the same password,
`wingstheory`, and is flagged `must_change_password=1` in the database for
exactly this reason. **Production is not ready until every row below reads
"rotated."**

Two ways to check this without reading the database directly:
- `GET /auth/login` response for any account includes `"must_change_password"`.
- An admin can query directly: `SELECT username, must_change_password FROM app_user;`

| Account | Role | Current state | Required action |
|---|---|---|---|
| `owner` | admin | shared password `wingstheory` | Sign in, call `POST /auth/change_password` with the current password and a new one only the owner knows. |
| `manager` | manager | shared password `wingstheory` | Same — manager signs in and changes their own password. |
| `cashier1` | cashier | shared password `wingstheory` | Same, or have the admin issue a reset (below) and hand the cashier the one-time password to change on first login. |
| `kitchen1` | kitchen | shared password `wingstheory` | Same as `cashier1`. |

**Self-service change** (any signed-in account, for their own password):

```
POST /auth/change_password
{ "current_password": "...", "new_password": "...", "confirm_new_password": "..." }
```

Requires the current password, a new password at least 10 characters,
not equal to the username, and not the retired shared password itself.
The response includes a fresh token — the account is not logged out by its
own change.

**Admin emergency reset** (admin only, for a different account):

```
POST /console/users/{username}/reset_password
```

Generates a random 16-character password server-side and returns it
**once**, in that response only — it is never logged and never stored
anywhere but the hash. If the response is lost, reset again. The target
account is flagged `must_change_password=1` again so this checklist (and
the account holder) can tell a temporary password apart from one they
actually chose.

Either action **immediately invalidates every token already issued** for
that account, not just future logins — a session open on another device
stops working the moment the password changes, rather than remaining
valid for up to its 12-hour natural expiry.

This checklist item is not something Claude (or any automated process) can
complete on the owner's behalf: the four rows above must be marked
"rotated" by the account holders themselves, or by an admin performing a
reset and physically handing the one-time password to the right person.

---

## 2. Production environment variables (P0-1)

`backend/start_server.ps1` already sets these correctly for a production
launch; this is what to verify if the server ever needs to be started by
hand instead:

| Variable | Required value in production | What happens if wrong |
|---|---|---|
| `WT_ENV` | `production` | Unset, or any other value, and the server refuses to start (`db.ConfigurationError`). |
| `WT_DB` | the full path to `backend/wings_theory.db` | Any other path under `WT_ENV=production` is refused. |
| `WT_SECRET` | a real random secret, generated once and persisted at `backend/.secret` (gitignored) | Unset, or left as the source-code default, and the server refuses to start. |

`backend/.secret` is generated automatically by `start_server.ps1` on first
run and reused on every restart after that. **Back it up** — if it's lost,
every signed-in device is logged out and must sign in again (tokens signed
under the old secret stop verifying), which is disruptive but not
data-destructive.

---

## 3. Backup / restore (P1-1)

See `backend/docs/BACKUP_RESTORE.md` for the full procedure. In short:

- A backup is taken automatically before every schema migration and before
  any destructive reset.
- A manual backup can be taken at any time with `python -m wt.backup_restore backup`.
- Restoring requires explicitly naming both the backup file and the target
  (production or development) — there is no default target, and restoring
  into production requires an extra explicit flag.

**Restoring into production has not been exercised against the real
production database during this phase, on instruction** — it has been
proven against disposable copies only (see BACKUP_RESTORE.md's own test
log). The owner or an admin should run one real restore drill against a
copy of the production database before relying on this procedure during
an actual incident.
