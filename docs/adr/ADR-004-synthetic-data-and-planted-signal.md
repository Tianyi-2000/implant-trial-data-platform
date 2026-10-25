# ADR-004: Synthetic data and the planted signal

Status: accepted, 2026-10-24

## Context

The prototype must not touch real patient data. It still needs data realistic enough to exercise every rule, and an analysis result whose correct answer is known in advance.

## Decision

- **100 subjects at 4 sites** (25 each, in 4 time zones), 4 visits (D7, D30, D90, D180) and a 256-electrode array. With 100 subjects, each manufacturing lot has about 33 implants, so a lot comparison is not a story about 4 devices.
- **Lots are balanced within every site** (8 or 9 implants per lot per site). If one lot had gone mostly to one site, a faster drift could just as well be that site's surgical technique or measuring habit. Site and lot would be confounded, and the data couldn't tell them apart.
- **Lot C is given a faster impedance drift, on purpose.** This is a known-answer test, not a faked result: when the true answer is planted, the test is whether the pipeline and report find it. The signal is clear in aggregate (median log-change D7 to D180: about 0.32 for lot C, against 0.08 and 0.11 for lots A and B), but individual implants overlap, as real data would.
- Other realism, all in `src/itdp/params.py`:
  - about 1% of electrodes open from the start, and a few more open later;
  - about 5% no-response thresholds;
  - 3 withdrawals after D90;
  - 14 adverse events;
  - 2 or 3 versioned stimulation configs per implant.
- The **same seed gives the same bytes**. Content hashes stay stable across regenerations, and the tests can rely on exact counts.
- The ground truth is written to `raw/_truth/truth.json` for the tests and is **never loaded** into the database.

## Consequences

- The parameters are invented, not physiological reference values. The README says so.
- Five planted problem files (A to E) replace or duplicate specific normal files, so each ingestion rule meets exactly one realistic failure. For example, file C is a real session exported under the wrong subject: the subject was picked by a person, and the device serial was read from the hardware.

## Alternatives considered

- 12 subjects. Faster to read, but 4 implants per lot can't support a lot comparison.
- Randomizing lots without stratifying by site. Simpler, but confounding becomes possible by chance.
