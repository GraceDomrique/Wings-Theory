-- =============================================================================
-- 0051  Extend v_unli_wings_daily_from_flavor (migration 0050) with the two
--       feature columns wt/anomaly.py's _consumption_records actually needs
--       (actual_per_head, leftover_ratio) -- same "DROP VIEW, CREATE VIEW"
--       pattern migration 0042 used to add a column to an existing view.
--
--       leftover_ratio here is approximated from the flavor sheet's own
--       piece-based leftover/served figures (SUM(leftover_pcs) /
--       SUM(wings_served_pcs)): the generic pass's own leftover_ratio
--       definition (closing / (opening + added), all in ingredient base
--       units) has no analogue in unli_flavor_reconciliation, which never
--       recorded base-unit opening/added/closing -- only piece counts. Both
--       are dimensionless ratios of like-for-like quantities (pieces over
--       pieces vs. base-unit over base-unit), so this is a reasonable
--       same-kind approximation, not a unit mismatch -- documented here so
--       it is never mistaken for the identical measurement.
-- =============================================================================

DROP VIEW v_unli_wings_daily_from_flavor;

CREATE VIEW v_unli_wings_daily_from_flavor AS
SELECT service_date,
       (SELECT id FROM unli_item WHERE item_key = 'wings') AS unli_item_id,
       SUM(heads)                                       AS heads,
       SUM(provisional_base)                            AS theoretical_base,
       SUM(actual_measured_base)                        AS actual_base,
       CASE WHEN SUM(heads) > 0
            THEN SUM(actual_measured_base) / SUM(heads) END AS actual_per_head,
       SUM(actual_measured_base) - SUM(provisional_base) AS variance_base,
       CASE WHEN SUM(provisional_base) > 0
            THEN (SUM(actual_measured_base) - SUM(provisional_base)) / SUM(provisional_base)
            END                                          AS variance_ratio,
       CASE WHEN SUM(wings_served_pcs) > 0
            THEN SUM(leftover_pcs) / SUM(wings_served_pcs) END AS leftover_ratio,
       recorded_by,
       source_batch_id
  FROM unli_flavor_reconciliation
 WHERE observation_status <> 'rejected'
 GROUP BY service_date, recorded_by, source_batch_id;
