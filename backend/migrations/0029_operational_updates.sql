-- =============================================================================
-- 0029  Operational updates: the other half of Owner <-> Admin communication.
--
-- owner_instruction (migration 0025) is deliberately one-directional -- the
-- owner names a task, admin works it through a fixed state machine, and the
-- only free text admin can attach is the single completion_note at the very
-- end. That leaves two real gaps: admin cannot post an interim progress note
-- while a task is still open/in_progress ("Supplier contacted, confirmed for
-- tomorrow"), and admin cannot originate a message to the owner at all
-- ("Chicken wing demand is above recent average") without the owner having
-- asked first.
--
-- operational_update fills both, as one small table rather than a second
-- messaging system: a free-text note either role can post, optionally
-- threaded onto an existing owner_instruction (owner_instruction_id set) or
-- standalone (NULL -- a heads-up not tied to any specific task). It is not a
-- chat: no per-user inbox, no read/unread, no editing or deleting a posted
-- update -- only creation and a single optional acknowledgement by someone
-- on the OTHER side (enforced in wt/updates.py, not here, since that check
-- needs the caller's role), the same explicit-acknowledgement pattern
-- instructions already established.
--
-- created_by_role is captured at creation time rather than joined from
-- app_user, for the same reason owner_instruction's own created_by is a
-- plain username string: the record should read the same way years later
-- even if the account is later deactivated.
--
-- 2026-10 DEPRECATED, ORPHANED: wt/updates.py (this table's only reader and
-- writer) and its 3 API routes were deleted when the free-text "Messages"
-- channel was replaced by Instructions (owner_instruction) as the sole
-- Owner<->Admin channel -- see that work's own commit for the reasoning.
-- Nothing in this codebase reads or writes this table any more (confirmed:
-- grep -rn operational_update backend/wt backend/migrations finds zero
-- hits outside this file). Left in place deliberately, not dropped: any
-- rows a real deployment already wrote before the removal are historical
-- records, and dropping a table on a live database is a materially
-- different risk than leaving an unused one — see the 2026-10
-- post-implementation audit, which flagged this as safe-to-leave rather
-- than safe-to-drop. If this is ever revisited, drop it only via its own
-- reviewed migration with a real dependency check against production data,
-- never as a side effect of an unrelated change.
-- -----------------------------------------------------------------------------
CREATE TABLE operational_update (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_instruction_id  INTEGER REFERENCES owner_instruction(id) ON DELETE CASCADE,
  body                  TEXT    NOT NULL,
  created_by            TEXT,
  created_by_role       TEXT    NOT NULL CHECK (created_by_role IN ('owner','admin')),
  created_at            TEXT    NOT NULL DEFAULT (datetime('now','subsec')),
  acknowledged_at       TEXT,
  acknowledged_by       TEXT
);

CREATE INDEX ix_operational_update_instruction ON operational_update (owner_instruction_id, created_at);
CREATE INDEX ix_operational_update_standalone ON operational_update (created_at) WHERE owner_instruction_id IS NULL;

INSERT INTO schema_version (version, name) VALUES (29, 'operational_updates');
