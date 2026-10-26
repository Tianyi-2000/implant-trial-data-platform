-- Loading fitting exports (ADR-005, ADR-007, ADR-009). Functions run with the caller's privileges.
--
-- src/itdp/ingest.py parses a file, records its receipt with ops.receive_file, copies the rows into
-- a temp table (pg_temp.stg_row) and calls ops.load_measurements, all in one transaction. Python has
-- checked everything the file alone can tell. Everything that needs the database is checked here,
-- in the same transaction as the write, so no other session can change the answer in between.
SET ROLE platform_owner;

-- Records a file's receipt. Returns NULL if this file was already loaded or quarantined, or was already
-- rejected by this parser version. The unique indexes on ops.ingest_batch decide, not a "have I seen it?"
-- query: a second loader racing on the same file waits here, then gets NULL (ADR-009).
CREATE FUNCTION ops.receive_file(p_source_system text, p_source_name text, p_source_uri text,
                                 p_sha256 text, p_parser_version text, p_row_count int)
RETURNS bigint LANGUAGE sql AS $$
  INSERT INTO ops.ingest_batch (source_system, source_name, source_uri, sha256, parser_version, status, row_count_expected)
  VALUES (p_source_system, p_source_name, p_source_uri, p_sha256, p_parser_version, 'received', p_row_count)
  ON CONFLICT DO NOTHING
  RETURNING ingest_batch_id
$$;

-- Holds back a whole file and raises a data query about it.
--   rejected:    the parser cannot read it (format, unit, columns, row count). Retry needs a new parser version.
--   quarantined: readable, but whose data it is is in doubt. Nothing is loaded until someone investigates.
CREATE FUNCTION ops.hold_file(p_batch bigint, p_status text, p_reason text)
RETURNS ops.ingest_batch LANGUAGE plpgsql AS $$
DECLARE
  v_batch ops.ingest_batch;
BEGIN
  IF p_status NOT IN ('rejected', 'quarantined') THEN
    RAISE EXCEPTION 'A file is held back as rejected or quarantined, not %', p_status;
  END IF;
  UPDATE ops.ingest_batch SET status = p_status, status_reason = p_reason
   WHERE ingest_batch_id = p_batch AND status = 'received'
  RETURNING * INTO v_batch;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Batch % is not waiting to be processed', p_batch;
  END IF;
  INSERT INTO ops.data_query (query_type, table_name, record_id, ingest_batch_id, issue)
  VALUES (CASE p_status WHEN 'rejected' THEN 'rejected_file' ELSE 'quarantined_file' END,
          'ops.ingest_batch', p_batch, p_batch, format('%s %s: %s', v_batch.source_name, p_status, p_reason));
  RETURN v_batch;
END $$;

-- Checks and writes one parsed fitting export. p_header is the file's header block as JSON.
-- Every row ends as exactly one of: new, duplicate, conflict, rejected. The reconciles CHECK on
-- ops.ingest_batch refuses to mark the batch loaded unless the four add up to the header's row_count.
CREATE FUNCTION ops.load_measurements(p_batch bigint, p_header jsonb)
RETURNS ops.ingest_batch LANGUAGE plpgsql AS $$
DECLARE
  v_kind    text := p_header->>'measurement';           -- 'impedance' or 'threshold'
  v_freq    int  := (p_header->>'test_freq_hz')::int;    -- impedance only
  v_pw      int  := (p_header->>'pulse_width_us')::int;  -- threshold only
  v_method  text := p_header->>'method';                 -- threshold only
  v_subject bigint;
  v_site_ok boolean;
  v_implant bigint;
  v_owner   text;
  v_visit   bigint;
  v_locked  boolean;
  v_reason  text;
  v_n       int;
  v_batch   ops.ingest_batch;
BEGIN
  PERFORM FROM ops.ingest_batch WHERE ingest_batch_id = p_batch AND status = 'received' FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Batch % is not waiting to be processed', p_batch;
  END IF;

  -- 1. Whose data is this? Any doubt holds back the whole file (ADR-005).
  --    The device serial is read from the implant; the subject code was picked by a person.
  SELECT s.subject_id, si.site_code = p_header->>'site_code' INTO v_subject, v_site_ok
    FROM core.subject s JOIN core.site si USING (site_id)
   WHERE s.subject_code = p_header->>'subject_code';
  SELECT i.implant_id, s.subject_code INTO v_implant, v_owner
    FROM core.implant i JOIN core.subject s USING (subject_id)
   WHERE i.device_serial = p_header->>'device_serial';
  SELECT visit_id, status = 'locked' INTO v_visit, v_locked
    FROM core.visit
   WHERE subject_id = v_subject AND visit_code = p_header->>'visit_code' AND visit_seq = 1;

  v_reason := CASE
    WHEN v_subject IS NULL THEN format('unknown subject %s', p_header->>'subject_code')
    WHEN v_implant IS NULL THEN format('unknown device serial %s', p_header->>'device_serial')
    WHEN v_owner <> p_header->>'subject_code' THEN
      format('device serial %s belongs to subject %s, not %s', p_header->>'device_serial', v_owner, p_header->>'subject_code')
    WHEN NOT v_site_ok THEN format('subject %s is not enrolled at %s', p_header->>'subject_code', p_header->>'site_code')
    WHEN v_visit IS NULL THEN format('no visit %s recorded for subject %s', p_header->>'visit_code', p_header->>'subject_code')
  END;
  IF v_reason IS NULL THEN
    SELECT count(*) INTO v_n FROM stg_row s
     WHERE NOT EXISTS (SELECT FROM core.electrode e WHERE e.implant_id = v_implant AND e.electrode_no = s.electrode_no);
    IF v_n > 0 THEN
      v_reason := format('%s rows name an electrode this device does not have', v_n);
    END IF;
  END IF;
  IF v_reason IS NOT NULL THEN
    RETURN ops.hold_file(p_batch, 'quarantined', v_reason);
  END IF;

  -- 2. Classify every row against what is already stored, by business key (ADR-005).
  --    The plausibility rules mirror the CHECK constraints, which stay as the last line of defense (ADR-007).
  DROP TABLE IF EXISTS pg_temp.classified;
  CREATE TEMP TABLE classified (
    line_no int, electrode_no int, electrode_id bigint, value numeric, no_response boolean,
    measured_at timestamptz, raw_line text, stored_id bigint, stored jsonb, outcome text, reason text
  ) ON COMMIT DROP;

  IF v_kind = 'impedance' THEN
    INSERT INTO classified
    SELECT s.line_no, s.electrode_no, e.electrode_id, s.value, s.no_response, s.measured_at, s.raw_line,
           m.impedance_id, to_jsonb(m),
           CASE WHEN r.reason IS NOT NULL THEN 'rejected'
                WHEN m.impedance_id IS NULL THEN 'new'
                WHEN m.impedance_kohm = s.value AND m.measured_at = s.measured_at THEN 'duplicate'
                ELSE 'conflict' END,
           r.reason
      FROM stg_row s
      JOIN core.electrode e ON e.implant_id = v_implant AND e.electrode_no = s.electrode_no
      CROSS JOIN LATERAL (SELECT CASE WHEN NOT (s.value > 0 AND s.value < 1000000)
                                      THEN format('impedance %s kOhm is not physically possible', s.value) END AS reason) r
      LEFT JOIN core.impedance_measurement m
             ON m.visit_id = v_visit AND m.electrode_id = e.electrode_id AND m.test_freq_hz = v_freq;
  ELSE
    INSERT INTO classified
    SELECT s.line_no, s.electrode_no, e.electrode_id, s.value, s.no_response, s.measured_at, s.raw_line,
           m.threshold_id, to_jsonb(m),
           CASE WHEN r.reason IS NOT NULL THEN 'rejected'
                WHEN m.threshold_id IS NULL THEN 'new'
                WHEN m.threshold_ua IS NOT DISTINCT FROM s.value AND m.no_response = s.no_response
                     AND m.measured_at = s.measured_at AND m.pulse_width_us = v_pw AND m.method = v_method THEN 'duplicate'
                ELSE 'conflict' END,
           r.reason
      FROM stg_row s
      JOIN core.electrode e ON e.implant_id = v_implant AND e.electrode_no = s.electrode_no
      CROSS JOIN LATERAL (SELECT CASE
             WHEN s.no_response AND s.value IS NOT NULL THEN 'a threshold is given although no_response is true'
             WHEN NOT s.no_response AND s.value IS NULL THEN 'no threshold is given although no_response is false'
             WHEN NOT (s.value > 0 AND s.value <= 1000) THEN format('threshold %s uA is outside 0 to 1000 uA', s.value)
           END AS reason) r
      LEFT JOIN core.threshold_measurement m ON m.visit_id = v_visit AND m.electrode_id = e.electrode_id;
  END IF;

  -- 3. A locked visit takes no new rows (ADR-009). Duplicates change nothing, and conflicts only raise queries.
  --    If the visit is locked after step 1 read it, the locked-visit trigger (db/50_corrections.sql) refuses
  --    the INSERT below: the file fails as a whole, and the next run quarantines it here.
  IF v_locked AND EXISTS (SELECT FROM classified WHERE outcome = 'new') THEN
    RETURN ops.hold_file(p_batch, 'quarantined',
      format('visit %s of subject %s is locked; it must be unlocked before new rows can be added',
             p_header->>'visit_code', p_header->>'subject_code'));
  END IF;

  -- 4. Write. New rows go in; stored rows are never overwritten by a load.
  IF v_kind = 'impedance' THEN
    INSERT INTO core.impedance_measurement (visit_id, electrode_id, test_freq_hz, impedance_kohm, measured_at, ingest_batch_id)
    SELECT v_visit, electrode_id, v_freq, value, measured_at, p_batch FROM classified WHERE outcome = 'new';
  ELSE
    INSERT INTO core.threshold_measurement (visit_id, electrode_id, pulse_width_us, threshold_ua, no_response, method,
                                            measured_at, ingest_batch_id)
    SELECT v_visit, electrode_id, v_pw, value, no_response, v_method, measured_at, p_batch FROM classified WHERE outcome = 'new';
  END IF;

  -- A conflict keeps the stored value. The resent one waits in the query until a data manager decides (ADR-008).
  INSERT INTO ops.data_query (query_type, table_name, record_id, ingest_batch_id, issue, stored, proposed)
  SELECT 'conflict', format('core.%s_measurement', v_kind), c.stored_id, p_batch,
         format('%s %s, electrode %s: the resent %s differs from the stored one (stored %s, resent %s)',
                p_header->>'subject_code', p_header->>'visit_code', c.electrode_no, v_kind,
                CASE v_kind WHEN 'impedance' THEN (c.stored->>'impedance_kohm') || ' kOhm'
                            ELSE coalesce((c.stored->>'threshold_ua') || ' uA', 'no response') END,
                CASE v_kind WHEN 'impedance' THEN c.value || ' kOhm'
                            ELSE coalesce(c.value || ' uA', 'no response') END),
         c.stored,
         CASE v_kind
           WHEN 'impedance' THEN jsonb_build_object('impedance_kohm', c.value, 'measured_at', c.measured_at)
           ELSE jsonb_build_object('threshold_ua', c.value, 'no_response', c.no_response, 'measured_at', c.measured_at,
                                   'pulse_width_us', v_pw, 'method', v_method)
         END
    FROM classified c WHERE c.outcome = 'conflict';

  -- A rejected row is kept exactly as received, and raises a query of its own.
  WITH r AS (
    INSERT INTO ops.rejected_row (ingest_batch_id, line_no, raw_line, reason)
    SELECT p_batch, line_no, raw_line, reason FROM classified WHERE outcome = 'rejected'
    RETURNING rejected_row_id, line_no, reason
  )
  INSERT INTO ops.data_query (query_type, table_name, record_id, ingest_batch_id, issue)
  SELECT 'rejected_row', 'ops.rejected_row', r.rejected_row_id, p_batch,
         format('%s %s %s, line %s not loaded: %s', p_header->>'subject_code', p_header->>'visit_code', v_kind,
                r.line_no, r.reason)
    FROM r;

  -- 5. Reconcile. If the counts don't add up to the manifest, the CHECK fails and nothing is kept.
  UPDATE ops.ingest_batch SET
         status         = 'loaded',
         rows_inserted  = (SELECT count(*) FROM classified WHERE outcome = 'new'),
         rows_duplicate = (SELECT count(*) FROM classified WHERE outcome = 'duplicate'),
         rows_conflict  = (SELECT count(*) FROM classified WHERE outcome = 'conflict'),
         rows_rejected  = (SELECT count(*) FROM classified WHERE outcome = 'rejected')
   WHERE ingest_batch_id = p_batch
  RETURNING * INTO v_batch;
  RETURN v_batch;
END $$;

REVOKE ALL ON FUNCTION ops.receive_file(text, text, text, text, text, int), ops.hold_file(bigint, text, text),
                       ops.load_measurements(bigint, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.receive_file(text, text, text, text, text, int), ops.hold_file(bigint, text, text),
                          ops.load_measurements(bigint, jsonb) TO loader;

RESET ROLE;
