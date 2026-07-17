from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = (
    "DatabaseWorker",
    "DatabaseWorkerUnavailableError",
    "DbCommandHandle",
    "DbProgressCallback",
    "DbSessionState",
    "DbWorkerTask",
    "DbWorkerEvent",
    "DbWorkerFinished",
    "DbWorkerProgress",
)


@dataclass(frozen=True)
class DbSessionState:
    connected: bool
    autocommit: bool
    read_only: bool
    has_uncommitted_changes: bool


@dataclass(frozen=True)
class DbWorkerProgress:
    command_id: int
    label: str
    current: int | None = None
    total: int | None = None


@dataclass(frozen=True)
class DbWorkerFinished:
    command_id: int
    result: Any
    error: Exception | None
    session_state: DbSessionState


DbWorkerEvent = DbWorkerProgress | DbWorkerFinished


class DbProgressCallback(Protocol):
    def __call__(
        self,
        label: str,
        *,
        current: int | None = None,
        total: int | None = None,
    ) -> None: ...


DbWorkerTask = Callable[[Any, DbProgressCallback], Any]


@dataclass
class DbCommandHandle:
    command_id: int
    events: queue.Queue[DbWorkerEvent]
    done: threading.Event
    background: bool = False
    _ignored: bool = field(default=False, repr=False)
    _event_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def ignored(self) -> bool:
        with self._event_lock:
            return self._ignored

    def ignore(self) -> None:
        """Discard this command's events without cancelling its database work."""
        with self._event_lock:
            self._ignored = True
            while True:
                try:
                    self.events.get_nowait()
                except queue.Empty:
                    return

    def _emit(self, event: DbWorkerEvent) -> None:
        with self._event_lock:
            if not self._ignored:
                self.events.put(event)


@dataclass(frozen=True)
class _Command:
    task: DbWorkerTask
    handle: DbCommandHandle


_STOP = object()
_MISSING = object()


class DatabaseWorkerUnavailableError(RuntimeError):
    """Raised when the database worker can no longer process commands."""


class DatabaseWorker:
    """Serialize all access to one database workspace on one persistent thread."""

    def __init__(self, workspace: Any):
        self._workspace = workspace
        self._commands: queue.Queue[_Command | object] = queue.Queue()
        self._state_lock = threading.Lock()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._accepting_commands = True
        self._next_command_id = 1
        self._current_command_id: int | None = None
        self._shutdown_error: BaseException | None = None
        self._terminal_error: Exception | None = None
        self._session_state = DbSessionState(False, False, False, False)
        self._thread = threading.Thread(
            target=self._run,
            name="plsqlwks-db-worker",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    @property
    def session_state(self) -> DbSessionState:
        with self._state_lock:
            return self._session_state

    @property
    def thread(self) -> threading.Thread:
        """The persistent thread, exposed for waiting and diagnostics only."""
        return self._thread

    @property
    def terminal(self) -> bool:
        """Return whether this worker can never accept another command."""
        with self._state_lock:
            return self._terminal_error is not None or self._stopped.is_set()

    def submit(
        self,
        task: DbWorkerTask,
        *,
        ignored: bool = False,
        background: bool = False,
    ) -> DbCommandHandle:
        if not callable(task):
            raise TypeError("database worker task must be callable")
        with self._state_lock:
            if not self._accepting_commands:
                if self._terminal_error is not None:
                    raise DatabaseWorkerUnavailableError(
                        f"database worker is unavailable: {self._terminal_error}"
                    ) from self._terminal_error
                raise RuntimeError("database worker is shut down")
            command_id = self._next_command_id
            self._next_command_id += 1
            handle = DbCommandHandle(
                command_id=command_id,
                events=queue.Queue(),
                done=threading.Event(),
                background=background,
                _ignored=ignored,
            )
            self._commands.put(_Command(task, handle))
        return handle

    def cancel_current_operation(self, command_id: int) -> bool:
        """Cancel only if *command_id* is still the command being executed."""
        with self._state_lock:
            if command_id != self._current_command_id:
                return False
            cancel = getattr(self._workspace, "cancel_current_operation", None)
            if not callable(cancel):
                return False
            return bool(cancel())

    def shutdown(self, timeout: float | None = None) -> None:
        """Drain queued work, close the workspace on its owner thread, and stop."""
        if threading.current_thread() is self._thread:
            raise RuntimeError("database worker cannot shut itself down")
        with self._state_lock:
            if self._accepting_commands:
                self._accepting_commands = False
                self._commands.put(_STOP)
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("database worker did not shut down before the timeout")
        if self._shutdown_error is not None:
            raise self._shutdown_error

    def _run(self) -> None:
        fatal_error: Exception | None = None
        try:
            initial_state, initial_error, _ = self._safe_snapshot_workspace(self._session_state)
            self._set_session_state(initial_state)
            if initial_error is not None:
                fatal_error = DatabaseWorkerUnavailableError(
                    f"initial database session snapshot failed: {initial_error}"
                )
                raise fatal_error
            self._ready.set()
            while True:
                command = self._commands.get()
                if command is _STOP:
                    break
                assert isinstance(command, _Command)
                command_fatal = self._run_command(command)
                if command_fatal is not None:
                    fatal_error = command_fatal
                    raise command_fatal
        except BaseException as exc:
            if fatal_error is None:
                fatal_error = self._exception_as_unavailable(exc)
            with self._state_lock:
                previous_state = self._session_state
                self._session_state = DbSessionState(
                    connected=False,
                    autocommit=previous_state.autocommit,
                    read_only=previous_state.read_only,
                    has_uncommitted_changes=previous_state.has_uncommitted_changes,
                )
                self._terminal_error = fatal_error
                self._accepting_commands = False
            self._fail_queued_commands(fatal_error)
        finally:
            self._ready.set()
            pre_close_state = self.session_state
            try:
                close = getattr(self._workspace, "close", None)
                if callable(close):
                    close()
            except BaseException as exc:
                if fatal_error is None:
                    self._shutdown_error = exc
            finally:
                (
                    final_state,
                    final_snapshot_error,
                    final_snapshot_fatal,
                ) = self._safe_snapshot_workspace(self.session_state)
                if (
                    fatal_error is None
                    and self._shutdown_error is None
                    and final_snapshot_fatal
                    and final_snapshot_error is not None
                ):
                    self._shutdown_error = final_snapshot_error
                if fatal_error is not None:
                    final_state = DbSessionState(
                        connected=False,
                        autocommit=final_state.autocommit,
                        read_only=final_state.read_only,
                        has_uncommitted_changes=(
                            pre_close_state.has_uncommitted_changes or final_state.has_uncommitted_changes
                        ),
                    )
                self._set_session_state(final_state)
                with self._state_lock:
                    self._accepting_commands = False
                if fatal_error is not None:
                    self._shutdown_error = fatal_error
                self._stopped.set()

    def _run_command(self, command: _Command) -> Exception | None:
        handle = command.handle
        with self._state_lock:
            self._current_command_id = handle.command_id

        result: Any = None
        error: Exception | None = None

        def report_progress(
            label: str,
            *,
            current: int | None = None,
            total: int | None = None,
        ) -> None:
            handle._emit(DbWorkerProgress(handle.command_id, label, current, total))

        fatal_error: Exception | None = None
        try:
            result = command.task(self._workspace, report_progress)
        except Exception as exc:
            error = exc
        except BaseException as exc:
            fatal_error = self._exception_as_unavailable(exc)
            error = fatal_error
            with self._state_lock:
                self._terminal_error = fatal_error
                self._accepting_commands = False
        finally:
            with self._state_lock:
                self._current_command_id = None
                fallback_state = self._session_state
            (
                session_state,
                snapshot_error,
                snapshot_fatal,
            ) = self._safe_snapshot_workspace(fallback_state)
            if fatal_error is None and snapshot_fatal and snapshot_error is not None:
                fatal_error = snapshot_error
                with self._state_lock:
                    self._terminal_error = fatal_error
                    self._accepting_commands = False
            if fatal_error is not None:
                session_state = DbSessionState(
                    connected=False,
                    autocommit=session_state.autocommit,
                    read_only=session_state.read_only,
                    has_uncommitted_changes=session_state.has_uncommitted_changes,
                )
            if snapshot_error is not None:
                if error is None:
                    error = DatabaseWorkerUnavailableError(f"database session snapshot failed: {snapshot_error}")
                else:
                    with suppress(Exception):
                        setattr(error, "worker_snapshot_error", snapshot_error)  # noqa: B010  # reason: cleanup diagnostics are attached to arbitrary exception types when supported
            with self._state_lock:
                self._session_state = session_state
            self._finish_handle(
                handle,
                DbWorkerFinished(handle.command_id, result, error, session_state),
            )
        return fatal_error

    def _set_session_state(self, session_state: DbSessionState) -> None:
        with self._state_lock:
            self._session_state = session_state

    def _snapshot_workspace(self) -> DbSessionState:
        connection = getattr(self._workspace, "connection", _MISSING)
        if connection is _MISSING:
            connected = bool(getattr(self._workspace, "connected", False))
        else:
            health = getattr(self._workspace, "connection_is_healthy", None)
            if callable(health):
                connected = bool(health())
            elif connection is None:
                connected = False
            else:
                is_healthy = getattr(connection, "is_healthy", None)
                connected = bool(is_healthy()) if callable(is_healthy) else True
        return DbSessionState(
            connected=connected,
            autocommit=bool(getattr(self._workspace, "autocommit", False)),
            read_only=bool(getattr(self._workspace, "read_only", False)),
            has_uncommitted_changes=bool(getattr(self._workspace, "has_uncommitted_changes", False)),
        )

    def _safe_snapshot_workspace(
        self,
        fallback: DbSessionState,
    ) -> tuple[DbSessionState, Exception | None, bool]:
        try:
            return self._snapshot_workspace(), None, False
        except Exception as exc:
            return fallback, exc, False
        except BaseException as exc:
            return fallback, self._exception_as_unavailable(exc), True

    def _fail_queued_commands(self, error: Exception) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            if command is _STOP:
                continue
            assert isinstance(command, _Command)
            try:
                self._finish_handle(
                    command.handle,
                    DbWorkerFinished(
                        command.handle.command_id,
                        None,
                        error,
                        self.session_state,
                    ),
                )
            except BaseException:
                # A broken event sink must not strand later queued handles.
                continue

    @staticmethod
    def _finish_handle(handle: DbCommandHandle, event: DbWorkerFinished) -> None:
        try:
            handle._emit(event)
        finally:
            handle.done.set()

    @staticmethod
    def _exception_as_unavailable(exc: BaseException) -> Exception:
        if isinstance(exc, Exception):
            return DatabaseWorkerUnavailableError(str(exc) or type(exc).__name__)
        return DatabaseWorkerUnavailableError(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
