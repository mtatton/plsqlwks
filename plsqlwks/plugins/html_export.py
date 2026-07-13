"""Built-in command plugin for exporting loaded result rows as safe HTML.

The command is implemented entirely through Plugin API v1.  It receives an
immutable :class:`~plsqlwks.plugins.ResultSnapshot`, prompts through the host,
and writes a standalone document without accessing curses, application state,
Oracle, or result continuations.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import TextIO

from ..exporting import atomic_write_text
from ..html_exporting import render_html_result
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
class HtmlExportOptions:
    """Formatting choices owned by the built-in HTML export plugin.

    These values intentionally do not expand Plugin API v1.  ``null_value``
    replaces the exact ``<NULL>`` grid display sentinel, ``date_format`` uses
    :meth:`datetime.datetime.strftime` syntax for strict ISO-shaped display
    values, and ``theme`` selects one of the renderer's static CSS themes.
    """

    null_value: str = ""
    theme: str = "bright"
    date_format: str = ""


_NULL_VALUE_ENV = "PLSQLWKS_HTML_EXPORT_NULL_VALUE"
_THEME_ENV = "PLSQLWKS_HTML_EXPORT_THEME"
_DATE_FORMAT_ENV = "PLSQLWKS_HTML_EXPORT_DATE_FORMAT"


def _environment_options() -> HtmlExportOptions:
    """Read plugin-specific environment settings without host integration."""
    defaults = HtmlExportOptions()
    return HtmlExportOptions(
        null_value=os.environ.get(_NULL_VALUE_ENV, defaults.null_value),
        theme=os.environ.get(_THEME_ENV, defaults.theme),
        date_format=os.environ.get(_DATE_FORMAT_ENV, defaults.date_format),
    )


def export_loaded_rows_to_html(
    context: PluginContext,
    options: HtmlExportOptions = HtmlExportOptions(),
) -> None:
    """Prompt for and atomically export the command-start result snapshot.

    The shared workflow rejects insert drafts before obtaining a snapshot,
    handles cancellation and overwrite confirmation, and resolves relative
    paths beneath the configured results directory.  Rendering uses only the
    immutable snapshot values; it cannot fetch or mutate database results.
    """
    try:
        prepared = prepare_result_export(
            context,
            label="Export loaded rows to HTML",
            default_filename=default_result_filename(local_now(), "html"),
        )
        if prepared is None:
            return
        snapshot, path = prepared
        document = render_html_result(
            title=snapshot.title,
            columns=snapshot.columns,
            rows=format_export_rows(
                snapshot.rows,
                null_value=options.null_value,
                date_format=options.date_format,
            ),
            has_more=snapshot.has_more,
            theme=options.theme,
        )

        def write_document(stream: TextIO) -> None:
            stream.write(document)

        atomic_write_text(path, write_document)
    except Exception as error:
        context.set_status(f"HTML export failed: {short_error(error)}")
        context.report_error("HTML export failed", error)
        return

    context.set_status(success_message(snapshot, path))


def create_plugin(options: HtmlExportOptions | None = None) -> Plugin:
    """Return plugin metadata, capturing explicit or environment options.

    Calling this factory without an argument retains the zero-argument plugin
    loading contract and reads only HTML-plugin-specific environment values.
    Option validation happens when the command renders, so a bad environment
    theme becomes a normal HTML export error rather than a startup failure.
    """
    selected_options = options if options is not None else _environment_options()

    def configured_export(context: PluginContext) -> None:
        export_loaded_rows_to_html(context, selected_options)

    return Plugin(
        id="html-export",
        name="HTML result export",
        commands=(
            PluginCommand(
                id="export-loaded-rows",
                section="Results",
                title="Export loaded rows to HTML",
                handler=configured_export,
                shortcut="",
                keywords="export html web browser table result loaded rows",
            ),
        ),
    )
