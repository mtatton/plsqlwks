"""Built-in command plugin and writer for CSV result export.

The standalone handler depends only on Plugin API v1 and exports its immutable
loaded-row snapshot.  The application may inject its private coordinator to
select and fetch additional rows before invoking the same neutral writer; that
integration does not expand the public plugin contract.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..exporting import write_csv
from ._result_export import (
    BuiltinExportHandler,
    default_result_filename,
    format_export_rows,
    mark_private_host_export_handler,
    prepare_result_export,
    short_error,
    success_message,
)
from ._result_export import (
    local_now as _local_now,
)
from ._result_export import (
    resolve_export_path as _resolve_export_path,
)
from .api import Plugin, PluginCommand, PluginContext, ResultSnapshot

_NULL_DISPLAY_VALUE = ""


@dataclass(frozen=True)
class CsvExportOptions:
    """Formatting choices owned by the built-in CSV plugin.

    These options intentionally are not part of Plugin API v1.  The defaults
    use comma-separated output, write the result grid's ``<NULL>`` sentinel as
    an empty CSV field, and preserve ISO-display values.  ``date_format`` uses
    :meth:`datetime.strftime` syntax; an empty format leaves date and timestamp
    display strings unchanged.
    ``protect_formulas`` opts into spreadsheet-oriented formula-injection
    protection; it is disabled by default so machine-readable CSV output stays
    byte-for-byte compatible.
    """

    separator: str = ","
    null_value: str = _NULL_DISPLAY_VALUE
    date_format: str = ""
    protect_formulas: bool = False


_DEFAULT_CSV_EXPORT_OPTIONS = CsvExportOptions()


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
    options: CsvExportOptions = _DEFAULT_CSV_EXPORT_OPTIONS,
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
        write_csv_snapshot(path, snapshot, options)
    except Exception as error:
        context.set_status(f"CSV export failed: {short_error(error)}")
        context.report_error("CSV export", error)
        return

    context.set_status(success_message(snapshot, path))


def write_csv_snapshot(
    path: Path,
    snapshot: ResultSnapshot,
    options: CsvExportOptions,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    rows = format_export_rows(
        snapshot.rows,
        null_value=options.null_value,
        date_format=options.date_format,
        cancelled=cancelled,
    )
    write_csv(
        path,
        snapshot.columns,
        rows,
        delimiter=options.separator,
        protect_formulas=options.protect_formulas,
        on_progress=on_progress,
        cancelled=cancelled,
    )


def create_plugin(
    options: CsvExportOptions | None = None,
    *,
    host_export: BuiltinExportHandler | None = None,
) -> Plugin:
    """Return plugin metadata, optionally capturing configured CSV choices.

    With no argument this remains the zero-argument factory required for
    built-in and entry-point loading.  The returned command closes over an
    immutable options value rather than expanding the public PluginContext.
    """
    selected_options = options if options is not None else CsvExportOptions()

    def configured_export(context: PluginContext) -> None:
        if host_export is None:
            export_loaded_rows(context, selected_options)
        else:
            host_export("csv", context, selected_options)

    if host_export is not None:
        mark_private_host_export_handler(configured_export)

    return Plugin(
        id="csv-export",
        name="CSV result export",
        commands=(
            PluginCommand(
                id="export-loaded-rows",
                section="Results",
                title="Export result to CSV",
                handler=configured_export,
                keywords="export csv save result loaded rows",
            ),
        ),
    )
