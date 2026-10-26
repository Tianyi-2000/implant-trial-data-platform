# implant-trial-data-platform

A prototype I am building to learn clinical device data management: a trial database for an implantable stimulating electrode array, run entirely on synthetic data.

**Status: work in progress.** This is not a validated system and has not been through 21 CFR Part 11 validation. It shows design choices; it is not production software.

## What it demonstrates

- Raw files are kept exactly as received, read-only, named by their SHA-256.
- Loads are idempotent but never overwrite silently. A resent file changes nothing; a resent file with a different value raises a data query instead.
- A file for the wrong patient is quarantined whole. A file whose format or unit changed is rejected whole. One implausible value is held back on its own.
- Every file reconciles: the database refuses to mark it loaded unless every row in the header's count is accounted for.
- Every change is attributable and needs a reason. The database enforces this, and no application role can write to or edit the audit trail.
- Only a data manager closes a data query, in the same transaction as the change it makes. A locked visit can't be changed by anyone until it is unlocked with a reason.
- Every test names the requirement it verifies.
- Electrode impedance can be traced back to the manufacturing lot.

## Quick start

Needs Docker and [uv](https://docs.astral.sh/uv/).

```bash
make up        # PostgreSQL 17 on localhost:5433
make db        # roles, schemas, audit trail, tables, grants, load and correction functions
make seed      # synthetic trial into raw/ (100 subjects, 791 fitting exports, 5 planted problem files)
make load      # load everything; run it again and nothing changes
make planted   # the five problem files, A to E: skipped, conflict, quarantined, row rejected, file rejected
make batches   # every file's receipt, reconciled by status
make queries   # the open data queries
make correct   # a data manager works through the queries and locks a visit, then the audit trail
make test      # pytest on a throwaway database, built and loaded from scratch
```

`make reset` drops the database and rebuilds it empty.

## How a file is loaded

```
fitting export → landing/ (kept, named by hash) → parser (Python) → receipt (ops.receive_file)
              → COPY into a temp table → ops.load_measurements (SQL, same transaction) → system of record
```

The parser checks only what the file itself can tell. Everything that needs the database is checked in SQL, in the same transaction as the write (ADR-007). Each row ends as new, duplicate, conflict or rejected (ADR-005).

## Tests

`make test` builds a database called `itdp_test`, generates the trial into a temp folder, loads it, and runs every test inside a transaction that is rolled back. The demo database is never touched.

Requirements are listed in [`docs/requirements.yaml`](docs/requirements.yaml). Each test is marked with the requirement it verifies, for example `@pytest.mark.req("REQ-08")`. A test without one, or with an unknown one, stops the run.

## Design decisions

| ADR | Decision |
|---|---|
| [001](docs/adr/ADR-001-export-file-format.md) | One export per subject, visit and measurement, with a metadata header; raw files are write-once |
| [002](docs/adr/ADR-002-who-made-the-change.md) | Audit the authenticated login, plus who a service account says it acts for |
| [003](docs/adr/ADR-003-protecting-the-audit-trail.md) | The audit trail is owned by a role nobody logs in as; application roles cannot touch it |
| [004](docs/adr/ADR-004-synthetic-data-and-planted-signal.md) | 100 synthetic subjects, lots balanced within sites, and a planted known-answer signal |
| [005](docs/adr/ADR-005-partial-loads-and-reconciliation.md) | Idempotent by file hash and by business key; one bad value holds back one row, an identity doubt holds back the file; reconciliation enforced by a CHECK |
| [006](docs/adr/ADR-006-reject-format-changes.md) | A changed unit or format rejects the file; only a newer parser version may try it again |
| [007](docs/adr/ADR-007-where-checks-live.md) | Python checks the file, SQL checks facts in the write's transaction, constraints are the last line of defense |
| [008](docs/adr/ADR-008-closing-a-data-query.md) | Loads never overwrite; a data manager closes a query with a decision and a reason, and stale decisions are refused |
| [009](docs/adr/ADR-009-concurrency-and-locked-visits.md) | The unique index decides who loads a file; a locked visit refuses every change until it is unlocked |

## Known limitations so far

- A superuser can get around the audit trail, for example by disabling triggers (ADR-003).
- When many users share one application login, `app_user` is only as trustworthy as the application's own authentication (ADR-002).
- A quarantined file can't be released; the site sends a corrected export instead (ADR-005).
- The same role locks and unlocks visits. There is no second-person approval and no electronic signature (ADR-009).
- Refused attempts are not recorded, because they roll back (ADR-008).

All data is synthetic, and its parameters are invented (`src/itdp/params.py`). They are not physiological reference values.
