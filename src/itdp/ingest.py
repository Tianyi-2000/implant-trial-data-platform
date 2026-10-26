"""Load raw files into the database (ADR-005 to ADR-009).

    python -m itdp.ingest master raw                        # study setup, EDC exports, stimulation configs
    python -m itdp.ingest exports raw/fitting/measurements  # every fitting export under a folder
    python -m itdp.ingest exports raw/planted

Each fitting export is loaded in its own transaction:
  1. fingerprint the bytes (SHA-256) and keep a read-only copy in landing/, named by the hash
  2. parse it (parsers.py). A file the parser cannot read is rejected as a whole, with the parser version
  3. record the receipt with ops.receive_file. If this file was seen before, stop: nothing changes
  4. COPY the rows into a temp table that only this transaction can see (ADR-009)
  5. call ops.load_measurements, which checks the facts against the database and writes (db/40_load.sql)
A crash anywhere rolls the whole file back, so running the load again is always safe.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from collections import Counter
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from itdp import parsers
from itdp.db import connect
from itdp.master import load_master

ACTOR = os.environ.get("ITDP_ACTOR", "ingest-job")   # what the loader's service login acts for (ADR-002)


class Landing:
    """A write-once landing zone. In production: object storage with object lock (ADR-001)."""

    def __init__(self, root: Path):
        self.root = root

    def __call__(self, system: str, data: bytes) -> tuple[str, str]:
        sha = hashlib.sha256(data).hexdigest()
        path = self.root / system / f"{sha}.csv"
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise RuntimeError(f"{path} no longer matches its name: the landing zone was tampered with")
        else:
            # A temp name of its own, then an atomic rename: two loaders keeping the same file can't collide.
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".part")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o444)
            os.replace(tmp, path)
        return sha, str(path)


def load_export(conn: psycopg.Connection, path: Path, keep: Landing) -> dict:
    """Load one fitting export. Returns its ops.ingest_batch row, or {'status': 'skipped', ...}."""
    data = path.read_bytes()
    sha, uri = keep("fitting_export", data)
    try:
        export, error = parsers.parse(data), None
    except parsers.ParseError as e:
        export, error = None, str(e)
    row_count = int(export.header["row_count"]) if export else None

    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT ops.receive_file(%s, %s, %s, %s, %s, %s) AS batch",
                    ("fitting_export", path.name, uri, sha, parsers.PARSER_VERSION, row_count))
        batch = cur.fetchone()["batch"]
        if batch is None:
            cur.execute("SELECT ingest_batch_id, source_name, status FROM ops.ingest_batch "
                        "WHERE source_system = 'fitting_export' AND sha256 = %s ORDER BY ingest_batch_id DESC LIMIT 1",
                        (sha,))
            seen = cur.fetchone()
            return {"status": "skipped", "status_reason":
                    f"same bytes as {seen['source_name']} (batch {seen['ingest_batch_id']}, {seen['status']})"}
        if error:
            cur.execute("SELECT * FROM ops.hold_file(%s, 'rejected', %s)", (batch, error))
            return cur.fetchone()

        cur.execute("DROP TABLE IF EXISTS pg_temp.stg_row")
        cur.execute("""CREATE TEMP TABLE stg_row (line_no int, electrode_no int, value numeric, no_response boolean,
                       measured_at timestamptz, raw_line text) ON COMMIT DROP""")
        with cur.copy("COPY stg_row (line_no, electrode_no, value, no_response, measured_at, raw_line) FROM STDIN") as copy:
            for r in export.rows:
                copy.write_row((r.line_no, r.electrode_no, r.value, r.no_response, r.measured_at, r.raw_line))
        cur.execute("SELECT * FROM ops.load_measurements(%s, %s)", (batch, Jsonb(export.header)))
        return cur.fetchone()


def describe(b: dict) -> str:
    if b["status"] == "loaded":
        return (f"{b['rows_inserted']} new, {b['rows_duplicate']} duplicate, "
                f"{b['rows_conflict']} conflict, {b['rows_rejected']} rejected")
    return b["status_reason"]


def load_exports(conn: psycopg.Connection, folder: Path, keep: Landing) -> Counter:
    files = sorted(folder.rglob("*.csv")) if folder.is_dir() else [folder]
    outcomes, rows = Counter(), Counter()
    for path in files:
        b = load_export(conn, path, keep)
        outcomes[b["status"]] += 1
        if b["status"] == "loaded":
            rows.update({k: b[f"rows_{k}"] for k in ("inserted", "duplicate", "conflict", "rejected")})
        routine = b["status"] == "skipped" or b["status"] == "loaded" and b["rows_inserted"] == b["row_count_expected"]
        if not routine or len(files) <= 20:
            print(f"  {path.name:58} {b['status']:12} {describe(b)}")
    print(f"{len(files)} files: " + ", ".join(f"{n} {s}" for s, n in sorted(outcomes.items())))
    print("rows: " + ", ".join(f"{rows[k]:,} {k}" for k in ("inserted", "duplicate", "conflict", "rejected")))
    return outcomes


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Load raw files into the trial database.")
    ap.add_argument("what", choices=["master", "exports"])
    ap.add_argument("path", type=Path)
    ap.add_argument("--landing", type=Path, default=Path("landing"))
    args = ap.parse_args(argv)
    keep = Landing(args.landing)
    with connect("loader_svc", actor=ACTOR, autocommit=True) as conn:
        if args.what == "master":
            for name, outcome in load_master(conn, args.path, keep):
                print(f"  {name:36} {outcome}")
        else:
            load_exports(conn, args.path, keep)


if __name__ == "__main__":
    main()
