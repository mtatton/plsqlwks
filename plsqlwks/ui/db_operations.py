from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from .db_worker import (
    DatabaseWorker,
    DatabaseWorkerUnavailableError,
    DbCommandHandle,
    DbSessionState,
    DbWorkerFinished,
    DbWorkerProgress,
)
from .errors import is_execution_interrupted, short_execution_error_message
from .ports import DatabaseWorkerPort, DbTask
from .state import (
    DbOperation,
    DbOperationFinished,
    FileTab,
    ScriptExecutionFailed,
    UIState,
    begin_database_operation,
    finish_database_operation,
    request_database_operation_cancel,
    update_database_operation_progress,
)

ResultHandler = Callable[[DbOperationFinished], None]
WorkerFactory = Callable[[object], DatabaseWorkerPort]


class DatabaseOperations:
    """Coordinate one foreground database command with the curses UI thread."""

    def __init__(
        self,
        state: UIState,
        worker: DatabaseWorkerPort | None = None,
        *,
        worker_factory: WorkerFactory = DatabaseWorker,
        result_handler: ResultHandler | None = None,
    ) -> None:
        self.state = state
        self._worker = worker
        self._session_state_is_authoritative = worker is not None or isinstance(state.db, DbSessionState)
        self._worker_factory = worker_factory
        self._result_handler = result_handler

    @property
    def active(self) -> bool:
        return self.state.db_operation is not None

    @property
    def completion_target_was_active(self) -> bool:
        return self.state.database.completion_target_was_active

    @property
    def completion_interrupted(self) -> bool:
        """Return whether the completion callback is handling an interruption."""
        return self.state.database.completion_interrupted

    def set_result_handler(self, handler: ResultHandler) -> None:
        """Set the UI-thread fallback used for operations without callbacks."""
        self._result_handler = handler

    def reject_if_active(self) -> bool:
        if not self.active:
            return False
        self.state.status = "Database operation already running"
        return True

    def interrupt(self) -> None:
        operation = self.state.db_operation
        if operation is None:
            self.state.status = "No database operation running"
            return
        if operation.cancel_requested:
            self.state.status = (
                "Database interrupt already requested"
                if operation.interrupt_database
                else "Cancellation already requested"
            )
            return
        cooperative = False
        try:
            if operation.on_interrupt is not None:
                operation.on_interrupt()
                cooperative = True
            database_cancelled = False
            if operation.interrupt_database:
                database_cancelled = self._ensure_worker().cancel_current_operation(operation.handle.command_id)
            if operation.interrupt_database and not database_cancelled:
                if cooperative:
                    operation = request_database_operation_cancel(
                        operation,
                        "Cancellation requested",
                    )
                    self.state.db_operation = operation
                    self.state.status = (
                        "Cancellation requested; database interrupt unavailable; "
                        "the operation will stop after the current unit of work"
                    )
                    return
                operation = update_database_operation_progress(
                    operation,
                    "Database interrupt unavailable",
                    operation.progress_current,
                    operation.progress_total,
                )
                self.state.db_operation = operation
                self.state.status = "Database interrupt unavailable"
                return
        except Exception as exc:
            message = short_execution_error_message(exc) or str(exc)
            if cooperative:
                operation = request_database_operation_cancel(
                    operation,
                    "Cancellation requested",
                )
                self.state.db_operation = operation
                self.state.status = "Cancellation requested"
                if message:
                    self.state.status += f"; database interrupt failed: {message}"
                self.state.status += "; the operation will stop after the current unit of work"
            else:
                operation = update_database_operation_progress(
                    operation,
                    "Database interrupt failed",
                    operation.progress_current,
                    operation.progress_total,
                )
                self.state.db_operation = operation
                self.state.status = f"Database interrupt failed: {message}" if message else "Database interrupt failed"
            return
        operation = request_database_operation_cancel(
            operation,
            operation.label,
        )
        self.state.db_operation = operation
        self.state.status = (
            "Cancellation requested" if not operation.interrupt_database else "Database interrupt requested"
        )

    def start(
        self,
        kind: str,
        label: str,
        task: DbTask,
        *,
        statement_start_line: int = 1,
        statement_start_col: int = 0,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        restore_active_tab: bool = True,
        source_text: str | None = None,
        statement_count: int = 1,
        replace_terminal_worker: bool = False,
        progress_current: int | None = None,
        progress_total: int | None = None,
        on_interrupt: Callable[[], object] | None = None,
        interrupt_database: bool = True,
    ) -> bool:
        if self.reject_if_active():
            return False
        worker: DatabaseWorkerPort | None = None
        try:
            worker = self._ensure_worker(
                replace_terminal=replace_terminal_worker,
            )
            try:
                handle = worker.submit(task)
            except DatabaseWorkerUnavailableError:
                if not replace_terminal_worker:
                    raise
                worker = self._replace_worker()
                handle = worker.submit(task)
        except Exception as exc:
            disconnected_state = self._sync_worker_session_state(worker)
            message = short_execution_error_message(exc) or str(exc)
            self.state.status = (
                f"Database operation unavailable: {message}" if message else "Database operation unavailable"
            )
            if disconnected_state is not None:
                self.state.status += f" | {self._disconnect_warning(disconnected_state)}"
            return False
        operation = DbOperation(
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
            source_text=source_text,
            statement_count=max(1, statement_count),
            progress_current=progress_current,
            progress_total=progress_total,
            on_interrupt=on_interrupt,
            interrupt_database=interrupt_database,
        )
        self.state.database = begin_database_operation(self.state.database, operation)
        self.state.status = label
        return True

    def submit_background(self, task: DbTask) -> DbCommandHandle:
        worker: DatabaseWorkerPort | None = None
        try:
            worker = self._ensure_worker()
            return worker.submit(task, ignored=True, background=True)
        except Exception:
            self._sync_worker_session_state(worker)
            handle = DbCommandHandle(
                command_id=0,
                events=queue.Queue(),
                done=threading.Event(),
                background=True,
            )
            handle.ignore()
            handle.done.set()
            return handle

    def poll(self) -> None:
        operation = self.state.db_operation
        if operation is None:
            return
        while True:
            try:
                event = operation.handle.events.get_nowait()
            except queue.Empty:
                if not operation.handle.done.is_set():
                    return
                session_state = self._worker_session_state_or_disconnected()
                event = DbWorkerFinished(
                    operation.handle.command_id,
                    None,
                    DatabaseWorkerUnavailableError("database worker stopped without a completion event"),
                    session_state,
                )
            if isinstance(event, DbWorkerProgress):
                operation = update_database_operation_progress(
                    operation,
                    event.label,
                    event.current,
                    event.total,
                )
                self.state.db_operation = operation
                continue
            if not isinstance(event, DbWorkerFinished):
                continue
            was_connected = bool(getattr(self.state.db, "connected", False))
            self.state.db = event.session_state
            disconnected = not event.session_state.connected and (was_connected or self._session_state_is_authoritative)
            session_lost = was_connected and disconnected
            if disconnected:
                self._detach_live_result_state("Connection lost; materialized rows are read-only")
            self.state.database = finish_database_operation(self.state.database)
            error = event.error
            partial_results = None
            statement_start_line = operation.statement_start_line
            statement_start_col = operation.statement_start_col
            failed_statement_index = None
            statement_count = operation.statement_count
            if isinstance(error, ScriptExecutionFailed):
                partial_results = error.partial_results
                statement_start_line = error.statement_start_line
                statement_start_col = error.statement_start_col
                failed_statement_index = error.statement_index
                statement_count = error.statement_count
                error = error.original
            interrupted = operation.interrupt_database and (
                operation.cancel_requested or (error is not None and is_execution_interrupted(error))
            )
            if interrupted:
                self._detach_live_result_state("Database operation interrupted; materialized rows are read-only")
            completed = DbOperationFinished(
                kind=operation.kind,
                result=event.result,
                error=error,
                statement_start_line=statement_start_line,
                statement_start_col=statement_start_col,
                partial_results=partial_results,
                source_text=operation.source_text,
                source_unchanged=(
                    operation.source_text is None or operation.tab.buffer.text() == operation.source_text
                ),
                statement_count=statement_count,
                failed_statement_index=failed_statement_index,
                interrupted=interrupted,
            )
            try:
                self._complete(completed, operation)
            finally:
                if disconnected:
                    # Completion handlers can attach a returned query page or a
                    # script's partial results after the first detach above.
                    # Those rows remain useful, but their cursor tokens belong
                    # to the session that just disappeared.
                    self._detach_live_result_state("Connection lost; materialized rows are read-only")
                elif interrupted:
                    self._detach_live_result_state("Database operation interrupted; materialized rows are read-only")
                    self.submit_background(lambda db, progress: db.close_all_result_continuations())
            if session_lost:
                warning = self._disconnect_warning(event.session_state)
                self.state.status = f"{self.state.status} | {warning}"
            elif interrupted:
                detail = "Database operation interrupted; materialized results are read-only"
                normalized_status = self.state.status.casefold()
                if "interrupted" in normalized_status and "read-only" not in normalized_status:
                    self.state.status += " | Materialized results are read-only"
                elif "interrupted" not in normalized_status and "cancel" not in normalized_status:
                    self.state.status = f"{self.state.status} | {detail}"
                self.state.status += f" | {self._interrupt_transaction_warning(event.session_state)}"
            return

    def wait(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while (operation := self.state.db_operation) is not None:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            operation.handle.done.wait(timeout=remaining)
            self.poll()
            if deadline is not None and time.monotonic() >= deadline:
                return

    def shutdown(self, timeout: float | None = None) -> None:
        if self._worker is None:
            return
        self._worker.shutdown(timeout=timeout)

    def _complete(
        self,
        event: DbOperationFinished,
        operation: DbOperation,
    ) -> None:
        original_idx = self.state.active_tab_idx
        target_idx = self._find_tab_index(operation.tab)
        if target_idx is None:
            target_idx = original_idx
        self.state.active_tab_idx = target_idx
        self.state.database.completion_target_was_active = target_idx == original_idx
        self.state.database.completion_interrupted = event.interrupted
        try:
            try:
                if event.error is not None and operation.on_error is not None:
                    operation.on_error(event.error)
                elif event.error is None and operation.on_success is not None:
                    operation.on_success(event.result)
                elif self._result_handler is not None:
                    self._result_handler(event)
                else:
                    raise RuntimeError("database operation result handler is not configured")
            except Exception as exc:
                message = short_execution_error_message(exc) or str(exc)
                self.state.results = [
                    "ERROR completing database operation:",
                    message or type(exc).__name__,
                ]
                self.state.status = (
                    f"Database operation completion failed: {message}"
                    if message
                    else "Database operation completion failed"
                )
        finally:
            self.state.database.completion_interrupted = False
            self.state.database.completion_target_was_active = False
            if operation.restore_active_tab:
                if original_idx < len(self.state.tabs):
                    self.state.active_tab_idx = original_idx
                else:
                    self.state.ensure_tab()

    def _find_tab_index(self, target: FileTab) -> int | None:
        for idx, tab in enumerate(self.state.tabs):
            if tab is target:
                return idx
        return None

    def detach_live_result_state(self, reason: str) -> None:
        self._detach_live_result_state(reason)

    def _detach_live_result_state(
        self,
        reason: str = "Connection lost; materialized rows are read-only",
    ) -> None:
        """Keep materialized rows visible while removing dead-session handles."""
        for tab in self.state.tabs:
            seen: set[int] = set()
            for result in (tab.active_result, tab.last_result):
                if result is None or id(result) in seen:
                    continue
                seen.add(id(result))
                result.continuation = None
                result.editable_context = None
                result.edit_message = reason
                result.detached_reason = reason
            draft = tab.result_insert_draft
            if draft is not None:
                if 0 <= draft.row_index < len(draft.result.rows) and draft.result.rows[draft.row_index] is draft.row:
                    draft.result.rows.pop(draft.row_index)
                    if draft.row_index < len(draft.result.original_rows):
                        draft.result.original_rows.pop(draft.row_index)
                tab.result_insert_draft = None

    def _sync_worker_session_state(
        self,
        worker: DatabaseWorkerPort | None,
    ) -> DbSessionState | None:
        if worker is None:
            return None
        try:
            session_state = worker.session_state
        except Exception:
            return None
        self.state.db = session_state
        if session_state.connected:
            return None
        self._detach_live_result_state("Connection lost; materialized rows are read-only")
        return session_state

    def _worker_session_state_or_disconnected(self) -> DbSessionState:
        try:
            session_state = self._worker.session_state if self._worker is not None else self.state.db
        except Exception:
            session_state = self.state.db
        return DbSessionState(
            connected=False,
            autocommit=bool(getattr(session_state, "autocommit", False)),
            read_only=bool(getattr(session_state, "read_only", False)),
            has_uncommitted_changes=bool(getattr(session_state, "has_uncommitted_changes", False)),
        )

    @staticmethod
    def _disconnect_warning(session_state: DbSessionState) -> str:
        if session_state.has_uncommitted_changes:
            return (
                "Disconnected; transaction outcome is unknown. Reconnect and explicitly resolve or discard the session"
            )
        return "Disconnected; no pending transaction was tracked; server outcome cannot be confirmed"

    @staticmethod
    def _interrupt_transaction_warning(session_state: DbSessionState) -> str:
        if session_state.autocommit:
            return "Autocommit was enabled; verify whether interrupted changes took effect"
        if session_state.has_uncommitted_changes:
            return "Pending transaction remains unresolved; commit or roll back explicitly"
        return "No pending transaction was tracked"

    def _ensure_worker(
        self,
        *,
        replace_terminal: bool = False,
    ) -> DatabaseWorkerPort:
        if self._worker is None:
            self._worker = self._worker_factory(self.state.db)
        elif replace_terminal and self._worker_is_terminal(self._worker):
            self._worker = self._replace_worker()
        return self._worker

    def _replace_worker(self) -> DatabaseWorkerPort:
        worker = self._worker_factory(self.state.db)
        self._worker = worker
        return worker

    @staticmethod
    def _worker_is_terminal(worker: DatabaseWorkerPort) -> bool:
        try:
            return bool(worker.terminal)
        except Exception:
            return False
