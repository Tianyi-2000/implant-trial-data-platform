-- Stop 4 demo: a data manager works through the open data queries, then locks a visit.
-- Run with `make correct` after `make load planted` on a fresh database. Runs as data_manager_1,
-- then reads the audit trail as auditor_1.
--
-- Every "refused:" line is expected. pg_temp.refused() runs a statement, and if the database refuses
-- it, rolls it back and prints why. If the database ever accepts one, the demo stops with an error.
\set QUIET on
\pset footer off

CREATE FUNCTION pg_temp.refused(sql text) RETURNS text LANGUAGE plpgsql AS $$
BEGIN
  BEGIN
    EXECUTE sql;
  EXCEPTION WHEN OTHERS THEN
    RETURN 'refused: ' || SQLERRM;
  END;
  RAISE EXCEPTION 'expected the database to refuse this, but it was accepted: %', sql;
END $$;

SELECT NOT EXISTS (SELECT FROM ops.data_query WHERE query_type = 'conflict' AND status = 'open') AS nothing_open \gset
\if :nothing_open
  \echo 'No open conflict to resolve. Start from a fresh database: make reset seed load planted correct'
  \quit
\endif

SELECT max(query_id) FILTER (WHERE query_type = 'conflict')         AS conflict,
       max(query_id) FILTER (WHERE query_type = 'rejected_row')     AS rejected_row,
       max(query_id) FILTER (WHERE query_type = 'quarantined_file') AS quarantined_file,
       max(query_id) FILTER (WHERE query_type = 'rejected_file')    AS rejected_file
  FROM ops.data_query WHERE status = 'open' \gset

\echo
\echo '== 1. Open data queries: what the loader refused to decide on its own'
SELECT query_id, query_type, issue FROM ops.data_query WHERE status = 'open' ORDER BY query_id;

\echo '== 2. Nobody closes a query by editing it. The only way is ops.resolve_query, with a reason'
SELECT pg_temp.refused(format($$UPDATE ops.data_query SET status = 'closed' WHERE query_id = %s$$, :conflict)) AS result
UNION ALL
SELECT pg_temp.refused(format($$SELECT ops.resolve_query(%s, 'accept_resent', '')$$, :conflict));

\echo '== 3. The conflict: the site confirms the re-export is right, so the data manager accepts it'
SELECT query_id, decision, resolved_by, resolution
  FROM ops.resolve_query(:conflict, 'accept_resent',
       'Site confirmed the first export ran before the current source was recalibrated; the re-export is correct');

\echo '   The stored row now holds the resent value, and points to the file it came from:'
SELECT s.subject_code, v.visit_code, e.electrode_no, t.threshold_ua, b.source_name AS loaded_from
  FROM core.threshold_measurement t
  JOIN core.visit v USING (visit_id) JOIN core.subject s USING (subject_id)
  JOIN core.electrode e USING (electrode_id) JOIN ops.ingest_batch b ON b.ingest_batch_id = t.ingest_batch_id
 WHERE t.threshold_id = (SELECT record_id FROM ops.data_query WHERE query_id = :conflict);

\echo '== 4. The other three change no data. A rejected row has no resent value to accept'
SELECT pg_temp.refused(format($$SELECT ops.resolve_query(%s, 'accept_resent', 'load it anyway')$$, :rejected_row)) AS result;
SELECT query_id, query_type, decision, resolution FROM ops.resolve_query(:rejected_row, 'keep_stored',
       'Site confirmed an instrument fault on this reading; it stays out, and the electrode is re-tested at the next visit')
UNION ALL
SELECT query_id, query_type, decision, resolution FROM ops.resolve_query(:quarantined_file, 'keep_stored',
       'Site confirmed DEV-3028 is S094: the file was exported under the wrong subject code. The site re-exports it as S094')
UNION ALL
SELECT query_id, query_type, decision, resolution FROM ops.resolve_query(:rejected_file, 'keep_stored',
       'The export tool was set to ohm by mistake; the site re-exports in kohm. No parser change needed');

\echo '== 5. Locking S091 D30: entered -> verified -> locked'
SELECT v.visit_id AS visit, t.threshold_id AS e17
  FROM core.visit v JOIN core.subject s USING (subject_id)
  JOIN core.threshold_measurement t USING (visit_id) JOIN core.electrode e USING (electrode_id)
 WHERE s.subject_code = 'S091' AND v.visit_code = 'D30' AND e.electrode_no = 17 \gset
BEGIN;
SET LOCAL app.change_reason = 'Locked for the interim analysis';
SELECT pg_temp.refused(format($$UPDATE core.visit SET status = 'locked' WHERE visit_id = %s$$, :visit)) AS result;
SET LOCAL app.change_reason = 'Source data verified against the site record';
UPDATE core.visit SET status = 'verified' WHERE visit_id = :visit;
SET LOCAL app.change_reason = 'Locked for the interim analysis';
UPDATE core.visit SET status = 'locked' WHERE visit_id = :visit;
COMMIT;

\echo '   A locked visit refuses every change to it or its measurements, even with a reason:'
BEGIN;
SET LOCAL app.change_reason = 'Late correction';
SELECT pg_temp.refused(format('UPDATE core.threshold_measurement SET threshold_ua = 80 WHERE threshold_id = %s', :e17)) AS result
UNION ALL
SELECT pg_temp.refused(format('UPDATE core.visit SET visit_date = visit_date - 1 WHERE visit_id = %s', :visit));
COMMIT;

\echo '== 6. To correct it anyway: unlock with a reason, correct with a reason, lock again'
SELECT pg_temp.refused(format($$UPDATE core.visit SET status = 'verified' WHERE visit_id = %s$$, :visit)) AS result;
BEGIN;
SET LOCAL app.change_reason = 'Unlocked: the site reported the visit date was entered one day late';
UPDATE core.visit SET status = 'verified' WHERE visit_id = :visit;
COMMIT;
SELECT pg_temp.refused(format('UPDATE core.visit SET visit_date = visit_date - 1 WHERE visit_id = %s', :visit)) AS result;
BEGIN;
SET LOCAL app.change_reason = 'Visit date corrected to the site source record';
UPDATE core.visit SET visit_date = visit_date - 1 WHERE visit_id = :visit;
SET LOCAL app.change_reason = 'Locked again after the date correction';
UPDATE core.visit SET status = 'locked' WHERE visit_id = :visit;
COMMIT;

\connect - auditor_1
\set QUIET on
\echo '== 7. As auditor_1: who changed what, when and why. One transaction shares one xact_id'
SELECT a.xact_id, a.changed_at::time(0) AS at, a.db_user, a.table_name, a.reason,
       (SELECT jsonb_object_agg(k, jsonb_build_array(a.old_row -> k, a.new_row -> k))
          FROM jsonb_object_keys(a.new_row) AS k
         WHERE a.old_row -> k IS DISTINCT FROM a.new_row -> k
           AND k NOT IN ('resolution', 'resolved_at')) AS "changed: [old, new]"
  FROM audit.audit_log a
 WHERE a.db_user = 'data_manager_1'
 ORDER BY a.audit_id;
