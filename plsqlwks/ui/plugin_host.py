"""Curses-facing adapter and command dispatcher for Plugin API v1.

This internal UI module is the sole bridge from plugin protocols to application
callbacks.  It copies mutable query results into snapshots, builds an
application-owned command menu, and keeps plugin callables out of ``App``
attribute dispatch.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from ..db import QueryResult
from ..plugins import PluginCommand, PluginContext, ResultSnapshot
from ..plugins.loader import PluginRegistry
from .commands import COMMAND_MENU_ITEMS, CommandMenuItem
from .errors import short_error, wrap_error


_PLUGIN_HANDLER_PREFIX = "__plugin__:"

PromptTextCallback = Callable[[str, str, bool], str | None]
StatusCallback = Callable[[str], None]
ResultsCallback = Callable[[list[str], bool], None]
PluginContextFactory = Callable[[], "UIPluginContext"]


def _snapshot_numeric_values(
    result: QueryResult,
) -> tuple[tuple[Decimal | int | float | None, ...], ...]:
    """Copy only aligned immutable numeric source values into a snapshot."""
    if len(result.original_rows) != len(result.rows):
        return ()
    if any(
        len(original_row) != len(display_row)
        for original_row, display_row in zip(result.original_rows, result.rows)
    ):
        return ()
    return tuple(
        tuple(
            original
            if type(original) in (Decimal, int, float) and str(original) == display
            else None
            for original, display in zip(original_row, display_row)
        )
        for original_row, display_row in zip(result.original_rows, result.rows)
    )


def snapshot_result(result: QueryResult | None) -> ResultSnapshot | None:
    """Copy a mutable query result into the tuple-based public snapshot shape.

    Display columns and rows plus aligned immutable numeric values cross the
    boundary.  Continuation presence is reduced to ``has_more``; arbitrary
    original values and editable context are not exposed.
    """
    if result is None:
        return None
    return ResultSnapshot(
        title=result.title,
        columns=tuple(result.columns),
        rows=tuple(tuple(row) for row in result.rows),
        has_more=result.continuation is not None,
        numeric_values=_snapshot_numeric_values(result),
    )


class UIPluginContext(PluginContext):
    """Concrete UI adapter containing only capabilities permitted by API v1.

    The result snapshot and insert-draft state are captured before construction.
    Slots make the deliberately narrow instance surface explicit and prevent
    accidental attachment of application or database objects.
    """

    __slots__ = (
        "_results_dir",
        "_result_snapshot",
        "_insert_draft",
        "_prompt",
        "_set_status",
        "_set_results",
    )

    def __init__(
        self,
        results_dir: Path,
        result_snapshot: ResultSnapshot | None,
        insert_draft: bool,
        prompt: PromptTextCallback,
        set_status: StatusCallback,
        set_results: ResultsCallback,
    ) -> None:
        self._results_dir = results_dir
        self._result_snapshot = result_snapshot
        self._insert_draft = insert_draft
        self._prompt = prompt
        self._set_status = set_status
        self._set_results = set_results

    @property
    def results_dir(self) -> Path:
        return self._results_dir

    def get_active_result(self) -> ResultSnapshot | None:
        return self._result_snapshot

    def has_active_insert_draft(self) -> bool:
        return self._insert_draft

    def prompt_text(
        self,
        label: str,
        default: str = "",
        *,
        strip: bool = True,
    ) -> str | None:
        return self._prompt(label, default, strip)

    def confirm_overwrite(self, path: Path) -> bool:
        answer = self._prompt(f"Overwrite {path}? y/n", "", True)
        return bool(answer and answer.casefold().startswith("y"))

    def set_status(self, message: str) -> None:
        self._set_status(message)

    def report_error(self, title: str, error: Exception) -> None:
        """Render formatted error details without clearing the active result."""
        self._set_results([f"ERROR {title}:", *wrap_error(error)], False)


class PluginHost:
    """Own registered plugin menu entries and invoke their handlers safely.

    Handler IDs use a private reserved namespace mapped directly to callables;
    they are never resolved as application attributes.  ``execute`` catches
    ordinary plugin exceptions at the trust boundary so one command cannot end
    the curses session, while process-control ``BaseException`` subclasses keep
    their normal behavior.
    """

    def __init__(
        self,
        registry: PluginRegistry,
        context_factory: PluginContextFactory,
    ) -> None:
        self._context_factory = context_factory
        self._commands: dict[str, PluginCommand] = {}
        plugin_menu_items: list[CommandMenuItem] = []
        for plugin in registry.plugins:
            for command in plugin.commands:
                handler_id = self._handler_id(plugin.id, command.id)
                self._commands[handler_id] = command
                plugin_menu_items.append(
                    CommandMenuItem(
                        section=command.section,
                        title=command.title,
                        shortcut=command.shortcut,
                        handler=handler_id,
                        keywords=command.keywords,
                    )
                )
        self.command_menu_items = (*COMMAND_MENU_ITEMS, *plugin_menu_items)
        self.startup_warnings = tuple(registry.warnings)

    @staticmethod
    def _handler_id(plugin_id: str, command_id: str) -> str:
        return f"{_PLUGIN_HANDLER_PREFIX}{plugin_id}:{command_id}"

    def execute(self, handler_id: str) -> bool:
        """Run a registered command and report whether the ID belonged to a plugin."""
        command = self._commands.get(handler_id)
        if command is None:
            return False
        context = self._context_factory()
        try:
            command.handler(context)
        except Exception as exc:
            context.set_status(f"{command.title} failed: {short_error(exc)}")
            context.report_error(f"Plugin command failed: {command.title}", exc)
        return True
