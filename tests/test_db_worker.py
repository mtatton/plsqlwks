from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import plsqlwks.db as db_module
from plsqlwks.config import AppConfig
from plsqlwks.db import (
    OracleWorkspace,
    QueryResult,
    QueryResultContinuation,
    QueryResultPage,
)
from plsqlwks.ui.app import App
from plsqlwks.ui.db_operations import DatabaseOperations
from plsqlwks.ui.result_presenter import ResultPresenter
from plsqlwks.ui.db_session import DatabaseSessionController
from plsqlwks.ui.db_worker import (
    DatabaseWorker,
    DatabaseWorkerUnavailableError,
    DbCommandHandle,
    DbSessionState,
    DbWorkerFinished,
    DbWorkerProgress,
)
from plsqlwks.ui.results import ResultInsertDraft
from plsqlwks.ui.state import FileTab, ResultFetchMore, ScriptExecutionFailed, UIState


def wait_for(handle) -> list[object]:
    assert handle.done.wait(2), "database command did not finish"
    events: list[object] = []
    while True:
        try:
            events.append(handle.events.get_nowait())
        except queue.Empty:
            return events


class ImmediateFinishedWorker:
    terminal = False

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.session_state = DbSessionState(True, False, False, False)

    def submit(self, task, *, ignored=False, background=False):
        handle = DbCommandHandle(1, queue.Queue(), threading.Event(), background)
        handle._emit(
            DbWorkerFinished(
                1,
                self.result,
                self.error,
                DbSessionState(False, False, False, False),
            )
        )
        handle.done.set()
        return handle

    def cancel_current_operation(self, command_id):
        return False

    def shutdown(self, timeout=None):
        return None


class FakeWorkspace:
    def __init__(self):
        self.connection = None
        self.autocommit = True
        self.read_only = False
        self.has_uncommitted_changes = False
        self.calls: list[tuple[str, int]] = []
        self.close_calls = 0
        self.close_thread_id: int | None = None

    def close(self) -> None:
        self.close_calls += 1
        self.close_thread_id = threading.get_ident()
        self.connection = None


def test_worker_runs_commands_fifo_on_one_persistent_thread_and_emits_events():
    workspace = FakeWorkspace()
    worker = DatabaseWorker(workspace)
    caller_thread_id = threading.get_ident()

    def task(label: str):
        def run(db, progress):
            thread_id = threading.get_ident()
            db.calls.append((label, thread_id))
            db.connection = object()
            db.has_uncommitted_changes = label == "second"
            progress(f"{label} progress")
            return f"{label} result"

        return run

    try:
        first = worker.submit(task("first"))
        second = worker.submit(task("second"))

        first_events = wait_for(first)
        second_events = wait_for(second)

        worker_thread_ids = {thread_id for _, thread_id in workspace.calls}
        assert [label for label, _ in workspace.calls] == ["first", "second"]
        assert len(worker_thread_ids) == 1
        assert caller_thread_id not in worker_thread_ids
        assert first_events[0] == DbWorkerProgress(first.command_id, "first progress")
        assert first_events[1] == DbWorkerFinished(
            first.command_id,
            "first result",
            None,
            DbSessionState(True, True, False, False),
        )
        assert second_events[0] == DbWorkerProgress(second.command_id, "second progress")
        assert second_events[1] == DbWorkerFinished(
            second.command_id,
            "second result",
            None,
            DbSessionState(True, True, False, True),
        )
        assert worker.session_state == DbSessionState(True, True, False, True)
        assert worker.thread.ident in worker_thread_ids
    finally:
        worker.shutdown()


def test_worker_reports_task_failure_and_keeps_processing_commands():
    workspace = FakeWorkspace()
    worker = DatabaseWorker(workspace)
    failure = RuntimeError("query failed")

    def fail(db, progress):
        db.read_only = True
        raise failure

    try:
        failed = worker.submit(fail)
        succeeded = worker.submit(lambda db, progress: "next result")

        failed_event = wait_for(failed)[0]
        succeeded_event = wait_for(succeeded)[0]

        assert isinstance(failed_event, DbWorkerFinished)
        assert failed_event.result is None
        assert failed_event.error is failure
        assert failed_event.session_state.read_only is True
        assert isinstance(succeeded_event, DbWorkerFinished)
        assert succeeded_event.result == "next result"
        assert succeeded_event.error is None
        assert succeeded_event.session_state.read_only is True
    finally:
        worker.shutdown()


def test_snapshot_failure_still_completes_handle_and_keeps_last_state():
    workspace = FakeWorkspace()
    worker = DatabaseWorker(workspace)

    def fail_snapshot():
        raise RuntimeError("snapshot failed")

    worker._snapshot_workspace = fail_snapshot
    try:
        handle = worker.submit(lambda db, progress: "executed")
        event = wait_for(handle)[0]

        assert isinstance(event, DbWorkerFinished)
        assert event.result == "executed"
        assert isinstance(event.error, DatabaseWorkerUnavailableError)
        assert "snapshot failed" in str(event.error)
        assert event.session_state == DbSessionState(False, True, False, False)
    finally:
        worker.shutdown()


def test_base_exception_during_session_snapshot_makes_worker_terminal():
    workspace = FakeWorkspace()
    workspace.connection = object()
    worker = DatabaseWorker(workspace)
    original_snapshot = worker._snapshot_workspace
    interrupted = False

    def interrupt_snapshot_once():
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt("snapshot interrupted")
        return original_snapshot()

    worker._snapshot_workspace = interrupt_snapshot_once
    handle = worker.submit(lambda db, progress: "executed")
    event = wait_for(handle)[0]

    assert isinstance(event, DbWorkerFinished)
    assert isinstance(event.error, DatabaseWorkerUnavailableError)
    assert "snapshot interrupted" in str(event.error)
    assert event.session_state.connected is False
    assert worker.terminal is True
    with pytest.raises(DatabaseWorkerUnavailableError, match="snapshot interrupted"):
        worker.submit(lambda db, progress: "must not run")
    with pytest.raises(DatabaseWorkerUnavailableError, match="snapshot interrupted"):
        worker.shutdown()


def test_fatal_task_failure_completes_and_fails_queued_commands():
    workspace = FakeWorkspace()
    worker = DatabaseWorker(workspace)

    first = worker.submit(
        lambda db, progress: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    second = worker.submit(lambda db, progress: "must not run")

    first_event = wait_for(first)[0]
    second_event = wait_for(second)[0]

    assert isinstance(first_event, DbWorkerFinished)
    assert isinstance(first_event.error, DatabaseWorkerUnavailableError)
    assert isinstance(second_event, DbWorkerFinished)
    assert second_event.result is None
    assert second_event.error is first_event.error
    with pytest.raises(DatabaseWorkerUnavailableError, match="unavailable"):
        worker.submit(lambda db, progress: None)
    with pytest.raises(DatabaseWorkerUnavailableError, match="KeyboardInterrupt"):
        worker.shutdown()


def test_explicit_connect_replaces_terminal_worker_but_ordinary_sql_does_not(
    tmp_path,
):
    class ReconnectWorkspace(FakeWorkspace):
        def ensure_connected(self):
            self.connection = object()

    class Dialogs:
        def prompt(self, label, default="", strip=True):
            raise AssertionError(f"unexpected prompt: {label}")

    class Presenter:
        def __init__(self):
            self.results: list[list[str]] = []

        def set_results(self, lines, clear_table=True):
            self.results.append(list(lines))

        def close_all_result_continuations(self):
            return None

        def invalidate_results_after_rollback(self):
            return None

    initial_workspace = ReconnectWorkspace()
    initial_worker = DatabaseWorker(initial_workspace)
    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=tmp_path / "orapass",
        workspace_dir=tmp_path,
    )
    state = UIState(config, initial_worker.session_state)
    replacement_workers: list[DatabaseWorker] = []

    def create_replacement(previous_session):
        assert previous_session == DbSessionState(False, False, False, False)
        workspace = ReconnectWorkspace()
        workspace.autocommit = previous_session.autocommit
        worker = DatabaseWorker(workspace)
        replacement_workers.append(worker)
        return worker

    operations = DatabaseOperations(
        state,
        worker=initial_worker,
        worker_factory=create_replacement,
    )
    controller = DatabaseSessionController(
        state,
        operations,
        Dialogs(),
        Presenter(),
    )
    failures: list[Exception] = []

    try:
        def fail_terminally(db, progress):
            db.autocommit = False
            raise KeyboardInterrupt()

        assert operations.start(
            "execute",
            "Executing",
            fail_terminally,
            on_error=failures.append,
        )
        operations.wait(timeout=2)

        assert len(failures) == 1
        assert initial_worker.terminal is True
        assert replacement_workers == []

        assert operations.start(
            "execute",
            "Executing ordinary SQL",
            lambda db, progress: "must not run",
        ) is False
        assert replacement_workers == []

        controller.try_connect()
        assert len(replacement_workers) == 1
        operations.wait(timeout=2)

        assert state.db.connected is True
        assert state.db.autocommit is False
        assert state.status == "Connected as hr"
    finally:
        try:
            initial_worker.shutdown()
        except DatabaseWorkerUnavailableError:
            pass
        operations.shutdown()


def test_app_replacement_worker_preserves_runtime_modes_not_pending_work(tmp_path):
    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=tmp_path / "orapass",
        workspace_dir=tmp_path,
        autocommit=True,
        read_only=False,
    )
    app = SimpleNamespace(state=SimpleNamespace(config=config), db_worker=None)

    worker = App._create_replacement_database_worker(
        app,
        DbSessionState(False, False, True, True),
    )
    try:
        assert worker.session_state == DbSessionState(False, False, True, False)
        assert app.db_worker is worker
    finally:
        worker.shutdown()


def test_completion_publication_failure_sets_done_and_fails_queued_commands():
    workspace = FakeWorkspace()
    workspace.connection = object()
    workspace.has_uncommitted_changes = True
    started = threading.Event()
    release = threading.Event()
    worker = DatabaseWorker(workspace)

    def blocking_task(db, progress):
        started.set()
        assert release.wait(2)
        return "first"

    first = worker.submit(blocking_task)
    second = worker.submit(lambda db, progress: "must not run")
    assert started.wait(2)
    first._emit = lambda event: (_ for _ in ()).throw(
        RuntimeError("event sink failed")
    )
    release.set()

    assert first.done.wait(2)
    second_event = wait_for(second)[0]
    assert isinstance(second_event, DbWorkerFinished)
    assert isinstance(second_event.error, DatabaseWorkerUnavailableError)
    assert second_event.session_state.connected is False
    assert second_event.session_state.has_uncommitted_changes is True
    with pytest.raises(DatabaseWorkerUnavailableError, match="event sink failed"):
        worker.shutdown()
    assert worker.session_state.has_uncommitted_changes is True


def test_dead_worker_submission_detaches_live_handles_without_running_callbacks(
    tmp_path,
):
    class DeadWorker:
        session_state = DbSessionState(False, False, False, True)

        def submit(self, task, *, ignored=False, background=False):
            raise DatabaseWorkerUnavailableError("worker stopped")

        def cancel_current_operation(self, command_id):
            return False

        def shutdown(self, timeout=None):
            return None

    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=tmp_path / "orapass",
        workspace_dir=tmp_path,
    )
    state = UIState(config, DbSessionState(True, False, False, True))
    result = QueryResult(
        "data",
        ["VALUE"],
        [["loaded"]],
        "1 row (more available)",
        continuation=QueryResultContinuation("dead-cursor"),
    )
    state.active_result = result
    state.last_result = result
    callbacks: list[str] = []
    operations = DatabaseOperations(state, worker=DeadWorker())

    assert operations.start(
        "execute",
        "Executing",
        lambda db, progress: None,
        on_error=lambda exc: callbacks.append(str(exc)),
    ) is False

    assert callbacks == []
    assert state.db == DbSessionState(False, False, False, True)
    assert state.active_result is result
    assert result.continuation is None
    assert "transaction outcome is unknown" in state.status

    background = operations.submit_background(lambda db, progress: None)
    assert background.done.is_set()
    assert background.ignored is True


def test_dead_worker_submission_detaches_live_state_from_every_tab(tmp_path):
    class DeadWorker:
        session_state = DbSessionState(False, False, False, False)

        def submit(self, task, *, ignored=False, background=False):
            raise DatabaseWorkerUnavailableError("worker stopped")

        def cancel_current_operation(self, command_id):
            return False

        def shutdown(self, timeout=None):
            return None

    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=tmp_path / "orapass",
        workspace_dir=tmp_path,
    )
    state = UIState(config, DbSessionState(True, False, False, False))
    expected: list[tuple[QueryResult, QueryResult, list[str]]] = []
    tabs: list[FileTab] = []
    for index in range(2):
        draft_row = [f"draft-{index}"]
        loaded_row = [f"loaded-{index}"]
        active = QueryResult(
            "data",
            ["VALUE"],
            [draft_row, loaded_row],
            "1 row (more available)",
            continuation=QueryResultContinuation(f"active-{index}"),
            original_rows=[[None], list(loaded_row)],
        )
        previous = QueryResult(
            "data",
            ["VALUE"],
            [[f"previous-{index}"]],
            "1 row (more available)",
            continuation=QueryResultContinuation(f"previous-{index}"),
        )
        tab = FileTab(active_result=active, last_result=previous)
        tab.result_insert_draft = ResultInsertDraft(active, 0, draft_row)
        tabs.append(tab)
        expected.append((active, previous, loaded_row))
    state.tabs = tabs
    operations = DatabaseOperations(state, worker=DeadWorker())

    assert operations.start(
        "execute",
        "Executing",
        lambda db, progress: None,
    ) is False

    for tab, (active, previous, loaded_row) in zip(state.tabs, expected):
        assert tab.active_result is active
        assert tab.last_result is previous
        assert active.rows == [loaded_row]
        assert active.original_rows == [loaded_row]
        assert active.continuation is None
        assert previous.continuation is None
        assert tab.result_insert_draft is None


def test_completion_callback_failure_is_contained_on_ui_poll(tmp_path):
    workspace = FakeWorkspace()
    workspace.connection = object()
    worker = DatabaseWorker(workspace)
    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=tmp_path / "orapass",
        workspace_dir=tmp_path,
    )
    state = UIState(config, worker.session_state)
    operations = DatabaseOperations(state, worker=worker)

    try:
        assert operations.start(
            "execute",
            "Executing",
            lambda db, progress: "done",
            on_success=lambda result: (_ for _ in ()).throw(
                RuntimeError("callback failed")
            ),
        )
        operations.wait(timeout=2)

        assert state.db_operation is None
        assert state.status == (
            "Database operation completion failed: RuntimeError: callback failed"
        )
        assert state.results == [
            "ERROR completing database operation:",
            "RuntimeError: callback failed",
        ]
    finally:
        worker.shutdown()


def test_done_handle_without_completion_event_cannot_strand_ui_operation(tmp_path):
    class NoCompletionWorker:
        session_state = DbSessionState(False, False, False, True)

        def submit(self, task, *, ignored=False, background=False):
            handle = DbCommandHandle(7, queue.Queue(), threading.Event())
            handle.done.set()
            return handle

        def cancel_current_operation(self, command_id):
            return False

        def shutdown(self, timeout=None):
            return None

    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=tmp_path / "orapass",
        workspace_dir=tmp_path,
    )
    state = UIState(config, DbSessionState(True, False, False, True))
    failures: list[Exception] = []
    operations = DatabaseOperations(state, worker=NoCompletionWorker())

    assert operations.start(
        "execute",
        "Executing",
        lambda db, progress: None,
        on_error=failures.append,
    )
    operations.poll()

    assert state.db_operation is None
    assert len(failures) == 1
    assert isinstance(failures[0], DatabaseWorkerUnavailableError)
    assert "transaction outcome is unknown" in state.status


def test_unexpected_session_loss_preserves_loaded_rows_and_clears_live_state(
    tmp_path,
):
    class HealthyConnection:
        def is_healthy(self):
            return True

    class UnhealthyConnection:
        def is_healthy(self):
            return False

    workspace = FakeWorkspace()
    workspace.connection = HealthyConnection()
    worker = DatabaseWorker(workspace)
    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=tmp_path / "orapass",
        workspace_dir=tmp_path,
    )
    state = UIState(config, worker.session_state)
    draft_row = ["draft"]
    result = QueryResult(
        "data",
        ["VALUE"],
        [draft_row, ["loaded"]],
        "1 row (more available)",
        continuation=QueryResultContinuation("lost-cursor"),
        original_rows=[[None], ["loaded"]],
    )
    state.active_result = result
    state.last_result = result
    state.active_tab.result_insert_draft = ResultInsertDraft(result, 0, draft_row)
    failures: list[Exception] = []
    operations = DatabaseOperations(state, worker=worker)

    def lose_session(db, progress):
        db.has_uncommitted_changes = True
        db.connection = UnhealthyConnection()
        raise RuntimeError("connection lost")

    try:
        assert operations.start(
            "execute",
            "Executing",
            lose_session,
            on_error=failures.append,
        )
        operations.wait(timeout=2)

        assert len(failures) == 1
        assert state.db == DbSessionState(False, True, False, True)
        assert state.active_result is result
        assert state.last_result is result
        assert result.rows == [["loaded"]]
        assert result.original_rows == [["loaded"]]
        assert result.continuation is None
        assert state.active_tab.result_insert_draft is None
        assert "transaction outcome is unknown" in state.status
    finally:
        worker.shutdown()


@pytest.mark.parametrize("initially_connected", [True, False])
def test_disconnected_completion_detaches_success_result_payload(
    tmp_path,
    initially_connected,
):
    result = QueryResult(
        "data",
        ["VALUE"],
        [["loaded"]],
        "1 row (more available)",
        continuation=QueryResultContinuation("dead-success-cursor"),
    )
    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=tmp_path / "orapass",
        workspace_dir=tmp_path,
    )
    state = UIState(
        config,
        DbSessionState(initially_connected, False, False, False),
    )
    operations = DatabaseOperations(state, worker=ImmediateFinishedWorker([result]))
    presenter = ResultPresenter(state, operations)
    operations.set_result_handler(presenter.apply_db_operation_result)

    assert operations.start("execute", "Executing", lambda db, progress: None)
    operations.poll()

    assert state.active_result is result
    assert result.rows == [["loaded"]]
    assert result.continuation is None
    assert result.has_more_rows is True


def test_session_loss_detaches_continuation_added_by_fetch_more_payload(tmp_path):
    result = QueryResult(
        "data",
        ["VALUE"],
        [["first"]],
        "1 row (more available)",
        continuation=QueryResultContinuation("old-cursor"),
    )
    fetched = ResultFetchMore(
        result,
        QueryResultPage(
            [["second"]],
            "2 rows (more available)",
            continuation=QueryResultContinuation("dead-next-cursor"),
        ),
        1,
    )
    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=tmp_path / "orapass",
        workspace_dir=tmp_path,
    )
    state = UIState(config, DbSessionState(True, False, False, False))
    state.active_result = result
    state.last_result = result
    operations = DatabaseOperations(state, worker=ImmediateFinishedWorker(fetched))
    presenter = ResultPresenter(state, operations)
    operations.set_result_handler(presenter.apply_db_operation_result)

    assert operations.start("fetch-more", "Fetching", lambda db, progress: None)
    operations.poll()

    assert state.active_result is result
    assert result.rows == [["first"], ["second"]]
    assert result.continuation is None
    assert result.has_more_rows is True


def test_session_loss_detaches_continuation_from_script_partial_results(tmp_path):
    partial = QueryResult(
        "Statement 1",
        ["VALUE"],
        [["loaded"]],
        "1 row (more available)",
        continuation=QueryResultContinuation("dead-partial-cursor"),
    )
    failure = ScriptExecutionFailed(
        RuntimeError("connection lost"),
        2,
        0,
        [partial],
        statement_index=2,
        statement_count=2,
    )
    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=tmp_path / "orapass",
        workspace_dir=tmp_path,
    )
    state = UIState(config, DbSessionState(True, False, False, False))
    operations = DatabaseOperations(state, worker=ImmediateFinishedWorker(error=failure))
    presenter = ResultPresenter(state, operations)
    operations.set_result_handler(presenter.apply_db_operation_result)

    assert operations.start("execute", "Executing script", lambda db, progress: None)
    operations.poll()

    assert state.active_result is partial
    assert partial.rows == [["loaded"]]
    assert partial.continuation is None
    assert partial.has_more_rows is True


def test_initial_session_snapshot_is_captured_before_first_command():
    workspace = FakeWorkspace()
    workspace.connection = object()
    workspace.autocommit = False
    workspace.read_only = True
    workspace.has_uncommitted_changes = True

    worker = DatabaseWorker(workspace)
    try:
        assert worker.session_state == DbSessionState(True, False, True, True)
    finally:
        worker.shutdown()


def test_session_snapshot_uses_local_connection_health():
    class UnhealthyConnection:
        def is_healthy(self):
            return False

    workspace = FakeWorkspace()
    workspace.connection = UnhealthyConnection()
    workspace.has_uncommitted_changes = True

    worker = DatabaseWorker(workspace)
    try:
        assert worker.session_state == DbSessionState(False, True, False, True)
    finally:
        worker.shutdown()


def test_ignored_and_background_commands_still_run_without_emitting_events():
    workspace = FakeWorkspace()
    worker = DatabaseWorker(workspace)

    def change_state(db, progress):
        progress("not displayed")
        db.has_uncommitted_changes = True
        return "not displayed"

    try:
        handle = worker.submit(change_state, ignored=True, background=True)

        assert wait_for(handle) == []
        assert handle.ignored is True
        assert handle.background is True
        assert worker.session_state.has_uncommitted_changes is True
    finally:
        worker.shutdown()


def test_ignore_discards_already_published_progress_and_suppresses_completion():
    workspace = FakeWorkspace()
    worker = DatabaseWorker(workspace)
    continue_task = threading.Event()

    def task(db, progress):
        progress("ready")
        assert continue_task.wait(2)
        return "ignored"

    try:
        handle = worker.submit(task)
        progress = handle.events.get(timeout=2)
        assert isinstance(progress, DbWorkerProgress)
        handle.events.put(progress)

        handle.ignore()
        continue_task.set()

        assert wait_for(handle) == []
    finally:
        continue_task.set()
        worker.shutdown()


class CancelWorkspace(FakeWorkspace):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel_calls: list[int] = []

    def cancel_current_operation(self) -> bool:
        self.cancel_calls.append(threading.get_ident())
        self.release.set()
        return True


def test_cancel_is_out_of_band_and_guarded_by_current_command_id():
    workspace = CancelWorkspace()
    worker = DatabaseWorker(workspace)
    second_started = threading.Event()
    release_second = threading.Event()

    def blocking_task(db, progress):
        db.started.set()
        assert db.release.wait(2)
        return "cancelled"

    def second_task(db, progress):
        second_started.set()
        assert release_second.wait(2)
        return "second"

    try:
        first = worker.submit(blocking_task)
        second = worker.submit(second_task)
        assert workspace.started.wait(2)

        assert worker.cancel_current_operation(second.command_id) is False
        assert worker.cancel_current_operation(first.command_id) is True
        assert wait_for(first)[0] == DbWorkerFinished(
            first.command_id,
            "cancelled",
            None,
            DbSessionState(False, True, False, False),
        )
        assert second_started.wait(2)
        assert worker.cancel_current_operation(first.command_id) is False
        assert workspace.cancel_calls == [threading.get_ident()]
        release_second.set()
        wait_for(second)
        assert worker.cancel_current_operation(first.command_id) is False
    finally:
        workspace.release.set()
        release_second.set()
        worker.shutdown()


def test_cancel_returns_false_when_workspace_does_not_support_it():
    workspace = FakeWorkspace()
    started = threading.Event()
    release = threading.Event()
    worker = DatabaseWorker(workspace)

    def task(db, progress):
        started.set()
        assert release.wait(2)

    try:
        handle = worker.submit(task)
        assert started.wait(2)
        assert worker.cancel_current_operation(handle.command_id) is False
    finally:
        release.set()
        worker.shutdown()


def test_shutdown_drains_work_closes_on_worker_thread_and_is_idempotent():
    workspace = FakeWorkspace()
    worker = DatabaseWorker(workspace)
    caller_thread_id = threading.get_ident()
    handle = worker.submit(lambda db, progress: db.calls.append(("queued", threading.get_ident())))

    worker.shutdown()
    worker.shutdown()

    assert handle.done.is_set()
    assert workspace.calls[0][0] == "queued"
    assert workspace.close_calls == 1
    assert workspace.close_thread_id == worker.thread.ident
    assert workspace.close_thread_id != caller_thread_id
    assert worker.session_state.connected is False
    with pytest.raises(RuntimeError, match="shut down"):
        worker.submit(lambda db, progress: None)


def test_shutdown_timeout_does_not_enqueue_more_than_one_stop_command():
    workspace = FakeWorkspace()
    started = threading.Event()
    release = threading.Event()
    worker = DatabaseWorker(workspace)

    def task(db, progress):
        started.set()
        assert release.wait(2)

    handle = worker.submit(task)
    assert started.wait(2)
    with pytest.raises(TimeoutError):
        worker.shutdown(timeout=0)

    release.set()
    worker.shutdown()

    assert handle.done.is_set()
    assert workspace.close_calls == 1


class ThreadRecordingCursor:
    def __init__(self, rows: list[tuple[int]]):
        self.rows = rows
        self.offset = 0
        self.arraysize = 0
        self.outputtypehandler = None
        self.description = [("VALUE",)]
        self.rowcount = -1
        self.thread_calls: list[tuple[str, int]] = []

    def execute(self, statement: str, params=None) -> None:
        self.thread_calls.append(("execute", threading.get_ident()))

    def fetchmany(self, size: int) -> list[tuple[int]]:
        self.thread_calls.append(("fetchmany", threading.get_ident()))
        rows = self.rows[self.offset : self.offset + size]
        self.offset += len(rows)
        return rows

    def close(self) -> None:
        self.thread_calls.append(("close", threading.get_ident()))


class ThreadRecordingConnection:
    def __init__(self):
        self.cursor_instance = ThreadRecordingCursor([(1,), (2,), (3,), (4,), (5,)])
        self.cursor_call_thread_ids: list[int] = []
        self.autocommit = True
        self.transaction_in_progress = False
        self.close_thread_id: int | None = None

    def cursor(self) -> ThreadRecordingCursor:
        self.cursor_call_thread_ids.append(threading.get_ident())
        return self.cursor_instance

    def close(self) -> None:
        self.close_thread_id = threading.get_ident()


def test_real_workspace_keeps_paged_cursor_on_the_persistent_worker_thread(tmp_path):
    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=Path(tmp_path) / "orapass",
        workspace_dir=Path(tmp_path),
        max_rows=2,
    )
    workspace = OracleWorkspace(config)
    connection = ThreadRecordingConnection()
    workspace.connection = connection
    worker = DatabaseWorker(workspace)
    caller_thread_id = threading.get_ident()

    try:
        executed = worker.submit(
            lambda db, progress: db.execute_statement(
                "select 1 as value from dual union all select 2 from dual"
            )
        )
        executed_event = wait_for(executed)[0]
        assert isinstance(executed_event, DbWorkerFinished)
        assert isinstance(executed_event.result, QueryResult)
        result = executed_event.result
        assert result.continuation is not None

        first_page = worker.submit(
            lambda db, progress: db.fetch_more_rows(result.continuation, len(result.rows))
        )
        first_page_event = wait_for(first_page)[0]
        assert isinstance(first_page_event, DbWorkerFinished)
        result.rows.extend(first_page_event.result.rows)
        result.continuation = first_page_event.result.continuation
        assert result.continuation is not None

        final_page = worker.submit(
            lambda db, progress: db.fetch_more_rows(result.continuation, len(result.rows))
        )
        final_page_event = wait_for(final_page)[0]
        assert isinstance(final_page_event, DbWorkerFinished)
        assert final_page_event.result.continuation is None
    finally:
        worker.shutdown()

    cursor_thread_ids = {thread_id for _call, thread_id in connection.cursor_instance.thread_calls}
    assert connection.cursor_call_thread_ids
    assert set(connection.cursor_call_thread_ids) == {worker.thread.ident}
    assert cursor_thread_ids == {worker.thread.ident}
    assert caller_thread_id not in cursor_thread_ids
    assert connection.close_thread_id == worker.thread.ident


def test_reconnect_creates_connection_and_cleans_up_on_worker_thread(tmp_path, monkeypatch):
    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=Path(tmp_path) / "orapass",
        workspace_dir=Path(tmp_path),
        max_rows=2,
    )
    workspace = OracleWorkspace(config)
    old_connection = ThreadRecordingConnection()
    new_connection = ThreadRecordingConnection()
    connect_thread_ids: list[int] = []
    workspace.connection = old_connection
    workspace.enable_dbms_output = lambda: None

    monkeypatch.setattr(db_module, "read_password", lambda path: "secret")

    def connect(**params):
        connect_thread_ids.append(threading.get_ident())
        return new_connection

    monkeypatch.setattr(db_module.oracledb, "connect", connect)
    worker = DatabaseWorker(workspace)

    try:
        executed = worker.submit(
            lambda db, progress: db.execute_statement(
                "select 1 as value from dual union all select 2 from dual"
            )
        )
        executed_event = wait_for(executed)[0]
        assert isinstance(executed_event, DbWorkerFinished)
        assert isinstance(executed_event.result, QueryResult)
        assert executed_event.result.continuation is not None

        def reconnect(db, progress):
            db.close()
            db.ensure_connected()

        reconnected = wait_for(worker.submit(reconnect))[0]
        assert isinstance(reconnected, DbWorkerFinished)
        assert reconnected.error is None
        assert workspace.connection is new_connection
    finally:
        worker.shutdown()

    worker_thread_id = worker.thread.ident
    old_cursor_thread_ids = {
        thread_id for _call, thread_id in old_connection.cursor_instance.thread_calls
    }
    assert old_connection.cursor_call_thread_ids
    assert set(old_connection.cursor_call_thread_ids) == {worker_thread_id}
    assert old_cursor_thread_ids == {worker_thread_id}
    assert old_connection.close_thread_id == worker_thread_id
    assert connect_thread_ids == [worker_thread_id]
    assert new_connection.close_thread_id == worker_thread_id


def test_workspace_close_error_still_publishes_disconnected_clean_session_state(tmp_path):
    class CloseFailingConnection:
        autocommit = False

        def close(self) -> None:
            raise RuntimeError("connection close failed")

    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=Path(tmp_path) / "orapass",
        workspace_dir=Path(tmp_path),
        autocommit=False,
    )
    workspace = OracleWorkspace(config)
    workspace.connection = CloseFailingConnection()
    workspace.record_pending_rows(3)
    worker = DatabaseWorker(workspace)

    with pytest.raises(RuntimeError, match="connection close failed"):
        worker.shutdown()

    assert worker.session_state.connected is False
    assert worker.session_state.has_uncommitted_changes is False
