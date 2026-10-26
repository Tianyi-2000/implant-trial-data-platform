"""Load study setup, EDC exports and stimulation configs (a deliberately simplified loader).

Each file is fingerprinted, kept in landing/ and recorded in ops.ingest_batch like a fitting export,
so a resent file is skipped. Unlike fitting exports, these rows are insert-only: a key that is already
stored makes the load fail loudly instead of raising a data query, and a row whose reference doesn't
match (an unknown site, say) drops out of the join, so the reconciliation CHECK refuses the batch.
Changes to this data would come through the EDC's own audited corrections, which are out of scope here.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import psycopg

PARSER_VERSION = "master-csv parser 1.0.0"

# (source system, file under raw/, INSERT ... SELECT from the temp table stg, whose columns are all text)
FILES = [
    ("setup", "setup/study.csv", """
        INSERT INTO core.study (study_code, protocol_version) SELECT study_code, protocol_version FROM stg"""),
    ("setup", "setup/sites.csv", """
        INSERT INTO core.site (site_code, country, timezone) SELECT site_code, country, timezone FROM stg"""),
    ("setup", "setup/visit_schedule.csv", """
        INSERT INTO core.visit_schedule (study_id, visit_code, target_day, window_days)
        SELECT st.study_id, visit_code, nullif(target_day, '')::int, nullif(window_days, '')::int
          FROM stg CROSS JOIN core.study st WHERE st.study_code = %(study)s"""),
    ("setup", "setup/device_models.csv", """
        INSERT INTO ref.device_model (model_code, electrode_count, grid_cols, pitch_um)
        SELECT model_code, electrode_count::int, grid_cols::int, pitch_um::numeric FROM stg"""),
    ("setup", "setup/lots.csv", """
        INSERT INTO core.manufacturing_lot (lot_code, manufactured_on, process_rev)
        SELECT lot_code, manufactured_on::date, process_rev FROM stg"""),
    ("edc", "edc/subjects.csv", """
        INSERT INTO core.subject (subject_code, study_id, site_id, enrolled_on, birth_year, sex, status)
        SELECT s.subject_code, st.study_id, si.site_id, s.enrolled_on::date, s.birth_year::smallint, s.sex, s.status
          FROM stg s JOIN core.site si USING (site_code) CROSS JOIN core.study st WHERE st.study_code = %(study)s"""),
    ("edc", "edc/implants.csv", """
        INSERT INTO core.implant (subject_id, device_model_id, device_serial, lot_id, implanted_on)
        SELECT su.subject_id, m.device_model_id, s.device_serial, l.lot_id, s.implanted_on::date
          FROM stg s JOIN core.subject su USING (subject_code) JOIN ref.device_model m USING (model_code)
          JOIN core.manufacturing_lot l USING (lot_code)"""),
    ("edc", "edc/visits.csv", """
        INSERT INTO core.visit (subject_id, study_id, visit_code, visit_seq, visit_date)
        SELECT su.subject_id, su.study_id, s.visit_code, s.visit_seq::smallint, s.visit_date::date
          FROM stg s JOIN core.subject su USING (subject_code)"""),
    ("edc", "edc/adverse_events.csv", """
        INSERT INTO core.adverse_event (subject_id, term, onset_date, resolved_date, serious, severity, device_related, outcome)
        SELECT su.subject_id, s.term, s.onset_date::date, nullif(s.resolved_date, '')::date, s.serious::boolean,
               s.severity, s.device_related, nullif(s.outcome, '')
          FROM stg s JOIN core.subject su USING (subject_code)"""),
    ("fitting_export", "fitting/stim_configs.csv", """
        INSERT INTO core.stim_config (implant_id, version, created_by, approved_by, active_from, active_to)
        SELECT i.implant_id, s.version::int, s.created_by, nullif(s.approved_by, ''), s.active_from::timestamptz,
               nullif(s.active_to, '')::timestamptz
          FROM stg s JOIN core.implant i USING (device_serial)
          JOIN core.subject su ON su.subject_id = i.subject_id AND su.subject_code = s.subject_code"""),
    ("fitting_export", "fitting/stim_config_electrodes.csv", """
        INSERT INTO core.stim_config_electrode (config_id, electrode_id, enabled, min_ua, max_ua)
        SELECT c.config_id, e.electrode_id, s.enabled::boolean, nullif(s.min_ua, '')::numeric, nullif(s.max_ua, '')::numeric
          FROM stg s JOIN core.implant i USING (device_serial)
          JOIN core.stim_config c ON c.implant_id = i.implant_id AND c.version = s.version::int
          JOIN core.electrode e ON e.implant_id = i.implant_id AND e.electrode_no = s.electrode_no::int"""),
]

# Every implant gets one row per electrode, laid out on its model's grid.
ELECTRODES = """
    INSERT INTO core.electrode (implant_id, electrode_no, x_um, y_um)
    SELECT i.implant_id, n, ((n - 1) % m.grid_cols) * m.pitch_um, ((n - 1) / m.grid_cols) * m.pitch_um
      FROM core.implant i JOIN ref.device_model m USING (device_model_id)
     CROSS JOIN generate_series(1, m.electrode_count) AS n
     WHERE NOT EXISTS (SELECT FROM core.electrode e WHERE e.implant_id = i.implant_id)"""


def load_master(conn: psycopg.Connection, raw: Path, keep) -> list[tuple[str, str]]:
    """Load every master file under raw/, in dependency order. Returns (file, outcome) pairs."""
    study = next(csv.DictReader(io.StringIO((raw / "setup/study.csv").read_text())))["study_code"]
    results = []
    for system, name, insert in FILES:
        path = raw / name
        data = path.read_bytes()
        sha, uri = keep(system, data)
        header, *rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT ops.receive_file(%s, %s, %s, %s, %s, %s)",
                        (system, path.name, uri, sha, PARSER_VERSION, len(rows)))
            batch = cur.fetchone()[0]
            if batch is None:
                results.append((name, "skipped: already received"))
                continue
            cur.execute("DROP TABLE IF EXISTS pg_temp.stg")
            cur.execute(f"CREATE TEMP TABLE stg ({', '.join(f'{c} text' for c in header)}) ON COMMIT DROP")
            with cur.copy(f"COPY stg ({', '.join(header)}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
            cur.execute(insert, {"study": study})
            inserted = cur.rowcount
            if name == "edc/implants.csv":
                cur.execute(ELECTRODES)
            cur.execute("UPDATE ops.ingest_batch SET status = 'loaded', rows_inserted = %s WHERE ingest_batch_id = %s",
                        (inserted, batch))
            results.append((name, f"loaded: {inserted} rows"))
    return results
