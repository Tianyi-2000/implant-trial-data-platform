"""Loading fitting exports: idempotency, the five planted problems, reconciliation (ADR-005 to ADR-009)."""
import hashlib
import stat
import threading
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from itdp import ingest, parsers
from itdp.db import connect
from tests.support import counts, lock_visit, reexport, threshold


@pytest.mark.req("REQ-01")
def test_every_received_file_is_kept_read_only_under_its_hash(trial, db):
    conn = db("auditor_1")
    batches = conn.execute("SELECT sha256, source_uri, status FROM ops.ingest_batch").fetchall()
    assert "rejected" in {status for _, _, status in batches}       # E is kept too, though it was never loaded
    for sha, uri, _ in batches:
        path = Path(uri)
        assert path.name.startswith(sha)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha
        assert stat.S_IMODE(path.stat().st_mode) == 0o444


@pytest.mark.req("REQ-02")
def test_a_resent_file_is_skipped_whatever_its_name(trial):
    a = trial.planted["A"]
    assert a["status"] == "skipped"
    assert f"same bytes as {trial.truth['planted']['A']['duplicate_of']}" in a["status_reason"]


@pytest.mark.req("REQ-02")
def test_loading_everything_again_changes_nothing(trial, db):
    conn = db("loader_svc", actor="ingest-job")
    before = counts(conn)
    assert all(outcome.startswith("skipped") for _, outcome in ingest.load_master(conn, trial.raw, trial.keep))
    again = ingest.load_exports(conn, trial.raw / "fitting" / "measurements", trial.keep)
    again += ingest.load_exports(conn, trial.raw / "planted", trial.keep)
    assert again == Counter(skipped=trial.truth["counts"]["fitting_files"] + 5)
    assert counts(conn) == before


@pytest.mark.req("REQ-02")
def test_two_loaders_racing_on_the_same_new_file_load_it_once(trial, tmp_path):
    """The first loader holds its transaction open; the second must wait on the unique index, then skip."""
    src = trial.export(trial.truth["planted"]["B"]["subject"], "D30", "impedance")
    new = reexport(src, tmp_path, exported_at="2030-01-01T00:00:00+00:00")
    sha = hashlib.sha256(new.read_bytes()).hexdigest()
    with (connect("loader_svc", actor="ingest-job") as first,
          connect("loader_svc", actor="ingest-job", autocommit=True) as second,
          connect("itdp_admin", autocommit=True) as admin):
        first.execute("SELECT 1")
        first_result = ingest.load_export(first, new, trial.keep)          # loaded, not committed yet
        second_result = {}
        racer = threading.Thread(target=lambda: second_result.update(ingest.load_export(second, new, trial.keep)))
        racer.start()
        deadline = time.monotonic() + 10
        while admin.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                            (second.info.backend_pid,)).fetchone()[0] != "Lock":
            assert time.monotonic() < deadline, "the second loader never waited for the first"
            time.sleep(0.05)
        first.commit()
        racer.join(10)
        assert first_result["status"] == "loaded"
        assert second_result["status"] == "skipped"
        assert admin.execute("SELECT count(*) FROM ops.ingest_batch WHERE sha256 = %s", (sha,)).fetchone()[0] == 1


@pytest.mark.req("REQ-03")
def test_a_reexport_with_the_same_values_adds_nothing(trial, db, tmp_path):
    conn = db("loader_svc", actor="ingest-job")
    src = trial.export(trial.truth["planted"]["B"]["subject"], "D30", "impedance")
    b = ingest.load_export(conn, reexport(src, tmp_path, exported_at="2026-01-01T00:00:00+00:00"), trial.keep)
    assert (b["status"], b["rows_inserted"], b["rows_conflict"]) == ("loaded", 0, 0)
    assert b["rows_duplicate"] == b["row_count_expected"] > 0


@pytest.mark.req("REQ-04")
def test_a_changed_value_raises_a_query_and_the_stored_value_stays(trial, db):
    t, b = trial.truth["planted"]["B"], trial.planted["B"]
    assert (b["status"], b["rows_inserted"], b["rows_duplicate"], b["rows_conflict"]) == ("loaded", 0, t["rows"] - 1, 1)
    conn = db("data_manager_1")
    stored = threshold(conn, t["subject"], t["visit"], t["electrode"])
    assert stored.threshold_ua == Decimal(str(t["stored_ua"]))
    assert stored.ingest_batch_id != b["ingest_batch_id"]
    status, record_id, proposed = conn.execute(
        "SELECT status, record_id, proposed FROM ops.data_query WHERE query_type = 'conflict' AND ingest_batch_id = %s",
        (b["ingest_batch_id"],)).fetchone()
    assert (status, record_id) == ("open", stored.threshold_id)
    assert Decimal(str(proposed["threshold_ua"])) == Decimal(str(t["resent_ua"]))


@pytest.mark.req("REQ-05")
def test_a_file_for_the_wrong_subject_is_quarantined_whole(trial, db):
    t, c = trial.truth["planted"]["C"], trial.planted["C"]
    assert c["status"] == "quarantined"
    assert f"belongs to subject {t['serial_belongs_to']}, not {t['labelled_subject']}" in c["status_reason"]
    conn = db("auditor_1")
    assert conn.execute("""SELECT (SELECT count(*) FROM core.threshold_measurement WHERE ingest_batch_id = %(b)s)
                                + (SELECT count(*) FROM core.impedance_measurement WHERE ingest_batch_id = %(b)s)""",
                        {"b": c["ingest_batch_id"]}).fetchone()[0] == 0
    assert conn.execute("SELECT query_type FROM ops.data_query WHERE ingest_batch_id = %s",
                        (c["ingest_batch_id"],)).fetchall() == [("quarantined_file",)]


@pytest.mark.req("REQ-06")
def test_an_implausible_value_is_held_back_and_the_rest_loads(trial, db):
    t, d = trial.truth["planted"]["D"], trial.planted["D"]
    assert (d["status"], d["rows_inserted"], d["rows_rejected"]) == ("loaded", t["rows"] - 1, 1)
    sent = next(line for line in (trial.raw / "planted" / t["file"]).read_text().splitlines()
                if line.startswith(f"{t['electrode']},-"))
    conn = db("auditor_1")
    assert conn.execute("SELECT raw_line FROM ops.rejected_row WHERE ingest_batch_id = %s",
                        (d["ingest_batch_id"],)).fetchall() == [(sent,)]
    assert conn.execute("SELECT query_type FROM ops.data_query WHERE ingest_batch_id = %s",
                        (d["ingest_batch_id"],)).fetchall() == [("rejected_row",)]


@pytest.mark.req("REQ-07")
def test_an_unreadable_file_is_rejected_with_the_parser_version(trial, db):
    e = trial.planted["E"]
    assert (e["status"], e["rows_inserted"], e["parser_version"]) == ("rejected", 0, parsers.PARSER_VERSION)
    assert "unit is ohm" in e["status_reason"]
    conn = db("auditor_1")
    assert conn.execute("SELECT query_type FROM ops.data_query WHERE ingest_batch_id = %s",
                        (e["ingest_batch_id"],)).fetchall() == [("rejected_file",)]


@pytest.mark.req("REQ-07")
def test_only_a_newer_parser_may_try_a_rejected_file_again(trial, db):
    conn = db("loader_svc", actor="ingest-job")

    def receive(batch: dict, parser_version: str):
        return conn.execute("SELECT ops.receive_file('fitting_export', 'retry.csv', 'retry', %s, %s, NULL)",
                            (batch["sha256"], parser_version)).fetchone()[0]

    rejected, loaded = trial.planted["E"], trial.planted["B"]
    assert receive(rejected, parsers.PARSER_VERSION) is None              # same parser: same answer, so no retry
    assert receive(rejected, "fitting-export-v1 parser 1.1.0") is not None
    assert receive(loaded, "fitting-export-v1 parser 1.1.0") is None      # a loaded file is never loaded again


@pytest.mark.req("REQ-08")
def test_a_row_lost_on_the_way_in_fails_the_whole_file(trial, db, tmp_path, monkeypatch):
    """Simulates a loader bug: the header says N rows, the staging table gets N - 1."""
    real_parse = parsers.parse

    def drop_last_row(data: bytes) -> parsers.Export:
        export = real_parse(data)
        return parsers.Export(export.header, export.rows[:-1])

    monkeypatch.setattr(parsers, "parse", drop_last_row)
    conn = db("loader_svc", actor="ingest-job")
    src = trial.export(trial.truth["planted"]["B"]["subject"], "D30", "impedance")
    before = counts(conn)
    with pytest.raises(psycopg.errors.CheckViolation, match="reconciles"):
        ingest.load_export(conn, reexport(src, tmp_path, exported_at="2026-02-01T00:00:00+00:00"), trial.keep)
    assert counts(conn) == before


@pytest.mark.req("REQ-13")
def test_a_load_with_new_rows_for_a_locked_visit_is_quarantined(trial, db, tmp_path):
    """One superuser transaction plays the data manager who locks the visit, then the loader."""
    t = trial.truth["planted"]["B"]
    conn = db("itdp_admin", actor="test-suite")
    lock_visit(conn, threshold(conn, t["subject"], t["visit"], t["electrode"]).visit_id)
    src = trial.export(t["subject"], t["visit"], "impedance")
    new_rows = reexport(src, tmp_path, test_freq_hz="2000")              # another test frequency: every row is new
    b = ingest.load_export(conn, new_rows, trial.keep)
    assert b["status"] == "quarantined"
    assert f"visit {t['visit']} of subject {t['subject']} is locked" in b["status_reason"]
