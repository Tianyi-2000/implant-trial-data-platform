"""Test fixtures. Needs the Compose database running (make up).

The tests never touch the demo database. Each run builds a throwaway database, itdp_test, from the same
SQL files, generates the synthetic trial into a temp folder, and loads it once, planted files included.
Each test then works inside transactions that are rolled back, so tests don't see each other's changes.
"""
from __future__ import annotations

import json
import os
import re

import pytest

from itdp import generate, ingest
from itdp.db import connect
from tests.support import ROOT, TEST_DB, Trial

os.environ["ITDP_DB"] = TEST_DB   # connect() reads it on every call: nothing here can reach the demo database

SCHEMA_SQL = ["05_database.sql", "10_audit.sql", "20_schema.sql", "30_grants.sql", "40_load.sql", "50_corrections.sql"]
REQUIREMENTS = set(re.findall(r"^- id: (REQ-\d+)", (ROOT / "docs" / "requirements.yaml").read_text(), re.M))


def pytest_collection_modifyitems(config, items):
    """Traceability: every test names a requirement, and every requirement it names exists."""
    problems = []
    for item in items:
        ids = [m.args[0] for m in item.iter_markers("req")]
        if not ids:
            problems.append(f"{item.nodeid} names no requirement")
        problems += [f"{item.nodeid} names {i}, which is not in docs/requirements.yaml" for i in ids if i not in REQUIREMENTS]
    if problems:
        raise pytest.UsageError("\n".join(problems))


@pytest.fixture(scope="session")
def trial(tmp_path_factory) -> Trial:
    with connect("itdp_admin", dbname="postgres", autocommit=True) as admin:
        if not admin.execute("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = 'loader_svc')").fetchone()[0]:
            admin.execute((ROOT / "db" / "00_roles.sql").read_text())
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    with connect("itdp_admin", autocommit=True) as admin:
        for name in SCHEMA_SQL:
            admin.execute((ROOT / "db" / name).read_text())

    raw = tmp_path_factory.mktemp("trial") / "raw"
    generate.main(["--out", str(raw)])
    truth = json.loads((raw / "_truth" / "truth.json").read_text())
    keep = ingest.Landing(tmp_path_factory.mktemp("landing"))
    with connect("loader_svc", actor="ingest-job", autocommit=True) as conn:
        ingest.load_master(conn, raw, keep)
        ingest.load_exports(conn, raw / "fitting" / "measurements", keep)
        planted = {k: ingest.load_export(conn, raw / "planted" / p["file"], keep) for k, p in truth["planted"].items()}
    return Trial(raw, keep, truth, planted)


@pytest.fixture
def db(trial):
    """db(login, actor=None) opens a connection as that login. Everything it does is rolled back at the end."""
    opened = []

    def open_(user: str, actor: str | None = None):
        conn = connect(user, actor=actor)
        conn.execute("SELECT 1")   # start the transaction now, so ingest's own transaction blocks become savepoints
        opened.append(conn)
        return conn

    yield open_
    for conn in opened:
        conn.rollback()
        conn.close()
