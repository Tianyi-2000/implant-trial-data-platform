# ADR-006: Reject a changed format or unit, never convert it on the way in

Status: accepted, 2026-10-25. Requirements: REQ-07.

## Context

After a software update, a fitting export arrives with `unit=ohm` instead of `kohm`. Converting it would take one line of code.

## Decision

- **The format is a contract with the team that writes the export.** The contract covers the format version, the unit, the column names and the number of decimals. Parser version 1 accepts exactly that contract.
- **Any deviation rejects the whole file.** The receipt records the reason and the parser version, and a data query is raised.
- **Why not convert?**
  - An unannounced change means the exporter changed without change control. Something else may have changed too.
  - Maybe only the header is wrong, and the numbers are still kilo-ohms.
  - If the file were accepted, two units would sit in one column, and any query that forgot the unit would be off by a factor of 1,000.
- **The way forward:**
  1. Confirm the change with the exporting team.
  2. Release a new, tested parser version.
  3. Reload from the untouched raw file in the landing zone.

  `UNIQUE (source_system, sha256, parser_version)` lets a newer parser try a rejected file again, but never lets the same parser try twice. A file that was loaded is never loaded again, whichever parser reads it.

## Consequences

- The data arrives days later.
- Every batch records the parser version that read it, so it is always known which code read which file.

## Alternatives considered

- **Converting automatically.** Rejected, for the reasons above.
- **Storing the original value and unit next to a standardized value**, the way CDISC SDTM pairs `--ORRES`/`--ORRESU` with `--STRESN`/`--STRESU`. This is the right design when sources legitimately use different units, for example several bench instruments. Even then, the conversion is an explicit, versioned and tested mapping, not a silent fix inside the parser.
