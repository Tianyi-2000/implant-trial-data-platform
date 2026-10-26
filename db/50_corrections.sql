-- Corrections after a load (ADR-008, ADR-009): closing data queries, and locked visits.
--
-- A data manager closes a query only through ops.resolve_query, which changes the record (if the
-- decision is to accept) and closes the query in one transaction, under one audited reason.
-- A locked visit refuses every change to it and its measurements, from a load or from a person,
-- until it is unlocked, which is itself an audited change with a reason (21 CFR Part 11 §11.10(f)).
SET ROLE platform_owner;

-- ============================================================
-- Closing a data query
-- ============================================================

-- p_decision: accept_resent (conflicts only) applies the resent values to the stored row;
--             keep_stored changes no data. Either way the query is closed with the reason.
-- SECURITY DEFINER: runs as platform_owner, because nobody has UPDATE on ops.data_query, so this is
-- the only way to close a query. session_user is still the person, in resolved_by and in the audit.
CREATE FUNCTION ops.resolve_query(p_query bigint, p_decision text, p_reason text)
RETURNS ops.data_query
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  q      ops.data_query;
  v_prev text := current_setting('app.change_reason', true);
BEGIN
  IF p_decision IS NULL OR p_decision NOT IN ('accept_resent', 'keep_stored') THEN
    RAISE EXCEPTION 'A decision is accept_resent or keep_stored, not %', coalesce(p_decision, 'null');
  END IF;
  IF nullif(btrim(p_reason), '') IS NULL THEN
    RAISE EXCEPTION 'Closing a data query needs a reason';
  END IF;

  -- Two people closing the same query: the second waits here, then finds it closed.
  SELECT * INTO q FROM ops.data_query WHERE query_id = p_query FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'There is no data query %', p_query;
  END IF;
  IF q.status = 'closed' THEN
    RAISE EXCEPTION 'Data query % was already closed by % at %', p_query, q.resolved_by, q.resolved_at;
  END IF;
  IF p_decision = 'accept_resent' AND q.query_type <> 'conflict' THEN
    RAISE EXCEPTION 'Data query % is a %: only a conflict has resent values to accept', p_query, q.query_type;
  END IF;

  -- Every audit row this transaction writes from here on carries the query id and the decision.
  PERFORM set_config('app.change_reason', format('DQ-%s %s: %s', p_query, p_decision, p_reason), true);

  -- Accept only while the stored row still holds what the query showed. If it changed since (a
  -- correction, or another accepted query), accepting would silently undo that: decide again.
  IF p_decision = 'accept_resent' THEN
    IF q.table_name = 'core.impedance_measurement' THEN
      UPDATE core.impedance_measurement m
         SET impedance_kohm  = (q.proposed->>'impedance_kohm')::numeric,
             measured_at     = (q.proposed->>'measured_at')::timestamptz,
             ingest_batch_id = q.ingest_batch_id
       WHERE m.impedance_id = q.record_id
         AND m.impedance_kohm = (q.stored->>'impedance_kohm')::numeric
         AND m.measured_at    = (q.stored->>'measured_at')::timestamptz;
    ELSE
      UPDATE core.threshold_measurement m
         SET threshold_ua    = (q.proposed->>'threshold_ua')::numeric,
             no_response     = (q.proposed->>'no_response')::boolean,
             measured_at     = (q.proposed->>'measured_at')::timestamptz,
             pulse_width_us  = (q.proposed->>'pulse_width_us')::int,
             method          = q.proposed->>'method',
             ingest_batch_id = q.ingest_batch_id
       WHERE m.threshold_id = q.record_id
         AND m.threshold_ua IS NOT DISTINCT FROM (q.stored->>'threshold_ua')::numeric
         AND m.no_response    = (q.stored->>'no_response')::boolean
         AND m.measured_at    = (q.stored->>'measured_at')::timestamptz
         AND m.pulse_width_us = (q.stored->>'pulse_width_us')::int
         AND m.method         = q.stored->>'method';
    END IF;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'The stored row for data query % changed after the query was raised: review it again', p_query;
    END IF;
  END IF;

  UPDATE ops.data_query
     SET status = 'closed', decision = p_decision, resolution = p_reason,
         resolved_by = session_user, resolved_at = now()
   WHERE query_id = p_query
  RETURNING * INTO q;

  PERFORM set_config('app.change_reason', coalesce(v_prev, ''), true);
  RETURN q;
END $$;

-- ============================================================
-- Locked visits
-- ============================================================

-- A visit goes entered -> verified -> locked. Once locked, the only change allowed is unlocking it
-- (back to verified); the audit trigger makes that, like every update, give a reason.
CREATE FUNCTION core.visit_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_visit text := (SELECT format('%s %s', subject_code, OLD.visit_code) FROM core.subject WHERE subject_id = OLD.subject_id);
BEGIN
  IF OLD.status = 'locked' AND (NEW.status <> 'verified' OR (to_jsonb(NEW) - 'status') <> (to_jsonb(OLD) - 'status')) THEN
    RAISE EXCEPTION 'Visit % is locked: the only change allowed is unlocking it (status verified)', v_visit;
  END IF;
  IF OLD.status = 'entered' AND NEW.status = 'locked' THEN
    RAISE EXCEPTION 'Visit % is entered, not verified: verify it before locking it', v_visit;
  END IF;
  RETURN NEW;
END $$;

-- Measurements of a locked visit can't be added, changed or removed, by anyone.
-- FOR SHARE holds the visit row until this transaction ends: a visit can't be locked while a change to
-- its measurements is still open, and a change that waited for a lock to commit sees the visit locked.
-- SECURITY DEFINER because FOR SHARE needs UPDATE on core.visit, which the loader doesn't have.
CREATE FUNCTION core.refuse_locked_visit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_ids   bigint[];
  v_visit text;
BEGIN
  IF TG_OP <> 'INSERT' THEN v_ids := v_ids || OLD.visit_id; END IF;
  IF TG_OP <> 'DELETE' THEN v_ids := v_ids || NEW.visit_id; END IF;
  SELECT format('%s %s', s.subject_code, v.visit_code) INTO v_visit
    FROM (SELECT subject_id, visit_code, status FROM core.visit WHERE visit_id = ANY (v_ids) FOR SHARE) v
    JOIN core.subject s USING (subject_id)
   WHERE v.status = 'locked'
   LIMIT 1;
  IF v_visit IS NOT NULL THEN
    RAISE EXCEPTION 'Visit % is locked: unlock it, with a reason, before changing its measurements', v_visit;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER lifecycle BEFORE UPDATE ON core.visit
  FOR EACH ROW EXECUTE FUNCTION core.visit_lifecycle();
CREATE TRIGGER locked_visit BEFORE INSERT OR UPDATE OR DELETE ON core.impedance_measurement
  FOR EACH ROW EXECUTE FUNCTION core.refuse_locked_visit();
CREATE TRIGGER locked_visit BEFORE INSERT OR UPDATE OR DELETE ON core.threshold_measurement
  FOR EACH ROW EXECUTE FUNCTION core.refuse_locked_visit();

REVOKE ALL ON FUNCTION ops.resolve_query(bigint, text, text), core.visit_lifecycle(), core.refuse_locked_visit() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ops.resolve_query(bigint, text, text) TO data_manager;

RESET ROLE;
