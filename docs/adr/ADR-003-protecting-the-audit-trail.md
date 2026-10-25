# ADR-003: Protecting the audit trail

Status: accepted, 2026-10-24

## Context

In the first draft the audit trigger ran with the caller's privileges. So the loading account needed INSERT on the audit table, which also let it write a fake audit row. An audit trail that the audited account can write to proves nothing.

## Decision

Think of a security camera: staff are recorded, but they can't touch the recorder.

- The audit table and its trigger function live in their own `audit` schema, owned by `audit_owner`, a role nobody logs in as. A separate schema matters because the owner of a schema can drop tables in it. The `platform_owner` that runs migrations can't drop the audit table.
- The trigger function is `SECURITY DEFINER`, so it runs as `audit_owner`. Application roles have **no privilege at all** on the audit schema. Changing a record produces an audit row, but no application role can write, change or delete one. `auditor` can only read.
- The function pins `search_path`, so a caller can't plant a look-alike object that the function would pick up.
- `audit_log` refuses UPDATE, DELETE and TRUNCATE through its own triggers, even for its owner.
- Every system-of-record table refuses TRUNCATE, because TRUNCATE skips row triggers and would erase history without a trace.

## Consequences

- **A superuser can still get around all of this**, for example by disabling the triggers. No design inside the database can fully stop its own superuser. The honest answer:
  - keep superuser access rare, named, and audited outside the database;
  - stream audit rows to write-once storage the database admin can't reach, such as object storage with object lock;
  - reconcile the copy against the database regularly.
  This prototype states the limit; streaming to write-once storage is listed as further work.
- Each audit row stores the full old and new row as JSON. That is simple and complete, but large. At 100 subjects the initial load writes about 200,000 audit rows, which is fine at this scale.

## Alternatives considered

- Application-level audit logging. It is easy to bypass with any direct database connection.
- PostgreSQL's pgaudit extension. It logs statements to the server log, which is useful as a second layer for admin activity. But it doesn't give a queryable old and new value with a reason for each record.
