"""Unit tests for the CSV export's encoding — no database needed.

Descriptions and addresses are citizen-submitted text, and the export is opened
in spreadsheet software by staff. These pin the two things that make that safe:
formula triggers are neutralised, and the writer's quoting keeps hostile text
inside its own cell.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.analytics_service import (
    EXPORT_COLUMNS,
    _csv_cell,
    export_filename,
    neutralise_formula,
    render_csv,
)

# ── Formula neutralisation ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        "=1+1",
        '=HYPERLINK("http://evil.example","x")',
        "+91 98765 43210",
        "-2+3",
        "@SUM(A1:A2)",
        "\t=1+1",
        "\r=1+1",
        "  =1+1",
        "\n@cmd",
    ],
)
def test_formula_triggers_are_neutralised(payload: str) -> None:
    assert neutralise_formula(payload) == "'" + payload


@pytest.mark.parametrize("text", ["Deep pothole", "", "Ward 3, Bengaluru", "1=1 is fine mid-cell", "'quoted"])
def test_ordinary_text_is_untouched(text: str) -> None:
    assert neutralise_formula(text) == text


def test_text_cells_go_through_neutralisation() -> None:
    assert _csv_cell("=evil()") == "'=evil()"


def test_numbers_are_not_mistaken_for_formulas() -> None:
    """A negative coordinate is data; prefixing it would turn it into text."""
    assert _csv_cell(Decimal("-73.9857000")) == "-73.9857000"
    assert _csv_cell(-4) == "-4"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (True, "true"),
        (False, "false"),
        (12.3456, "12.35"),
        (7, "7"),
        (uuid.UUID(int=1), "00000000-0000-0000-0000-000000000001"),
        (date(2026, 9, 1), "2026-09-01"),
    ],
)
def test_cell_formatting(value: object, expected: str) -> None:
    assert _csv_cell(value) == expected


def test_datetimes_are_written_in_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    assert _csv_cell(datetime(2026, 9, 1, 10, 0, tzinfo=ist)) == "2026-09-01T04:30:00+00:00"


# ── Rendering ───────────────────────────────────────────────────────────


def _row(**overrides: object) -> tuple[object, ...]:
    values = dict.fromkeys(EXPORT_COLUMNS)
    values.update(overrides)
    return tuple(values[name] for name in EXPORT_COLUMNS)


def test_render_starts_with_a_bom_and_a_header() -> None:
    body = "".join(render_csv([]))
    assert body.startswith("﻿")
    assert body.lstrip("﻿") == ",".join(EXPORT_COLUMNS) + "\r\n"


def test_hostile_text_stays_in_its_cell() -> None:
    nasty = 'a, "b"\nc\r\nd'
    body = "".join(render_csv([_row(description=nasty, issue_number="ISS-1")]))
    rows = list(csv.DictReader(io.StringIO(body.lstrip("﻿"), newline="")))

    assert len(rows) == 1
    assert rows[0]["description"] == nasty
    assert rows[0]["issue_number"] == "ISS-1"


def test_large_exports_are_chunked() -> None:
    """The response streams in pieces rather than as one giant string."""
    chunks = list(render_csv([_row(issue_number=f"ISS-{i}") for i in range(1_200)]))
    assert len(chunks) >= 4  # BOM, two full chunks, the remainder
    assert "".join(chunks).count("\r\n") == 1_201


def test_export_filename_is_timestamped_and_header_safe() -> None:
    name = export_filename(datetime(2026, 10, 9, 14, 30, 5, tzinfo=UTC))
    assert name == "weft-issues-20261009T143005Z.csv"
