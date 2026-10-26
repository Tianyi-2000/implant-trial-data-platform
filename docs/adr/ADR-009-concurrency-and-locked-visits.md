# ADR-009: Concurrent loads and locked visits

Status: accepted, 2026-10-25. Requirements: REQ-02, REQ-13.

## Context

Two loaders may pick up the same file at the same time. And once a visit's data has been reviewed for an analysis, it must stop changing. 21 CFR Part 11 §11.10(f) asks the system itself to enforce that kind of operational sequence.

## Decision

**Concurrent loads**

- **Each load stages its rows in its own temp table** (`ON COMMIT DROP`). No other session can see it, and it disappears at commit.
- **The unique index decides whether a file is new, not a "have I seen it?" query.** `ops.receive_file` is `INSERT ... ON CONFLICT DO NOTHING`. Two indexes back it:
  - a partial unique index on `(source_system, sha256) WHERE status <> 'rejected'`;
  - `UNIQUE (source_system, sha256, parser_version)` (ADR-006).

  A second loader racing on the same file waits on the index until the first commits, then gets NULL back and skips the file.
- **The landing zone writes each file under a temp name of its own**, then renames it atomically. Two loaders keeping the same file can't collide.

**Locked visits**

- **A visit goes entered → verified → locked.** Only a verified visit can be locked.
- **Triggers refuse every change to a locked visit, whoever asks.**
  - A measurement of a locked visit can't be inserted, updated or deleted.
  - The locked visit row itself allows exactly one change: unlocking it, back to verified.
  - Unlocking and locking are updates, so the audit trail requires a reason for both.
- **A lock and a load can't slip past each other.** The measurement trigger takes `FOR SHARE` on the visit row:
  - while any change to a visit's measurements is still open, the visit can't be locked;
  - a change that had to wait for a lock to commit sees the visit locked.
- **A load into a locked visit is quarantined if it has new rows.** Duplicates change nothing. Conflicts only raise queries, and accepting one needs the visit to be unlocked first. If the visit is locked between the load's check and its insert, the file fails as a whole, and the next run quarantines it.

## Consequences

- Staged rows are gone after a failed load. The raw file in the landing zone reproduces them.
- **There is no separation of duties.** The same role locks and unlocks. In production, unlocking would need a second person's approval, with an electronic signature (Part 11 §11.50, §11.200). That is not built.
- A row-level trigger runs for every measurement. The initial load of about 200,000 rows still takes about 22 seconds, the same as without it.

## Alternatives considered

- **One shared staging table with a batch column.** It needs cleanup, and concurrent loads see each other's rows.
- **Check first, then insert.** Two loaders can both see "not received yet", then both insert.
- **Advisory locks.** They would work, but the unique index already is the source of truth.
- **Enforcing the lock in the application.** Anyone with a direct database connection would bypass it.
