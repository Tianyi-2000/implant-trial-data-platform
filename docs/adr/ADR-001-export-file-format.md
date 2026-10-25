# ADR-001: Export file format and landing zone

Status: accepted, 2026-10-24

## Context

Fitting software at each site exports electrode measurements. The platform has to tell, from the file alone, whose data it is, what was measured, in which unit, and whether the file arrived complete.

## Decision

- **One file per subject, per visit, per measurement type** (impedance or threshold). A file is the unit of loading, quarantine and re-sending, so a bad file holds back one session, not a whole day of clinic work.
- **Metadata lives in a header block** of `# key=value` lines, followed by a plain CSV table: format version, export tool version, export time, site, subject, device serial, visit, measurement, unit and row count.
  - `device_serial` is read from the implant, while `subject_code` is chosen by a person. Comparing the two catches wrong-patient files.
  - `unit` and the column name (`impedance_kohm`) both state the unit, so the parser can cross-check them and refuse a file whose unit changed.
  - `row_count` is the manifest: a truncated file shows up as a count mismatch.
- **Raw files are kept exactly as received**, in a write-once landing zone, named by their SHA-256. In production that zone is object storage with object lock, such as S3. Here a local `landing/` folder simulates it.
- Raw data never goes into git. Git history cannot be deleted, access can only be granted to the whole repository, and nothing in git is write-once. The synthetic `raw/` folder is regenerated from a fixed seed instead.

## Consequences

- The header format is a contract with the fitting software team. It needs a version number and a written spec, and any change to it goes through change control.
- Timestamps carry their UTC offset (`2026-07-27T11:20:02+02:00`), so sites in different time zones store the same instant unambiguously.

## Alternatives considered

- One file per day of clinic. Fewer files, but one bad record blocks every patient seen that day.
- Matching the visit by date instead of a `visit_code` in the header. That is closer to how some devices work, but it needs visit-window logic. Left for later.
