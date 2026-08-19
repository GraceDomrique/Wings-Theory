# Final database safety test (Phase 09A section 12)

Run 2026-08-19 against a disposable copy of the real production database
(`backend/wings_theory.db`, MD5 `2de914db17ecaba52c7881e23fc00a0e` before
and after — confirmed byte-identical, untouched throughout).

Sequence: **PRODUCTION COPY → BACKUP → MIGRATE → INTEGRITY CHECK → CLOSE →
REOPEN → START APPLICATION → READ → WRITE CONTROLLED TEST TRANSACTION →
RESTART → READ AGAIN → INTEGRITY CHECK → RESTORE BACKUP → INTEGRITY CHECK**

| Step | Result |
|---|---|
| Copy verified byte-identical to production | ✅ MD5 match |
| Migrate (the copy was at schema v12; head is v14) | ✅ v12 → v14, clean |
| Integrity check post-migrate | ✅ `integrity_check: ok`, 0 FK violations, 0 FEFO drift, 0 ledger drift |
| Backup (of the migrated copy), then verify the backup file itself | ✅ `ok: true` |
| Close connection | ✅ |
| Reopen | ✅ |
| Start application (real FastAPI startup hook via `TestClient(app)`) | ✅ `GET /health` reports clean |
| Read (`GET /pos/menu`) | ✅ 28 sellable, 1 blocked (matches known fixture state) |
| Write a controlled test transaction (`POST /console/ingredients/{id}/receive`) | ✅ `on_hand_base` moved 1000.0 → 1500.0 |
| Restart (fresh `TestClient`, re-runs `db.migrate()` + `validate_production_startup()`) | ✅ |
| Read again | ✅ `on_hand_base` still 1500.0 — the write survived the restart |
| Integrity check post-write/post-restart | ✅ clean; row counts moved by exactly the expected +1 ledger row, +1 FEFO batch |
| Restore the backup taken before the controlled write | ✅ |
| Integrity check post-restore | ✅ clean; `on_hand_base` back to 1000.0 — the restore reverted exactly the one write, nothing else |

## Invariants checked at every integrity-check step

| Invariant | Result (every step) |
|---|---|
| FK violations | **0** |
| FEFO drift (`SUM(open batch qty) == on_hand_base`, per ingredient) | **0** |
| Ledger drift (last `balance_after_base` == current `on_hand_base`, per ingredient) | **0** |
| Stale reservations (`state='held'` on an order not `punched`/`in_progress`) | **0** |
| Orphan records (`recipe_line`/`fefo_batch` referencing a missing parent) | **0** |
| Duplicate SKUs (`ingredient_id`, `menu_item_id`) | **0** |
| Row counts preserved where expected | ✅ — moved only by the one controlled write, and only in the two tables that write touches (`stock_ledger`, `fefo_batch`); reverted exactly by the restore |

## What this proves, and what it doesn't

Proves: migration, backup, restart/reopen, and restore all work correctly
against a real copy of the actual production data shape (111 ingredients,
9 orders, 180 ledger rows, 92 open batches, 4 users) — not a synthetic
fixture. The bug caught during P1-1's own drill (migration 0014's
`NOT NULL DEFAULT CURRENT_TIMESTAMP` failing against a populated table)
does not recur here because it was already fixed.

Does not prove: this is not a load test, not a concurrency test under
real multi-tablet traffic, and not a test against the production database
itself — by design, per Phase 09A's explicit instruction never to run
destructive operations against the real file. It also does not include a
real production `WT_SECRET` or a real network-facing uvicorn process;
`TestClient` drives the same FastAPI app in-process.

Script: `scripts/` does not retain this one-off drill script (it lived in
the session scratchpad and was deleted after the run, along with the
disposable copy and its backups) — this document is the record of what
ran and what it found.
