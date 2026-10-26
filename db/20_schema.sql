-- Schema for an implantable stimulating electrode array trial. Synthetic data only.
-- Layers: ops (lineage, data queries), ref (catalog), core (normalized system of record).
-- mart (analysis) is built by dbt from core and is never edited by hand.
SET ROLE platform_owner;

CREATE SCHEMA ops;
CREATE SCHEMA ref;
CREATE SCHEMA core;

-- ============================================================
-- ops: lineage and data queries
-- ============================================================

-- One row per received file: the receipt. Every loaded row points back to its batch (ADR-005).
-- A file is recognized by its content hash, never by its name.
CREATE TABLE ops.ingest_batch (
  ingest_batch_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_system      text     NOT NULL CHECK (source_system IN ('setup', 'edc', 'fitting_export', 'bench')),
  source_name        text     NOT NULL,                 -- the file name as received
  source_uri         text     NOT NULL,                 -- the raw file in the write-once landing zone
  sha256             char(64) NOT NULL,
  parser_version     text     NOT NULL,                 -- version of the parser that read it
  status             text     NOT NULL CHECK (status IN ('received', 'loaded', 'quarantined', 'rejected')),
  status_reason      text,
  row_count_expected int,                               -- from the file header: the manifest
  rows_inserted      int      NOT NULL DEFAULT 0,
  rows_duplicate     int      NOT NULL DEFAULT 0,       -- already stored with the same values
  rows_conflict      int      NOT NULL DEFAULT 0,       -- already stored with different values: data query
  rows_rejected      int      NOT NULL DEFAULT 0,       -- implausible value: ops.rejected_row and data query
  received_at        timestamptz NOT NULL DEFAULT now(),
  -- Reconciliation: a batch only counts as loaded when every row of the manifest is accounted for.
  CONSTRAINT reconciles CHECK (status <> 'loaded'
    OR row_count_expected = rows_inserted + rows_duplicate + rows_conflict + rows_rejected),
  CONSTRAINT nothing_loaded_when_held CHECK (status NOT IN ('quarantined', 'rejected') OR rows_inserted = 0),
  -- A rejected file may be retried by a newer parser version, never twice by the same one.
  UNIQUE (source_system, sha256, parser_version)
);
-- A file is loaded (or quarantined) at most once, whichever parser read it (ADR-009).
CREATE UNIQUE INDEX ingest_batch_once ON ops.ingest_batch (source_system, sha256) WHERE status <> 'rejected';

-- A question back to the site or the engineer instead of a silent fix (ADR-008).
CREATE TABLE ops.data_query (
  query_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  query_type      text   NOT NULL CHECK (query_type IN ('conflict', 'rejected_row', 'quarantined_file', 'rejected_file')),
  table_name      text   NOT NULL,
  record_id       bigint,                     -- conflict: the stored row the query is about
  ingest_batch_id bigint REFERENCES ops.ingest_batch,
  issue           text   NOT NULL,
  stored          jsonb,                      -- conflict: the stored row as the query saw it
  proposed        jsonb,                      -- conflict: the resent values, applied only if accepted
  status          text   NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'closed')),
  raised_at       timestamptz NOT NULL DEFAULT now(),
  decision        text   CHECK (decision IN ('accept_resent', 'keep_stored')),
  resolution      text,                       -- the data manager's reason
  resolved_by     text,
  resolved_at     timestamptz,
  CHECK ((status = 'closed') = (decision IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)),
  CHECK (decision IS DISTINCT FROM 'accept_resent' OR query_type = 'conflict')
);

-- Rows held back from a loaded batch, kept exactly as received (ADR-005). Each one raises a data query.
CREATE TABLE ops.rejected_row (
  rejected_row_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_batch_id bigint NOT NULL REFERENCES ops.ingest_batch,
  line_no         int    NOT NULL,            -- data row number in the file, 1 = first row after the column names
  raw_line        text   NOT NULL,
  reason          text   NOT NULL
);

-- ============================================================
-- ref: product catalog (a new array size is a new row, not a schema change)
-- ============================================================

CREATE TABLE ref.device_model (
  device_model_id int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  model_code      text NOT NULL UNIQUE,                 -- 'ARRAY-256'
  electrode_count int  NOT NULL CHECK (electrode_count > 0),
  grid_cols       int  NOT NULL CHECK (grid_cols > 0),  -- layout: electrode n sits at column (n-1) % grid_cols
  pitch_um        numeric(7,1) NOT NULL CHECK (pitch_um > 0)
);

-- ============================================================
-- core: normalized system of record
-- ============================================================

CREATE TABLE core.study (
  study_id         int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  study_code       text NOT NULL UNIQUE,
  protocol_version text NOT NULL
);

CREATE TABLE core.site (
  site_id   int     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  site_code text    NOT NULL UNIQUE,
  country   char(2) NOT NULL,
  timezone  text    NOT NULL                    -- IANA name; visit dates are local to the site
);

-- Pseudonymous: the key linking subject_code to a real person stays at the site.
CREATE TABLE core.subject (
  subject_id   bigint   GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  subject_code text     NOT NULL UNIQUE,
  study_id     int      NOT NULL REFERENCES core.study,
  site_id      int      NOT NULL REFERENCES core.site,
  enrolled_on  date     NOT NULL,
  birth_year   smallint CHECK (birth_year BETWEEN 1900 AND 2100),   -- year only, no birth date
  sex          text     CHECK (sex IN ('F', 'M', 'U')),
  status       text     NOT NULL DEFAULT 'active' CHECK (status IN ('screening', 'active', 'withdrawn', 'completed'))
);

-- Links clinic data back to manufacturing (and later to bench test data).
CREATE TABLE core.manufacturing_lot (
  lot_id          int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lot_code        text NOT NULL UNIQUE,
  manufactured_on date NOT NULL,
  process_rev     text NOT NULL
);

CREATE TABLE core.implant (
  implant_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  subject_id      bigint NOT NULL REFERENCES core.subject,
  device_model_id int    NOT NULL REFERENCES ref.device_model,
  device_serial   text   NOT NULL UNIQUE,        -- read from the device itself; used to catch wrong-patient files
  lot_id          int    NOT NULL REFERENCES core.manufacturing_lot,
  implanted_on    date   NOT NULL,
  explanted_on    date,
  CHECK (explanted_on IS NULL OR explanted_on >= implanted_on)
);

CREATE TABLE core.electrode (
  electrode_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  implant_id   bigint NOT NULL REFERENCES core.implant,
  electrode_no int    NOT NULL CHECK (electrode_no >= 1),
  x_um         numeric(8,1),
  y_um         numeric(8,1),
  UNIQUE (implant_id, electrode_no)
);

CREATE TABLE core.visit_schedule (
  study_id    int  NOT NULL REFERENCES core.study,
  visit_code  text NOT NULL,                 -- 'D7', 'D30', 'D90', 'D180', 'UNS' (unscheduled)
  target_day  int,                           -- days after implant; null for unscheduled
  window_days int  CHECK (window_days >= 0),
  PRIMARY KEY (study_id, visit_code)
);

CREATE TABLE core.visit (
  visit_id   bigint   GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  subject_id bigint   NOT NULL REFERENCES core.subject,
  study_id   int      NOT NULL,
  visit_code text     NOT NULL,
  visit_seq  smallint NOT NULL DEFAULT 1,    -- 2, 3... for repeated unscheduled visits
  visit_date date     NOT NULL,              -- local date at the site
  status     text     NOT NULL DEFAULT 'entered' CHECK (status IN ('entered', 'verified', 'locked')),
  FOREIGN KEY (study_id, visit_code) REFERENCES core.visit_schedule,
  UNIQUE (subject_id, visit_code, visit_seq)
);

-- Grain: one row per electrode per visit per test frequency.
CREATE TABLE core.impedance_measurement (
  impedance_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  visit_id        bigint NOT NULL REFERENCES core.visit,
  electrode_id    bigint NOT NULL REFERENCES core.electrode,
  test_freq_hz    int    NOT NULL DEFAULT 1000,
  impedance_kohm  numeric(8,2) NOT NULL CHECK (impedance_kohm > 0),
  measured_at     timestamptz NOT NULL,
  ingest_batch_id bigint NOT NULL REFERENCES ops.ingest_batch,
  UNIQUE (visit_id, electrode_id, test_freq_hz)
);

-- No row = not measured. no_response = measured, but no response up to the maximum current.
CREATE TABLE core.threshold_measurement (
  threshold_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  visit_id        bigint  NOT NULL REFERENCES core.visit,
  electrode_id    bigint  NOT NULL REFERENCES core.electrode,
  pulse_width_us  int     NOT NULL CHECK (pulse_width_us > 0),
  threshold_ua    numeric(6,1) CHECK (threshold_ua BETWEEN 0 AND 1000),
  no_response     boolean NOT NULL DEFAULT false,
  method          text    NOT NULL,
  measured_at     timestamptz NOT NULL,
  ingest_batch_id bigint  NOT NULL REFERENCES ops.ingest_batch,
  UNIQUE (visit_id, electrode_id),           -- add pulse_width_us to the key if fitting tests several
  CHECK (no_response OR threshold_ua IS NOT NULL)
);

-- Versioned fitting result. The database guarantees one active version per implant at any moment.
CREATE TABLE core.stim_config (
  config_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  implant_id  bigint NOT NULL REFERENCES core.implant,
  version     int    NOT NULL,
  created_by  text   NOT NULL,
  approved_by text,
  active_from timestamptz NOT NULL,
  active_to   timestamptz,                   -- null = still active
  UNIQUE (implant_id, version),
  CHECK (active_to IS NULL OR active_to > active_from),
  EXCLUDE USING gist (implant_id WITH =, tstzrange(active_from, active_to) WITH &&)
);

CREATE TABLE core.stim_config_electrode (
  config_id    bigint  NOT NULL REFERENCES core.stim_config,
  electrode_id bigint  NOT NULL REFERENCES core.electrode,
  enabled      boolean NOT NULL,
  min_ua       numeric(6,1),
  max_ua       numeric(6,1),
  PRIMARY KEY (config_id, electrode_id),
  CHECK (NOT enabled OR (min_ua IS NOT NULL AND max_ua > min_ua))
);

CREATE TABLE core.adverse_event (
  ae_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  subject_id     bigint NOT NULL REFERENCES core.subject,
  term           text   NOT NULL,
  onset_date     date   NOT NULL,
  resolved_date  date,
  serious        boolean NOT NULL,
  severity       text   NOT NULL CHECK (severity IN ('mild', 'moderate', 'severe')),
  device_related text   NOT NULL CHECK (device_related IN ('not', 'unlikely', 'possible', 'probable', 'definite')),
  outcome        text,
  CHECK (resolved_date IS NULL OR resolved_date >= onset_date)
);

-- ============================================================
-- Audit and truncate guards on every system-of-record table
-- ============================================================

CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON core.study                 FOR EACH ROW EXECUTE FUNCTION audit.audit_row('study_id');
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON core.site                  FOR EACH ROW EXECUTE FUNCTION audit.audit_row('site_id');
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON core.subject               FOR EACH ROW EXECUTE FUNCTION audit.audit_row('subject_id');
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON core.manufacturing_lot     FOR EACH ROW EXECUTE FUNCTION audit.audit_row('lot_id');
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON core.implant               FOR EACH ROW EXECUTE FUNCTION audit.audit_row('implant_id');
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON core.electrode             FOR EACH ROW EXECUTE FUNCTION audit.audit_row('electrode_id');
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON core.visit_schedule        FOR EACH ROW EXECUTE FUNCTION audit.audit_row('study_id', 'visit_code');
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON core.visit                 FOR EACH ROW EXECUTE FUNCTION audit.audit_row('visit_id');
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON core.impedance_measurement FOR EACH ROW EXECUTE FUNCTION audit.audit_row('impedance_id');
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON core.threshold_measurement FOR EACH ROW EXECUTE FUNCTION audit.audit_row('threshold_id');
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON core.stim_config           FOR EACH ROW EXECUTE FUNCTION audit.audit_row('config_id');
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON core.stim_config_electrode FOR EACH ROW EXECUTE FUNCTION audit.audit_row('config_id', 'electrode_id');
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON core.adverse_event         FOR EACH ROW EXECUTE FUNCTION audit.audit_row('ae_id');
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON ops.data_query             FOR EACH ROW EXECUTE FUNCTION audit.audit_row('query_id');

DO $$
DECLARE t regclass;
BEGIN
  FOR t IN SELECT c.oid::regclass FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname IN ('ops', 'ref', 'core') AND c.relkind = 'r'
  LOOP
    EXECUTE format('CREATE TRIGGER no_truncate BEFORE TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION audit.refuse_truncate()', t);
  END LOOP;
END $$;

RESET ROLE;
