"""Database- and UI-independent result-file writing helpers.

The atomic text and binary writers give format-specific exporters the same safe
replacement semantics.  Both the Plugin API CSV command and the legacy
database export facade also delegate here, keeping one standard-library CSV
encoding implementation.
"""

from __future__ import annotations

import csv
from io import StringIO
import os
from pathlib import Path
import stat
import tempfile
from typing import BinaryIO, Callable, Iterable, Sequence, TextIO, cast


CSV_LINE_TERMINATOR = "\n"
CSV_WRITER_LINE_TERMINATOR = "\r\n"
CSV_FORMULA_PREFIXES = frozenset(
    ("=", "+", "-", "@", "\t", "\r", "\n", "\0", "＝", "＋", "－", "＠")
)


def preserve_existing_posix_permissions(path: Path, temporary_path: Path) -> None:
    """Copy an existing destination's permission bits to its replacement."""
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return
    temporary_path.chmod(mode)


def atomic_write_binary(path: Path, writer: Callable[[BinaryIO], None]) -> None:
    """Write complete binary content and atomically install it at ``path``.

    The parent directory is created first. ``writer`` receives a temporary
    sibling opened for binary updates, which supports ZIP-based writers that
    seek while producing their output. The temporary file is closed before
    :func:`os.replace`. Any failure or interruption triggers best-effort
    temporary-file cleanup and leaves an existing destination untouched until
    replacement succeeds.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w+b",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            writer(cast(BinaryIO, handle))
        preserve_existing_posix_permissions(path, temporary_path)
        os.replace(temporary_path, path)
        temporary_path = None
    except BaseException:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        raise


def atomic_write_text(
    path: Path,
    writer: Callable[[TextIO], None],
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    """Write a complete UTF-8 text file and atomically install it at ``path``.

    The parent directory is created first.  ``writer`` receives a temporary
    sibling opened with ``newline=""`` and must write the complete document.
    The temporary file is closed before :func:`os.replace`, which is important
    on Windows.  Any failure, including a non-``Exception`` interruption,
    triggers best-effort temporary-file cleanup and leaves an existing
    destination untouched until replacement succeeds.  ``before_replace``,
    when provided, runs after the temporary file is closed and immediately
    before replacement so callers can perform a last-moment conflict check.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            writer(cast(TextIO, handle))
        if before_replace is not None:
            before_replace()
        preserve_existing_posix_permissions(path, temporary_path)
        os.replace(temporary_path, path)
        temporary_path = None
    except BaseException:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        raise


def write_csv(
    path: Path,
    columns: Sequence[str],
    rows: Iterable[Sequence[str]],
    *,
    delimiter: str = ",",
    protect_formulas: bool = False,
) -> None:
    """Atomically write display-ready columns and rows as UTF-8 CSV.

    The file is written with deterministic LF records to a temporary sibling,
    then installed with :func:`os.replace`.  If writing or replacement fails,
    the temporary file is removed where possible and an existing destination
    remains intact until replacement succeeds.  ``delimiter`` must be exactly
    one character and is forwarded to the standard-library CSV encoder.
    ``protect_formulas`` is an opt-in mode for files intended for spreadsheet
    viewing: formula-like fields receive a leading tab and every field is
    quoted so the tab stays inside its CSV field.
    """
    if not isinstance(delimiter, str) or len(delimiter) != 1:
        raise ValueError("CSV delimiter must be exactly one character")

    def write_rows(handle: TextIO) -> None:
        buffer = StringIO(newline="")

        def write_row(row: Sequence[str]) -> None:
            writer = csv.writer(
                buffer,
                delimiter=delimiter,
                # Python 3.10 only quotes characters present in a CRLF
                # terminator; normalize the record ending after each row.
                lineterminator=CSV_WRITER_LINE_TERMINATOR,
                quoting=csv.QUOTE_ALL if protect_formulas else csv.QUOTE_MINIMAL,
                escapechar=_csv_nul_workaround_escapechar(row, delimiter),
            )
            writer.writerow(row)
            encoded = buffer.getvalue()
            handle.write(encoded[: -len(CSV_WRITER_LINE_TERMINATOR)])
            handle.write(CSV_LINE_TERMINATOR)
            buffer.seek(0)
            buffer.truncate()

        if columns:
            write_row(_protect_csv_formulas(columns) if protect_formulas else columns)
        output_rows = (
            (_protect_csv_formulas(row) for row in rows)
            if protect_formulas
            else rows
        )
        for row in output_rows:
            write_row(row)

    atomic_write_text(path, write_rows)


def _protect_csv_formulas(values: Sequence[str]) -> tuple[str, ...]:
    """Neutralize spreadsheet formula prefixes without changing other text."""
    return tuple(
        f"\t{value}" if value and value[0] in CSV_FORMULA_PREFIXES else value
        for value in values
    )


def _csv_nul_workaround_escapechar(
    values: Sequence[str],
    delimiter: str,
) -> str | None:
    """Work around Python 3.10's requirement for an escapechar around NUL."""
    if all("\0" not in value for value in values):
        return None
    used = {delimiter, '"', "\r", "\n", "\0"}
    for value in values:
        used.update(value)
    for codepoint in range(1, 0x110000):
        if 0xD800 <= codepoint <= 0xDFFF:
            continue
        candidate = chr(codepoint)
        if candidate not in used:
            return candidate
    raise ValueError("CSV row contains every available escape character")


def csv_cell(value: str) -> str:
    """Return one value using the CSV encoding used by :func:`write_csv`."""
    buffer = StringIO(newline="")
    row = (value, "")
    writer = csv.writer(
        buffer,
        lineterminator=CSV_WRITER_LINE_TERMINATOR,
        escapechar=_csv_nul_workaround_escapechar(row, ","),
    )
    writer.writerow(row)
    return buffer.getvalue()[: -len("," + CSV_WRITER_LINE_TERMINATOR)]
