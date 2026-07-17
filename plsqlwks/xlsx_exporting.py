"""Database- and UI-independent XLSX result serialization.

``openpyxl`` is an optional plugin dependency and is imported only when an
export is requested.  Importing PLSQLWKS, its Plugin API, or this module does
not require that package.  The writer validates the complete table before
creating a workbook.  Headers and ordinary values remain literal strings so
spreadsheet applications cannot interpret result text as a formula, while
matching source-number hints become native Excel numbers when that conversion
does not exceed Excel's documented precision or range.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib import import_module
from pathlib import Path
from typing import Any, BinaryIO

from .exporting import (
    atomic_write_binary,
    raise_if_export_cancelled,
)

DEFAULT_XLSX_TITLE = "PLSQLWKS query result"
EXCEL_MAX_COLUMNS = 16_384
EXCEL_MAX_DATA_ROWS = 1_048_575
EXCEL_MAX_CELL_CHARACTERS = 32_767
_EXCEL_MAX_SIGNIFICANT_DIGITS = 15
_EXCEL_MIN_ABSOLUTE_NUMBER = Decimal("2.2251E-308")
_EXCEL_MAX_ABSOLUTE_NUMBER = Decimal("9.99999999999999E+307")
_EXCEL_MAX_NUMBER_FORMAT_CHARACTERS = 255
_MIN_COLUMN_WIDTH = 3
_MAX_COLUMN_WIDTH = 60
_AUTO_FILTER_HEADER_EXTRA_WIDTH = 3.0
_COLUMN_MARGIN_PIXELS = 17
_STANDARD_CHARACTER_PIXELS = 7
_CALIBRI_WIDTH_SCALE = 1_000
_HEADER_BOLD_FACTOR = 1.03

# Printable ASCII U+0020 through U+007E in thousandths of the Calibri 11
# maximum-digit width.  Keeping the metrics local makes sizing deterministic
# without adding a font-rendering dependency to the optional XLSX plugin.
_CALIBRI_ASCII_WIDTHS = (
    446,
    643,
    791,
    983,
    1_000,
    1_410,
    1_346,
    435,
    598,
    598,
    983,
    983,
    492,
    604,
    498,
    762,
    1_000,
    1_000,
    1_000,
    1_000,
    1_000,
    1_000,
    1_000,
    1_000,
    1_000,
    1_000,
    528,
    528,
    983,
    983,
    983,
    914,
    1_764,
    1_142,
    1_073,
    1_052,
    1_214,
    963,
    907,
    1_245,
    1_229,
    497,
    629,
    1_025,
    829,
    1_687,
    1_274,
    1_306,
    1_019,
    1_328,
    1_071,
    907,
    961,
    1_266,
    1_119,
    1_755,
    1_024,
    961,
    924,
    605,
    762,
    605,
    983,
    983,
    574,
    945,
    1_037,
    834,
    1_037,
    982,
    602,
    929,
    1_037,
    453,
    472,
    897,
    453,
    1_576,
    1_037,
    1_040,
    1_037,
    1_037,
    688,
    772,
    661,
    1_037,
    891,
    1_410,
    855,
    893,
    779,
    620,
    908,
    620,
    983,
)


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
        raise RuntimeError("XLSX export requires the optional 'openpyxl' package") from error
    return (
        openpyxl.Workbook,
        styles.Alignment,
        styles.Border,
        styles.Font,
        styles.PatternFill,
        styles.Side,
    )


def preflight_xlsx_export(theme: str = "bright") -> None:
    if theme not in _THEMES:
        raise ValueError("XLSX export theme must be 'bright' or 'dark'")
    _load_openpyxl()


def _validate_and_copy_result(
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    cancelled: Callable[[], bool] | None = None,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Validate Excel limits and return stable string-only table copies."""
    column_count = len(columns)
    if column_count > EXCEL_MAX_COLUMNS:
        raise ValueError(f"XLSX supports at most {EXCEL_MAX_COLUMNS} columns; result has {column_count}")

    row_count = len(rows)
    if row_count > EXCEL_MAX_DATA_ROWS:
        raise ValueError(f"XLSX supports at most {EXCEL_MAX_DATA_ROWS} loaded data rows; result has {row_count}")

    copied_column_values: list[str] = []
    for column in columns:
        raise_if_export_cancelled(cancelled)
        copied_column_values.append(str(column))
    copied_columns = tuple(copied_column_values)
    copied_rows: list[tuple[str, ...]] = []
    for row_index, row in enumerate(rows, start=1):
        raise_if_export_cancelled(cancelled)
        actual_width = len(row)
        if actual_width != column_count:
            raise ValueError(f"result row {row_index} has {actual_width} value(s); expected {column_count}")
        copied_row: list[str] = []
        for value in row:
            raise_if_export_cancelled(cancelled)
            copied_row.append(str(value))
        copied_rows.append(tuple(copied_row))

    for column_index, value in enumerate(copied_columns, start=1):
        raise_if_export_cancelled(cancelled)
        if len(value) > EXCEL_MAX_CELL_CHARACTERS:
            raise ValueError(
                f"column {column_index} exceeds the XLSX cell limit of {EXCEL_MAX_CELL_CHARACTERS} characters"
            )
    for row_index, row in enumerate(copied_rows, start=1):
        raise_if_export_cancelled(cancelled)
        for column_index, value in enumerate(row, start=1):
            raise_if_export_cancelled(cancelled)
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


def _copy_numeric_values(
    numeric_values: Sequence[Sequence[object]],
    *,
    row_count: int,
    column_count: int,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[tuple[object, ...], ...] | None:
    """Return a stable, correctly shaped hint matrix or disable all hints."""
    try:
        copied_rows: list[tuple[object, ...]] = []
        for row in numeric_values:
            raise_if_export_cancelled(cancelled)
            copied_row: list[object] = []
            for value in row:
                raise_if_export_cancelled(cancelled)
                copied_row.append(value)
            copied_rows.append(tuple(copied_row))
        copied = tuple(copied_rows)
    except (TypeError, ValueError):
        return None
    if not copied:
        return None
    if len(copied) != row_count:
        return None
    for row in copied:
        raise_if_export_cancelled(cancelled)
        if len(row) != column_count:
            return None
    return copied


def _significant_decimal_digits(value: Decimal) -> int:
    """Count meaningful decimal digits without context-dependent normalization."""
    digits = value.as_tuple().digits
    last = len(digits)
    while last > 1 and digits[last - 1] == 0:
        last -= 1
    return last


def _excel_number_format(display: str, source_value: object) -> str | None:
    """Return a scale-preserving format for one safe, matching source number."""
    if type(source_value) not in (Decimal, int, float):
        return None
    if str(source_value) != display:
        return None
    try:
        number = Decimal(display)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    if not number.is_zero():
        absolute = number.copy_abs()
        if not (_EXCEL_MIN_ABSOLUTE_NUMBER <= absolute <= _EXCEL_MAX_ABSOLUTE_NUMBER):
            return None
    if _significant_decimal_digits(number) > _EXCEL_MAX_SIGNIFICANT_DIGITS:
        return None

    if "e" in display.casefold():
        return "General"
    _, separator, fraction = display.partition(".")
    if not separator:
        return "0"
    if len(fraction) > _EXCEL_MAX_NUMBER_FORMAT_CHARACTERS - 2:
        return None
    return "0." + "0" * len(fraction)


def _set_number_or_literal_string(
    cell: Any,
    display: str,
    source_value: object,
) -> None:
    """Write a safe source number natively, otherwise preserve literal text."""
    number_format = _excel_number_format(display, source_value)
    if number_format is None:
        _set_literal_string(cell, display)
        return
    # Assign the validated display literal rather than the Python number so
    # openpyxl does not pass it through its precision-changing ``%.16g`` path.
    cell.value = display
    cell.data_type = "n"
    cell.number_format = number_format


def _character_width(character: str) -> int:
    """Estimate one character's width in spreadsheet character units."""
    if character == "\t":
        return 4
    if unicodedata.combining(character):
        return 0
    if unicodedata.category(character)[0] == "C":
        return 0
    if unicodedata.east_asian_width(character) in ("F", "W"):
        return 2
    return 1


def _text_width(value: str) -> int:
    """Return the widest logical line in ``value`` using visual units."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return max(sum(_character_width(character) for character in line) for line in normalized.split("\n"))


def _proportional_character_width(character: str) -> float:
    """Estimate a glyph in Calibri-compatible spreadsheet width units."""
    if character == "\t":
        return 4.0
    if unicodedata.combining(character):
        return 0.0
    if unicodedata.category(character)[0] == "C":
        return 0.0

    codepoint = ord(character)
    if 0x20 <= codepoint <= 0x7E:
        return _CALIBRI_ASCII_WIDTHS[codepoint - 0x20] / _CALIBRI_WIDTH_SCALE
    if unicodedata.east_asian_width(character) in ("F", "W"):
        return 2.0

    decomposed = [unit for unit in unicodedata.normalize("NFKD", character) if not unicodedata.combining(unit)]
    if decomposed and all(0x20 <= ord(unit) <= 0x7E for unit in decomposed):
        return sum(_CALIBRI_ASCII_WIDTHS[ord(unit) - 0x20] / _CALIBRI_WIDTH_SCALE for unit in decomposed)
    return 1.0


def _column_text_width(value: str, *, bold: bool = False) -> float:
    """Return the widest proportional logical line used for column sizing."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    width = max(sum(_proportional_character_width(character) for character in line) for line in normalized.split("\n"))
    return width * _HEADER_BOLD_FACTOR if bold else width


def _fitted_column_width(content_width: float) -> float:
    """Clamp measured content and add a small pixel-equivalent fit margin."""
    base_width = min(_MAX_COLUMN_WIDTH, max(_MIN_COLUMN_WIDTH, content_width))
    return base_width + _COLUMN_MARGIN_PIXELS / _STANDARD_CHARACTER_PIXELS


def _requires_wrapping(value: str, content_width: int) -> bool:
    """Return whether one cell exceeds the cap or contains a line break."""
    return content_width > _MAX_COLUMN_WIDTH or "\r" in value or "\n" in value


def write_xlsx_result(
    path: Path,
    *,
    title: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    numeric_values: Sequence[Sequence[object]] = (),
    theme: str = "bright",
    auto_filter: bool = True,
    auto_width: bool = True,
    freeze_top_row: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Atomically write one styled worksheet containing exactly ``rows``.

    Excel dimensions, row widths, cell text lengths, and the static theme name
    are validated before the optional backend or atomic writer is invoked.
    When ``auto_width`` is enabled, columns use the widest proportional header
    or data line up to a readable maximum; otherwise no explicit column widths
    are emitted.  An enabled auto-filter adds three character units to the
    header candidate for its dropdown control.  When ``freeze_top_row`` is
    enabled and columns exist, the first data row anchors a frozen pane so the
    header remains visible while scrolling.  Wrapping remains based on visual
    character count regardless of those settings, so only over-limit or
    explicitly multiline cells use wrapped alignment.  A correctly shaped
    ``numeric_values`` matrix may identify original numeric values.  A hint is
    used only when its exact
    supported Python type and text agree with the display value and Excel can
    preserve its range, precision, and fixed-point scale; malformed or unsafe
    hints fall back to literal text.  The snapshot title is stored only as
    document metadata; the worksheet is therefore exactly one header row
    followed by the supplied data rows.  When requested, its auto-filter covers
    that complete table.
    """
    try:
        selected_theme = _THEMES[theme]
    except KeyError:
        raise ValueError("XLSX export theme must be 'bright' or 'dark'") from None
    copied_columns, copied_rows = _validate_and_copy_result(
        columns,
        rows,
        cancelled,
    )
    copied_numeric_values = _copy_numeric_values(
        numeric_values,
        row_count=len(copied_rows),
        column_count=len(copied_columns),
        cancelled=cancelled,
    )
    raise_if_export_cancelled(cancelled)
    if on_progress is not None:
        on_progress(0, len(copied_rows))

    Workbook, Alignment, Border, Font, PatternFill, Side = _load_openpyxl()
    raise_if_export_cancelled(cancelled)
    workbook = Workbook()
    try:
        workbook.properties.title = title if title else DEFAULT_XLSX_TITLE
        worksheet = workbook.active
        worksheet.title = "Query result"

        edge = Side(style="thin", color=selected_theme.border)
        border = Border(left=edge, right=edge, top=edge, bottom=edge)
        alignment = Alignment(vertical="top")
        wrapped_alignment = Alignment(vertical="top", wrap_text=True)
        data_font = Font(color=selected_theme.foreground)
        header_font = Font(color=selected_theme.foreground, bold=True)
        data_fill = PatternFill("solid", fgColor=selected_theme.background)
        header_fill = PatternFill("solid", fgColor=selected_theme.header_background)
        content_widths = [0.0] * len(copied_columns) if auto_width else None

        for column_index, value in enumerate(copied_columns, start=1):
            raise_if_export_cancelled(cancelled)
            cell = worksheet.cell(row=1, column=column_index)
            wrap_width = _text_width(value)
            _set_literal_string(cell, value)
            cell.font = header_font
            cell.fill = header_fill
            cell.border = border
            cell.alignment = wrapped_alignment if _requires_wrapping(value, wrap_width) else alignment
            if content_widths is not None:
                header_width = _column_text_width(
                    value,
                    bold=True,
                )
                if auto_filter:
                    header_width += _AUTO_FILTER_HEADER_EXTRA_WIDTH
                content_widths[column_index - 1] = header_width

        for row_index, row in enumerate(copied_rows, start=2):
            raise_if_export_cancelled(cancelled)
            for column_index, value in enumerate(row, start=1):
                raise_if_export_cancelled(cancelled)
                cell = worksheet.cell(row=row_index, column=column_index)
                wrap_width = _text_width(value)
                source_value = (
                    None if copied_numeric_values is None else copied_numeric_values[row_index - 2][column_index - 1]
                )
                _set_number_or_literal_string(cell, value, source_value)
                cell.font = data_font
                cell.fill = data_fill
                cell.border = border
                cell.alignment = wrapped_alignment if _requires_wrapping(value, wrap_width) else alignment
                if content_widths is not None:
                    content_widths[column_index - 1] = max(
                        content_widths[column_index - 1],
                        _column_text_width(value),
                    )
            if on_progress is not None:
                on_progress(row_index - 1, len(copied_rows))

        if content_widths is not None:
            for column_index, content_width in enumerate(content_widths, start=1):
                raise_if_export_cancelled(cancelled)
                column_letter = worksheet.cell(
                    row=1,
                    column=column_index,
                ).column_letter
                worksheet.column_dimensions[column_letter].width = _fitted_column_width(content_width)

        if freeze_top_row and copied_columns:
            raise_if_export_cancelled(cancelled)
            worksheet.freeze_panes = "A2"

        if auto_filter and copied_columns:
            raise_if_export_cancelled(cancelled)
            last_cell = worksheet.cell(
                row=len(copied_rows) + 1,
                column=len(copied_columns),
            ).coordinate
            worksheet.auto_filter.ref = f"A1:{last_cell}"

        def save_workbook(stream: BinaryIO) -> None:
            workbook.save(stream)

        atomic_write_binary(
            path,
            save_workbook,
            before_replace=lambda: raise_if_export_cancelled(cancelled),
        )
    finally:
        workbook.close()
