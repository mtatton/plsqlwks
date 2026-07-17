from __future__ import annotations

import contextlib
import curses
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..db import (
    NULL_DISPLAY_TOKEN,
    ExplainPlanResult,
    ExplainPlanStep,
    QueryResult,
    TransactionReport,
    TruncatedLobValue,
    edit_metadata_rejection_reason,
)
from .constants import (
    PLAN_CONNECTOR,
    PLAN_METRICS,
    PLAN_OBJECT,
    PLAN_OPERATION,
    PLAN_TEXT,
    RESULT_PANE_LAYOUTS,
    RESULT_RATIO_EDITOR_FULLSCREEN,
    RESULT_RATIO_EPSILON,
    RESULT_RATIO_FULLSCREEN,
    RESULT_ROW_DETAIL,
)
from .display import clip_text, display_width, fit_text, wrap_display_text
from .sql import INSERT_ROWID_MARKER

if TYPE_CHECKING:
    from .state import UIState


@dataclass(frozen=True)
class ResultPosition:
    row: int = 0
    col: int = 0
    row_scroll: int = 0
    col_scroll: int = 0


@dataclass(frozen=True)
class ExplainPlanSegment:
    text: str
    kind: str


@dataclass(frozen=True)
class ExplainPlanLine:
    segments: list[ExplainPlanSegment]

    @property
    def text(self) -> str:
        return "".join(segment.text for segment in self.segments)


@dataclass(frozen=True)
class EditableCell:
    table_name: str
    table_column: str
    rowid: str
    current_value: str
    original_value: Any


@dataclass(frozen=True)
class ResultCell:
    column_name: str
    row_idx: int
    col_idx: int
    value: str


@dataclass
class ResultInsertDraft:
    result: QueryResult
    row_index: int
    row: list[str]


@dataclass(frozen=True)
class VisibleColumn:
    index: int
    x: int
    width: int


def result_pane_is_fullscreen(result_ratio: float) -> bool:
    return result_ratio >= RESULT_RATIO_FULLSCREEN - RESULT_RATIO_EPSILON


def result_pane_is_editor_fullscreen(result_ratio: float) -> bool:
    return result_ratio <= RESULT_RATIO_EDITOR_FULLSCREEN + RESULT_RATIO_EPSILON


def result_pane_tab_height(result_ratio: float) -> int:
    return 0 if result_pane_is_fullscreen(result_ratio) or result_pane_is_editor_fullscreen(result_ratio) else 1


def editor_result_pane_heights(content_height: int, result_ratio: float) -> tuple[int, int]:
    content_height = max(0, content_height)
    if result_pane_is_editor_fullscreen(result_ratio):
        return content_height, 0
    if result_pane_is_fullscreen(result_ratio):
        return 0, content_height
    result_h = max(4, int(content_height * result_ratio))
    editor_h = max(3, content_height - result_h)
    if editor_h + result_h > content_height:
        result_h = content_height - editor_h
    return max(0, editor_h), max(0, result_h)


def next_result_pane_ratio(result_ratio: float) -> float:
    for idx, (layout_ratio, _) in enumerate(RESULT_PANE_LAYOUTS):
        if abs(result_ratio - layout_ratio) <= RESULT_RATIO_EPSILON:
            return RESULT_PANE_LAYOUTS[(idx + 1) % len(RESULT_PANE_LAYOUTS)][0]
    for layout_ratio, _ in RESULT_PANE_LAYOUTS:
        if result_ratio < layout_ratio - RESULT_RATIO_EPSILON:
            return layout_ratio
    return RESULT_PANE_LAYOUTS[0][0]


def result_pane_status(result_ratio: float) -> str:
    for layout_ratio, name in RESULT_PANE_LAYOUTS:
        if abs(result_ratio - layout_ratio) <= RESULT_RATIO_EPSILON:
            return f"Results pane: {name}"
    return f"Results pane: {result_ratio:.0%}"


def selected_result_cell(result: QueryResult, row_idx: int, col_idx: int) -> tuple[ResultCell | None, str]:
    if not result.columns:
        return None, "No table result is available"
    if not result.rows:
        return None, "No row selected"
    if row_idx < 0 or row_idx >= len(result.rows):
        return None, "No row selected"
    if col_idx < 0 or col_idx >= len(result.columns):
        return None, "No column selected"
    row = result.rows[row_idx]
    if col_idx >= len(row):
        return None, "Selected cell is empty"
    return (
        ResultCell(
            column_name=result.columns[col_idx],
            row_idx=row_idx,
            col_idx=col_idx,
            value=row[col_idx],
        ),
        "",
    )


def cell_view_lines(cell: ResultCell, width: int) -> list[str]:
    width = max(1, width)
    value_lines = wrap_display_text(cell.value, width) or [""]
    return [
        f"Column: {cell.column_name}",
        f"Position: row {cell.row_idx + 1}, col {cell.col_idx + 1}",
        "",
        *value_lines,
    ]


def clamp_cell_view_scroll(scroll: int, total_lines: int, visible_lines: int) -> int:
    if total_lines <= 0 or visible_lines <= 0 or total_lines <= visible_lines:
        return 0
    return min(max(scroll, 0), total_lines - visible_lines)


def scroll_start(scroll: int | None, total_lines: int, visible_lines: int) -> int:
    if scroll is None:
        return clamp_cell_view_scroll(total_lines, total_lines, visible_lines)
    return clamp_cell_view_scroll(scroll, total_lines, visible_lines)


def wrapped_dbms_output_lines(lines: list[str], width: int) -> list[str]:
    wrapped: list[str] = []
    for line in lines:
        if not line:
            wrapped.append("")
            continue
        wrapped.extend(wrap_display_text(line, max(1, width)) or [""])
    return wrapped


def safe_window_addstr(window: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = window.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    with contextlib.suppress(curses.error):
        window.addstr(y, x, clip_text(text, max(0, width - x)), attr)


def selected_editable_cell(result: QueryResult, row_idx: int, col_idx: int) -> tuple[EditableCell | None, str]:
    context = result.editable_context
    if context is None:
        return None, result.edit_message or "Result is not ROWID-editable"
    if not result.rows:
        return None, "No row selected"
    if row_idx < 0 or row_idx >= len(result.rows):
        return None, "No row selected"
    if col_idx < 0 or col_idx >= len(result.columns):
        return None, "No column selected"
    if col_idx == context.rowid_column:
        return None, "ROWID column is read-only"
    table_column = context.editable_columns.get(col_idx)
    if table_column is None:
        return None, "Selected column is not editable"
    rejection = edit_metadata_rejection_reason(context.column_metadata.get(col_idx))
    if rejection:
        return None, f"{table_column}: {rejection}"
    row = result.rows[row_idx]
    if context.rowid_column >= len(row):
        return None, "Selected row has no ROWID"
    if col_idx >= len(row):
        return None, "Selected cell is empty"
    rowid = row[context.rowid_column]
    if not rowid or rowid == NULL_DISPLAY_TOKEN:
        return None, "Selected row has no ROWID"
    original_value: Any = row[col_idx]
    if result.original_rows:
        if row_idx >= len(result.original_rows) or col_idx >= len(result.original_rows[row_idx]):
            return None, "Original result value is unavailable; refresh the query"
        original_value = result.original_rows[row_idx][col_idx]
    if isinstance(original_value, TruncatedLobValue):
        return None, f"{original_value.type_name} value is truncated and cannot be safely edited"
    return (
        EditableCell(
            table_name=context.table_name,
            table_column=table_column,
            rowid=rowid,
            current_value=row[col_idx],
            original_value=original_value,
        ),
        "",
    )


def insert_draft_row(result: QueryResult) -> list[str]:
    context = result.editable_context
    row = [NULL_DISPLAY_TOKEN] * len(result.columns)
    if context is not None and 0 <= context.rowid_column < len(row):
        row[context.rowid_column] = INSERT_ROWID_MARKER
    return row


def first_editable_result_column(result: QueryResult) -> int:
    context = result.editable_context
    if context is None or not context.editable_columns:
        return 0
    return min(context.editable_columns)


def insert_draft_active_status() -> str:
    return "Insert draft active: Enter edits cell, Ctrl-Alt-C inserts, Esc cancels"


def is_autocommit_enabled(db: object) -> bool:
    return bool(getattr(db, "autocommit", True))


def is_read_only_enabled(db: object) -> bool:
    return bool(getattr(db, "read_only", False))


def is_database_connected(db: object) -> bool:
    connected = getattr(db, "connected", None)
    return True if connected is None else bool(connected)


def has_uncommitted_changes(db: object) -> bool:
    return bool(getattr(db, "has_uncommitted_changes", False))


def transaction_pending_indicator(db: object) -> str:
    return "[*]" if has_uncommitted_changes(db) else "[ ]"


def transaction_mode_name(db: object) -> str:
    return "autocommit" if is_autocommit_enabled(db) else "manual"


def access_mode_name(db: object) -> str:
    return "read-only" if is_read_only_enabled(db) else "read-write"


def format_elapsed_hhmmss(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def active_operation_status(state: UIState) -> str:
    operation = state.db_operation
    if operation is None:
        return state.status
    elapsed = time.monotonic() - operation.started_at
    suffix = ""
    if operation.cancel_requested:
        suffix = " (interrupt requested)" if operation.interrupt_database else " (cancellation requested)"
    progress = ""
    current = operation.progress_current
    total = operation.progress_total
    if current is not None and total is not None and total >= 0:
        bounded_current = min(max(0, current), total) if total else 0
        percent = 100 if total == 0 else int(bounded_current * 100 / total)
        filled = int(percent * 20 / 100)
        bar = "#" * filled + "-" * (20 - filled)
        progress = f" [{bar}] {percent}% ({current:,}/{total:,})"
    return f"{operation.label}{progress}{suffix} {format_elapsed_hhmmss(elapsed)}"


def transaction_rows_changed_text(report: TransactionReport) -> str:
    if report.has_unknown_changes:
        if report.rows_changed > 0:
            return f"{report.rows_changed}+ row(s) changed"
        return "unknown row(s) changed"
    return f"{report.rows_changed} row(s) changed"


def transaction_report_status(action: str, report: TransactionReport) -> str:
    timestamp = report.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    return f"{action}, {timestamp}, {transaction_rows_changed_text(report)}"


def set_db_autocommit(db: object, enabled: bool) -> None:
    setter = getattr(db, "set_autocommit", None)
    if callable(setter):
        setter(enabled)
        return
    with suppress(Exception):
        setattr(db, "autocommit", enabled)  # noqa: B010  # reason: legacy database doubles expose autocommit as a dynamic compatibility attribute


def is_dbms_output_result(result: QueryResult) -> bool:
    """Return whether *result* carries explicit output without a table result.

    Kept for compatibility with script-result bookkeeping.  A query can carry
    DBMS_OUTPUT alongside real columns, so callers that aggregate output should
    read ``result.dbms_output`` directly instead of using this predicate.
    """
    return not result.columns and bool(result.dbms_output or result.dbms_output_error)


def more_rows_status(result: QueryResult, connected: bool) -> str:
    if not result.has_more_rows:
        return ""
    if connected and result.continuation is not None:
        return "More rows available; PageDown fetches more"
    if result.detached_reason:
        return f"More rows were not loaded; {result.detached_reason}"
    if connected:
        return "More rows were not loaded; rerun the query"
    return "More rows were not loaded; reconnect and rerun the query"


def close_query_result_continuation(result: QueryResult | None) -> None:
    """Detach an opaque continuation from a result.

    Cursor cleanup is performed by ``App.release_result_continuation()`` on the
    database worker.  This compatibility helper intentionally has no Oracle
    object to close.
    """
    if result is None or result.continuation is None:
        return
    result.continuation = None


def result_label(
    result: QueryResult,
    mode: str,
    focused: bool,
    has_dbms_output: bool = False,
    read_only: bool = False,
    connected: bool = True,
) -> str:
    marker = ">" if focused else "-"
    mode_name = "row" if mode == RESULT_ROW_DETAIL else "grid"
    other = "grid" if mode == RESULT_ROW_DETAIL else "row"
    if result.detached_reason:
        edit = f" | view-only: {result.detached_reason}"
    elif not connected:
        edit = " | disconnected: view-only"
    elif result.editable_context is not None and not read_only:
        edit = " | Enter edit | INS row"
    else:
        edit = ""
    output = " | F6 output" if has_dbms_output else ""
    paging = more_rows_status(result, connected)
    paging_hint = f" | {paging}" if paging else ""
    return (
        f" Results {marker} {mode_name} | {result.title}: {result.message}"
        f"{paging_hint} | Tab editor{output} | F8 {other} | F10 view{edit} | Ctrl-C copy "
    )


def explain_plan_label(result: ExplainPlanResult, focused: bool, has_lines: bool) -> str:
    marker = ">" if focused else "-"
    scroll = " | Up/Down scroll" if has_lines else ""
    return f" Explain Plan {marker} | {result.title}: {result.message} | Tab editor{scroll} "


def explain_plan_lines(result: ExplainPlanResult) -> list[ExplainPlanLine]:
    if result.steps:
        return explain_plan_tree_lines(result.steps)
    if result.raw_lines:
        return [ExplainPlanLine([ExplainPlanSegment(line, PLAN_TEXT)]) for line in result.raw_lines]
    return explain_plan_tree_lines([])


def explain_plan_tree_lines(steps: list[ExplainPlanStep]) -> list[ExplainPlanLine]:
    if not steps:
        return [ExplainPlanLine([ExplainPlanSegment("(no plan rows)", PLAN_METRICS)])]

    by_id = {step.id: step for step in steps}
    children: dict[int, list[int]] = {step.id: [] for step in steps}
    roots: list[int] = []
    for step in sorted(steps, key=lambda item: item.id):
        if step.parent_id is not None and step.parent_id in by_id:
            children.setdefault(step.parent_id, []).append(step.id)
        else:
            roots.append(step.id)
    for child_ids in children.values():
        child_ids.sort()

    lines: list[ExplainPlanLine] = []
    visited: set[int] = set()
    show_root_connectors = len(roots) > 1

    def render(step_id: int, prefix: str, is_last: bool, is_root: bool) -> None:
        if step_id in visited:
            return
        visited.add(step_id)
        connector = "" if is_root else ("\\- " if is_last else "+- ")
        lines.append(explain_plan_line(by_id[step_id], prefix, connector))
        next_prefix = prefix if is_root else prefix + ("   " if is_last else "|  ")
        node_children = children.get(step_id, [])
        for idx, child_id in enumerate(node_children):
            render(child_id, next_prefix, idx == len(node_children) - 1, False)

    for idx, root_id in enumerate(roots):
        render(root_id, "", idx == len(roots) - 1, not show_root_connectors)
    for step in sorted(steps, key=lambda item: item.id):
        if step.id not in visited:
            render(step.id, "", True, False)
    return lines


def explain_plan_line(step: ExplainPlanStep, prefix: str, connector: str) -> ExplainPlanLine:
    segments: list[ExplainPlanSegment] = []
    branch = prefix + connector
    if branch:
        segments.append(ExplainPlanSegment(branch, PLAN_CONNECTOR))
    segments.append(ExplainPlanSegment(explain_plan_operation_text(step), PLAN_OPERATION))
    object_text = explain_plan_object_text(step)
    if object_text:
        segments.append(ExplainPlanSegment(f"  {object_text}", PLAN_OBJECT))
    metrics = explain_plan_metrics_text(step)
    if metrics:
        segments.append(ExplainPlanSegment(f"  {metrics}", PLAN_METRICS))
    return ExplainPlanLine(segments)


def explain_plan_operation_text(step: ExplainPlanStep) -> str:
    parts = [step.operation.strip(), step.options.strip()]
    text = " ".join(part for part in parts if part)
    return text or f"Step {step.id}"


def explain_plan_object_text(step: ExplainPlanStep) -> str:
    name = step.object_name.strip()
    if not name:
        return ""
    owner = step.object_owner.strip()
    object_name = f"{owner}.{name}" if owner else name
    object_type = step.object_type.strip()
    return f"{object_name} ({object_type})" if object_type else object_name


def explain_plan_metrics_text(step: ExplainPlanStep) -> str:
    metrics = []
    if step.cost:
        metrics.append(f"cost={step.cost}")
    if step.cardinality:
        metrics.append(f"rows={step.cardinality}")
    if step.bytes:
        metrics.append(f"bytes={step.bytes}")
    if step.time:
        metrics.append(f"time={step.time}")
    return "[" + ", ".join(metrics) + "]" if metrics else ""


def clamp_result_position(
    result: QueryResult | None,
    row: int,
    col: int,
    row_scroll: int,
    col_scroll: int,
) -> ResultPosition:
    if result is None or not result.columns:
        return ResultPosition()
    max_row = max(0, len(result.rows) - 1)
    max_col = max(0, len(result.columns) - 1)
    row = min(max(row, 0), max_row)
    col = min(max(col, 0), max_col)
    row_scroll = min(max(row_scroll, 0), max_row)
    col_scroll = min(max(col_scroll, 0), max_col)
    return ResultPosition(row=row, col=col, row_scroll=row_scroll, col_scroll=col_scroll)


def table_column_widths(result: QueryResult, max_width: int = 40) -> list[int]:
    widths = [max(3, display_width(col)) for col in result.columns]
    for row in result.rows:
        for idx, cell in enumerate(row[: len(widths)]):
            widths[idx] = min(max(widths[idx], display_width(cell)), max_width)
    return widths


def visible_table_columns(widths: list[int], col_scroll: int, available_width: int) -> list[VisibleColumn]:
    if available_width <= 0 or not widths:
        return []
    col_scroll = min(max(col_scroll, 0), len(widths) - 1)
    visible: list[VisibleColumn] = []
    x = 0
    for idx in range(col_scroll, len(widths)):
        sep_width = 3 if visible else 0
        remaining = available_width - x - sep_width
        if remaining <= 0:
            break
        x += sep_width
        width = min(widths[idx], remaining)
        if width <= 0:
            break
        visible.append(VisibleColumn(index=idx, x=x, width=width))
        x += width
    return visible


def row_detail_lines(result: QueryResult, row_idx: int, width: int, col_scroll: int = 0) -> list[tuple[int, str]]:
    if not result.columns:
        return [(-1, "No table result.")]
    if not result.rows:
        return [(-1, "No rows.")]
    row_idx = min(max(row_idx, 0), len(result.rows) - 1)
    col_scroll = min(max(col_scroll, 0), len(result.columns) - 1)
    row = result.rows[row_idx]
    output: list[tuple[int, str]] = []
    for col_idx in range(col_scroll, len(result.columns)):
        column = result.columns[col_idx]
        value = row[col_idx] if col_idx < len(row) else ""
        wrapped = wrap_display_text(f"{column} = {value}", width) or [""]
        output.extend((col_idx, line) for line in wrapped)
    return output


def format_table(columns: list[str], rows: list[list[str]]) -> list[str]:
    widths = [display_width(col) for col in columns]
    for row in rows:
        for idx, cell in enumerate(row[: len(widths)]):
            widths[idx] = min(max(widths[idx], display_width(cell)), 40)
    header = " | ".join(fit_text(col, widths[idx]) for idx, col in enumerate(columns))
    sep = "-+-".join("-" * width for width in widths)
    output = [header, sep]
    for row in rows:
        output.append(
            " | ".join(fit_text(row[idx] if idx < len(row) else "", widths[idx]) for idx in range(len(columns)))
        )
    return output
