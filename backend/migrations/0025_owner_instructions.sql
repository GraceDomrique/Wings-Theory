-- =============================================================================
-- 0025  Owner -> Admin instructions.
--
-- A lightweight operational task, not a chat: the owner names something that
-- needs doing ("verify chicken stock before lunch"), admin works it through a
-- fixed set of states, and every transition is attributable and timestamped.
-- No 'draft' state -- an instruction exists from the moment it's created.
--
-- Legal transitions (enforced in wt/instructions.py, not by a DB trigger,
-- since the owner-cancel-own-open case needs the caller's identity, which a
-- trigger cannot see):
--   open -> acknowledged -> in_progress -> completed   (admin)
--   open | acknowledged | in_progress -> cancelled      (admin; owner only
--                                                         from 'open', and
--                                                         only their own row)
-- completed/cancelled are terminal.
--
-- Not append-only like admin_audit_log/login_event -- status legitimately
-- changes over the record's life -- but every creation and transition also
-- writes one admin_audit_log row (entity_type 'owner_instruction') via the
-- existing audit.log(), inside the same transaction as the row update, so
-- the full history is visible from the one place the new Activity Center
-- already reads, rather than a second parallel history table.
-- -----------------------------------------------------------------------------
CREATE TABLE owner_instruction (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  title            TEXT    NOT NULL,
  body             TEXT    NOT NULL,
  priority         TEXT    NOT NULL CHECK (priority IN ('normal','important','urgent')),
  status           TEXT    NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open','acknowledged','in_progress','completed','cancelled')),
  assigned_to      TEXT,
  due_date         TEXT,
  created_by       TEXT,
  created_at       TEXT    NOT NULL DEFAULT (datetime('now','subsec')),
  updated_at       TEXT    NOT NULL DEFAULT (datetime('now','subsec')),
  acknowledged_at  TEXT,
  acknowledged_by  TEXT,
  in_progress_at   TEXT,
  completed_at     TEXT,
  completed_by     TEXT,
  completion_note  TEXT,
  cancelled_at     TEXT,
  cancelled_by     TEXT,
  cancel_reason    TEXT
);

CREATE INDEX ix_owner_instruction_status ON owner_instruction (status, created_at);

INSERT INTO schema_version (version, name) VALUES (25, 'owner_instructions');
