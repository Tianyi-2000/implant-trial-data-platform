# implant-trial-data-platform

A prototype I am building to learn clinical device data management: a trial database for an implantable stimulating electrode array, run entirely on synthetic data.

**Status: work in progress.** This is not a validated system and has not been through 21 CFR Part 11 validation. It shows design choices; it is not production software.

## What it demonstrates

- Raw files are kept exactly as received, fingerprinted with SHA-256.
- Loads are idempotent but never overwrite silently: a resent file with a different value raises a data query instead.
- Every change is attributable and needs a reason. The database enforces this, and no application role can write to or edit the audit trail.
- Electrode impedance can be traced back to the manufacturing lot.

## Quick start

Needs Docker and [uv](https://docs.astral.sh/uv/).

```bash
make up      # PostgreSQL 17 on localhost:5433
make db      # roles, schemas, audit trail, tables, grants
make seed    # synthetic trial into raw/ (100 subjects, 791 fitting exports, 5 planted problem files)
```

## Design decisions

| ADR | Decision |
|---|---|
| [001](docs/adr/ADR-001-export-file-format.md) | One export per subject, visit and measurement, with a metadata header; raw files are write-once |
| [002](docs/adr/ADR-002-who-made-the-change.md) | Audit the authenticated login, plus who a service account says it acts for |
| [003](docs/adr/ADR-003-protecting-the-audit-trail.md) | The audit trail is owned by a role nobody logs in as; application roles cannot touch it |
| [004](docs/adr/ADR-004-synthetic-data-and-planted-signal.md) | 100 synthetic subjects, lots balanced within sites, and a planted known-answer signal |

All data is synthetic, and its parameters are invented (`src/itdp/params.py`). They are not physiological reference values.
