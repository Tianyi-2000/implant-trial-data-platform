-- Least privilege, as the superuser. Nobody gets TRUNCATE or DELETE, and no application role
-- has any privilege on the audit schema (auditor can read it).

GRANT USAGE  ON SCHEMA ops, ref, core TO loader, data_manager, auditor, transformer;
GRANT SELECT ON ALL TABLES IN SCHEMA ops, ref, core TO loader, data_manager, auditor, transformer;

-- Loads only ever add rows to the system of record.
GRANT INSERT ON ALL TABLES IN SCHEMA ref, core TO loader;
GRANT INSERT, UPDATE ON ops.ingest_batch TO loader;
GRANT INSERT ON ops.data_query, ops.rejected_row TO loader;

-- Corrections are updates with a reason, made by a person. Data queries are closed only through
-- ops.resolve_query (db/50_corrections.sql): nobody gets UPDATE on ops.data_query directly.
GRANT UPDATE ON core.visit, core.impedance_measurement, core.threshold_measurement, core.adverse_event TO data_manager;

-- Analysts see only the analysis layer, which dbt (transformer) owns and rebuilds.
CREATE SCHEMA mart AUTHORIZATION transformer;
GRANT USAGE ON SCHEMA mart TO analyst;
ALTER DEFAULT PRIVILEGES FOR ROLE transformer IN SCHEMA mart GRANT SELECT ON TABLES TO analyst;
