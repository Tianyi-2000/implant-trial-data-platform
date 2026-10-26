"""Closing data queries and locked visits (ADR-008, ADR-009)."""
from decimal import Decimal

import pytest

from tests.support import give_reason, lock_visit, login_as, query_id, refused, threshold

RESOLVE = "SELECT status, decision, resolved_by FROM ops.resolve_query(%s, %s, %s)"


@pytest.fixture
def b(trial):
    """Planted file B: the stored threshold of one electrode, and the re-export that disagrees with it."""
    return trial.truth["planted"]["B"] | {"batch": trial.planted["B"]["ingest_batch_id"]}


@pytest.mark.req("REQ-12")
def test_accepting_a_conflict_changes_the_row_and_closes_the_query_in_one_transaction(trial, db, b):
    admin = db("itdp_admin", actor="test-suite")
    q = query_id(admin, "conflict")
    with login_as(admin, "data_manager_1"):
        closed = admin.execute(RESOLVE, (q, "accept_resent", "Site confirmed the re-export is correct")).fetchone()
    assert closed == ("closed", "accept_resent", "data_manager_1")
    row = threshold(admin, b["subject"], b["visit"], b["electrode"])
    assert (row.threshold_ua, row.ingest_batch_id) == (Decimal(str(b["resent_ua"])), b["batch"])
    audit = admin.execute("SELECT table_name, db_user, xact_id FROM audit.audit_log WHERE reason = %s ORDER BY audit_id",
                          (f"DQ-{q} accept_resent: Site confirmed the re-export is correct",)).fetchall()
    assert [(t, u) for t, u, _ in audit] == [("core.threshold_measurement", "data_manager_1"),
                                             ("ops.data_query", "data_manager_1")]
    assert len({x for _, _, x in audit}) == 1     # one transaction: the change and the closed query share it


@pytest.mark.req("REQ-12")
def test_keeping_the_stored_value_changes_no_data(trial, db, b):
    conn = db("data_manager_1")
    before = threshold(conn, b["subject"], b["visit"], b["electrode"])
    closed = conn.execute(RESOLVE, (query_id(conn, "conflict"), "keep_stored", "Site confirmed the first export")).fetchone()
    assert closed == ("closed", "keep_stored", "data_manager_1")
    assert threshold(conn, b["subject"], b["visit"], b["electrode"]) == before


@pytest.mark.req("REQ-12")
@pytest.mark.parametrize("login", ["loader_svc", "dbt_svc", "analyst_1", "auditor_1"])
def test_only_a_data_manager_can_close_a_query(trial, db, login):
    conn = db(login, actor="test-suite")
    q = db("data_manager_1").execute("SELECT min(query_id) FROM ops.data_query").fetchone()[0]
    assert "permission denied" in refused(conn, RESOLVE, (q, "keep_stored", "not mine to close"))


@pytest.mark.req("REQ-12")
def test_a_query_is_closed_only_through_resolve_query(trial, db):
    conn = db("data_manager_1")
    assert "permission denied" in refused(conn, "UPDATE ops.data_query SET status = 'closed'")


@pytest.mark.req("REQ-12")
def test_closing_a_query_needs_a_reason_and_happens_once(trial, db):
    conn = db("data_manager_1")
    q = query_id(conn, "rejected_row")
    assert "needs a reason" in refused(conn, RESOLVE, (q, "keep_stored", "   "))
    conn.execute(RESOLVE, (q, "keep_stored", "Instrument fault confirmed by the site"))
    assert "already closed by data_manager_1" in refused(conn, RESOLVE, (q, "keep_stored", "again"))


@pytest.mark.req("REQ-12")
def test_only_a_conflict_has_resent_values_to_accept(trial, db):
    conn = db("data_manager_1")
    assert "only a conflict has resent values to accept" in refused(
        conn, RESOLVE, (query_id(conn, "rejected_row"), "accept_resent", "load it anyway"))


@pytest.mark.req("REQ-12")
def test_accepting_is_refused_if_the_stored_row_changed_since_the_query(trial, db, b):
    conn = db("data_manager_1")
    row = threshold(conn, b["subject"], b["visit"], b["electrode"])
    give_reason(conn, "Correction from the site source record")
    conn.execute("UPDATE core.threshold_measurement SET threshold_ua = threshold_ua - 1 WHERE threshold_id = %s",
                 (row.threshold_id,))
    assert "changed after the query was raised" in refused(
        conn, RESOLVE, (query_id(conn, "conflict"), "accept_resent", "Site confirmed the re-export"))


@pytest.mark.req("REQ-13")
def test_a_locked_visit_refuses_every_change_even_with_a_reason(trial, db, b):
    conn = db("data_manager_1")
    row = threshold(conn, b["subject"], b["visit"], b["electrode"])
    lock_visit(conn, row.visit_id)
    give_reason(conn, "Late correction")
    assert "is locked" in refused(conn, "UPDATE core.threshold_measurement SET threshold_ua = 80 WHERE threshold_id = %s",
                                  (row.threshold_id,))
    assert "is locked" in refused(conn, "UPDATE core.visit SET visit_date = visit_date - 1 WHERE visit_id = %s",
                                  (row.visit_id,))
    assert "is locked" in refused(conn, RESOLVE, (query_id(conn, "conflict"), "accept_resent", "Site confirmed"))


@pytest.mark.req("REQ-13")
def test_only_a_verified_visit_can_be_locked(trial, db, b):
    conn = db("data_manager_1")
    visit_id = threshold(conn, b["subject"], b["visit"], b["electrode"]).visit_id
    give_reason(conn, "Locked for the interim analysis")
    assert "verify it before locking it" in refused(conn, "UPDATE core.visit SET status = 'locked' WHERE visit_id = %s",
                                                    (visit_id,))


@pytest.mark.req("REQ-13")
def test_unlocking_with_a_reason_allows_a_correction(trial, db, b):
    conn = db("data_manager_1")
    row = threshold(conn, b["subject"], b["visit"], b["electrode"])
    lock_visit(conn, row.visit_id)
    give_reason(conn, "")
    assert "A reason for change is required" in refused(
        conn, "UPDATE core.visit SET status = 'verified' WHERE visit_id = %s", (row.visit_id,))
    give_reason(conn, "Unlocked: the site reported a wrong visit date")
    conn.execute("UPDATE core.visit SET status = 'verified' WHERE visit_id = %s", (row.visit_id,))
    give_reason(conn, "Visit date corrected to the site source record")
    conn.execute("UPDATE core.visit SET visit_date = visit_date - 1 WHERE visit_id = %s", (row.visit_id,))
    lock_visit(conn, row.visit_id)


@pytest.mark.req("REQ-14")
def test_an_implant_has_one_active_config_at_a_time(trial, db):
    conn = db("loader_svc", actor="ingest-job")
    implant_id, version = conn.execute("""SELECT implant_id, max(version) FROM core.stim_config
                                          WHERE active_to IS NULL GROUP BY implant_id ORDER BY implant_id LIMIT 1""").fetchone()
    assert "exclusion constraint" in refused(conn, """
        INSERT INTO core.stim_config (implant_id, version, created_by, active_from) VALUES (%s, %s, 'fitter', now())""",
        (implant_id, version + 1))
