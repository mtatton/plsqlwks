"""Private workflow helpers shared by built-in result-export plugins.

This module centralizes only the UI-neutral steps that every synchronous file
export command needs: choosing a destination, enforcing insert-draft and
overwrite rules, and formatting concise status text.  Format-specific
rendering remains in each plugin and these helpers are intentionally absent
from the public :mod:`plsqlwks.plugins` API.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import TypeAlias

from ..exporting import raise_if_export_cancelled
from .api import PluginContext, PluginHandler, ResultSnapshot

_MAX_ERROR_DETAIL_LENGTH = 160
_NULL_DISPLAY_VALUE = "<NULL>"
_ISO_DATE_DISPLAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_ISO_TIMESTAMP_DISPLAY_RE = re.compile(
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:[+-]\d{2}:\d{2})?\Z"
)
_INSERT_DRAFT_STATUS = "Export unavailable while an insert draft is active; commit or cancel the draft first"
_NO_RESULT_STATUS = "No table result is available for export"
_CANCELLED_STATUS = "Export cancelled"

BuiltinExportHandler: TypeAlias = Callable[[str, PluginContext, object], None]
_PRIVATE_HOST_EXPORT_HANDLER_ATTR = "_plsqlwks_private_host_export"


def mark_private_host_export_handler(handler: PluginHandler) -> PluginHandler:
    """Mark one bundled handler for the application's deferred context path."""
    setattr(handler, _PRIVATE_HOST_EXPORT_HANDLER_ATTR, True)
    return handler


def is_private_host_export_handler(handler: object) -> bool:
    """Return whether a bundled command uses private host orchestration."""
    return bool(getattr(handler, _PRIVATE_HOST_EXPORT_HANDLER_ATTR, False))


def local_now() -> datetime:
    """Return the current local time used for proposed result filenames."""
    return datetime.now().astimezone()


def default_result_filename(now: datetime, extension: str) -> str:
    """Build a filesystem-safe timestamped result filename.

    ``extension`` is normally supplied without a leading dot.  Accepting one
    keeps callers simple without changing the deterministic filename shape.
    """
    normalized_extension = extension.lstrip(".")
    if not normalized_extension:
        raise ValueError("result filename extension must be nonempty")
    return f"result_{now:%Y%m%d_%H%M%S}.{normalized_extension}"


def resolve_export_path(value: str, results_dir: Path) -> Path:
    """Expand and normalize a response, anchoring relative paths in results_dir."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = results_dir / path
    return path.resolve()


def short_error(error: Exception) -> str:
    """Return one bounded status-line-safe detail from an exception."""
    message = str(error).strip().splitlines()
    detail = message[0] if message else type(error).__name__
    if len(detail) <= _MAX_ERROR_DETAIL_LENGTH:
        return detail
    return detail[: _MAX_ERROR_DETAIL_LENGTH - 1].rstrip() + "…"


def success_message(snapshot: ResultSnapshot, path: Path) -> str:
    """Describe a successful loaded-row export and any remaining rows."""
    message = f"Exported {len(snapshot.rows)} loaded row(s) to {path}"
    if snapshot.has_more:
        message += "; additional rows are available"
    return message


def format_export_value(
    value: str,
    *,
    null_value: str,
    date_format: str,
) -> str:
    """Apply shared NULL and strict ISO-display transformations to one cell.

    Plugin API v1 carries dates as display strings; its optional numeric
    provenance does not identify date or timestamp values.  Date formatting
    therefore applies only to the exact ISO shapes emitted by PLSQLWKS.  A text
    cell with the same shape is indistinguishable and is formatted as well;
    invalid or nonmatching values remain unchanged.
    """
    if value == _NULL_DISPLAY_VALUE:
        return null_value
    if not date_format:
        return value

    try:
        if _ISO_DATE_DISPLAY_RE.fullmatch(value):
            parsed: date | datetime = date.fromisoformat(value)
        elif _ISO_TIMESTAMP_DISPLAY_RE.fullmatch(value):
            parsed = datetime.fromisoformat(value)
        else:
            return value
    except ValueError:
        return value
    return parsed.strftime(date_format)


def format_export_rows(
    rows: Sequence[Sequence[str]],
    *,
    null_value: str,
    date_format: str,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Return transformed row copies without mutating a result snapshot."""
    raise_if_export_cancelled(cancelled)
    formatted_rows: list[tuple[str, ...]] = []
    for row in rows:
        raise_if_export_cancelled(cancelled)
        formatted_row: list[str] = []
        for value in row:
            raise_if_export_cancelled(cancelled)
            formatted_row.append(
                format_export_value(
                    value,
                    null_value=null_value,
                    date_format=date_format,
                )
            )
        formatted_rows.append(tuple(formatted_row))
    raise_if_export_cancelled(cancelled)
    return tuple(formatted_rows)


def prepare_result_export(
    context: PluginContext,
    label: str,
    default_filename: str,
) -> tuple[ResultSnapshot, Path] | None:
    """Apply the common prompt workflow and return an immutable export target.

    The insert-draft check intentionally precedes snapshot creation so a
    temporary draft row can never cross the plugin boundary.  A ``None``
    return means the helper has already published the appropriate no-result or
    cancellation status.  Path-resolution and filesystem errors propagate to
    the format-specific handler so it can report an appropriately titled
    failure through :class:`~plsqlwks.plugins.api.PluginContext`.
    """
    snapshot = result_snapshot_for_export(context)
    if snapshot is None:
        return None
    path = prepare_export_path(context, label, default_filename)
    if path is None:
        return None
    return snapshot, path


def result_snapshot_for_export(context: PluginContext) -> ResultSnapshot | None:
    """Validate the active result without prompting for a destination."""
    if context.has_active_insert_draft():
        context.set_status(_INSERT_DRAFT_STATUS)
        return None

    snapshot = context.get_active_result()
    if snapshot is None or not snapshot.columns:
        context.set_status(_NO_RESULT_STATUS)
        return None
    return snapshot


def prepare_export_path(
    context: PluginContext,
    label: str,
    default_filename: str,
) -> Path | None:
    """Prompt for and validate a destination for an already-approved result."""

    results_dir = context.results_dir
    default_path = results_dir / default_filename
    response = context.prompt_text(label, str(default_path), strip=True)
    if not response:
        context.set_status(_CANCELLED_STATUS)
        return None

    path = resolve_export_path(response, results_dir)
    if path.exists() and not context.confirm_overwrite(path):
        context.set_status(_CANCELLED_STATUS)
        return None
    return path
