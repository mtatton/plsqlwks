"""Built-in command plugin for exporting the currently loaded result rows.

The implementation depends only on Plugin API v1 and the neutral CSV writer.
It never requests more result pages or interacts with a database, transaction,
mutable result, application state, or curses object.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..exporting import write_csv
from ._result_export import (
    default_result_filename,
    format_export_rows,
    local_now as _local_now,
    prepare_result_export,
    resolve_export_path as _resolve_export_path,
    short_error,
    success_message,
)
from .api import Plugin, PluginCommand, PluginContext


_NULL_DISPLAY_VALUE = "<NULL>"


@dataclass(frozen=True)
class CsvExportOptions:
    """Formatting choices owned by the built-in CSV plugin.

    These options intentionally are not part of Plugin API v1.  The defaults
    retain the result grid's comma-separated, ``<NULL>``, ISO-display output.
    ``date_format`` uses :meth:`datetime.strftime` syntax; an empty format
    leaves date and timestamp display strings unchanged.
    """

    separator: str = ","
    null_value: str = _NULL_DISPLAY_VALUE
    date_format: str = ""


def local_now() -> datetime:
    """Return the current local time used for the proposed export name."""
    return _local_now()


def default_csv_filename(now: datetime) -> str:
    """Build a filesystem-safe CSV filename from an explicit local time."""
    return default_result_filename(now, "csv")


def resolve_export_path(value: str, results_dir: Path) -> Path:
    """Expand and normalize a response, anchoring relative paths in results_dir."""
    return _resolve_export_path(value, results_dir)


def export_loaded_rows(
    context: PluginContext,
    options: CsvExportOptions = CsvExportOptions(),
) -> None:
    """Prompt for and atomically export one immutable loaded-result snapshot.

    Insert drafts are rejected before reading the result so their temporary row
    cannot be copied.  Existing destinations require host confirmation.  The
    shared writer handles UTF-8 CSV encoding and atomic replacement; failures
    are reported without mutating the active result or its continuation.
    """
    try:
        prepared = prepare_result_export(
            context,
            "Export loaded rows to CSV",
            default_csv_filename(local_now()),
        )
        if prepared is None:
            return
        snapshot, path = prepared
        write_csv(
            path,
            snapshot.columns,
            format_export_rows(
                snapshot.rows,
                null_value=options.null_value,
                date_format=options.date_format,
            ),
            delimiter=options.separator,
        )
    except Exception as error:
        context.set_status(f"CSV export failed: {short_error(error)}")
        context.report_error("CSV export", error)
        return

    context.set_status(success_message(snapshot, path))


def create_plugin(options: CsvExportOptions | None = None) -> Plugin:
    """Return plugin metadata, optionally capturing configured CSV choices.

    With no argument this remains the zero-argument factory required for
    built-in and entry-point loading.  The returned command closes over an
    immutable options value rather than expanding the public PluginContext.
    """
    selected_options = options if options is not None else CsvExportOptions()

    def configured_export(context: PluginContext) -> None:
        export_loaded_rows(context, selected_options)

    return Plugin(
        id="csv-export",
        name="CSV result export",
        commands=(
            PluginCommand(
                id="export-loaded-rows",
                section="Results",
                title="Export loaded rows to CSV",
                handler=configured_export,
                keywords="export csv save result loaded rows",
            ),
        ),
    )
