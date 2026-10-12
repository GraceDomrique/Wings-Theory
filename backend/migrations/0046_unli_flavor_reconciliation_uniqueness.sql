-- =============================================================================
-- 0046  unli_flavor_reconciliation (migration 0044) was missing the same
--       UNIQUE(service_date, <stream>) guard unli_reconciliation has always
--       had -- found during the flavor-level detector's test-coverage pass
--       (Part 5's "duplicate reconciliation" case). Under normal ops a
--       given flavour is physically reconciled at most once per service
--       date; two rows for the same (service_date, flavor_id) would be
--       either a double-submission or a correction that should instead be
--       its own append-only replacement mechanism (not built, since
--       nothing today needs to correct a flavour reconciliation after the
--       fact -- the same way unli_reconciliation itself has none).
--
-- Verified safe against the live production data before writing this:
-- zero existing (service_date, flavor_id) duplicates among the 5,071
-- simulated-workbook rows imported so far.
-- =============================================================================

CREATE UNIQUE INDEX ux_unli_flavor_reconciliation_date_flavor
  ON unli_flavor_reconciliation (service_date, flavor_id);

INSERT INTO schema_version (version, name) VALUES (46, 'unli_flavor_reconciliation_uniqueness');
