# Production configuration sweep (P0-1 / section 8)

Full-repository search for every term Phase 09A named, each occurrence
classified SAFE / DEVELOPMENT-ONLY / TEST-ONLY / PRODUCTION RISK. No
legitimate development/test reference was deleted; only actual production
risks were fixed (P0-1/P0-2/P1-1, in prior commits this phase).

## `wingstheory` (the shared seed password)

| Location | Classification | Notes |
|---|---|---|
| `wt/seed.py` (`seed_users`) | DEVELOPMENT/TEST-ONLY | Seeds the four accounts with this password, explicitly flagged `must_change_password=1` for exactly this reason. |
| `wt/auth.py` (`KNOWN_COMPROMISED_PASSWORDS`) | SAFE | Rejected outright as a *new* password on both the self-service change and admin-reset paths — see P0-2. |
| `tests/*.py` | TEST-ONLY | Login credential for automated tests. |
| `README.md`, `docs/GO_LIVE_CHECKLIST.md` | SAFE | Documentation naming the credential that must be rotated. |
| The real production database | **PRODUCTION RISK, not yet resolved by code** | See `docs/GO_LIVE_CHECKLIST.md` §1 — this is the actual go-live blocker: the four seeded accounts still carry this password until the owner/admin rotates them. Code cannot fix this; only the account holders can. |

## `dev-only-change-in-production` (the default JWT signing secret)

| Location | Classification | Notes |
|---|---|---|
| `wt/auth.py` (`DEFAULT_SECRET`) | SAFE | Fallback only when `WT_SECRET` is unset. `db.validate_production_startup()` refuses to start under `WT_ENV=production` if `WT_SECRET` is unset *or* still equals this literal — see P0-1. |

## `WT_DB` / `WT_ENV` / `WT_SECRET` / `WT_ALLOW_DESTRUCTIVE`

All four are now required, fail-closed, and cross-checked against each
other by `wt/db.py`'s `environment()`/`db_path()` (P0-1) — every call site
was audited and is one of:

| Location | Classification |
|---|---|
| `start_server.ps1` | SAFE — sets `WT_ENV=production`, `WT_DB` to the real file, generates/persists a real `WT_SECRET`. |
| `start_server_dev.ps1` | SAFE — sets `WT_ENV=development`, `WT_DB` under `dev/`, generates/persists its own separate secret. |
| `scripts/generate_demo_history.py` | DEVELOPMENT-ONLY | Hardcodes `WT_ENV=development` and refuses if `WT_DB` doesn't resolve under `dev/`. |
| `tests/run_tests.py` (8 test classes) | TEST-ONLY | Sets `WT_ENV=test` + a tempfile `WT_DB` in `setUpClass`/`setUp`, pops both in teardown. |
| `wt/db.py`, `wt/backup_restore.py` | SAFE | The gate itself / consumers of it. |

## `seed --fresh` / `seed.run(fresh=True)` / `db.reset()`

| Location | Classification |
|---|---|
| `wt/seed.py` `__main__` | SAFE | Guarded by `db.require_destructive_confirmation()` (typed filename) *and* by `db.reset()`'s own self-protection (P1-3) — production is refused unconditionally, development requires `WT_ALLOW_DESTRUCTIVE=1`. |
| `tests/run_tests.py` (9 call sites) | TEST-ONLY | Always under `WT_ENV=test`, which `db.reset()` allows without extra authorization. |
| `wt/reference.py`, migration 0012 comments | SAFE | Documentation referencing the fresh-install code path, not a call site. |

## `demo_seed` / `generate_demo_history`

| Location | Classification |
|---|---|
| `wt/demo_seed.py` | DEVELOPMENT-ONLY, self-describing | Module docstring states it is "never against production"; its `__main__` requires `db.require_destructive_confirmation()`. Not imported by `wt/api.py` — never runs automatically. |
| `scripts/generate_demo_history.py` | DEVELOPMENT-ONLY | The recommended entrypoint; asserts `WT_DB` resolves under `dev/` and is never the production path before importing anything that could open a connection. |

## `localhost` / `127.0.0.1`

| Location | Classification |
|---|---|
| `start_server_dev.ps1` (`--host 127.0.0.1`) | SAFE, intentional | Dev server binds to loopback only — not reachable from another device on the network, which is the point (a developer at the keyboard, not a shop floor). |
| `start_server.ps1` (`--host 0.0.0.0`) | SAFE, intentional | Production binds to all interfaces so shop tablets on the LAN can reach it — this is required by the actual deployment (a LAN of tablets), not a risk in itself given the threat model in `wt/auth.py`'s module docstring (not reachable from the internet). |

## `debug` / debug mode

No `debug=True` on the FastAPI app, no `--debug` / `--reload` flag in
`start_server.ps1` (production). `start_server_dev.ps1` does pass
`--reload` to uvicorn — SAFE/DEVELOPMENT-ONLY, standard for a dev
auto-restart workflow, and confined to the dev launcher only.

## Hardcoded paths

Only `wt/db.py`'s own `PRODUCTION_DB` constant hardcodes
`backend/wings_theory.db` — every other reference (launchers, scripts,
docs) either reads it from there or exists specifically to assert *against*
it (`generate_demo_history.py`'s refusal check). SAFE — this is the one
place that should name it.

## Other findings, not on the named list

- **`backend/static/console.html.bak`** — a stray, tracked, superseded copy
  of `console.html` (confirmed by diff: an older CSS token layout). Not
  served by any route, contains no secrets. SAFE, but housekeeping: worth
  deleting in a future cleanup pass now that git history makes a `.bak`
  copy unnecessary. Left in place this phase rather than deleted
  unilaterally.
- No `.env`, `.secret`, or `*.db` file is tracked in git (`git ls-files`
  checked directly) — the `.gitignore` rules from the original baseline
  commit, extended this phase to also cover `backend/dev/.secret`, are
  holding.
- No API keys, third-party credentials, or other secret-shaped strings
  found anywhere in the repository outside the two already-covered cases
  above (`wingstheory`, `DEFAULT_SECRET`).
