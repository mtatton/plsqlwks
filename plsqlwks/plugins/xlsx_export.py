"""Built-in command plugin and writer for type-aware XLSX result export.

The standalone handler uses only the immutable loaded-row snapshot supplied by
Plugin API v1.  The application can privately coordinate a larger snapshot and
then invoke the same writer. Source-number hints create precision-safe Excel
numbers without guessing from display text; every other value remains a
formula-safe literal string. Workbook support is loaded lazily, so optional
``openpyxl`` is unnecessary for core PLSQLWKS startup.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..xlsx_exporting import preflight_xlsx_export, write_xlsx_result
from ._result_export import (
    BuiltinExportHandler,
    default_result_filename,
    format_export_rows,
    local_now,
    mark_private_host_export_handler,
    prepare_result_export,
    short_error,
    success_message,
)
from .api import Plugin, PluginCommand, PluginContext, ResultSnapshot


@dataclass(frozen=True)
class XlsxExportOptions:
    """Formatting choices owned by the built-in XLSX export plugin.

    ``null_value`` replaces the exact ``<NULL>`` display sentinel,
    ``date_format`` formats strict ISO-shaped display values with ``strftime``,
    ``theme`` selects bundled static cell styles, and ``auto_filter`` controls
    whether Excel filtering is enabled for the exported table.  ``auto_width``
    controls deterministic content-based column sizing, while
    ``freeze_top_row`` keeps the header visible during vertical scrolling.
    These settings do not expand the public Plugin API.
    """

    null_value: str = ""
    theme: str = "bright"
    date_format: str = ""
    auto_filter: bool = True
    auto_width: bool = True
    freeze_top_row: bool = True


_DEFAULT_XLSX_EXPORT_OPTIONS = XlsxExportOptions()


_NULL_VALUE_ENV = "PLSQLWKS_XLSX_EXPORT_NULL_VALUE"
_THEME_ENV = "PLSQLWKS_XLSX_EXPORT_THEME"
_DATE_FORMAT_ENV = "PLSQLWKS_XLSX_EXPORT_DATE_FORMAT"
_AUTO_FILTER_ENV = "PLSQLWKS_XLSX_EXPORT_AUTO_FILTER"
_AUTO_WIDTH_ENV = "PLSQLWKS_XLSX_EXPORT_AUTO_WIDTH"
_FREEZE_TOP_ROW_ENV = "PLSQLWKS_XLSX_EXPORT_FREEZE_TOP_ROW"
_TRUE_ENV_VALUES = frozenset(("1", "yes", "true", "on"))
_FALSE_ENV_VALUES = frozenset(("0", "no", "false", "off"))


def _environment_boolean(name: str, default: bool) -> bool:
    """Return a recognized environment boolean or ``default``."""
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_ENV_VALUES:
        return True
    if normalized in _FALSE_ENV_VALUES:
        return False
    return default


def _environment_options() -> XlsxExportOptions:
    """Capture XLSX-plugin-specific environment settings at factory time."""
    defaults = XlsxExportOptions()
    return XlsxExportOptions(
        null_value=os.environ.get(_NULL_VALUE_ENV, defaults.null_value),
        theme=os.environ.get(_THEME_ENV, defaults.theme),
        date_format=os.environ.get(_DATE_FORMAT_ENV, defaults.date_format),
        auto_filter=_environment_boolean(
            _AUTO_FILTER_ENV,
            defaults.auto_filter,
        ),
        auto_width=_environment_boolean(
            _AUTO_WIDTH_ENV,
            defaults.auto_width,
        ),
        freeze_top_row=_environment_boolean(
            _FREEZE_TOP_ROW_ENV,
            defaults.freeze_top_row,
        ),
    )


def export_loaded_rows_to_xlsx(
    context: PluginContext,
    options: XlsxExportOptions = _DEFAULT_XLSX_EXPORT_OPTIONS,
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
        write_xlsx_snapshot(path, snapshot, options)
    except Exception as error:
        context.set_status(f"XLSX export failed: {short_error(error)}")
        context.report_error("XLSX export failed", error)
        return

    context.set_status(success_message(snapshot, path))


def preflight_xlsx_snapshot(options: XlsxExportOptions) -> None:
    preflight_xlsx_export(options.theme)


def write_xlsx_snapshot(
    path: Path,
    snapshot: ResultSnapshot,
    options: XlsxExportOptions,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    write_xlsx_result(
        path,
        title=snapshot.title,
        columns=snapshot.columns,
        rows=format_export_rows(
            snapshot.rows,
            null_value=options.null_value,
            date_format=options.date_format,
            cancelled=cancelled,
        ),
        numeric_values=snapshot.numeric_values,
        theme=options.theme,
        auto_filter=options.auto_filter,
        auto_width=options.auto_width,
        freeze_top_row=options.freeze_top_row,
        on_progress=on_progress,
        cancelled=cancelled,
    )


def create_plugin(
    options: XlsxExportOptions | None = None,
    *,
    host_export: BuiltinExportHandler | None = None,
) -> Plugin:
    """Return metadata capturing explicit or environment-backed options."""
    selected_options = options if options is not None else _environment_options()

    def configured_export(context: PluginContext) -> None:
        if host_export is None:
            export_loaded_rows_to_xlsx(context, selected_options)
        else:
            host_export("xlsx", context, selected_options)

    if host_export is not None:
        mark_private_host_export_handler(configured_export)

    return Plugin(
        id="xlsx-export",
        name="XLSX result export",
        commands=(
            PluginCommand(
                id="export-loaded-rows",
                section="Results",
                title="Export result to XLSX",
                handler=configured_export,
                shortcut="",
                keywords="export xlsx excel spreadsheet table result loaded rows",
            ),
        ),
    )
