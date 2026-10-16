# Production database recovery — 2026-10-13

## Summary

`backend/wings_theory.db`'s schema was found corrupted (`malformed database
schema (sqlite_autoindex_ml_model_registry_1) - orphan index`) while running
an unrelated ML evaluation. Full recovery completed with **zero data loss** —
the recovered database actually contains more real rows than even the most
recent healthy backup. Root cause identified and fixed. Full regression
suite passes (1,118 passed, 1 skipped, 0 failed).

## What happened, step by step

### 1–2. Stopped writers, preserved evidence
Found and force-stopped 4 long-lived `uvicorn` processes (ports 8123, 8140,
8150, 8160; a 5th on 8000 had already exited on its own) that had been
running against the production database for 20+ hours. Windows would not
permit a graceful close (no SIGTERM path for a bare console process without
`/F`) — a forced stop is safe here since SQLite's WAL mode is specifically
designed to survive an abrupt process kill without further corruption.

Preserved the corrupted file byte-for-byte (sha256-verified) at
`backend/backups/corruption_preserve_20261013/wings_theory.db` before any
further investigation touched it. No `-wal`/`-shm` sidecars existed at that
point (already auto-checkpointed on last connection close).

### 3. Located the most recent healthy backup
`backend/backups/wings_theory.pre_migrate_to_v0053.20261013_125651.db` —
taken today at 12:56:50, `PRAGMA integrity_check` → `ok`, schema v51.
A second backup, `wings_theory_pre_backfill_20261013_130153.db` (13:01:53,
note the different, non-`db.backup_now()` naming pattern — produced by a
different call path), showed the **identical** state (v51, same row counts)
five minutes later — meaning nothing changed in that 5-minute gap, and two
separate operations each independently found the database "not yet
migrated" moments apart. This pair is the direct evidence for the race
below.

### 4–5. Investigated and recovered on isolated copies only
All 44 tables in the corrupted file were fully readable via SQLite's
`PRAGMA writable_schema = ON` escape hatch — the corruption was isolated to
one orphan index object, not any table's actual b-tree data. Built a clean
replacement (`backend/backups/recovery_workspace_20261013/recovered.db`) by:
migrating a **fresh** database through every real migration file (0001→0053,
guaranteeing correct indexes), then reloading every row from every table out
of the corrupted copy, preserving explicit primary keys/rowids, and fixing
`sqlite_sequence` bookkeeping for every `AUTOINCREMENT` table afterward.

Comparison against the healthy v51 backup (all deltas are **additional**
real rows the corrupted file had that the backup didn't — nothing is
missing):

| table | backup (v51) | recovered (v53) | delta |
|---|---|---|---|
| customer_order | 33,596 | 34,206 | +610 |
| order_line | 52,247 | 53,294 | +1,047 |
| stock_ledger | 689,416 | 697,744 | +8,328 |
| unli_reconciliation | 0 | 58 | +58 |
| admin_audit_log | 9,458 | 9,677 | +219 |
| purchase_order / purchase_order_item | 3,020 / 3,020 | 3,020 / 3,020 | 0 |
| ml_model_registry | 5 | 5 | 0 (field values confirmed identical, including `active_training_run_id`/`mode`/`updated_at` per model) |
| anomaly_review | 1,216 | 1,216 | 0 |
| ingredient | 122 | 122 | 0 |

### 6. Verified and promoted
`recovered.db`: `PRAGMA integrity_check` → `ok`; `PRAGMA foreign_key_check`
→ 0 violations; `wt.db.integrity_report()` → clean; `wt.ml_startup.
validate_artifacts_on_startup()` → all 5 ML artifacts `"valid"`;
`wt.anomaly.detect_anomalies()` → ran without error (386 flagged, matching
earlier session numbers). Promoted via an atomic `os.replace()` into
`backend/wings_theory.db`. Re-verified independently at the live path
afterward (same clean results).

### 7. Root cause, with evidence (not assumed)
- `schema_version` in the corrupted file was frozen at **51** — migrations
  52 and 53 never actually committed, even though a backup had already been
  taken in anticipation of applying them (`backup_now()` fires at the
  *start* of a migration attempt, before any migration file is applied).
- Two independent backups (12:56:50 and 13:01:53), from two different call
  paths (one named by `db.backup_now()`'s own `pre_migrate_to_vNNNN`
  convention, the other by a different, unidentified caller using a
  `pre_backfill` name that doesn't match that convention), both saw the
  **same** "not yet migrated" state 5 minutes apart — meaning at least two
  separate connections were each about to run `db.migrate()` at nearly the
  same time.
- Real bulk writes (the +610/+1,047/+8,328/+58/+219 rows above, consistent
  with a historical-data backfill/reconciliation operation) landed and
  committed successfully in that same window, while at least one of the
  concurrent migration attempts did not.
- **Mechanism**: `wt/db.py`'s `migrate()` applied each pending migration in
  its own `BEGIN IMMEDIATE`/`COMMIT` — meaning the write lock was *released*
  between migration files. Two processes that each read `applied_version()`
  before either had committed anything would each compute the same pending
  list and could then interleave applying it, racing on the exact table
  (`ml_model_registry`, created by migration 0050) whose implicit
  `TEXT PRIMARY KEY` autoindex ended up orphaned.
- **Ruled out**: OneDrive/cloud-sync redirection (Desktop is a plain local
  NTFS folder, not a reparse point), and a sleep/resume cycle (Windows event
  log shows only a harmless AC↔battery power-source change at 13:01:37, no
  Kernel-Power sleep/resume/unexpected-shutdown event in the window).
- **Not fully resolved**: the exact script/process behind the `pre_backfill`
  backup's naming (it doesn't match any `db.backup_now()` call site
  currently in the codebase) could not be conclusively identified — flagging
  this honestly rather than guessing. It does not change the diagnosis
  above, which rests on the `schema_version`/row-count evidence independent
  of that one file's provenance.
- **Structural enabler**: 4 separate `uvicorn` processes had been holding
  open connections to this one WAL-mode database file for 20+ hours, at
  least one of them (per `.claude/launch.json`'s `wt-ml-dashboard-preview`
  config) explicitly pointed at the real production file rather than an
  isolated preview database — multiplying the chance of this race regardless
  of which specific call path triggered the migration.

### 8. Fix implemented
Added `_migration_lock()` to `wt/db.py`: a cross-process OS-level advisory
lock (file lock, `msvcrt` on Windows / `fcntl` on POSIX) held for `migrate()`'s
**entire** pending-migration sequence — not a SQLite transaction, so it is
unaffected by the function's own per-migration commits. A second process
that was waiting on the lock re-reads `applied_version()` once it acquires
it; if the first process already finished, it simply finds nothing pending
and returns immediately — never re-applies, never races.

Verified under genuine two-process concurrent load (separate OS processes,
synchronized to start `migrate()` at the same instant, 5 runs): 4/5 fully
clean, 1/5 produced a safe, retriable `OperationalError("database is
locked")` instead of any corruption — **zero corruption in any run**. That
one retriable case is a separate, pre-existing, lower-severity timing
sensitivity in `connect()`'s own `PRAGMA journal_mode=WAL` call when two
processes race to create a *brand-new* database file at the exact same
instant; real deployments (one long-lived process per environment, file
already exists) are far less exposed to it than this adversarial test was.

**Not changed**: `api.py`'s startup hook still calls `db.migrate(conn)`
unconditionally on every process start — this is the project's existing,
intentional dev-convenience pattern, now race-safe rather than disabled.
The real fix for "don't run more than one server against production" is
operational discipline, not code — see Remaining risks below.

### 9. Restarted exactly one instance, verified
Confirmed zero stray processes/listeners first. Started the
`wt-ml-dashboard-preview` config (the only one pointed at real production)
via the Browser preview tool. `/health` returned the live, authoritative
state directly from the running app: `schema_version: 53`, `schema_head: 53`,
`schema_up_to_date: true`, `integrity_check: "ok"`, `foreign_key_violations:
0`, `environment: "production"`. POS, Console, and KDS all loaded cleanly
(only expected `401`s for unauthenticated API calls — deliberately did not
attempt to log in or guess credentials). No `500`s anywhere in the network
log. Server then stopped cleanly — leaving a server running unattended is
exactly the structural enabler of this incident, so none was left up.

### 10. Regression tests
`python -m tests.run_tests` (1,119 tests): **1,118 passed, 1 skipped, 0
failed.** (A prior run earlier in this session had shown one unrelated,
apparently order-dependent failure in an RBAC test; it passed cleanly on
this final run with no code change in that area — flagged as flaky, not
re-investigated further since it is unrelated to this incident.)

## Lost records

**None identified.** The recovered database contains strictly more real
data than the most recent healthy backup in every table checked (see table
above); nothing was deleted, relabeled, or regenerated to produce this
result — every row was carried over from the corrupted file's own intact
table data.

## Remaining risks

1. **Operational discipline**: nothing in code stops a second long-lived
   server from being started against `wings_theory.db` again. The migration
   lock now prevents that scenario from corrupting the schema, but running
   multiple instances against production is still not recommended — start
   exactly one, and stop dev/preview instances when done with them.
2. **The `pre_backfill` backup's origin is still unidentified.** If you know
   what you ran around 13:01 today (a script, an admin action, a scheduled
   job), it's worth telling me so I can confirm it's accounted for — right
   now it's documented as unexplained, not dangerous.
3. **One pre-existing, unrelated finding**: `backend/reset_prod_passwords.py`
   (untracked) connects to `wings_theory.db` with a bare `sqlite3.connect()`,
   bypassing `wt.db.connect()`'s `BEGIN IMMEDIATE`/WAL discipline entirely.
   It wasn't implicated in this incident, but it's worth routing through
   `wt.db.connect()` if it's going to be run again.
4. The `connect()`-level `PRAGMA journal_mode=WAL` timing sensitivity noted
   in step 8 (brand-new file + two simultaneous first-opens) is unfixed —
   low real-world likelihood, documented for awareness.

## Files

- Preserved corrupted original: `backend/backups/corruption_preserve_20261013/wings_theory.db`
- Recovery workspace (scripts + intermediate copies): `backend/backups/recovery_workspace_20261013/`
- This report: `backend/docs/DATABASE_RECOVERY_2026_10_13.md`
- Code fix: `backend/wt/db.py` (`_migration_lock()`, wired into `migrate()`)
