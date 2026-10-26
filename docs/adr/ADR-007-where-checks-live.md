# ADR-007: Where each check lives

Status: accepted, 2026-10-25. Requirements: REQ-05, REQ-06, REQ-07.

## Context

Checks could live in the Python loader, in SQL, or in table constraints. Putting a check in the wrong place either opens a race, or makes it easy to bypass.

## Decision

A file flows through these steps:

```
fitting export → landing zone (kept, named by hash) → parser (Python) → receipt (ops.receive_file)
              → COPY into a temp table → ops.load_measurements (SQL, same transaction) → system of record
```

- **The parser checks only what the file itself can tell:**
  - required header keys and the format version;
  - unit and column names;
  - each value's shape and number of decimals;
  - duplicate electrode numbers;
  - timestamps, which must have a UTC offset;
  - the row count against the header.

  A negative number is still a number. Whether it is plausible is not the parser's job.
- **SQL checks everything that needs the database, in the same transaction as the write:**
  - whose device the serial belongs to;
  - whether the subject is enrolled at the site;
  - whether the visit exists, and whether it is locked;
  - whether each electrode exists on the device;
  - plausible ranges;
  - duplicate or conflict.

  Because the check and the write share one transaction, no other session can change the answer in between.
- **CHECK constraints stay as the last line of defense**, for example `impedance_kohm > 0` and threshold 0–1000 µA.
- **Files, not streaming.** A fitting session produces a file, and at this volume (about 800 files) a message queue would add operations without adding value. If devices streamed continuously, or many sites needed near-real-time monitoring, a queue would go in front, with the same idempotent load behind it.

## Consequences

- Plausibility rules are written twice, in the stored procedure and in the CHECK constraints, so changing a rule means changing both. In return, a bug in the procedure still can't store an impossible value.

## Alternatives considered

- **Everything in Python.** Checking facts means reading first and writing later, which is a race. It is also bypassed by anyone with a direct database connection.
- **Everything in SQL.** Parsing a header block plus CSV in SQL is awkward, and harder to unit test.
