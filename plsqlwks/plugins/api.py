"""Stable, command-oriented public contract for PLSQLWKS plugins.

API version 1 deliberately exposes only command registration and a narrow host
context.  Plugins receive immutable result data and UI-mediated operations; the
database worker, workspace, mutable application state, and curses objects are
not part of this contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable, Protocol


PLUGIN_API_VERSION = 1
PLUGIN_ENTRY_POINT_GROUP = "plsqlwks.plugins"


@dataclass(frozen=True)
class ResultSnapshot:
    """Immutable copy of the tabular result visible when a command starts.

    ``rows`` contains only values already loaded into the result grid.  A true
    ``has_more`` flag reports that further rows exist, but intentionally gives
    the plugin no continuation token or way to fetch them.  When available,
    ``numeric_values`` parallels ``rows`` with immutable source numbers and
    ``None`` for every other cell.
    """

    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    has_more: bool
    numeric_values: tuple[tuple[Decimal | int | float | None, ...], ...] = field(
        default=(),
        repr=False,
        compare=False,
        kw_only=True,
    )


class PluginContext(Protocol):
    """Operations that the application makes available to a command handler.

    Implementations are supplied by the UI host.  The protocol is intentionally
    small: it supports result inspection and synchronous prompt, status, and
    error interactions, not direct access to application or database objects.
    """

    @property
    def results_dir(self) -> Path:
        """Return the configured directory for result files."""
        ...

    def get_active_result(self) -> ResultSnapshot | None:
        """Return the command-start snapshot, or ``None`` without a table."""
        ...

    def has_active_insert_draft(self) -> bool:
        """Return whether the grid currently contains an uncommitted insert draft."""
        ...

    def prompt_text(
        self,
        label: str,
        default: str = "",
        *,
        strip: bool = True,
    ) -> str | None:
        """Prompt for text, returning ``None`` when the user cancels."""
        ...

    def confirm_overwrite(self, path: Path) -> bool:
        """Ask whether an existing destination may be replaced."""
        ...

    def set_status(self, message: str) -> None:
        """Replace the application status message with ``message``."""
        ...

    def report_error(self, title: str, error: Exception) -> None:
        """Show formatted error details through the application UI."""
        ...


PluginHandler = Callable[[PluginContext], None]


@dataclass(frozen=True)
class PluginCommand:
    """One command contributed to the Alt-O command menu.

    ``id`` is unique within its plugin.  ``section`` controls menu grouping,
    while ``keywords`` adds searchable terms.  API v1 does not register global
    keyboard shortcuts; ``shortcut`` is display metadata for the menu only.
    """

    id: str
    section: str
    title: str
    handler: PluginHandler
    shortcut: str = ""
    keywords: str = ""


@dataclass(frozen=True)
class Plugin:
    """Metadata and commands returned by a plugin entry-point factory.

    Plugin IDs are globally unique safe identifiers.  The API version defaults
    to the current host version and must match it exactly when the plugin loads.
    """

    id: str
    name: str
    commands: tuple[PluginCommand, ...]
    api_version: int = PLUGIN_API_VERSION


PluginFactory = Callable[[], Plugin]
