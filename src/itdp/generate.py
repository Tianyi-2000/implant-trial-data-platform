"""Write a deterministic synthetic trial into raw/ (ADR-001, ADR-004).

    python -m itdp.generate --out raw

raw/setup/     study setup: study, sites, visit schedule, device model, manufacturing lots
raw/edc/       case report form exports: subjects, implants, visits, adverse events
raw/fitting/   fitting software exports: one file per subject x visit x measurement, plus configs
raw/planted/   five problem files (A to E), each meant to trip one ingestion rule
raw/_truth/    the answers the tests check against; never loaded into the database
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from itdp import params as P

ELECTRODES = range(1, P.DEVICE_MODEL["electrode_count"] + 1)


@dataclass
class Session:
    visit_code: str
    visit_date: date
    impedance: list            # (electrode_no, kohm, measured_at)
    threshold: list            # (electrode_no, ua or None, no_response, measured_at); open electrodes absent
    impedance_exported: datetime
    threshold_exported: datetime


@dataclass
class Subject:
    code: str
    site: tuple
    lot: tuple
    implanted_on: date
    enrolled_on: date
    birth_year: int
    sex: str
    serial: str = ""
    withdrawn: bool = False
    slope: float = 0.0                                  # this implant's drift, log units per 180 days
    open_from: dict = field(default_factory=dict)       # electrode_no -> index of the first visit it is open
    sessions: dict = field(default_factory=dict)        # visit_code -> Session


def iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds")


def make_subjects(rng: random.Random) -> list[Subject]:
    first, last = date.fromisoformat(P.IMPLANT_FIRST), date.fromisoformat(P.IMPLANT_LAST)
    subjects = []
    for site_idx, site in enumerate(P.SITES):
        # Lots are balanced within every site, so a lot effect cannot hide a site effect (ADR-004).
        lots = [P.LOTS[(i + site_idx) % len(P.LOTS)] for i in range(site[3])]
        rng.shuffle(lots)
        for lot in lots:
            implanted = first + timedelta(days=rng.randint(0, (last - first).days))
            subjects.append(Subject(
                code=f"S{len(subjects) + 1:03d}", site=site, lot=lot, implanted_on=implanted,
                enrolled_on=implanted - timedelta(days=rng.randint(*P.ENROLL_BEFORE_IMPLANT_DAYS)),
                birth_year=rng.randint(*P.BIRTH_YEARS), sex=rng.choice("FM")))
    # Serials are issued per lot in implant order, as device tracking would.
    issued = Counter()
    for s in sorted(subjects, key=lambda s: (s.implanted_on, s.code)):
        issued[s.lot[0]] += 1
        s.serial = f"{s.lot[3]}{issued[s.lot[0]]:03d}"
    for s in rng.sample(subjects, P.WITHDRAWN_SUBJECTS):
        s.withdrawn = True
    return subjects


def simulate(s: Subject, rng: random.Random) -> None:
    tz = ZoneInfo(s.site[2])
    s.slope = rng.gauss(s.lot[4], P.IMPEDANCE_IMPLANT_SLOPE_SD)
    z0 = {e: P.IMPEDANCE_BASELINE_MEDIAN * math.exp(rng.gauss(0, P.IMPEDANCE_ELECTRODE_SD)) for e in ELECTRODES}
    t0 = {e: P.THRESHOLD_MEDIAN * math.exp(rng.gauss(0, P.THRESHOLD_ELECTRODE_SD)) for e in ELECTRODES}
    silent = {e for e in ELECTRODES if rng.random() < P.NO_RESPONSE_PERSISTENT_P}
    for e in ELECTRODES:
        if rng.random() < P.OPEN_AT_IMPLANT_P:
            s.open_from[e] = 0
            continue
        for k in range(1, len(P.VISITS)):
            if rng.random() < P.OPEN_LATER_P:
                s.open_from[e] = k
                break

    visits = P.VISITS[:-1] if s.withdrawn else P.VISITS
    for k, (code, target, window) in enumerate(visits):
        day = target + rng.randint(-window, window)
        visit_date = s.implanted_on + timedelta(days=day)
        clock = datetime.combine(visit_date, time(9, 0), tzinfo=tz) + timedelta(minutes=rng.randint(0, 300))

        impedance = []
        for e in ELECTRODES:
            clock += timedelta(seconds=rng.randint(1, 3))
            if s.open_from.get(e, 99) <= k:
                kohm = rng.uniform(*P.OPEN_KOHM_RANGE)
            else:
                kohm = z0[e] * math.exp(s.slope * day / 180 + rng.gauss(0, P.IMPEDANCE_NOISE_SD))
            impedance.append((e, round(kohm, 2), clock))
        impedance_exported = clock + timedelta(minutes=rng.randint(2, 10))

        threshold, clock = [], clock + timedelta(minutes=15)
        for e in ELECTRODES:
            if s.open_from.get(e, 99) <= k:
                continue                   # an open electrode cannot be stimulated: not measured, no row
            clock += timedelta(seconds=rng.randint(12, 25))
            ua = t0[e] * math.exp(rng.gauss(0, P.THRESHOLD_NOISE_SD))
            no_response = e in silent or rng.random() < P.NO_RESPONSE_TRANSIENT_P or ua > P.MAX_CURRENT_UA
            threshold.append((e, None if no_response else round(ua, 1), no_response, clock))
        threshold_exported = clock + timedelta(minutes=rng.randint(5, 40))

        s.sessions[code] = Session(code, visit_date, impedance, threshold, impedance_exported, threshold_exported)


# ---------- fitting exports (ADR-001: metadata header, then a CSV table) ----------

def export_meta(s: Subject, sess: Session, measurement: str, exported: datetime) -> dict:
    return {"format": "fitting-export", "format_version": P.FORMAT_VERSION, "export_tool": P.EXPORT_TOOL,
            "exported_at": iso(exported), "site_code": s.site[0], "subject_code": s.code,
            "device_serial": s.serial, "visit_code": sess.visit_code, "measurement": measurement}


def impedance_export(s: Subject, sess: Session):
    meta = export_meta(s, sess, "impedance", sess.impedance_exported) | {"unit": "kohm", "test_freq_hz": 1000}
    rows = [(e, f"{kohm:.2f}", iso(ts)) for e, kohm, ts in sess.impedance]
    return meta, ["electrode_no", "impedance_kohm", "measured_at"], rows


def threshold_export(s: Subject, sess: Session):
    meta = export_meta(s, sess, "threshold", sess.threshold_exported) | {
        "unit": "uA", "pulse_width_us": P.PULSE_WIDTH_US, "method": P.THRESHOLD_METHOD}
    rows = [(e, "" if ua is None else f"{ua:.1f}", "true" if nr else "false", iso(ts))
            for e, ua, nr, ts in sess.threshold]
    return meta, ["electrode_no", "threshold_ua", "no_response", "measured_at"], rows


def export_name(meta: dict) -> str:
    stamp = datetime.fromisoformat(meta["exported_at"]).strftime("%Y%m%dT%H%M%S")
    return f"{meta['subject_code']}_{meta['visit_code']}_{meta['measurement']}_{stamp}.csv"


def write_export(path: Path, meta: dict, columns: list, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        for key, value in (meta | {"row_count": len(rows)}).items():
            f.write(f"# {key}={value}\n")
        w = csv.writer(f, lineterminator="\n")
        w.writerow(columns)
        w.writerows(rows)
    return path


def write_csv(path: Path, columns: list, rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(columns)
        w.writerows(rows)
    return len(rows)


def threshold_at(s: Subject, visit: str, electrode: int):
    return next((ua for e, ua, _, _ in s.sessions[visit].threshold if e == electrode), None)


# ---------- the five planted problems ----------

def choose_planted(subjects: list[Subject], rng: random.Random) -> dict:
    pool = [s for s in subjects if not s.withdrawn]
    rng.shuffle(pool)
    a = pool.pop()
    b = next(s for s in pool if threshold_at(s, "D30", 17) is not None)
    pool.remove(b)
    c = pool.pop()
    real = next(s for s in pool if s.site == c.site)      # whose session C really is: same clinic
    pool.remove(real)
    d, e = pool.pop(), pool.pop()
    d_electrode = next(n for n in range(101, len(ELECTRODES) + 1) if d.open_from.get(n, 99) > 2)
    return {"A": a, "B": b, "C": c, "C_real": real, "D": d, "D_electrode": d_electrode, "E": e}


def withheld(pick: dict) -> set:
    """Normal files replaced by a planted stand-in: the data only exists in its faulty form."""
    return {(pick["C_real"].code, "D90", "threshold"),
            (pick["D"].code, "D90", "impedance"),
            (pick["E"].code, "D180", "impedance")}


def write_planted(pick: dict, raw: Path, written: dict) -> dict:
    out = raw / "planted"
    out.mkdir(parents=True)
    truth = {}

    a = pick["A"]
    original = written[(a.code, "D30", "impedance")]
    target = out / f"A_resend_{original.name}"
    shutil.copyfile(original, target)
    truth["A"] = {"file": target.name, "duplicate_of": original.name, "rows": len(a.sessions["D30"].impedance),
                  "expect": "skipped: same sha256 as a file already loaded, whatever its name"}

    b = pick["B"]
    meta, cols, rows = threshold_export(b, b.sessions["D30"])
    meta["exported_at"] = iso(b.sessions["D30"].threshold_exported + timedelta(days=2, hours=3))
    stored = threshold_at(b, "D30", 17)
    resent = round(min(stored * 1.25, P.MAX_CURRENT_UA - 1), 1)
    rows = [(e, f"{resent:.1f}", nr, ts) if e == 17 else (e, ua, nr, ts) for e, ua, nr, ts in rows]
    target = write_export(out / f"B_reexport_{export_name(meta)}", meta, cols, rows)
    truth["B"] = {"file": target.name, "subject": b.code, "visit": "D30", "electrode": 17,
                  "stored_ua": stored, "resent_ua": resent, "rows": len(rows),
                  "expect": "0 new, 1 conflict raised as a data query, stored value unchanged"}

    c, real = pick["C"], pick["C_real"]
    meta, cols, rows = threshold_export(real, real.sessions["D90"])
    meta["subject_code"] = c.code      # wrong patient picked in the fitting software; the serial comes from the device
    target = write_export(out / f"C_wrong_subject_{export_name(meta)}", meta, cols, rows)
    truth["C"] = {"file": target.name, "labelled_subject": c.code, "device_serial": real.serial,
                  "serial_belongs_to": real.code, "rows": len(rows),
                  "expect": "whole file quarantined, 0 rows loaded, data query raised"}

    d, n = pick["D"], pick["D_electrode"]
    meta, cols, rows = impedance_export(d, d.sessions["D90"])
    rows = [(e, f"-{v}", ts) if e == n else (e, v, ts) for e, v, ts in rows]
    target = write_export(out / f"D_negative_value_{export_name(meta)}", meta, cols, rows)
    truth["D"] = {"file": target.name, "subject": d.code, "visit": "D90", "electrode": n,
                  "value": float(f"-{d.sessions['D90'].impedance[n - 1][1]}"), "rows": len(rows),
                  "expect": "the bad row is not loaded and raises a data query (policy: ADR-005)"}

    e_subj = pick["E"]
    sess = e_subj.sessions["D180"]
    meta, _, _ = impedance_export(e_subj, sess)
    meta |= {"export_tool": "fitsuite 3.3.0", "unit": "ohm"}   # a software update switched the unit
    rows = [(e, f"{kohm * 1000:.0f}", iso(ts)) for e, kohm, ts in sess.impedance]
    target = write_export(out / f"E_unit_ohm_{export_name(meta)}", meta,
                          ["electrode_no", "impedance_ohm", "measured_at"], rows)
    truth["E"] = {"file": target.name, "subject": e_subj.code, "visit": "D180", "unit": "ohm",
                  "export_tool": "fitsuite 3.3.0", "rows": len(rows),
                  "expect": "whole file rejected by the parser, parser version recorded"}
    return truth


# ---------- configs, adverse events, setup and EDC files ----------

def configs(s: Subject, rng: random.Random):
    plan = ["D7", "D30"] + (["D90"] if rng.random() < P.CONFIG_V3_P else [])
    plan = [v for v in plan if v in s.sessions]
    starts = [s.sessions[v].threshold_exported + timedelta(minutes=30) for v in plan]
    who = s.site[0][-1].lower()
    heads, electrodes = [], []
    for version, (visit, start) in enumerate(zip(plan, starts), 1):
        end = starts[version] if version < len(starts) else None
        heads.append((s.code, s.serial, version, f"fitting_eng_{who}", f"clinician_{who}",
                      iso(start), iso(end) if end else ""))
        measured = {e: ua for e, ua, _, _ in s.sessions[visit].threshold}
        for e in ELECTRODES:
            ua = measured.get(e)
            hi = None if ua is None else round(min(ua * P.CONFIG_MAX_FACTOR, P.MAX_CURRENT_UA), 1)
            on = ua is not None and hi > ua
            electrodes.append((s.serial, version, e, "true" if on else "false",
                               f"{ua:.1f}" if on else "", f"{hi:.1f}" if on else ""))
    return heads, electrodes


def adverse_events(subjects: list[Subject], rng: random.Random):
    rows = []
    for s in sorted(rng.sample(subjects, P.ADVERSE_EVENTS), key=lambda s: s.code):
        onset = s.implanted_on + timedelta(days=rng.randint(0, 170))
        resolved = onset + timedelta(days=rng.randint(2, 30)) if rng.random() < 0.8 else None
        serious = rng.random() < 0.15
        rows.append((s.code, rng.choice(P.AE_TERMS), onset, resolved or "", "true" if serious else "false",
                     "severe" if serious else rng.choice(["mild", "mild", "moderate"]),
                     rng.choice(["not", "unlikely", "possible", "probable"]),
                     "recovered" if resolved else "ongoing"))
    return rows


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Write a deterministic synthetic trial into a raw/ folder.")
    ap.add_argument("--out", type=Path, default=Path("raw"))
    ap.add_argument("--seed", type=int, default=P.SEED)
    args = ap.parse_args(argv)
    raw: Path = args.out
    if raw.exists():
        if any(raw.iterdir()) and not (raw / "_truth" / "truth.json").exists():
            raise SystemExit(f"{raw} is not empty and was not made by this generator; refusing to overwrite it")
        shutil.rmtree(raw)

    rng = random.Random(args.seed)
    subjects = make_subjects(rng)
    for s in subjects:
        simulate(s, rng)
    pick = choose_planted(subjects, rng)
    skip = withheld(pick)

    write_csv(raw / "setup" / "study.csv", ["study_code", "protocol_version"], [(P.STUDY_CODE, P.PROTOCOL_VERSION)])
    write_csv(raw / "setup" / "sites.csv", ["site_code", "country", "timezone"], [site[:3] for site in P.SITES])
    write_csv(raw / "setup" / "visit_schedule.csv", ["visit_code", "target_day", "window_days"],
              [*P.VISITS, ("UNS", "", "")])
    write_csv(raw / "setup" / "device_models.csv", list(P.DEVICE_MODEL), [tuple(P.DEVICE_MODEL.values())])
    write_csv(raw / "setup" / "lots.csv", ["lot_code", "manufactured_on", "process_rev"], [lot[:3] for lot in P.LOTS])

    write_csv(raw / "edc" / "subjects.csv", ["subject_code", "site_code", "enrolled_on", "birth_year", "sex", "status"],
              [(s.code, s.site[0], s.enrolled_on, s.birth_year, s.sex, "withdrawn" if s.withdrawn else "completed")
               for s in subjects])
    write_csv(raw / "edc" / "implants.csv", ["subject_code", "model_code", "device_serial", "lot_code", "implanted_on"],
              [(s.code, P.DEVICE_MODEL["model_code"], s.serial, s.lot[0], s.implanted_on) for s in subjects])
    write_csv(raw / "edc" / "visits.csv", ["subject_code", "visit_code", "visit_seq", "visit_date"],
              [(s.code, v.visit_code, 1, v.visit_date) for s in subjects for v in s.sessions.values()])
    n_ae = write_csv(raw / "edc" / "adverse_events.csv",
                     ["subject_code", "term", "onset_date", "resolved_date", "serious", "severity",
                      "device_related", "outcome"], adverse_events(subjects, rng))

    written, n_imp, n_thr = {}, 0, 0
    for s in subjects:
        for sess in s.sessions.values():
            for measurement, build in (("impedance", impedance_export), ("threshold", threshold_export)):
                if (s.code, sess.visit_code, measurement) in skip:
                    continue
                meta, cols, rows = build(s, sess)
                path = raw / "fitting" / "measurements" / s.site[0] / export_name(meta)
                written[(s.code, sess.visit_code, measurement)] = write_export(path, meta, cols, rows)
                n_imp += len(rows) if measurement == "impedance" else 0
                n_thr += len(rows) if measurement == "threshold" else 0

    heads, electrodes = [], []
    for s in subjects:
        h, e = configs(s, rng)
        heads += h
        electrodes += e
    write_csv(raw / "fitting" / "stim_configs.csv",
              ["subject_code", "device_serial", "version", "created_by", "approved_by", "active_from", "active_to"], heads)
    write_csv(raw / "fitting" / "stim_config_electrodes.csv",
              ["device_serial", "version", "electrode_no", "enabled", "min_ua", "max_ua"], electrodes)

    planted = write_planted(pick, raw, written)

    lot_by_site = {site[0]: dict(sorted(Counter(s.lot[0] for s in subjects if s.site == site).items()))
                   for site in P.SITES}
    truth = {
        "note": "Ground truth for tests. Never loaded into the database.",
        "seed": args.seed,
        "lot_drift_log_per_180d": {lot[0]: lot[4] for lot in P.LOTS},
        "implant_slope_log_per_180d": {s.code: round(s.slope, 4) for s in subjects},
        "lot_by_subject": {s.code: s.lot[0] for s in subjects},
        "lot_by_site": lot_by_site,
        "withdrawn": sorted(s.code for s in subjects if s.withdrawn),
        "open_electrodes": {s.code: {str(e): P.VISITS[k][0] for e, k in sorted(s.open_from.items())}
                            for s in subjects if s.open_from},
        "withheld_normal_files": sorted("_".join(w) for w in skip),
        "planted": planted,
        "counts": {"subjects": len(subjects), "fitting_files": len(written), "impedance_rows": n_imp,
                   "threshold_rows": n_thr, "stim_configs": len(heads), "adverse_events": n_ae},
    }
    (raw / "_truth").mkdir()
    (raw / "_truth" / "truth.json").write_text(json.dumps(truth, indent=2, default=str) + "\n", encoding="utf-8")

    c = truth["counts"]
    print(f"{c['subjects']} subjects at {len(P.SITES)} sites; lots per site: {lot_by_site}")
    print(f"{c['fitting_files']} fitting files ({c['impedance_rows']:,} impedance rows, "
          f"{c['threshold_rows']:,} threshold rows), {len(planted)} planted files, "
          f"{c['stim_configs']} configs, {c['adverse_events']} adverse events -> {raw}/")


if __name__ == "__main__":
    main()
