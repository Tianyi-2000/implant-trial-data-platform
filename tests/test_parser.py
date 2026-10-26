"""The parser checks only what the file itself can tell (ADR-006, ADR-007). No database needed."""
from decimal import Decimal

import pytest

from itdp import parsers

pytestmark = pytest.mark.req("REQ-07")

GOOD = """\
# format=fitting-export
# format_version=1
# export_tool=fitsuite 3.2.0
# exported_at=2025-12-14T10:55:43+09:00
# site_code=SITE-A
# subject_code=S001
# device_serial=DEV-0001
# visit_code=D30
# measurement=impedance
# unit=kohm
# test_freq_hz=1000
# row_count=2
electrode_no,impedance_kohm,measured_at
1,7.33,2025-12-14T10:49:01+09:00
2,7.17,2025-12-14T10:49:04+09:00
"""


def test_a_good_export_parses():
    export = parsers.parse(GOOD.encode())
    assert export.header["device_serial"] == "DEV-0001"
    assert [(r.electrode_no, r.value) for r in export.rows] == [(1, Decimal("7.33")), (2, Decimal("7.17"))]


def test_a_negative_value_still_parses_because_plausibility_is_checked_in_sql():
    export = parsers.parse(GOOD.replace("1,7.33", "1,-7.33").encode())
    assert export.rows[0].value == Decimal("-7.33")


@pytest.mark.parametrize("old, new, message", [
    ("# unit=kohm", "# unit=ohm", "unit is ohm, but format version 1 exports impedance in kohm"),
    ("# format_version=1", "# format_version=2", "version 2 is not supported"),
    ("# device_serial=DEV-0001\n", "", "header is missing device_serial"),
    ("# row_count=2", "# row_count=3", "header says 3 rows, the file has 2"),
    ("electrode_no,impedance_kohm", "electrode_no,impedance", "columns are electrode_no,impedance,measured_at"),
    ("2,7.17", "1,7.17", "electrode 1 appears twice"),
    ("1,7.33", "1,7.333", "at most 2 decimal places"),
    ("1,7.33", "1,abc", "not a number"),
    ("1,7.33,2025-12-14T10:49:01+09:00", "1,7.33,2025-12-14T10:49:01", "no UTC offset"),
    ("1,7.33,", "1,7.33,,", "row 1 has 4 fields, not 3"),
])
def test_a_file_the_parser_does_not_fully_understand_is_rejected_whole(old, new, message):
    assert old in GOOD
    with pytest.raises(parsers.ParseError, match=message):
        parsers.parse(GOOD.replace(old, new, 1).encode())
