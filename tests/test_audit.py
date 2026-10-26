"""The audit trail: who, when, what and why, enforced by the database (ADR-002, ADR-003)."""
import pytest

from tests.support import give_reason, login_as, refused

FIRST_VISIT = "(SELECT min(visit_id) FROM core.visit)"


@pytest.mark.req("REQ-09")
def test_an_update_without_a_reason_is_refused(trial, db):
    conn = db("data_manager_1")
    assert "A reason for change is required" in refused(
        conn, f"UPDATE core.visit SET visit_date = visit_date - 1 WHERE visit_id = {FIRST_VISIT}")


@pytest.mark.req("REQ-09")
def test_an_update_records_who_when_why_and_the_old_and_new_values(trial, db):
    admin = db("itdp_admin", actor="test-suite")
    with login_as(admin, "data_manager_1"):
        give_reason(admin, "Visit date corrected to the site source record")
        visit_id, old, new = admin.execute(
            f"UPDATE core.visit SET visit_date = visit_date - 1 WHERE visit_id = {FIRST_VISIT} "
            "RETURNING visit_id, (visit_date + 1)::text, visit_date::text").fetchone()
    row = admin.execute("""
        SELECT action, db_user, reason, old_row->>'visit_date', new_row->>'visit_date', changed_at = now()
          FROM audit.audit_log
         WHERE table_name = 'core.visit' AND record_key = jsonb_build_object('visit_id', %s)
         ORDER BY audit_id DESC LIMIT 1""", (visit_id,)).fetchone()
    assert row == ("UPDATE", "data_manager_1", "Visit date corrected to the site source record", old, new, True)


@pytest.mark.req("REQ-09")
def test_every_loaded_row_has_an_audit_row(trial, db):
    admin = db("itdp_admin", actor="test-suite")
    stored, audited = admin.execute("""
        SELECT (SELECT count(*) FROM core.threshold_measurement),
               (SELECT count(*) FROM audit.audit_log
                 WHERE table_name = 'core.threshold_measurement' AND action = 'INSERT'
                   AND db_user = 'loader_svc' AND app_user = 'ingest-job')""").fetchone()
    assert audited == stored > 0


@pytest.mark.req("REQ-10")
def test_a_service_account_must_say_who_it_acts_for(trial, db):
    conn = db("loader_svc")    # logged in, but no app.user_id
    assert "is a service account" in refused(
        conn, "INSERT INTO ops.data_query (query_type, table_name, issue) VALUES ('rejected_file', 'ops.ingest_batch', 'x')")


@pytest.mark.req("REQ-11")
@pytest.mark.parametrize("login", ["loader_svc", "dbt_svc", "data_manager_1", "analyst_1", "auditor_1"])
def test_no_application_role_can_write_an_audit_row(trial, db, login):
    conn = db(login, actor="test-suite")
    assert "permission denied" in refused(conn, """
        INSERT INTO audit.audit_log (table_name, record_key, action, db_user, xact_id)
        VALUES ('core.visit', '{"visit_id": 1}', 'UPDATE', 'someone_else', 1)""")


@pytest.mark.req("REQ-11")
@pytest.mark.parametrize("statement", ["UPDATE audit.audit_log SET reason = 'edited'",
                                       "DELETE FROM audit.audit_log",
                                       "TRUNCATE audit.audit_log"])
def test_the_audit_trail_is_append_only_even_for_a_superuser(trial, db, statement):
    assert "append-only" in refused(db("itdp_admin", actor="test-suite"), statement)


@pytest.mark.req("REQ-11")
def test_truncate_is_refused_on_audited_tables(trial, db):
    assert "bypass the audit trail" in refused(db("itdp_admin", actor="test-suite"), "TRUNCATE core.threshold_measurement")
    assert "permission denied" in refused(db("loader_svc", actor="test-suite"), "DELETE FROM core.threshold_measurement")
