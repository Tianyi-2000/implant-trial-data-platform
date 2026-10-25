-- Audit trail: who, when, what and why, enforced by the database (see docs/adr/ADR-002, ADR-003).
-- Owned by audit_owner, which nobody logs in as. Application roles have no privilege on this schema:
-- changing an audited table causes an audit row, but no application role can write, change or delete one.
SET ROLE audit_owner;

CREATE SCHEMA audit;

CREATE TABLE audit.audit_log (
  audit_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  table_name text        NOT NULL,
  record_key jsonb       NOT NULL,           -- primary key of the changed row, e.g. {"threshold_id": 42}
  action     text        NOT NULL CHECK (action IN ('INSERT', 'UPDATE', 'DELETE')),
  old_row    jsonb,
  new_row    jsonb,
  db_user    text        NOT NULL,           -- session_user: the login the database authenticated
  app_user   text,                           -- app.user_id: who a service account says it acts for (a claim)
  reason     text,                           -- required for UPDATE and DELETE
  xact_id    bigint      NOT NULL,           -- changes made in one transaction share it
  changed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON audit.audit_log (table_name, record_key);

-- SECURITY DEFINER: runs as audit_owner, so the caller needs no privilege on audit_log.
-- A fixed search_path stops a caller from planting look-alike objects. Arguments: primary-key column(s).
CREATE FUNCTION audit.audit_row() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  v_app_user text  := NULLIF(current_setting('app.user_id', true), '');
  v_reason   text  := NULLIF(current_setting('app.change_reason', true), '');
  v_row      jsonb := to_jsonb(CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END);
  v_key      jsonb;
BEGIN
  -- pg_has_role is true for superusers too, so an admin fixing data by hand must also say for whom.
  IF v_app_user IS NULL AND pg_has_role(session_user, 'service_account', 'MEMBER') THEN
    RAISE EXCEPTION 'Login % is a service account or superuser: set app.user_id to say who or what this change is for', session_user;
  END IF;
  IF TG_OP IN ('UPDATE', 'DELETE') AND v_reason IS NULL THEN
    RAISE EXCEPTION 'A reason for change is required (SET LOCAL app.change_reason = ...)';
  END IF;
  SELECT jsonb_object_agg(k, v_row -> k) INTO v_key FROM unnest(TG_ARGV) AS k;
  INSERT INTO audit.audit_log (table_name, record_key, action, old_row, new_row, db_user, app_user, reason, xact_id)
  VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, v_key, TG_OP,
          CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END,
          CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END,
          session_user, v_app_user, v_reason, pg_current_xact_id()::text::bigint);
  RETURN NULL;
END $$;

-- TRUNCATE skips row triggers, so it would erase history without a trace: refuse it on audited tables.
CREATE FUNCTION audit.refuse_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'TRUNCATE on %.% is not allowed: it would bypass the audit trail', TG_TABLE_SCHEMA, TG_TABLE_NAME;
END $$;

-- Audit rows are never changed or removed, not even by their owner.
-- A superuser can still disable this trigger; that limit is documented, not hidden.
CREATE FUNCTION audit.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit.audit_log is append-only';
END $$;
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON audit.audit_log
  FOR EACH ROW EXECUTE FUNCTION audit.refuse_change();
CREATE TRIGGER append_only_truncate BEFORE TRUNCATE ON audit.audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION audit.refuse_change();

REVOKE ALL ON FUNCTION audit.audit_row(), audit.refuse_truncate(), audit.refuse_change() FROM PUBLIC;
GRANT USAGE ON SCHEMA audit TO platform_owner, auditor;
GRANT EXECUTE ON FUNCTION audit.audit_row(), audit.refuse_truncate() TO platform_owner;
GRANT SELECT ON audit.audit_log TO auditor;

RESET ROLE;
