"""Parser for fitting-software exports, format version 1 (ADR-001, ADR-006, ADR-007).

The parser checks only what the file itself can tell: the header, the format version, the unit and
column names, every value's shape, and the row count against the header's manifest. Anything that
needs the database (whose device it is, whether a value is plausible, whether it is already stored)
is checked in SQL, in the same transaction as the write: db/40_load.sql.

A file this parser does not fully understand is rejected as a whole and never "fixed" on the way in.
A changed unit means the export changed: that needs a new, tested parser version, not a conversion.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

PARSER_VERSION = "fitting-export-v1 parser 1.0.0"

REQUIRED = ("format", "format_version", "export_tool", "exported_at", "site_code", "subject_code",
            "device_serial", "visit_code", "measurement", "unit", "row_count")

# measurement -> what format version 1 promises for it
SPEC = {
    "impedance": {"unit": "kohm", "columns": ["electrode_no", "impedance_kohm", "measured_at"],
                  "decimals": 2, "keys": ("test_freq_hz",)},
    "threshold": {"unit": "uA", "columns": ["electrode_no", "threshold_ua", "no_response", "measured_at"],
                  "decimals": 1, "keys": ("pulse_width_us", "method")},
}


class ParseError(Exception):
    """The file is not a format version 1 export this parser can read. The whole file is rejected."""


@dataclass(frozen=True)
class Row:
    line_no: int                 # 1 = first row after the column names
    electrode_no: int
    value: Decimal | None        # kOhm or uA; None only for a threshold with no response
    no_response: bool | None     # threshold only
    measured_at: datetime
    raw_line: str


@dataclass(frozen=True)
class Export:
    header: dict[str, str]
    rows: list[Row]


def parse(data: bytes) -> Export:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as e:
        raise ParseError(f"not UTF-8 text: {e}") from None

    header, i = {}, 0
    while i < len(lines) and lines[i].startswith("#"):
        key, sep, value = lines[i][1:].strip().partition("=")
        if not sep or not key:
            raise ParseError(f"header line {i + 1} is not key=value")
        if key in header:
            raise ParseError(f"header key {key} appears twice")
        header[key] = value
        i += 1

    missing = [k for k in REQUIRED if k not in header]
    if missing:
        raise ParseError(f"header is missing {', '.join(missing)}")
    if (header["format"], header["format_version"]) != ("fitting-export", "1"):
        raise ParseError(f"format {header['format']} version {header['format_version']} is not supported")
    spec = SPEC.get(header["measurement"])
    if spec is None:
        raise ParseError(f"unknown measurement {header['measurement']}")
    if header["unit"] != spec["unit"]:
        raise ParseError(f"unit is {header['unit']}, but format version 1 exports {header['measurement']} "
                         f"in {spec['unit']}; the export changed, so the parser must change first")
    missing = [k for k in spec["keys"] if k not in header]
    if missing:
        raise ParseError(f"a {header['measurement']} header needs {', '.join(missing)}")
    for key in ("row_count", "test_freq_hz", "pulse_width_us"):
        if key in header and not header[key].isdigit():
            raise ParseError(f"{key} is {header[key]!r}, not a whole number")
    timestamp(header["exported_at"], "exported_at")

    if i >= len(lines):
        raise ParseError("no column names after the header")
    columns = next(csv.reader([lines[i]]))
    if columns != spec["columns"]:
        raise ParseError(f"columns are {','.join(columns)}; format version 1 has {','.join(spec['columns'])}")

    rows, seen = [], set()
    for raw in lines[i + 1:]:
        if not raw.strip():
            continue
        line_no = len(rows) + 1
        fields = next(csv.reader([raw]))
        if len(fields) != len(columns):
            raise ParseError(f"row {line_no} has {len(fields)} fields, not {len(columns)}")
        values = dict(zip(columns, fields))
        electrode = whole_number(values["electrode_no"], f"row {line_no} electrode_no")
        if electrode in seen:
            raise ParseError(f"electrode {electrode} appears twice")
        seen.add(electrode)
        if header["measurement"] == "impedance":
            value = decimal(values["impedance_kohm"], spec["decimals"], f"row {line_no} impedance_kohm")
            no_response = None
        else:
            no_response = {"true": True, "false": False}.get(values["no_response"])
            if no_response is None:
                raise ParseError(f"row {line_no} no_response is {values['no_response']!r}, not true or false")
            value = (decimal(values["threshold_ua"], spec["decimals"], f"row {line_no} threshold_ua")
                     if values["threshold_ua"] else None)
        rows.append(Row(line_no, electrode, value, no_response,
                        timestamp(values["measured_at"], f"row {line_no} measured_at"), raw))

    if len(rows) != int(header["row_count"]):
        raise ParseError(f"header says {header['row_count']} rows, the file has {len(rows)}")
    return Export(header, rows)


def whole_number(text: str, what: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise ParseError(f"{what} is {text!r}, not a positive whole number")
    return int(text)


def decimal(text: str, places: int, what: str) -> Decimal:
    # Shape only: a negative number is still a number. Whether it is plausible is decided in SQL.
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise ParseError(f"{what} is {text!r}, not a number") from None
    if not d.is_finite() or -d.as_tuple().exponent > places:
        raise ParseError(f"{what} is {text!r}; format version 1 has at most {places} decimal places")
    return d


def timestamp(text: str, what: str) -> datetime:
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        raise ParseError(f"{what} is {text!r}, not an ISO 8601 timestamp") from None
    if ts.tzinfo is None:
        raise ParseError(f"{what} is {text!r}, which has no UTC offset")
    return ts
