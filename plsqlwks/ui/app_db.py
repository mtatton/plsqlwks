from __future__ import annotations

import curses
from dataclasses import dataclass
from pathlib import Path
import queue
import time
from typing import Any, Callable

from ..config import save_autocommit
from ..db import (
    ExplainPlanResult,
    OracleWorkspace,
    QueryResult,
    QueryResultPage,
    TransactionReport,
    workspace_health,
)
from ..sqlbinds import bind_name_key, find_bind_names, find_unique_binds
from ..sqlsplit import Statement, split_script, statement_at_cursor
from ..workspace import list_workspace_files
from .constants import *
from .display import *
from .db_worker import DatabaseWorker, DbWorkerFinished, DbWorkerProgress
from .keys import *
from .buffer import *
from .help import *
from .errors import *
from .sql import *
from .completion import *
from .syntax import *
from .clipboard import *
from .browser import *
from .results import *
from .state import *


@dataclass(frozen=True)
class TransactionCompletion:
    report: TransactionReport
    cleanup_error: Exception | None = None


@dataclass(frozen=True)
class ConnectionCompletion:
    old_session_close_error: Exception | None = None


@dataclass(frozen=True)
class TransactionModeChange:
    resolution: str | None
    report: TransactionReport | None = None
    transaction_error: Exception | None = None
    cleanup_error: Exception | None = None
    mode_error: Exception | None = None


class AppDbMixin:
    def ensure_database_worker(self) -> DatabaseWorker:
        worker = getattr(self, "db_worker", None)
        if worker is None:
            worker = DatabaseWorker(self.state.db)
            self.db_worker = worker
        return worker

    def db_operation_active(self) -> bool:
        return self.state.db_operation is not None

    def reject_if_db_operation_active(self) -> bool:
        if not self.db_operation_active():
            return False
        self.state.status = "Database operation already running"
        return True

    def interrupt_db_operation(self) -> None:
        operation = self.state.db_operation
        if operation is None:
            self.state.status = "No database operation running"
            return
        if operation.cancel_requested:
            self.state.status = "Database interrupt already requested"
            return
        try:
            if not self.ensure_database_worker().cancel_current_operation(
                operation.handle.command_id
            ):
                operation.label = "Database interrupt unavailable"
                self.state.status = "Database interrupt unavailable"
                return
        except Exception as exc:
            message = short_execution_error_message(exc) or str(exc)
            operation.label = "Database interrupt failed"
            self.state.status = (
                f"Database interrupt failed: {message}" if message else "Database interrupt failed"
            )
            return
        operation.cancel_requested = True
        self.state.status = "Database interrupt requested"

    def start_db_operation(
        self,
        kind: str,
        label: str,
        task: Callable[[Any, Callable[[str], None]], Any],
        statement_start_line: int = 1,
        statement_start_col: int = 0,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        restore_active_tab: bool = True,
    ) -> bool:
        if self.reject_if_db_operation_active():
            return False
        handle = self.ensure_database_worker().submit(task)
        self.state.db_operation = DbOperation(
            kind=kind,
            label=label,
            started_at=time.monotonic(),
            handle=handle,
            tab=self.state.active_tab,
            statement_start_line=statement_start_line,
            statement_start_col=statement_start_col,
            on_success=on_success,
            on_error=on_error,
            restore_active_tab=restore_active_tab,
        )
        self.state.status = label
        return True

    def poll_db_operation(self) -> None:
        operation = self.state.db_operation
        if operation is None:
            return
        while True:
            try:
                event = operation.handle.events.get_nowait()
            except queue.Empty:
                return
            if isinstance(event, DbWorkerProgress):
                operation.label = event.label
                continue
            if not isinstance(event, DbWorkerFinished):
                continue
            self.state.db = event.session_state
            self.state.db_operation = None
            error = event.error
            partial_results = None
            statement_start_line = operation.statement_start_line
            statement_start_col = operation.statement_start_col
            if isinstance(error, ScriptExecutionFailed):
                partial_results = error.partial_results
                statement_start_line = error.statement_start_line
                statement_start_col = error.statement_start_col
                error = error.original
            completed = DbOperationFinished(
                kind=operation.kind,
                result=event.result,
                error=error,
                statement_start_line=statement_start_line,
                statement_start_col=statement_start_col,
                partial_results=partial_results,
            )
            self.complete_db_operation(completed, operation)
            return

    def wait_for_db_operation(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while (operation := self.state.db_operation) is not None:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            operation.handle.done.wait(timeout=remaining)
            self.poll_db_operation()
            if deadline is not None and time.monotonic() >= deadline:
                return

    def complete_db_operation(self, event: DbOperationFinished, operation: DbOperation) -> None:
        original_idx = self.state.active_tab_idx
        target_idx = self.find_tab_index(operation.tab)
        if target_idx is None:
            target_idx = original_idx
        self.state.active_tab_idx = target_idx
        self._db_completion_target_was_active = target_idx == original_idx
        try:
            if event.error is not None and operation.on_error is not None:
                operation.on_error(event.error)
            elif event.error is None and operation.on_success is not None:
                operation.on_success(event.result)
            else:
                self.apply_db_operation_result(event)
        finally:
            self._db_completion_target_was_active = False
            if operation.restore_active_tab:
                if original_idx < len(self.state.tabs):
                    self.state.active_tab_idx = original_idx
                else:
                    self.state.ensure_tab()

    def apply_db_operation_result(self, event: DbOperationFinished) -> None:
        if event.error is not None:
            if event.kind == "explain":
                self.handle_explain_error(event.error, event.statement_start_line, event.statement_start_col)
                return
            if event.kind == "fetch-more":
                self.handle_fetch_more_error(event.error)
                return
            self.handle_execution_error(
                event.error,
                event.statement_start_line,
                event.statement_start_col,
                event.partial_results,
            )
            return
        if event.kind == "explain":
            self.show_explain_result(event.result)
            return
        if event.kind == "fetch-more":
            self.apply_fetch_more_result(event.result)
            return
        self.finish_execution(event.result)

    def find_tab_index(self, target: FileTab) -> int | None:
        for idx, tab in enumerate(self.state.tabs):
            if tab is target:
                return idx
        return None

    def shutdown_database_worker(self, timeout: float | None = None) -> None:
        worker = getattr(self, "db_worker", None)
        if worker is None:
            return
        worker.shutdown(timeout=timeout)

    def try_connect(self, force: bool = False) -> None:
        if force:
            self.reconnect_database()
            return
        self._start_connect(force=False)

    def _start_connect(self, *, force: bool) -> None:
        if self.reject_if_db_operation_active():
            return

        if force:
            self.close_all_result_continuations()
            self.invalidate_results_after_rollback()

        def connect(db: Any, progress: Callable[[str], None]) -> ConnectionCompletion:
            close_error = None
            if force:
                try:
                    db.close()
                except Exception as exc:
                    close_error = exc
            db.ensure_connected()
            return ConnectionCompletion(close_error)

        def connected(result: ConnectionCompletion) -> None:
            self.state.status = f"Connected as {self.state.config.user}"
            if result.old_session_close_error is not None:
                self.state.status += (
                    " (warning: old session close failed: "
                    f"{short_error(result.old_session_close_error)})"
                )

        def connect_failed(exc: Exception) -> None:
            self.state.status = "Connection failed"
            self.set_results(["ERROR connecting to Oracle:", *wrap_error(exc)])

        self.start_db_operation(
            "connect",
            "Reconnecting to Oracle" if force else "Connecting to Oracle",
            connect,
            on_success=connected,
            on_error=connect_failed,
        )

    def choose_transaction_mode(self) -> None:
        if self.reject_if_db_operation_active():
            return
        current = "a" if is_autocommit_enabled(self.state.db) else "m"
        answer = self.prompt("Transaction mode (a=autocommit, m=manual)", current)
        if answer is None or not answer:
            self.state.status = "Transaction mode unchanged"
            return
        normalized = answer.lower()
        if normalized.startswith("a"):
            resolution = None
            if not is_autocommit_enabled(self.state.db) and has_uncommitted_changes(self.state.db):
                resolution = self.prompt_pending_transaction("Transaction mode unchanged")
                if resolution is None:
                    return
            self.set_transaction_mode(True, resolution)
            return
        if normalized.startswith("m"):
            self.set_transaction_mode(False)
            return
        self.state.status = "Transaction mode unchanged"

    def prompt_pending_transaction(
        self,
        cancel_status: str,
        *,
        allow_discard: bool = False,
    ) -> str | None:
        if not has_uncommitted_changes(self.state.db):
            return "none"
        choices = "c=commit, r=rollback, x=cancel"
        if allow_discard:
            choices = "c=commit, r=rollback, d=discard session, x=cancel"
        answer = self.prompt(
            f"Pending transaction: {choices}",
            "",
        )
        if answer is None:
            self.state.status = cancel_status
            return None
        normalized = answer.strip().lower()
        if normalized in {"c", "commit"}:
            return "commit"
        if normalized in {"r", "rollback"}:
            return "rollback"
        if allow_discard and normalized in {"d", "discard", "discard session"}:
            return "discard"
        self.state.status = cancel_status
        return None

    def set_transaction_mode(self, enabled: bool, resolution: str | None = None) -> bool:
        label = "autocommit" if enabled else "manual"

        def set_mode(db: Any, progress: Callable[[str], None]) -> TransactionModeChange:
            report = None
            try:
                if resolution == "commit":
                    report = db.commit()
                elif resolution == "rollback":
                    report = db.rollback()
            except Exception as exc:
                return TransactionModeChange(resolution, transaction_error=exc)
            cleanup_error = None
            if resolution == "rollback":
                close_all = getattr(db, "close_all_result_continuations", None)
                if callable(close_all):
                    try:
                        close_all()
                    except Exception as exc:
                        cleanup_error = exc
            try:
                db.set_autocommit(enabled)
            except Exception as exc:
                return TransactionModeChange(
                    resolution,
                    report=report,
                    cleanup_error=cleanup_error,
                    mode_error=exc,
                )
            return TransactionModeChange(
                resolution,
                report=report,
                cleanup_error=cleanup_error,
            )

        def mode_set(result: TransactionModeChange) -> None:
            if result.resolution == "rollback" and result.report is not None:
                self.invalidate_results_after_rollback()
            if result.transaction_error is not None:
                action = "Commit" if result.resolution == "commit" else "Rollback"
                error_header = (
                    "ERROR committing transaction:"
                    if result.resolution == "commit"
                    else "ERROR rolling back transaction:"
                )
                self.state.status = f"{action} failed"
                self.set_results(
                    [error_header, *wrap_error(result.transaction_error)]
                )
                return
            if result.mode_error is not None:
                suffix = f" after {result.resolution}" if result.resolution in {"commit", "rollback"} else ""
                self.state.status = f"Transaction mode change failed{suffix}"
                self.set_results(
                    ["ERROR changing transaction mode:", *wrap_error(result.mode_error)]
                )
                return
            save_autocommit(self.state.config, enabled)
            self.state.status = f"Transaction mode: {label}"
            if result.cleanup_error is not None:
                self.state.status += f" (warning: result cleanup failed: {short_error(result.cleanup_error)})"

        def mode_failed(exc: Exception) -> None:
            self.state.status = "Transaction mode change failed"
            self.set_results(["ERROR changing transaction mode:", *wrap_error(exc)])

        return self.start_db_operation(
            "transaction-mode",
            f"Setting transaction mode: {label}",
            set_mode,
            on_success=mode_set,
            on_error=mode_failed,
        )

    def commit_transaction(
        self,
        after_success: Callable[[], None] | None = None,
        after_error: Callable[[], None] | None = None,
        *,
        preserve_results_on_error: bool = False,
    ) -> bool:
        if self.reject_if_db_operation_active():
            return False

        def committed(report: TransactionReport) -> None:
            self.state.status = transaction_report_status("Committed transaction", report)
            if after_success is not None:
                after_success()

        def commit_failed(exc: Exception) -> None:
            self.state.status = "Commit failed"
            self.set_results(
                ["ERROR committing transaction:", *wrap_error(exc)],
                clear_table=not preserve_results_on_error,
            )
            if after_error is not None:
                after_error()

        return self.start_db_operation(
            "commit",
            "Committing transaction",
            lambda db, progress: db.commit(),
            on_success=committed,
            on_error=commit_failed,
        )

    def rollback_transaction(
        self,
        after_success: Callable[[], None] | None = None,
        after_error: Callable[[], None] | None = None,
        *,
        preserve_results_on_error: bool = False,
    ) -> bool:
        if self.reject_if_db_operation_active():
            return False

        def rollback(db: Any, progress: Callable[[str], None]) -> TransactionCompletion:
            report = db.rollback()
            cleanup_error = None
            close_all = getattr(db, "close_all_result_continuations", None)
            if callable(close_all):
                try:
                    close_all()
                except Exception as exc:
                    cleanup_error = exc
            return TransactionCompletion(report, cleanup_error)

        def rolled_back(completion: TransactionCompletion) -> None:
            self.invalidate_results_after_rollback()
            self.state.status = transaction_report_status("Rollback transaction", completion.report)
            if completion.cleanup_error is not None:
                self.state.status += (
                    f" (warning: result cleanup failed: {short_error(completion.cleanup_error)})"
                )
            if after_success is not None:
                after_success()

        def rollback_failed(exc: Exception) -> None:
            self.state.status = "Rollback failed"
            self.set_results(
                ["ERROR rolling back transaction:", *wrap_error(exc)],
                clear_table=not preserve_results_on_error,
            )
            if after_error is not None:
                after_error()

        return self.start_db_operation(
            "rollback",
            "Rolling back transaction",
            rollback,
            on_success=rolled_back,
            on_error=rollback_failed,
        )

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
            value = self.prompt_text_box(f"Value for :{name}", default, strip=False)
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
        if not bind_values:
            return {}
        values_by_key = {bind_name_key(name): value for name, value in bind_values.items()}
        return {
            name: values_by_key[key]
            for name in find_bind_names(statement)
            if (key := bind_name_key(name)) in values_by_key
        }

    def execute_statement_with_bind_values(
        self,
        db: Any,
        statement: str,
        title: str,
        bind_values: dict[str, str],
    ) -> QueryResult:
        statement_bind_values = self.bind_values_for_statement(statement, bind_values)
        if statement_bind_values:
            return db.execute_statement(statement, title, statement_bind_values)
        return db.execute_statement(statement, title)

    def explain_statement_with_bind_values(
        self,
        db: Any,
        statement: str,
        title: str,
        bind_values: dict[str, str],
    ) -> ExplainPlanResult:
        statement_bind_values = self.bind_values_for_statement(statement, bind_values)
        if statement_bind_values:
            return db.explain_statement(statement, title, statement_bind_values)
        return db.explain_statement(statement, title)

    def run_current_statement(self) -> None:
        if self.reject_if_db_operation_active():
            return
        selected = self.selected_script()
        if selected is not None:
            self.run_selected_script(*selected)
            return
        statement = statement_at_cursor(self.state.buffer.text(), self.state.buffer.row, self.state.buffer.col)
        if statement is None:
            self.state.status = "No statement at cursor"
            return
        bind_values = self.prompt_bind_values_for_statements([statement.text], "Execution cancelled")
        if bind_values is None:
            return
        self.start_db_operation(
            "execute",
            "Running current statement",
            lambda db, progress: [
                self.execute_statement_with_bind_values(
                    db, statement.text, "Current statement", bind_values
                )
            ],
            statement_start_line=statement.start_line,
            statement_start_col=statement.start_col,
        )

    def selected_script(self) -> tuple[str, int, int] | None:
        selected = self.state.buffer.selection_range()
        if selected is None:
            return None
        return self.state.buffer.selected_text(), selected[0][0] + 1, selected[0][1]

    def document_statement_lines(self, statement: Statement, line_offset: int = 0) -> tuple[int, int]:
        return statement.start_line + line_offset, statement.end_line + line_offset

    def document_statement_start_col(self, statement: Statement, selection_start_col: int = 0) -> int:
        if statement.start_line == 1:
            return statement.start_col + selection_start_col
        return statement.start_col

    def append_script_worker_result(
        self,
        db: Any,
        results: list[QueryResult],
        result: QueryResult,
    ) -> None:
        if result.columns and not is_dbms_output_result(result):
            for previous in reversed(results):
                if not previous.columns or is_dbms_output_result(previous):
                    continue
                if previous.continuation is not None:
                    try:
                        db.close_result_continuation(previous.continuation)
                    except Exception:
                        pass
                    previous.continuation = None
                break
        results.append(result)

    def run_selected_script(self, script: str, first_line: int, first_col: int = 0) -> None:
        statements = split_script(script)
        if not statements:
            self.finish_execution([QueryResult("Selection", [], [], "No statements to execute.")])
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
            start_line, end_line = self.document_statement_lines(statement, line_offset)
            start_col = self.document_statement_start_col(statement, first_col)
            title = f"Selection lines {start_line}-{end_line}"
            self.start_db_operation(
                "execute",
                "Running selection",
                lambda db, progress: [
                    self.execute_statement_with_bind_values(db, statement.text, title, bind_values)
                ],
                statement_start_line=start_line,
                statement_start_col=start_col,
            )
            return

        def run_selection_worker(
            db: Any, progress: Callable[[str], None]
        ) -> list[QueryResult]:
            results: list[QueryResult] = []
            for idx, statement in enumerate(statements, start=1):
                start_line, end_line = self.document_statement_lines(statement, line_offset)
                start_col = self.document_statement_start_col(statement, first_col)
                title = f"Selection {idx} lines {start_line}-{end_line}"
                progress(f"Running {title}")
                try:
                    result = self.execute_statement_with_bind_values(
                        db, statement.text, title, bind_values
                    )
                    self.append_script_worker_result(db, results, result)
                except Exception as exc:
                    raise ScriptExecutionFailed(exc, start_line, start_col, results) from exc
            return results

        self.start_db_operation("execute", "Running selection", run_selection_worker)

    def explain_current_statement(self) -> None:
        if self.reject_if_db_operation_active():
            return
        statement = statement_at_cursor(self.state.buffer.text(), self.state.buffer.row, self.state.buffer.col)
        if statement is None:
            self.state.status = "No statement at cursor"
            return
        bind_values = self.prompt_bind_values_for_statements([statement.text], "Explain cancelled")
        if bind_values is None:
            return
        self.start_db_operation(
            "explain",
            "Explaining current statement",
            lambda db, progress: self.explain_statement_with_bind_values(
                db,
                statement.text,
                "Current statement",
                bind_values,
            ),
            statement_start_line=statement.start_line,
            statement_start_col=statement.start_col,
        )

    def run_script(self) -> None:
        if self.reject_if_db_operation_active():
            return
        selected = self.selected_script()
        if selected is not None:
            self.run_selected_script(*selected)
            return
        statements = split_script(self.state.buffer.text())
        if not statements:
            self.finish_execution([QueryResult("Script", [], [], "No statements to execute.")])
            return
        bind_values = self.prompt_bind_values_for_statements(
            [statement.text for statement in statements],
            "Execution cancelled",
        )
        if bind_values is None:
            return

        def run_script_worker(db: Any, progress: Callable[[str], None]) -> list[QueryResult]:
            results: list[QueryResult] = []
            for idx, statement in enumerate(statements, start=1):
                start_line, end_line = self.document_statement_lines(statement)
                start_col = self.document_statement_start_col(statement)
                title = f"Statement {idx} lines {start_line}-{end_line}"
                progress(f"Running {title}")
                try:
                    result = self.execute_statement_with_bind_values(
                        db, statement.text, title, bind_values
                    )
                    self.append_script_worker_result(db, results, result)
                except Exception as exc:
                    raise ScriptExecutionFailed(exc, start_line, start_col, results) from exc
            return results

        self.start_db_operation("execute", "Running script", run_script_worker)

    def finish_execution(self, results: list[QueryResult]) -> None:
        self.render_results(results)
        self.state.status = results[-1].message if results else "Done"

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
        result.message = fetched.page.message
        self.refresh_result_summary_line(result)
        self.state.show_dbms_output = False
        self.state.focus = FOCUS_RESULTS
        if result.rows:
            self.move_result_selection(to_row=min(fetched.target_row, len(result.rows) - 1))
        self.update_result_status()
        self.state.status = f"{result.message} | {self.state.status}"

    def handle_fetch_more_error(self, exc: Exception) -> None:
        error_lines = execution_error_lines(exc)
        self.set_results([*self.state.results, *error_lines], clear_table=False)
        message = short_execution_error_message(exc)
        status = "Fetch rows interrupted" if is_execution_interrupted(exc) else "Fetch rows failed"
        self.state.status = f"{status}: {message}" if message else status
        if self.state.active_result is not None:
            self.state.active_result.continuation = None
            self.state.focus = FOCUS_RESULTS

    def refresh_result_summary_line(self, result: QueryResult) -> None:
        prefix = f"[{result.title}] "
        for idx, line in enumerate(self.state.results):
            if line.startswith(prefix):
                self.state.results[idx] = f"{prefix}{result.message}"
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

        self.ensure_database_worker().submit(close_continuation, ignored=True, background=True)

    def run_with_errors(self, runner) -> None:
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
    ) -> None:
        location = first_document_error_location(exc, statement_start_line, statement_start_col)
        error_lines = execution_error_lines(exc)
        moved_location = None
        if location is not None:
            moved_location = move_buffer_to_error(self.state.buffer, location)
            error_lines.insert(1, error_location_line(moved_location))
        if partial_results:
            self.render_results(partial_results)
            lines = [*self.state.results, *error_lines]
            self.set_results(lines, clear_table=False)
        else:
            self.set_results(error_lines)
        self.state.focus = FOCUS_EDITOR
        if location is None:
            message = short_execution_error_message(exc)
            status = "Execution interrupted" if is_execution_interrupted(exc) else "Execution failed"
            self.state.status = f"{status}: {message}" if message else status
            return
        message = short_execution_error_message(exc)
        suffix = f": {message}" if message else ""
        status = "Execution interrupted" if is_execution_interrupted(exc) else "Execution failed"
        self.state.status = f"{status} at line {moved_location.line}, column {moved_location.column}{suffix}"

    def render_results(self, results: list[QueryResult]) -> None:
        self.discard_insert_draft()
        output: list[str] = []
        dbms_output: list[str] = []
        active_result: QueryResult | None = None
        previous_result = self.state.active_result
        if previous_result is not None and not any(result is previous_result for result in results):
            self.release_result_continuation(previous_result)
        self.state.explain_result = None
        self.state.explain_scroll = 0
        for result in results:
            self.state.last_result = result
            output.append(f"[{result.title}] {result.message}")
            if is_dbms_output_result(result):
                lines = [row[0] if row else "" for row in result.rows]
                dbms_output.extend(lines)
                output.extend(lines)
            elif result.columns:
                active_result = result
                if result.editable_context is not None and not is_read_only_enabled(self.state.db):
                    output.append(
                        "Tab opens the result grid. F10 views the full selected cell. "
                        "Enter edits the selected ROWID-backed cell. INS prepares a draft insert row."
                    )
                else:
                    output.append("Tab opens the result grid. F10 views the full selected cell. F8 toggles row detail.")
            output.append("")
        for result in results:
            if result is not active_result:
                self.release_result_continuation(result)
        self.set_results(output, clear_table=False)
        self.state.active_result = active_result
        self.state.dbms_output = dbms_output
        self.state.active_tab.dbms_output_scroll = None
        self.state.show_dbms_output = bool(dbms_output) and active_result is None
        self.state.focus = FOCUS_EDITOR
        self.state.result_row = 0
        self.state.result_col = 0
        self.state.result_row_scroll = 0
        self.state.result_col_scroll = 0
        self.clamp_result_selection()

    def show_explain_result(self, result: ExplainPlanResult) -> None:
        self.discard_insert_draft()
        self.release_result_continuation(self.state.active_result)
        self.state.explain_result = result
        self.state.active_result = None
        self.state.dbms_output = []
        self.state.active_tab.dbms_output_scroll = None
        self.state.show_dbms_output = False
        self.state.focus = FOCUS_EDITOR
        self.state.explain_scroll = 0
        self.state.results = [
            f"[{result.title}] {result.message}",
            *[line.text for line in explain_plan_lines(result)],
        ]
        self.state.active_tab.results_scroll = None
        self.state.status = result.message

    def handle_explain_error(
        self,
        exc: Exception,
        statement_start_line: int = 1,
        statement_start_col: int = 0,
    ) -> None:
        location = first_document_error_location(exc, statement_start_line, statement_start_col)
        moved_location = None
        lines = ["ERROR explaining statement:", *wrap_error(exc)]
        if location is not None:
            moved_location = move_buffer_to_error(self.state.buffer, location)
            lines.insert(1, error_location_line(moved_location))
        self.set_results(lines)
        self.state.focus = FOCUS_EDITOR
        if location is None:
            message = short_execution_error_message(exc)
            self.state.status = f"Explain failed: {message}" if message else "Explain failed"
            return
        message = short_execution_error_message(exc)
        suffix = f": {message}" if message else ""
        self.state.status = f"Explain failed at line {moved_location.line}, column {moved_location.column}{suffix}"


def error_location_line(location: ErrorLocation) -> str:
    return f"Error location: line {location.line}, column {location.column}"
