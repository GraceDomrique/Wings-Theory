-- =============================================================================
-- 0006  0005 reclassified Mango/Banana/Cucumber/Parsley/Spring Onion to
-- expiry_mode='shelf_life_estimate' on the ingredient row, but their
-- already-open fefo_batch rows still carried the original seed import's
-- guessed date (nobody prints an expiry date on a mango — it was a
-- placeholder someone wrote on the stock sheet) and expiry_status
-- 'needs_verification', so they kept sitting in the Needs Verification
-- queue asking to confirm a date the console no longer displays at all —
-- shelf_life_estimate ingredients are tracked from received_at +
-- shelf_life_days regardless of what's stored here.
--
-- This clears exactly those batches back to expires_at=NULL / 'unset', the
-- same state seed.py now produces for these ingredients on a fresh install.
-- Scoped to expiry_mode='shelf_life_estimate' so it only ever touches the
-- ingredients 0005 explicitly named — nothing here is guessed.
-- =============================================================================

UPDATE fefo_batch
   SET expires_at = NULL, expiry_status = 'unset'
 WHERE state = 'open'
   AND expiry_status = 'needs_verification'
   AND ingredient_id IN (
     SELECT id FROM ingredient WHERE expiry_mode = 'shelf_life_estimate'
   );

INSERT INTO schema_version (version, name) VALUES (6, 'clear_shelf_life_batch_dates');
