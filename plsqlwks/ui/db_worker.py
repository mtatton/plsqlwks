from __future__ import annotations

from dataclasses import dataclass, field
import queue
import threading
from typing import Any, Callable

__all__ = (
    "DatabaseWorker",
    "DbCommandHandle",
    "DbSessionState",
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


@dataclass(frozen=True)
class DbWorkerFinished:
    command_id: int
    result: Any
    error: Exception | None
    session_state: DbSessionState


DbWorkerEvent = DbWorkerProgress | DbWorkerFinished
DbProgressCallback = Callable[[str], None]
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
        try:
            self._set_session_state(self._snapshot_workspace())
            self._ready.set()
            while True:
                command = self._commands.get()
                if command is _STOP:
                    break
                assert isinstance(command, _Command)
                self._run_command(command)
        finally:
            self._ready.set()
            try:
                close = getattr(self._workspace, "close", None)
                if callable(close):
                    close()
            except BaseException as exc:
                self._shutdown_error = exc
            finally:
                self._set_session_state(self._snapshot_workspace())
                self._stopped.set()

    def _run_command(self, command: _Command) -> None:
        handle = command.handle
        with self._state_lock:
            self._current_command_id = handle.command_id

        result: Any = None
        error: Exception | None = None

        def report_progress(label: str) -> None:
            handle._emit(DbWorkerProgress(handle.command_id, label))

        try:
            result = command.task(self._workspace, report_progress)
        except Exception as exc:
            error = exc

        with self._state_lock:
            self._current_command_id = None
        session_state = self._snapshot_workspace()
        with self._state_lock:
            self._session_state = session_state
        handle._emit(DbWorkerFinished(handle.command_id, result, error, session_state))
        handle.done.set()

    def _set_session_state(self, session_state: DbSessionState) -> None:
        with self._state_lock:
            self._session_state = session_state

    def _snapshot_workspace(self) -> DbSessionState:
        connection = getattr(self._workspace, "connection", _MISSING)
        if connection is _MISSING:
            connected = bool(getattr(self._workspace, "connected", False))
        else:
            connected = connection is not None
        return DbSessionState(
            connected=connected,
            autocommit=bool(getattr(self._workspace, "autocommit", False)),
            read_only=bool(getattr(self._workspace, "read_only", False)),
            has_uncommitted_changes=bool(
                getattr(self._workspace, "has_uncommitted_changes", False)
            ),
        )
