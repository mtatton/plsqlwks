from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..db import ExplainPlanResult, OracleExecutionError, QueryResult
from .constants import (
    FOCUS_EDITOR,
    FOCUS_RESULTS,
    RESULT_ROW_DETAIL,
    RESULT_STYLE_HELP,
)
from .display import wrap_display_text
from .errors import (
    ErrorLocation,
    document_error_diagnostics,
    execution_error_lines,
    is_execution_interrupted,
    move_buffer_to_error,
    short_execution_error_message,
    wrap_error,
)
from .help import build_help_lines
from .ports import DbOperationsPort
from .query_controller import error_location_line
from .results import (
    ResultInsertDraft,
    ResultPosition,
    clamp_result_position,
    explain_plan_lines,
    insert_draft_active_status,
    is_database_connected,
    is_read_only_enabled,
    more_rows_status,
)
from .state import (
    DbOperationFinished,
    ExecutionDiagnostic,
    FileTab,
    ResultFetchMore,
    UIState,
)

RESULT_LINE_ORIGIN_RE = re.compile(r"\blines\s+(\d+)-\d+\b", re.IGNORECASE)
SOURCE_CHANGED_NOTICE = (
    "Source changed since execution started; results and diagnostics refer to an earlier buffer revision."
)
PAGING_TRANSCRIPT_PREFIXES = (
    "More rows available;",
    "More rows were not loaded;",
)


def _default_screen_width() -> int:
    return 121


class ResultPresenter:
    """Apply database results to the shared UI state on the curses thread."""

    def __init__(
        self,
        state: UIState,
        db_operations: DbOperationsPort,
        *,
        screen_width: Callable[[], int] = _default_screen_width,
    ) -> None:
        self.state = state
        self.db_operations = db_operations
        self._screen_width = screen_width

    def apply_db_operation_result(self, event: DbOperationFinished) -> None:
        if event.error is not None:
            if event.kind == "explain":
                self.handle_explain_error(
                    event.error,
                    event.statement_start_line,
                    event.statement_start_col,
                    source_text=event.source_text,
                    source_unchanged=event.source_unchanged,
                    interrupted=event.interrupted,
                )
                return
            if event.kind == "fetch-more":
                self.handle_fetch_more_error(
                    event.error,
                    interrupted=event.interrupted,
                )
                return
            self.handle_execution_error(
                event.error,
                event.statement_start_line,
                event.statement_start_col,
                event.partial_results,
                source_text=event.source_text,
                source_unchanged=event.source_unchanged,
                statement_count=event.statement_count,
                failed_statement_index=event.failed_statement_index,
                interrupted=event.interrupted,
            )
            return
        if event.kind == "explain":
            self.show_explain_result(
                event.result,
                source_text=event.source_text,
                source_unchanged=event.source_unchanged,
            )
            return
        if event.kind == "fetch-more":
            self.apply_fetch_more_result(event.result)
            return
        self.finish_execution(
            event.result,
            source_text=event.source_text,
            source_unchanged=event.source_unchanged,
            statement_start_line=event.statement_start_line,
            statement_start_col=event.statement_start_col,
            statement_count=event.statement_count,
        )

    def finish_execution(
        self,
        results: list[QueryResult],
        *,
        source_text: str | None = None,
        source_unchanged: bool = True,
        statement_start_line: int = 1,
        statement_start_col: int = 0,
        statement_count: int = 1,
    ) -> None:
        self.render_results(results)
        self._record_success_diagnostics(
            results,
            source_text,
            statement_start_line,
            statement_start_col,
        )
        status = results[-1].message if results else "Done"
        if statement_count > 1:
            status = f"Completed {statement_count}/{statement_count}: {status}"
        if not source_unchanged:
            self._append_source_changed_notice()
            status = f"{status} | {SOURCE_CHANGED_NOTICE}"
        self.state.status = status

    def apply_fetch_more_result(self, fetched: ResultFetchMore) -> None:
        result = self.state.active_result
        if result is not fetched.result:
            self.release_result_continuation(fetched.result)
            self.state.status = "Fetched rows discarded because the result changed"
            return
        result.rows.extend(fetched.page.rows)
        if fetched.page.original_rows:
            result.original_rows.extend(fetched.page.original_rows)
        elif result.original_rows:
            result.original_rows.extend([list(row) for row in fetched.page.rows])
        result.continuation = fetched.page.continuation
        result.has_more_rows = fetched.page.has_more_rows
        if fetched.page.dbms_output:
            result.dbms_output.extend(fetched.page.dbms_output)
            self.state.dbms_output.extend(
                self._output_view_lines(
                    result.title,
                    fetched.page.dbms_output,
                    self.state.active_tab.dbms_output_grouped,
                )
            )
        if fetched.page.dbms_output_error:
            if result.dbms_output_error:
                result.dbms_output_error += f"; {fetched.page.dbms_output_error}"
            else:
                result.dbms_output_error = fetched.page.dbms_output_error
        result.warnings.extend(fetched.page.warnings)
        result.message = self._cumulative_page_message(result, fetched.page)
        self.refresh_result_summary_line(result)
        self.state.show_dbms_output = False
        self.state.focus = FOCUS_RESULTS
        if result.rows:
            self._move_result_selection(to_row=min(fetched.target_row, len(result.rows) - 1))
        self._update_result_status()
        self.state.status = f"{result.message} | {self.state.status}"

    def handle_fetch_more_error(
        self,
        exc: Exception,
        *,
        interrupted: bool = False,
    ) -> None:
        self._merge_error_dbms_output(exc)
        error_lines = execution_error_lines(exc)
        self.set_results([*self.state.results, *error_lines], clear_table=False)
        message = short_execution_error_message(exc)
        interrupted = (
            interrupted
            or bool(getattr(self.db_operations, "completion_interrupted", False))
            or is_execution_interrupted(exc)
        )
        status = "Fetch rows interrupted" if interrupted else "Fetch rows failed"
        self.state.status = f"{status}: {message}" if message else status
        if self.state.active_result is not None:
            self.state.active_result.continuation = None
            self.state.focus = FOCUS_RESULTS

    def refresh_result_summary_line(self, result: QueryResult) -> None:
        prefix = f"[{result.title}] "
        for idx, line in enumerate(self.state.results):
            if line.startswith(prefix):
                self.state.results[idx] = f"{prefix}{result.message}"
                next_idx = idx + 1
                if next_idx < len(self.state.results) and self.state.results[next_idx].startswith(
                    PAGING_TRANSCRIPT_PREFIXES
                ):
                    self.state.results.pop(next_idx)
                paging = more_rows_status(
                    result,
                    is_database_connected(self.state.db),
                )
                if paging:
                    self.state.results.insert(next_idx, paging)
                return

    def close_tab_result_continuations(self, tab: FileTab) -> None:
        seen: set[int] = set()
        for result in (tab.active_result, tab.last_result):
            if result is None or id(result) in seen:
                continue
            seen.add(id(result))
            self.release_result_continuation(result)

    def close_all_result_continuations(self) -> None:
        for tab in self.state.tabs:
            self.close_tab_result_continuations(tab)

    def release_result_continuation(self, result: QueryResult | None) -> None:
        if result is None or result.continuation is None:
            return
        continuation = result.continuation
        result.continuation = None

        def close_continuation(db: Any, progress: Callable[[str], None]) -> None:
            close = getattr(db, "close_result_continuation", None)
            if callable(close):
                close(continuation)

        self.db_operations.submit_background(close_continuation)

    def invalidate_results_after_rollback(self) -> None:
        for tab in self.state.tabs:
            seen: set[int] = set()
            for result in (tab.active_result, tab.last_result):
                if result is None or id(result) in seen:
                    continue
                seen.add(id(result))
                result.continuation = None
            tab.active_result = None
            tab.last_result = None
            tab.result_insert_draft = None
            tab.result_row = 0
            tab.result_col = 0
            tab.result_row_scroll = 0
            tab.result_col_scroll = 0
        if self.state.focus == FOCUS_RESULTS and self.state.explain_result is None:
            self.state.focus = FOCUS_EDITOR

    def run_with_errors(self, runner: Callable[[], list[QueryResult]]) -> None:
        try:
            self.finish_execution(runner())
        except Exception as exc:
            self.handle_execution_error(exc)

    def handle_execution_error(
        self,
        exc: Exception,
        statement_start_line: int = 1,
        statement_start_col: int = 0,
        partial_results: list[QueryResult] | None = None,
        *,
        source_text: str | None = None,
        source_unchanged: bool = True,
        statement_count: int = 1,
        failed_statement_index: int | None = None,
        interrupted: bool = False,
    ) -> None:
        interrupted = (
            interrupted
            or bool(getattr(self.db_operations, "completion_interrupted", False))
            or is_execution_interrupted(exc)
        )
        diagnostic_details = document_error_diagnostics(
            exc,
            statement_start_line,
            statement_start_col,
        )
        locations = [location for location, _ in diagnostic_details]
        message = short_execution_error_message(exc)
        self._replace_execution_diagnostics(diagnostic_details, source_text)
        diagnostic_location = locations[0] if locations else None
        moved_location = None
        if source_unchanged and diagnostic_location is not None:
            moved_location = move_buffer_to_error(self.state.buffer, diagnostic_location)
            self.state.active_tab.execution_diagnostic_index = 0
        error_lines = execution_error_lines(exc)
        if diagnostic_location is not None:
            display_location = moved_location or diagnostic_location
            error_lines.insert(1, error_location_line(display_location))
        if not source_unchanged:
            error_lines.insert(1, SOURCE_CHANGED_NOTICE)
        if partial_results:
            self.render_results(
                partial_results,
                group_dbms_output=statement_count > 1,
            )
            lines = [*self.state.results, *error_lines]
            self.set_results(lines, clear_table=False)
        else:
            self.set_results(
                error_lines,
                clear_table=(
                    (is_database_connected(self.state.db) and not interrupted) or self.state.active_result is None
                ),
            )
        if statement_count > 1:
            self._merge_error_dbms_output(exc, force_grouped=True)
        self.state.show_dbms_output = False
        self.state.focus = FOCUS_EDITOR
        status = "Execution interrupted" if interrupted else "Execution failed"
        shown_location = moved_location or diagnostic_location
        if shown_location is not None:
            status = f"{status} at line {shown_location.line}, column {shown_location.column}"
        suffix = f": {message}" if message else ""
        status = f"{status}{suffix}"
        if failed_statement_index is not None:
            status = f"{status} | stopped at {failed_statement_index}/{statement_count}"
        if not source_unchanged:
            status = f"{status} | {SOURCE_CHANGED_NOTICE}"
        self.state.status = status

    def render_results(
        self,
        results: list[QueryResult],
        *,
        group_dbms_output: bool | None = None,
    ) -> None:
        self._discard_insert_draft()
        output: list[str] = []
        dbms_output: list[str] = []
        active_result: QueryResult | None = None
        previous_result = self.state.active_result
        if previous_result is not None and not any(result is previous_result for result in results):
            self.release_result_continuation(previous_result)
        self.state.explain_result = None
        self.state.explain_scroll = 0
        grouped_output = len(results) > 1 if group_dbms_output is None else group_dbms_output
        for result in results:
            self.state.last_result = result
            output.append(f"[{result.title}] {result.message}")
            paging = more_rows_status(
                result,
                is_database_connected(self.state.db),
            )
            if paging:
                output.append(paging)
            if result.dbms_output:
                dbms_output.extend(
                    self._output_view_lines(
                        result.title,
                        result.dbms_output,
                        grouped_output,
                    )
                )
                output.extend(result.dbms_output)
            if result.dbms_output_error:
                output.append(f"DBMS_OUTPUT read failed: {result.dbms_output_error}")
            for diagnostic in getattr(result, "diagnostics", ()):
                severity = str(getattr(diagnostic, "severity", "warning")).capitalize()
                output.append(
                    f"{severity} at line {getattr(diagnostic, 'line', 1)}, "
                    f"column {getattr(diagnostic, 'position', 1)}: "
                    f"{getattr(diagnostic, 'text', '')}"
                )
            if result.columns:
                active_result = result
                if result.editable_context is not None and not is_read_only_enabled(self.state.db):
                    output.append(
                        "Tab opens the result grid. F10 views the full selected cell. "
                        "Ctrl-C copies the selected cell. "
                        "Enter edits the selected ROWID-backed cell. "
                        "INS prepares a draft insert row."
                    )
                else:
                    output.append(
                        "Tab opens the result grid. F10 views the full selected cell. "
                        "Ctrl-C copies the selected cell. "
                        "F8 toggles row detail."
                    )
            output.append("")
        for result in results:
            if result is not active_result:
                self.release_result_continuation(result)
        self.set_results(output, clear_table=False)
        self.state.active_result = active_result
        self.state.dbms_output = dbms_output
        self.state.active_tab.dbms_output_grouped = grouped_output
        self.state.active_tab.dbms_output_scroll = None
        self.state.show_dbms_output = bool(dbms_output) and active_result is None
        self.state.focus = FOCUS_EDITOR
        self.state.result_row = 0
        self.state.result_col = 0
        self.state.result_row_scroll = 0
        self.state.result_col_scroll = 0
        self._clamp_result_selection()

    def show_explain_result(
        self,
        result: ExplainPlanResult,
        *,
        source_text: str | None = None,
        source_unchanged: bool = True,
    ) -> None:
        self._discard_insert_draft()
        self.release_result_continuation(self.state.active_result)
        self.state.explain_result = result
        self.state.active_result = None
        self.state.dbms_output = []
        self.state.active_tab.dbms_output_grouped = False
        self.state.active_tab.dbms_output_scroll = None
        self.state.show_dbms_output = False
        self.state.focus = FOCUS_EDITOR
        self.state.explain_scroll = 0
        self.state.results = [
            f"[{result.title}] {result.message}",
            *[line.text for line in explain_plan_lines(result)],
        ]
        self.state.active_tab.results_scroll = None
        self._clear_execution_diagnostics()
        if source_unchanged:
            self.state.status = result.message
            return
        self._append_source_changed_notice()
        self.state.status = f"{result.message} | {SOURCE_CHANGED_NOTICE}"

    def handle_explain_error(
        self,
        exc: Exception,
        statement_start_line: int = 1,
        statement_start_col: int = 0,
        *,
        source_text: str | None = None,
        source_unchanged: bool = True,
        interrupted: bool = False,
    ) -> None:
        interrupted = (
            interrupted
            or bool(getattr(self.db_operations, "completion_interrupted", False))
            or is_execution_interrupted(exc)
        )
        diagnostic_details = document_error_diagnostics(
            exc,
            statement_start_line,
            statement_start_col,
        )
        locations = [location for location, _ in diagnostic_details]
        message = short_execution_error_message(exc)
        self._replace_execution_diagnostics(diagnostic_details, source_text)
        diagnostic_location = locations[0] if locations else None
        moved_location = None
        if source_unchanged and diagnostic_location is not None:
            moved_location = move_buffer_to_error(self.state.buffer, diagnostic_location)
            self.state.active_tab.execution_diagnostic_index = 0
        lines = ["ERROR explaining statement:", *wrap_error(exc)]
        if diagnostic_location is not None:
            lines.insert(1, error_location_line(moved_location or diagnostic_location))
        if not source_unchanged:
            lines.insert(1, SOURCE_CHANGED_NOTICE)
        self.set_results(
            lines,
            clear_table=(
                (is_database_connected(self.state.db) and not interrupted) or self.state.active_result is None
            ),
        )
        self.state.focus = FOCUS_EDITOR
        shown_location = moved_location or diagnostic_location
        status = "Explain interrupted" if interrupted else "Explain failed"
        if shown_location is not None:
            status = f"{status} at line {shown_location.line}, column {shown_location.column}"
        suffix = f": {message}" if message else ""
        status = f"{status}{suffix}"
        if not source_unchanged:
            status = f"{status} | {SOURCE_CHANGED_NOTICE}"
        self.state.status = status

    def _replace_execution_diagnostics(
        self,
        diagnostics: list[tuple[ErrorLocation, str]],
        source_text: str | None,
    ) -> None:
        tab = self.state.active_tab
        tab.execution_diagnostics = [
            ExecutionDiagnostic(location.line, location.column, message) for location, message in diagnostics
        ]
        tab.execution_diagnostic_index = -1
        tab.execution_diagnostic_source = self.state.buffer.text() if source_text is None else source_text

    def _record_success_diagnostics(
        self,
        results: list[QueryResult],
        source_text: str | None,
        statement_start_line: int,
        statement_start_col: int,
    ) -> None:
        diagnostics: list[ExecutionDiagnostic] = []
        for result in results:
            stored_origin_line = getattr(result, "statement_start_line", None)
            if stored_origin_line is None:
                match = RESULT_LINE_ORIGIN_RE.search(result.title)
                origin_line = int(match.group(1)) if match is not None else statement_start_line
            else:
                origin_line = int(stored_origin_line)
            default_col = statement_start_col if int(origin_line) == statement_start_line else 0
            stored_origin_col = getattr(result, "statement_start_col", None)
            origin_col = default_col if stored_origin_col is None else int(stored_origin_col)
            for diagnostic in getattr(result, "diagnostics", ()):
                relative_line = max(1, int(getattr(diagnostic, "line", 1)))
                column = max(1, int(getattr(diagnostic, "position", 1)))
                if relative_line == 1:
                    column += max(0, origin_col)
                severity = str(getattr(diagnostic, "severity", "warning")).capitalize()
                text = str(getattr(diagnostic, "text", "")).strip()
                message = f"{severity}: {text}" if text else severity
                diagnostics.append(
                    ExecutionDiagnostic(
                        int(origin_line) + relative_line - 1,
                        column,
                        message,
                    )
                )
        if not diagnostics:
            self._clear_execution_diagnostics()
            return
        tab = self.state.active_tab
        tab.execution_diagnostics = diagnostics
        tab.execution_diagnostic_index = -1
        tab.execution_diagnostic_source = self.state.buffer.text() if source_text is None else source_text

    def _clear_execution_diagnostics(self) -> None:
        tab = self.state.active_tab
        tab.execution_diagnostics = []
        tab.execution_diagnostic_index = -1
        tab.execution_diagnostic_source = None

    def _append_source_changed_notice(self) -> None:
        self.set_results(
            [*self.state.results, SOURCE_CHANGED_NOTICE],
            clear_table=False,
        )

    def set_results(self, lines: list[str], clear_table: bool = True) -> None:
        if clear_table:
            self._clear_table_result_state()
        wrapped: list[str] = []
        width = max(40, self._screen_width() - 1)
        for line in lines:
            if not line:
                wrapped.append("")
                continue
            wrapped.extend(wrap_display_text(line, width) or [""])
        self.state.results = wrapped
        self.state.active_tab.results_scroll = None

    def show_help(
        self,
        workspace_messages: list[str] | None = None,
        *,
        focus_results: bool = True,
    ) -> None:
        self._clear_table_result_state(FOCUS_RESULTS if focus_results else FOCUS_EDITOR)
        help_lines = build_help_lines(workspace_messages)
        self.state.active_tab.help_lines = help_lines
        self.state.results = [line.text for line in help_lines]
        self.state.results_style = RESULT_STYLE_HELP
        self.state.active_tab.results_scroll = 0
        self.state.status = "Help"

    def _clear_table_result_state(self, focus: str = FOCUS_EDITOR) -> None:
        self._discard_insert_draft()
        self.release_result_continuation(self.state.active_result)
        self.state.active_result = None
        self.state.explain_result = None
        self.state.explain_scroll = 0
        self.state.dbms_output = []
        self.state.active_tab.dbms_output_grouped = False
        self.state.show_dbms_output = False
        self.state.focus = focus
        self.state.result_row = 0
        self.state.result_col = 0
        self.state.result_row_scroll = 0
        self.state.result_col_scroll = 0
        self.state.active_tab.dbms_output_scroll = None

    @staticmethod
    def _output_view_lines(
        title: str,
        lines: list[str],
        grouped: bool,
    ) -> list[str]:
        if not grouped:
            return list(lines)
        return [f"[{title}] {line}" for line in lines]

    @staticmethod
    def _cumulative_page_message(result: QueryResult, page: Any) -> str:
        page_suffix = f"; {len(page.dbms_output)} dbms_output line(s)" if page.dbms_output else ""
        page_suffix += "".join(f"; warning: {warning}" for warning in page.warnings)
        base_message = page.message
        if page_suffix and base_message.endswith(page_suffix):
            base_message = base_message[: -len(page_suffix)]
        cumulative_suffix = f"; {len(result.dbms_output)} dbms_output line(s)" if result.dbms_output else ""
        cumulative_suffix += "".join(f"; warning: {warning}" for warning in result.warnings)
        return f"{base_message}{cumulative_suffix}"

    def _merge_error_dbms_output(
        self,
        exc: Exception,
        *,
        force_grouped: bool = False,
    ) -> None:
        if not isinstance(exc, OracleExecutionError):
            return
        result = self.state.active_result
        if result is not None and not force_grouped:
            result.dbms_output.extend(exc.dbms_output)
            if exc.dbms_output_error:
                if result.dbms_output_error:
                    result.dbms_output_error += f"; {exc.dbms_output_error}"
                else:
                    result.dbms_output_error = exc.dbms_output_error
            result.warnings.extend(exc.warnings)
        if force_grouped:
            self.state.active_tab.dbms_output_grouped = True
        grouped = self.state.active_tab.dbms_output_grouped
        if exc.dbms_output:
            self.state.dbms_output.extend(self._output_view_lines(exc.title, exc.dbms_output, grouped))

    def _active_insert_draft(self) -> ResultInsertDraft | None:
        draft = self.state.active_tab.result_insert_draft
        result = self.state.active_result
        if draft is None:
            return None
        if (
            result is not None
            and draft.result is result
            and 0 <= draft.row_index < len(result.rows)
            and result.rows[draft.row_index] is draft.row
        ):
            return draft
        self.state.active_tab.result_insert_draft = None
        return None

    def _discard_insert_draft(self) -> None:
        draft = self._active_insert_draft()
        if draft is not None and (
            0 <= draft.row_index < len(draft.result.rows) and draft.result.rows[draft.row_index] is draft.row
        ):
            draft.result.rows.pop(draft.row_index)
            if draft.row_index < len(draft.result.original_rows):
                draft.result.original_rows.pop(draft.row_index)
        self.state.active_tab.result_insert_draft = None

    def _move_result_selection(
        self,
        *,
        to_row: int | None = None,
        to_col: int | None = None,
    ) -> None:
        row = self.state.result_row if to_row is None else to_row
        col = self.state.result_col if to_col is None else to_col
        self._apply_result_position(
            clamp_result_position(
                self.state.active_result,
                row,
                col,
                self.state.result_row_scroll,
                self.state.result_col_scroll,
            )
        )

    def _clamp_result_selection(self) -> None:
        self._apply_result_position(
            clamp_result_position(
                self.state.active_result,
                self.state.result_row,
                self.state.result_col,
                self.state.result_row_scroll,
                self.state.result_col_scroll,
            )
        )

    def _apply_result_position(self, position: ResultPosition) -> None:
        self.state.result_row = position.row
        self.state.result_col = position.col
        self.state.result_row_scroll = position.row_scroll
        self.state.result_col_scroll = position.col_scroll

    def _update_result_status(self) -> None:
        result = self.state.active_result
        if result is None:
            self.state.status = "No table result is available"
            return
        row_total = len(result.rows)
        col_total = len(result.columns)
        row = min(self.state.result_row + 1, row_total) if row_total else 0
        col = min(self.state.result_col + 1, col_total) if col_total else 0
        mode = "row detail" if self.state.result_mode == RESULT_ROW_DETAIL else "grid"
        if self._active_insert_draft() is not None:
            self.state.status = (
                f"Results {mode}: row {row}/{row_total}, col {col}/{col_total} | {insert_draft_active_status()}"
            )
            return
        edit = ""
        if not is_database_connected(self.state.db):
            edit = " | disconnected; loaded rows are view-only; reconnect to continue"
        elif result.editable_context is not None and not is_read_only_enabled(self.state.db):
            edit = " | Enter edits cell | INS inserts row"
        status = (
            f"Results {mode}: row {row}/{row_total}, col {col}/{col_total} | F10 views cell{edit} | Ctrl-C copies cell"
        )
        paging = more_rows_status(result, is_database_connected(self.state.db))
        self.state.status = f"{status} | {paging}" if paging else status
