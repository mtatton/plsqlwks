"""Database- and UI-independent XLSX result serialization.

``openpyxl`` is an optional plugin dependency and is imported only when an
export is requested.  Importing PLSQLWKS, its Plugin API, or this module does
not require that package.  The writer validates the complete table before
creating a workbook and stores every header and value as a literal string so
spreadsheet applications cannot interpret result text as a formula.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, BinaryIO, Sequence

from .exporting import atomic_write_binary


DEFAULT_XLSX_TITLE = "PLSQLWKS query result"
EXCEL_MAX_COLUMNS = 16_384
EXCEL_MAX_DATA_ROWS = 1_048_575
EXCEL_MAX_CELL_CHARACTERS = 32_767


@dataclass(frozen=True)
class _Theme:
    background: str
    foreground: str
    header_background: str
    border: str


_THEMES = {
    "bright": _Theme(
        background="FFFFFF",
        foreground="202124",
        header_background="F1F3F4",
        border="777777",
    ),
    "dark": _Theme(
        background="17191D",
        foreground="F1F3F4",
        header_background="292C33",
        border="777C87",
    ),
}


def _load_openpyxl() -> tuple[Any, Any, Any, Any, Any, Any]:
    """Load the optional workbook backend only when an export runs."""
    try:
        openpyxl = import_module("openpyxl")
        styles = import_module("openpyxl.styles")
    except ImportError as error:
        raise RuntimeError(
            "XLSX export requires the optional 'openpyxl' package"
        ) from error
    return (
        openpyxl.Workbook,
        styles.Alignment,
        styles.Border,
        styles.Font,
        styles.PatternFill,
        styles.Side,
    )


def _validate_and_copy_result(
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Validate Excel limits and return stable string-only table copies."""
    column_count = len(columns)
    if column_count > EXCEL_MAX_COLUMNS:
        raise ValueError(
            f"XLSX supports at most {EXCEL_MAX_COLUMNS} columns; "
            f"result has {column_count}"
        )

    row_count = len(rows)
    if row_count > EXCEL_MAX_DATA_ROWS:
        raise ValueError(
            f"XLSX supports at most {EXCEL_MAX_DATA_ROWS} loaded data rows; "
            f"result has {row_count}"
        )

    copied_columns = tuple(str(column) for column in columns)
    copied_rows: list[tuple[str, ...]] = []
    for row_index, row in enumerate(rows, start=1):
        actual_width = len(row)
        if actual_width != column_count:
            raise ValueError(
                f"result row {row_index} has {actual_width} value(s); "
                f"expected {column_count}"
            )
        copied_rows.append(tuple(str(value) for value in row))

    for column_index, value in enumerate(copied_columns, start=1):
        if len(value) > EXCEL_MAX_CELL_CHARACTERS:
            raise ValueError(
                f"column {column_index} exceeds the XLSX cell limit of "
                f"{EXCEL_MAX_CELL_CHARACTERS} characters"
            )
    for row_index, row in enumerate(copied_rows, start=1):
        for column_index, value in enumerate(row, start=1):
            if len(value) > EXCEL_MAX_CELL_CHARACTERS:
                raise ValueError(
                    f"result row {row_index}, column {column_index} exceeds "
                    f"the XLSX cell limit of {EXCEL_MAX_CELL_CHARACTERS} characters"
                )
    return copied_columns, tuple(copied_rows)


def _set_literal_string(cell: Any, value: str) -> None:
    """Assign text while overriding openpyxl's leading-``=`` formula inference."""
    cell.value = value
    cell.data_type = "s"


def write_xlsx_result(
    path: Path,
    *,
    title: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    theme: str = "bright",
) -> None:
    """Atomically write one styled worksheet containing exactly ``rows``.

    Excel dimensions, row widths, cell text lengths, and the static theme name
    are validated before the optional backend or atomic writer is invoked.
    The snapshot title is stored only as document metadata; the worksheet is
    therefore exactly one header row followed by the supplied data rows.
    """
    try:
        selected_theme = _THEMES[theme]
    except KeyError:
        raise ValueError("XLSX export theme must be 'bright' or 'dark'") from None
    copied_columns, copied_rows = _validate_and_copy_result(columns, rows)

    Workbook, Alignment, Border, Font, PatternFill, Side = _load_openpyxl()
    workbook = Workbook()
    try:
        workbook.properties.title = title if title else DEFAULT_XLSX_TITLE
        worksheet = workbook.active
        worksheet.title = "Query result"

        edge = Side(style="thin", color=selected_theme.border)
        border = Border(left=edge, right=edge, top=edge, bottom=edge)
        alignment = Alignment(vertical="top", wrap_text=True)
        data_font = Font(color=selected_theme.foreground)
        header_font = Font(color=selected_theme.foreground, bold=True)
        data_fill = PatternFill("solid", fgColor=selected_theme.background)
        header_fill = PatternFill("solid", fgColor=selected_theme.header_background)

        for column_index, value in enumerate(copied_columns, start=1):
            cell = worksheet.cell(row=1, column=column_index)
            _set_literal_string(cell, value)
            cell.font = header_font
            cell.fill = header_fill
            cell.border = border
            cell.alignment = alignment

        for row_index, row in enumerate(copied_rows, start=2):
            for column_index, value in enumerate(row, start=1):
                cell = worksheet.cell(row=row_index, column=column_index)
                _set_literal_string(cell, value)
                cell.font = data_font
                cell.fill = data_fill
                cell.border = border
                cell.alignment = alignment

        def save_workbook(stream: BinaryIO) -> None:
            workbook.save(stream)

        atomic_write_binary(path, save_workbook)
    finally:
        workbook.close()
