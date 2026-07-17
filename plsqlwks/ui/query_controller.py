from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any, Protocol

from ..db import ExplainPlanResult, QueryResult
from ..sqlbinds import bind_name_key, find_bind_names, find_unique_binds
from ..sqlsplit import Statement, preflight_script, split_script, statement_at_cursor
from .constants import FOCUS_EDITOR
from .errors import (
    ErrorLocation,
    first_document_error_location,
    move_buffer_to_error,
)
from .ports import DbOperationsPort, DialogPort
from .results import is_dbms_output_result
from .state import ExecutionDiagnostic, ScriptExecutionFailed, UIState


class QueryResultPort(Protocol):
    def finish_execution(
        self,
        results: list[QueryResult],
        *,
        source_text: str | None = None,
        source_unchanged: bool = True,
        statement_start_line: int = 1,
        statement_start_col: int = 0,
        statement_count: int = 1,
    ) -> None: ...

    def set_results(self, lines: list[str], clear_table: bool = True) -> None: ...


def error_location_line(location: ErrorLocation) -> str:
    return f"Error location: line {location.line}, column {location.column}"


def move_to_document_error(
    state: UIState,
    exc: Exception,
    statement_start_line: int = 1,
    statement_start_col: int = 0,
) -> ErrorLocation | None:
    location = first_document_error_location(
        exc,
        statement_start_line,
        statement_start_col,
    )
    if location is None:
        return None
    return move_buffer_to_error(state.buffer, location)


def document_statement_lines(
    statement: Statement,
    line_offset: int = 0,
) -> tuple[int, int]:
    return statement.start_line + line_offset, statement.end_line + line_offset


def document_statement_start_col(
    statement: Statement,
    selection_start_col: int = 0,
) -> int:
    if statement.start_line == 1:
        return statement.start_col + selection_start_col
    return statement.start_col


def bind_values_for_statement(
    statement: str,
    bind_values: dict[str, str],
) -> dict[str, str]:
    if not bind_values:
        return {}
    values_by_key = {bind_name_key(name): value for name, value in bind_values.items()}
    return {
        name: values_by_key[key] for name in find_bind_names(statement) if (key := bind_name_key(name)) in values_by_key
    }


def execute_statement_with_bind_values(
    db: Any,
    statement: str,
    title: str,
    bind_values: dict[str, str],
) -> QueryResult:
    statement_bind_values = bind_values_for_statement(statement, bind_values)
    if statement_bind_values:
        return db.execute_statement(statement, title, statement_bind_values)
    return db.execute_statement(statement, title)


def explain_statement_with_bind_values(
    db: Any,
    statement: str,
    title: str,
    bind_values: dict[str, str],
) -> ExplainPlanResult:
    statement_bind_values = bind_values_for_statement(statement, bind_values)
    if statement_bind_values:
        return db.explain_statement(statement, title, statement_bind_values)
    return db.explain_statement(statement, title)


def append_script_worker_result(
    db: Any,
    results: list[QueryResult],
    result: QueryResult,
) -> None:
    if result.columns and not is_dbms_output_result(result):
        for previous in reversed(results):
            if not previous.columns or is_dbms_output_result(previous):
                continue
            if previous.continuation is not None:
                with contextlib.suppress(Exception):
                    db.close_result_continuation(previous.continuation)
                previous.continuation = None
            break
    results.append(result)


def set_result_statement_origin(
    result: QueryResult,
    line: int,
    column: int,
) -> QueryResult:
    """Attach the editor origin needed to map successful compile diagnostics."""
    result.statement_start_line = line
    result.statement_start_col = column
    return result


class QueryController:
    """Capture editor requests and submit database-only query tasks."""

    def __init__(
        self,
        state: UIState,
        db_operations: DbOperationsPort,
        dialogs: DialogPort,
        presenter: QueryResultPort,
    ) -> None:
        self.state = state
        self.db_operations = db_operations
        self.dialogs = dialogs
        self.presenter = presenter

    def bind_names_for_statements(self, statements: list[str]) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for statement in statements:
            for bind in find_unique_binds(statement):
                key = bind_name_key(bind.name, bind.quoted)
                if key in seen:
                    continue
                seen.add(key)
                names.append(bind.name)
        return names

    def reject_script_preflight(
        self,
        script: str,
        *,
        first_line: int = 1,
        first_col: int = 0,
        action: str = "Execution",
    ) -> bool:
        issues = preflight_script(script)
        if not issues:
            return False
        mapped: list[tuple[int, int, str]] = []
        for issue in issues:
            line = first_line + issue.line - 1
            column = issue.column + (first_col if issue.line == 1 else 0)
            mapped.append((line, column, issue.message))
        first_issue = mapped[0]
        tab = self.state.active_tab
        tab.execution_diagnostics = [ExecutionDiagnostic(line, column, message) for line, column, message in mapped]
        tab.execution_diagnostic_index = 0
        tab.execution_diagnostic_source = self.state.buffer.text()
        move_buffer_to_error(
            self.state.buffer,
            ErrorLocation(first_issue[0], first_issue[1]),
        )
        self.presenter.set_results(
            [
                f"{action} was not started; unsupported client syntax:",
                *[f"line {line}, column {column}: {message}" for line, column, message in mapped],
            ]
        )
        self.state.focus = FOCUS_EDITOR
        self.state.status = f"{action} blocked at line {first_issue[0]}, column {first_issue[1]}: {first_issue[2]}"
        return True

    def remembered_bind_value(self, name: str) -> str:
        key = bind_name_key(name)
        for remembered_name, value in self.state.remembered_bind_values.items():
            if bind_name_key(remembered_name) == key:
                return value
        return ""

    def remember_bind_values(self, values: dict[str, str]) -> None:
        remembered = self.state.remembered_bind_values
        for name, value in values.items():
            key = bind_name_key(name)
            for old_name in list(remembered):
                if bind_name_key(old_name) == key:
                    del remembered[old_name]
            remembered[name] = value

    def prompt_bind_values_for_statements(
        self,
        statements: list[str],
        cancel_status: str,
    ) -> dict[str, str] | None:
        values: dict[str, str] = {}
        for name in self.bind_names_for_statements(statements):
            default = self.remembered_bind_value(name) if self.state.config.remember_bind_values else ""
            value = self.dialogs.prompt_text_box(
                f"Value for :{name}",
                default,
                strip=False,
            )
            if value is None:
                self.state.status = cancel_status
                return None
            values[name] = value
        if self.state.config.remember_bind_values:
            self.remember_bind_values(values)
        return values

    def bind_values_for_statement(
        self,
        statement: str,
        bind_values: dict[str, str],
    ) -> dict[str, str]:
        return bind_values_for_statement(statement, bind_values)

    def execute_statement_with_bind_values(
        self,
        db: Any,
        statement: str,
        title: str,
        bind_values: dict[str, str],
    ) -> QueryResult:
        return execute_statement_with_bind_values(
            db,
            statement,
            title,
            bind_values,
        )

    def explain_statement_with_bind_values(
        self,
        db: Any,
        statement: str,
        title: str,
        bind_values: dict[str, str],
    ) -> ExplainPlanResult:
        return explain_statement_with_bind_values(
            db,
            statement,
            title,
            bind_values,
        )

    def run_current_statement(self) -> None:
        if self.db_operations.reject_if_active():
            return
        selected = self.selected_script()
        if selected is not None:
            self.run_selected_script(*selected)
            return
        statement = statement_at_cursor(
            self.state.buffer.text(),
            self.state.buffer.row,
            self.state.buffer.col,
        )
        if statement is None:
            self.state.status = "No statement at cursor"
            return
        if self.reject_script_preflight(
            statement.text,
            first_line=statement.start_line,
            first_col=statement.start_col,
        ):
            return
        bind_values = self.prompt_bind_values_for_statements(
            [statement.text],
            "Execution cancelled",
        )
        if bind_values is None:
            return
        statement_text = statement.text

        def run_statement(
            db: Any,
            progress: Callable[[str], None],
        ) -> list[QueryResult]:
            return [
                set_result_statement_origin(
                    execute_statement_with_bind_values(
                        db,
                        statement_text,
                        "Current statement",
                        bind_values,
                    ),
                    statement.start_line,
                    statement.start_col,
                )
            ]

        self.db_operations.start(
            "execute",
            "Running current statement",
            run_statement,
            statement_start_line=statement.start_line,
            statement_start_col=statement.start_col,
            source_text=self.state.buffer.text(),
        )

    def navigate_execution_diagnostic(self, direction: int) -> None:
        tab = self.state.active_tab
        diagnostics = tab.execution_diagnostics
        if not diagnostics:
            self.state.status = "No execution diagnostics"
            return
        if tab.execution_diagnostic_source is None or self.state.buffer.text() != tab.execution_diagnostic_source:
            self.state.status = "Execution diagnostics are stale because the source changed; run the statement again"
            return
        if direction >= 0:
            index = (tab.execution_diagnostic_index + 1) % len(diagnostics)
        else:
            current = tab.execution_diagnostic_index
            index = (current - 1) % len(diagnostics) if current >= 0 else len(diagnostics) - 1
        tab.execution_diagnostic_index = index
        diagnostic = diagnostics[index]
        moved = move_buffer_to_error(
            self.state.buffer,
            ErrorLocation(diagnostic.line, diagnostic.column),
        )
        self.state.focus = FOCUS_EDITOR
        message = f": {diagnostic.message}" if diagnostic.message else ""
        self.state.status = (
            f"Diagnostic {index + 1}/{len(diagnostics)} at line {moved.line}, column {moved.column}{message}"
        )

    def next_execution_diagnostic(self) -> None:
        self.navigate_execution_diagnostic(1)

    def previous_execution_diagnostic(self) -> None:
        self.navigate_execution_diagnostic(-1)

    def selected_script(self) -> tuple[str, int, int] | None:
        selected = self.state.buffer.selection_range()
        if selected is None:
            return None
        return self.state.buffer.selected_text(), selected[0][0] + 1, selected[0][1]

    def run_selected_script(
        self,
        script: str,
        first_line: int,
        first_col: int = 0,
    ) -> None:
        source_text = self.state.buffer.text()
        if self.reject_script_preflight(
            script,
            first_line=first_line,
            first_col=first_col,
        ):
            return
        statements = split_script(script)
        if not statements:
            self.presenter.finish_execution([QueryResult("Selection", [], [], "No statements to execute.")])
            return
        bind_values = self.prompt_bind_values_for_statements(
            [statement.text for statement in statements],
            "Execution cancelled",
        )
        if bind_values is None:
            return
        line_offset = first_line - 1
        if len(statements) == 1:
            statement = statements[0]
            start_line, end_line = document_statement_lines(statement, line_offset)
            start_col = document_statement_start_col(statement, first_col)
            title = f"Selection lines {start_line}-{end_line}"
            statement_text = statement.text

            def run_selection(
                db: Any,
                progress: Callable[[str], None],
            ) -> list[QueryResult]:
                return [
                    set_result_statement_origin(
                        execute_statement_with_bind_values(
                            db,
                            statement_text,
                            title,
                            bind_values,
                        ),
                        start_line,
                        start_col,
                    )
                ]

            self.db_operations.start(
                "execute",
                "Running selection 1/1",
                run_selection,
                statement_start_line=start_line,
                statement_start_col=start_col,
                source_text=source_text,
                statement_count=1,
            )
            return

        statement_count = len(statements)

        def run_selection_worker(
            db: Any,
            progress: Callable[[str], None],
        ) -> list[QueryResult]:
            results: list[QueryResult] = []
            for idx, statement in enumerate(statements, start=1):
                start_line, end_line = document_statement_lines(
                    statement,
                    line_offset,
                )
                start_col = document_statement_start_col(statement, first_col)
                title = f"Selection {idx} lines {start_line}-{end_line}"
                progress(f"Running selection {idx}/{statement_count}: lines {start_line}-{end_line}")
                try:
                    result = execute_statement_with_bind_values(
                        db,
                        statement.text,
                        title,
                        bind_values,
                    )
                    set_result_statement_origin(result, start_line, start_col)
                    append_script_worker_result(db, results, result)
                except Exception as exc:
                    raise ScriptExecutionFailed(
                        exc,
                        start_line,
                        start_col,
                        results,
                        statement_index=idx,
                        statement_count=statement_count,
                    ) from exc
            return results

        self.db_operations.start(
            "execute",
            f"Running selection 1/{statement_count}",
            run_selection_worker,
            source_text=source_text,
            statement_count=statement_count,
        )

    def explain_current_statement(self) -> None:
        if self.db_operations.reject_if_active():
            return
        statement = statement_at_cursor(
            self.state.buffer.text(),
            self.state.buffer.row,
            self.state.buffer.col,
        )
        if statement is None:
            self.state.status = "No statement at cursor"
            return
        if self.reject_script_preflight(
            statement.text,
            first_line=statement.start_line,
            first_col=statement.start_col,
            action="Explain",
        ):
            return
        bind_values = self.prompt_bind_values_for_statements(
            [statement.text],
            "Explain cancelled",
        )
        if bind_values is None:
            return
        statement_text = statement.text

        def explain_statement(
            db: Any,
            progress: Callable[[str], None],
        ) -> ExplainPlanResult:
            return explain_statement_with_bind_values(
                db,
                statement_text,
                "Current statement",
                bind_values,
            )

        self.db_operations.start(
            "explain",
            "Explaining current statement",
            explain_statement,
            statement_start_line=statement.start_line,
            statement_start_col=statement.start_col,
            source_text=self.state.buffer.text(),
        )

    def run_script(self) -> None:
        if self.db_operations.reject_if_active():
            return
        selected = self.selected_script()
        if selected is not None:
            self.run_selected_script(*selected)
            return
        source_text = self.state.buffer.text()
        if self.reject_script_preflight(source_text):
            return
        statements = split_script(source_text)
        if not statements:
            self.presenter.finish_execution([QueryResult("Script", [], [], "No statements to execute.")])
            return
        bind_values = self.prompt_bind_values_for_statements(
            [statement.text for statement in statements],
            "Execution cancelled",
        )
        if bind_values is None:
            return
        statement_count = len(statements)

        def run_script_worker(
            db: Any,
            progress: Callable[[str], None],
        ) -> list[QueryResult]:
            results: list[QueryResult] = []
            for idx, statement in enumerate(statements, start=1):
                start_line, end_line = document_statement_lines(statement)
                start_col = document_statement_start_col(statement)
                title = f"Statement {idx} lines {start_line}-{end_line}"
                progress(f"Running statement {idx}/{statement_count}: lines {start_line}-{end_line}")
                try:
                    result = execute_statement_with_bind_values(
                        db,
                        statement.text,
                        title,
                        bind_values,
                    )
                    set_result_statement_origin(result, start_line, start_col)
                    append_script_worker_result(db, results, result)
                except Exception as exc:
                    raise ScriptExecutionFailed(
                        exc,
                        start_line,
                        start_col,
                        results,
                        statement_index=idx,
                        statement_count=statement_count,
                    ) from exc
            return results

        self.db_operations.start(
            "execute",
            f"Running script 1/{statement_count}",
            run_script_worker,
            source_text=source_text,
            statement_count=statement_count,
        )
