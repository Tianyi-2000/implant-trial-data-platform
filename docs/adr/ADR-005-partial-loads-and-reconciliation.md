# ADR-005: Idempotent loads, partial loads and reconciliation

Status: accepted, 2026-10-25. Requirements: REQ-02, REQ-03, REQ-05, REQ-06, REQ-08.

## Context

Files are delivered at least once: a site may resend a file, re-export a session, or send a file with one bad reading in it. The platform must never load a file twice, never lose a row, and be able to prove, for every file, that nothing went missing between the file and the database.

## Decision

- **Idempotency works at two levels.**
  - A file is recognized by its SHA-256 hash, never by its name. Its receipt is a row in `ops.ingest_batch`. A file whose bytes were already received is skipped.
  - A row is recognized by its business key: visit, electrode and test condition (test frequency for impedance). A re-export with new bytes but the same values adds nothing.

  At-least-once delivery plus an idempotent write gives an effectively exactly-once result.
- **Every row ends as exactly one of four outcomes:**
  - *new*: inserted;
  - *duplicate*: already stored with the same values;
  - *conflict*: already stored with different values. The stored value stays, and a data query is raised (ADR-008);
  - *rejected*: physically implausible. The row is kept verbatim in `ops.rejected_row`, and a data query is raised.
- **The kind of error decides how much is held back.**
  - Doubt about identity or structure holds back the whole file. That means a device serial belonging to another subject, an unknown subject, site, visit or electrode, or a file the parser can't read (ADR-006). The file is quarantined or rejected, and no row of it is loaded.
  - One implausible value holds back only that row. The rest of the file loads.
- **Reconciliation is enforced by the database.** The expected count is the `row_count` in the file header, the manifest written by the exporter. The actual count is computed by the database from the rows it received. The `reconciles` CHECK on `ops.ingest_batch` refuses to mark a batch loaded unless new + duplicate + conflict + rejected equals the expected count. The loader never vouches for itself.
- **Each file loads in one transaction.** A failure anywhere rolls back everything for that file, including its receipt, so running the load again is always safe.

## Consequences

- A visit can end up one electrode short. Downstream analysis has to handle missing values.
- Reconciliation proves that every row in the file reached the database. It can't prove that the device measured every electrode: a file can honestly contain 255 rows. A completeness rule such as "256 electrodes per visit" belongs in the analysis layer's data tests.
- A quarantined file can't be released by sending it again, because the same bytes are skipped. The site sends a corrected export, which is a new file. A release step for quarantined files is not built.
- Skipped receipts still use up identity values, so batch ids have gaps. Never count by id.

## Alternatives considered

- **All or nothing for every error.** Simpler, but one sensor glitch would block 255 good readings. That would be the right rule if many rows in one file fail, because that points to a device or exporter problem. A threshold like that is not built.
- **Deduplicating by file name.** A file resent under another name would load twice.
- **Upsert (`ON CONFLICT DO UPDATE`).** It overwrites a stored value silently. See ADR-008.
