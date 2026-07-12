"""Built-in Plugin API v1 command for literal-string XLSX result export.

The command uses only the immutable result snapshot and UI-mediated operations
from :class:`PluginContext`.  Workbook support is loaded lazily by the neutral
writer, so the optional ``openpyxl`` dependency is unnecessary for core
PLSQLWKS startup and for plugins that do not export XLSX files.
"""

from __future__ import annotations

from dataclasses import dataclass
import os

from ..xlsx_exporting import write_xlsx_result
from ._result_export import (
    default_result_filename,
    format_export_rows,
    local_now,
    prepare_result_export,
    short_error,
    success_message,
)
from .api import Plugin, PluginCommand, PluginContext


@dataclass(frozen=True)
class XlsxExportOptions:
    """Formatting choices owned by the built-in XLSX export plugin.

    ``null_value`` replaces the exact ``<NULL>`` display sentinel,
    ``date_format`` formats strict ISO-shaped display values with ``strftime``,
    and ``theme`` selects bundled static cell styles.  These settings do not
    expand the public Plugin API.
    """

    null_value: str = "<NULL>"
    theme: str = "bright"
    date_format: str = ""


_NULL_VALUE_ENV = "PLSQLWKS_XLSX_EXPORT_NULL_VALUE"
_THEME_ENV = "PLSQLWKS_XLSX_EXPORT_THEME"
_DATE_FORMAT_ENV = "PLSQLWKS_XLSX_EXPORT_DATE_FORMAT"


def _environment_options() -> XlsxExportOptions:
    """Capture XLSX-plugin-specific environment settings at factory time."""
    defaults = XlsxExportOptions()
    return XlsxExportOptions(
        null_value=os.environ.get(_NULL_VALUE_ENV, defaults.null_value),
        theme=os.environ.get(_THEME_ENV, defaults.theme),
        date_format=os.environ.get(_DATE_FORMAT_ENV, defaults.date_format),
    )


def export_loaded_rows_to_xlsx(
    context: PluginContext,
    options: XlsxExportOptions = XlsxExportOptions(),
) -> None:
    """Prompt for and atomically export the command-start result snapshot."""
    try:
        prepared = prepare_result_export(
            context,
            label="Export loaded rows to XLSX",
            default_filename=default_result_filename(local_now(), "xlsx"),
        )
        if prepared is None:
            return
        snapshot, path = prepared
        write_xlsx_result(
            path,
            title=snapshot.title,
            columns=snapshot.columns,
            rows=format_export_rows(
                snapshot.rows,
                null_value=options.null_value,
                date_format=options.date_format,
            ),
            theme=options.theme,
        )
    except Exception as error:
        context.set_status(f"XLSX export failed: {short_error(error)}")
        context.report_error("XLSX export failed", error)
        return

    context.set_status(success_message(snapshot, path))


def create_plugin(options: XlsxExportOptions | None = None) -> Plugin:
    """Return metadata capturing explicit or environment-backed options."""
    selected_options = options if options is not None else _environment_options()

    def configured_export(context: PluginContext) -> None:
        export_loaded_rows_to_xlsx(context, selected_options)

    return Plugin(
        id="xlsx-export",
        name="XLSX result export",
        commands=(
            PluginCommand(
                id="export-loaded-rows",
                section="Results",
                title="Export loaded rows to XLSX",
                handler=configured_export,
                shortcut="",
                keywords="export xlsx excel spreadsheet table result loaded rows",
            ),
        ),
    )
