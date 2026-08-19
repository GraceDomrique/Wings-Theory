# Backup / restore procedure (P1-1)

Implemented in `wt/backup_restore.py`. Four operations, always in this
order: **BACKUP → VERIFY → RESTORE → VERIFY**.

```
python -m wt.backup_restore backup
python -m wt.backup_restore verify <path>
python -m wt.backup_restore restore <backup_path> --target development
python -m wt.backup_restore restore <backup_path> --target production --confirm-production
```

All three commands run under whatever `WT_ENV`/`WT_DB` is already
configured in the environment — there is no separate "which database"
argument, so this always operates under the same fail-closed gate as
every other part of the application (see `wt/db.py`'s `environment()` /
`db_path()`).

## What each step does

**`backup`** — backs up whatever `db.db_path()` currently resolves to
(via the existing `db.backup_now()`, which checkpoints the live WAL first
so nothing recently committed is missed), then immediately verifies the
file it just wrote.

**`verify <path>`** — opens `path` as its own independent connection
(never through `db.db_path()`, so it can inspect any backup file, not
just the live database) and checks:

- `PRAGMA integrity_check`
- `PRAGMA foreign_key_check` (this also covers "no orphan records" — every
  table in this schema enforces foreign keys, so an orphan row *is* a
  foreign-key violation; there is no separate weaker orphan scan)
- schema version against the migration head this codebase currently ships
- key row counts (`ingredient`, `customer_order`, `stock_ledger`,
  `fefo_batch`, `app_user`)
- the FEFO invariant: `SUM(open batch qty_base) == ingredient.on_hand_base`
  for every ingredient
- the ledger invariant: each ingredient's most recent `stock_ledger` row's
  `balance_after_base` equals its current `on_hand_base`

**`restore <backup_path> --target <env>`** — every guard below runs
*before* anything on disk is touched:

1. `backup_path` must exist and must itself pass `verify()`. A broken
   backup is refused outright, never restored over a working database.
2. `--target` is required and must equal the currently configured
   `WT_ENV`. Passing `--target development` while `WT_ENV=production` is
   actually set is refused as an **ambiguous target** — neither side is
   trusted over the other.
3. `--target production` additionally requires `--confirm-production`.
   `WT_ENV=production` and `--target production` agreeing is *not* enough
   on its own; this is a separate, explicit flag that environment
   configuration alone cannot satisfy. The CLI also runs the same
   type-the-filename-back interactive confirmation every other destructive
   operator command in this codebase requires (`db.require_destructive_confirmation`).

Once past the guards:

4. The live connection is checkpointed (`PRAGMA wal_checkpoint(TRUNCATE)`)
   and closed.
5. The **current** database is preserved as an emergency pre-restore
   backup — nothing has been deleted or overwritten yet at this point.
6. Stale `-wal`/`-shm` sidecars for the current file are cleared (they
   belong to the database being replaced; leaving them would make SQLite
   try to replay old WAL frames against unrelated restored content).
7. The already-verified backup is copied into place.
8. The database is reopened and registered.
9. It is verified again — now as the live, restored database.

Nothing is ever deleted. The current database is only ever *superseded*,
and only after both the incoming backup and the outgoing emergency copy
have already been safely written to disk.

## Tested against a disposable copy, 2026-08-19

Per Phase 09A's instruction, this was proven against a throwaway copy of
the real production database, never the file itself:

```
cp wings_theory.db  <disposable copy>
WT_ENV=development WT_DB=<disposable copy>  # never WT_ENV=production
```

1. **Migrated the copy** from its actual live schema version (v12) up to
   head (v14). This is what actually caught a real bug: migration 0014
   originally added `app_user.password_changed_at` as
   `NOT NULL DEFAULT CURRENT_TIMESTAMP`. SQLite accepts that against an
   *empty* table (which is all `fresh_db()`-based tests ever exercise —
   they migrate before any row exists), but refuses it with "Cannot add a
   column with non-constant default" against a table that already has
   rows **and** has foreign-key enforcement on — exactly the shape of the
   real, already-seeded `app_user` table. The test suite's 437 cases
   never caught this because none of them migrate a populated database.
   Fixed by adding the column nullable with no schema default and
   backfilling it with an `UPDATE` in the same migration transaction, and
   updating `seed.py`'s `seed_users()` to set it explicitly on every
   `INSERT` rather than relying on a column default. Re-ran the drill
   after the fix: migration applied cleanly.
2. **`backup`** — wrote a backup of the migrated copy, verified `ok: true`
   (integrity clean, 0 FK violations, schema v14 == head, 0 FEFO drift,
   0 ledger drift, real row counts: 111 ingredients / 9 orders / 180
   ledger rows / 92 batches / 4 users).
3. **Mutated** the copy directly (`ingredient.on_hand_base` for a real
   row, `43200.0 → 143199.0`) to simulate corrupted or bad data.
4. **`restore`** — ran the CLI restore command against the copy, target
   `development`. Reported `ok: true`, wrote an emergency pre-restore
   backup, and the post-restore verification was clean.
5. **Verified the mutation was actually gone**: read the same row back —
   `on_hand_base` was `43200.0` again, matching the pre-mutation value
   exactly.
6. Confirmed the real `backend/wings_theory.db` was untouched throughout
   (unchanged mtime, `git status` clean) and deleted the disposable copy
   and its scratch backups afterward.

**Not yet done, and explicitly out of scope for this phase**: a restore
*into the real production database* has never been run, on instruction.
The owner or an admin should run one real restore drill — against a
throwaway copy of a *current* production backup, following the steps
above — before relying on this procedure during an actual incident.

## Retention (P1-3 / section 10)

No automated deletion of backups has been added. `backend/backups/`
currently holds:

- one file per completed schema migration (`pre_migrate_to_vNNNN`,
  written automatically by `db.migrate()`)
- one file per destructive reset (`pre_restore_emergency`, written
  automatically by `restore()`)
- manual backups taken via `python -m wt.backup_restore backup`

At minimum, until an automated policy is deliberately added:

- **Daily backup**: not yet automated (would need a scheduled task
  calling `python -m wt.backup_restore backup`; not implemented this
  phase since a wrong retention/deletion policy is worse than no policy).
- **Pre-deployment backup**: already automatic — every `db.migrate()` call
  against an already-used database backs it up first.
- **Manual emergency backup**: `python -m wt.backup_restore backup`.
- **Restore**: this document.

Deleting old backups was deliberately not implemented — the instruction
for this phase was explicit that retention must not risk a needed
recovery point, and no retention policy (age-based, count-based, or
otherwise) was specified by the owner. This is a real, open item: left
unmanaged, `backend/backups/` will grow indefinitely. Recommend the owner
decide a retention window (e.g. "keep the last 30 daily backups plus every
pre-migration backup indefinitely") before this becomes a disk-space
problem, rather than an automated policy guessing at their risk tolerance.
