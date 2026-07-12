from __future__ import annotations

import queue
import threading
from pathlib import Path

import pytest

import plsqlwks.db as db_module
from plsqlwks.config import AppConfig
from plsqlwks.db import OracleWorkspace, QueryResult
from plsqlwks.ui.db_worker import (
    DatabaseWorker,
    DbSessionState,
    DbWorkerFinished,
    DbWorkerProgress,
)


def wait_for(handle) -> list[object]:
    assert handle.done.wait(2), "database command did not finish"
    events: list[object] = []
    while True:
        try:
            events.append(handle.events.get_nowait())
        except queue.Empty:
            return events


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
    assert connection.cursor_call_thread_ids == [worker.thread.ident]
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
    assert old_connection.cursor_call_thread_ids == [worker_thread_id]
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
