"""Helpers shared by the tests."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import namedtuple_row

from itdp import ingest

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = "itdp_test"


@dataclass
class Trial:
    raw: Path                 # the generated raw/ folder
    keep: ingest.Landing      # the landing zone the session loaded into
    truth: dict               # raw/_truth/truth.json: what the generator planted
    planted: dict[str, dict]  # A to E: what the first load of each planted file returned

    def export(self, subject: str, visit: str, kind: str) -> Path:
        return next((self.raw / "fitting" / "measurements").rglob(f"{subject}_{visit}_{kind}_*.csv"))


def refused(conn: psycopg.Connection, query: str, params=None) -> str:
    """Run a statement the database must refuse. Rolls back to before it and returns the error message."""
    with pytest.raises(psycopg.Error) as e, conn.transaction():
        conn.execute(query, params)
    return str(e.value)


def give_reason(conn: psycopg.Connection, reason: str) -> None:
    conn.execute("SELECT set_config('app.change_reason', %s, true)", (reason,))


@contextmanager
def login_as(admin: psycopg.Connection, user: str):
    """Inside the admin's transaction, act as another login (session_user and its privileges).
    The admin can then read what that login wrote, such as audit rows, before anything is committed."""
    admin.execute(f"SET SESSION AUTHORIZATION {user}")
    try:
        yield
    finally:
        admin.execute("RESET SESSION AUTHORIZATION")


def reexport(src: Path, out_dir: Path, **header: str) -> Path:
    """A copy of an export with some header values changed: new bytes, so a new hash, and the same rows."""
    lines = src.read_text().splitlines(keepends=True)
    for i, line in enumerate(lines):
        key = line[1:].strip().partition("=")[0] if line.startswith("#") else None
        if key in header:
            lines[i] = f"# {key}={header[key]}\n"
    out = out_dir / src.name
    out.write_text("".join(lines))
    return out


def counts(conn: psycopg.Connection) -> tuple:
    return conn.execute("""SELECT (SELECT count(*) FROM core.impedance_measurement),
                                  (SELECT count(*) FROM core.threshold_measurement),
                                  (SELECT count(*) FROM ops.ingest_batch),
                                  (SELECT count(*) FROM ops.data_query),
                                  (SELECT count(*) FROM ops.rejected_row)""").fetchone()


def threshold(conn: psycopg.Connection, subject: str, visit: str, electrode: int):
    """The stored threshold row for one electrode at one visit: threshold_id, threshold_ua, ingest_batch_id, visit_id."""
    with conn.cursor(row_factory=namedtuple_row) as cur:
        return cur.execute("""
            SELECT t.threshold_id, t.threshold_ua, t.ingest_batch_id, t.visit_id
              FROM core.threshold_measurement t
              JOIN core.visit v USING (visit_id) JOIN core.subject s USING (subject_id)
              JOIN core.electrode e USING (electrode_id)
             WHERE s.subject_code = %s AND v.visit_code = %s AND e.electrode_no = %s""",
            (subject, visit, electrode)).fetchone()


def query_id(conn: psycopg.Connection, query_type: str) -> int:
    return conn.execute("SELECT min(query_id) FROM ops.data_query WHERE query_type = %s AND status = 'open'",
                        (query_type,)).fetchone()[0]


def lock_visit(conn: psycopg.Connection, visit_id: int) -> None:
    give_reason(conn, "Source data verified against the site record")
    conn.execute("UPDATE core.visit SET status = 'verified' WHERE visit_id = %s", (visit_id,))
    give_reason(conn, "Locked for the interim analysis")
    conn.execute("UPDATE core.visit SET status = 'locked' WHERE visit_id = %s", (visit_id,))
