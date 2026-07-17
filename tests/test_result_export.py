from __future__ import annotations

import threading
from pathlib import Path

import pytest

import plsqlwks.ui as ui
import plsqlwks.ui.result_export as result_export_module
from plsqlwks.config import AppConfig
from plsqlwks.db import (
    EditableResultContext,
    QueryResult,
    QueryResultContinuation,
    QueryResultPage,
)
from plsqlwks.plugins.csv_export import CsvExportOptions
from plsqlwks.ui.plugin_host import UIPluginContext, snapshot_result
from plsqlwks.ui.state import UIState
from tests.ui_harness import ServiceHarness


class ExportPagingDb:
    def __init__(self, pages: list[tuple[list[list[str]], bool]]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[int, int | None]] = []
        self.connected = True
        self.autocommit = False
        self.read_only = False
        self.has_uncommitted_changes = False
        self.cancel_calls = 0

    def fetch_more_rows(
        self,
        continuation: QueryResultContinuation,
        loaded_rows: int,
        page_rows: int | None = None,
    ) -> QueryResultPage:
        self.calls.append((loaded_rows, page_rows))
        rows, more = self.pages.pop(0)
        return QueryResultPage(
            rows,
            f"{loaded_rows + len(rows)} row(s)",
            [[int(row[0])] for row in rows],
            continuation=continuation if more else None,
            has_more_rows=more,
        )

    def close_result_continuation(self, continuation: QueryResultContinuation) -> None:
        pass

    def close_all_result_continuations(self) -> None:
        pass

    def cancel_current_operation(self) -> bool:
        self.cancel_calls += 1
        return True

    def close(self) -> None:
        self.connected = False


def make_config(root: Path, *, max_rows: int = 2) -> AppConfig:
    return AppConfig(
        user="hr",
        dsn="db",
        password_file=root / "orapass",
        workspace_dir=root,
        max_rows=max_rows,
    )


def make_app(root: Path, db: object, *, max_rows: int = 2) -> ServiceHarness:
    app = ServiceHarness()
    app.state = UIState(config=make_config(root, max_rows=max_rows), db=db)
    app._wire()
    return app


def export_context(
    app: ServiceHarness,
    root: Path,
    responses: list[str | None],
    *,
    full: bool | None = False,
) -> UIPluginContext:
    answers = iter(responses)
    app.dialogs.pick = lambda title, options: None if full is None else int(full)
    active_result = app.state.active_result
    return UIPluginContext(
        root,
        result_snapshot=None,
        insert_draft=False,
        prompt=lambda label, default, strip: next(answers),
        set_status=lambda message: setattr(app.state, "status", message),
        set_results=lambda lines, clear_table: app.result_presenter.set_results(
            lines,
            clear_table=clear_table,
        ),
        result_snapshot_factory=lambda: snapshot_result(active_result),
    )


def continuing_result() -> QueryResult:
    return QueryResult(
        "Query",
        ["ID"],
        [["1"], ["2"]],
        "2 row(s); more rows available",
        continuation=QueryResultContinuation("export-token"),
        original_rows=[[1], [2]],
        has_more_rows=True,
    )


def test_export_mode_uses_shared_picker_with_loaded_rows_selected_first(tmp_path):
    app = make_app(tmp_path, ExportPagingDb([]))
    app.state.active_result = continuing_result()
    calls: list[tuple[str, list[str]]] = []
    context = export_context(app, tmp_path, [], full=None)

    def pick(title: str, options: list[str]) -> None:
        calls.append((title, options))
        return None

    app.dialogs.pick = pick

    app.result_export("csv", context, CsvExportOptions())

    assert calls == [
        (
            "Export rows",
            [
                "Loaded rows only (default)",
                "All available rows (keep the result grid unchanged)",
            ],
        )
    ]
    assert app.state.status == "Export cancelled"
    assert app.state.db_operation is None


def test_loaded_rows_remain_the_default_and_show_writer_progress(tmp_path):
    db = ExportPagingDb([([["3"]], False)])
    app = make_app(tmp_path, db)
    result = continuing_result()
    app.state.active_result = result
    destination = tmp_path / "loaded.csv"
    context = export_context(app, tmp_path, [str(destination)])

    try:
        app.result_export("csv", context, CsvExportOptions())
        operation = app.state.db_operation
        assert operation is not None
        assert operation.kind == "export-write"
        assert (operation.progress_current, operation.progress_total) == (0, 2)
        app.wait_for_db_operation(timeout=2)
    finally:
        app.shutdown_database_worker(timeout=2)

    assert db.calls == []
    assert destination.read_text(encoding="utf-8") == "ID\n1\n2\n"
    assert app.state.status.startswith("Exported 2 loaded row(s)")
    assert result.continuation is not None


def test_full_export_uses_private_pages_and_keeps_result_grid_unchanged(tmp_path):
    db = ExportPagingDb(
        [
            ([["3"], ["4"]], True),
            ([["5"]], False),
        ]
    )
    app = make_app(tmp_path, db)
    result = continuing_result()
    app.state.active_result = result
    app.state.result_row = 1
    app.state.result_col = 0
    app.state.result_row_scroll = 1
    app.state.results = ["visible result transcript"]
    destination = tmp_path / "full.csv"
    context = export_context(app, tmp_path, [str(destination)], full=True)

    try:
        app.result_export("csv", context, CsvExportOptions())
        app.wait_for_db_operation(timeout=2)
    finally:
        app.shutdown_database_worker(timeout=2)

    assert db.calls == [(2, 2), (4, 2)]
    assert result.rows == [["1"], ["2"]]
    assert result.original_rows == [[1], [2]]
    assert result.continuation is None
    assert result.message == "2 row(s); more rows available"
    assert result.has_more_rows is True
    assert (app.state.result_row, app.state.result_col, app.state.result_row_scroll) == (1, 0, 1)
    assert app.state.results == ["visible result transcript"]
    assert destination.read_text(encoding="utf-8") == "ID\n1\n2\n3\n4\n5\n"
    assert app.state.status.startswith("Exported all 5 available row(s)")


def test_full_export_can_write_more_than_ten_thousand_rows_without_growing_grid(tmp_path):
    first_page = [[str(value)] for value in range(3, 5_003)]
    second_page = [[str(value)] for value in range(5_003, 10_003)]
    db = ExportPagingDb(
        [
            (first_page, True),
            (second_page, True),
            ([["10003"]], False),
        ]
    )
    app = make_app(tmp_path, db, max_rows=5_000)
    result = continuing_result()
    app.state.active_result = result
    destination = tmp_path / "uncapped.csv"
    context = export_context(app, tmp_path, [str(destination)], full=True)

    try:
        app.result_export("csv", context, CsvExportOptions())
        app.wait_for_db_operation(timeout=2)
    finally:
        app.shutdown_database_worker(timeout=2)

    assert db.calls == [(2, 5_000), (5_002, 5_000), (10_002, 5_000)]
    assert result.rows == [["1"], ["2"]]
    assert result.original_rows == [[1], [2]]
    assert result.continuation is None
    exported_lines = destination.read_text(encoding="utf-8").splitlines()
    assert len(exported_lines) == 10_004
    assert exported_lines[:4] == ["ID", "1", "2", "3"]
    assert exported_lines[-1] == "10003"
    assert app.state.status.startswith("Exported all 10,003 available row(s)")


def test_full_export_refuses_detached_cursor_without_touching_destination(tmp_path):
    app = make_app(tmp_path, ExportPagingDb([]))
    result = continuing_result()
    result.continuation = None
    app.state.active_result = result
    destination = tmp_path / "detached.csv"
    context = export_context(app, tmp_path, [], full=True)

    app.result_export("csv", context, CsvExportOptions())

    assert app.state.db_operation is None
    assert not destination.exists()
    assert "cursor is no longer available" in app.state.status


@pytest.mark.parametrize("oracle_cancelled", [True, False])
def test_ctrl_c_cancels_full_export_fetch_and_detaches_live_result(
    tmp_path,
    oracle_cancelled,
):
    class BlockingPagingDb(ExportPagingDb):
        def __init__(self):
            super().__init__([])
            self.fetch_started = threading.Event()
            self.fetch_cancelled = threading.Event()

        def fetch_more_rows(
            self,
            continuation: QueryResultContinuation,
            loaded_rows: int,
            page_rows: int | None = None,
        ) -> QueryResultPage:
            self.calls.append((loaded_rows, page_rows))
            self.fetch_started.set()
            assert self.fetch_cancelled.wait(2)
            raise RuntimeError("fetch stopped after cancel")

        def cancel_current_operation(self) -> bool:
            self.cancel_calls += 1
            self.fetch_cancelled.set()
            return oracle_cancelled

    db = BlockingPagingDb()
    app = make_app(tmp_path, db)
    result = continuing_result()
    result.editable_context = EditableResultContext("DECISIONS", 0, {0: "ID"})
    app.state.active_result = result
    app.state.last_result = result
    destination = tmp_path / "cancelled-full.csv"
    context = export_context(app, tmp_path, [str(destination)], full=True)

    try:
        app.result_export("csv", context, CsvExportOptions())
        assert db.fetch_started.wait(1)

        app.handle_key(ui.CTRL_C)
        app.wait_for_db_operation(timeout=2)

        assert db.cancel_calls == 1
        assert result.rows == [["1"], ["2"]]
        assert result.original_rows == [[1], [2]]
        assert result.continuation is None
        assert result.editable_context is None
        assert result.detached_reason == ("Database operation interrupted; materialized rows are read-only")
        assert not destination.exists()
        assert "Export cancelled during row fetch after 2 row(s)" in app.state.status
        assert "No pending transaction was tracked" in app.state.status
    finally:
        app.shutdown_database_worker(timeout=2)


def test_full_export_cancelled_while_queued_never_starts_fetch(tmp_path):
    db = ExportPagingDb([([["3"]], False)])
    app = make_app(tmp_path, db)
    result = continuing_result()
    result.editable_context = EditableResultContext("DECISIONS", 0, {0: "ID"})
    app.state.active_result = result
    destination = tmp_path / "queued-cancel.csv"
    context = export_context(app, tmp_path, [str(destination)], full=True)
    blocker_started = threading.Event()
    release_blocker = threading.Event()

    def block_worker(workspace, progress):
        blocker_started.set()
        assert release_blocker.wait(2)

    try:
        background = app.db_operations.submit_background(block_worker)
        assert blocker_started.wait(1)
        app.result_export("csv", context, CsvExportOptions())

        app.handle_key(ui.CTRL_C)
        release_blocker.set()
        app.wait_for_db_operation(timeout=2)

        assert background.done.is_set()
        assert db.cancel_calls == 0
        assert db.calls == []
        assert result.continuation is None
        assert result.editable_context is None
        assert not destination.exists()
        assert "Export cancelled during row fetch after 2 row(s)" in app.state.status
    finally:
        release_blocker.set()
        app.shutdown_database_worker(timeout=2)


def test_ctrl_c_cancels_writer_without_oracle_cancel_or_state_loss(
    tmp_path,
    monkeypatch,
):
    writer_started = threading.Event()

    def blocking_writer(path, snapshot, options, *, on_progress, cancelled):
        writer_started.set()
        while not cancelled():
            threading.Event().wait(0.01)
        raise result_export_module.ExportCancelled("export cancelled")

    monkeypatch.setattr(result_export_module, "write_csv_snapshot", blocking_writer)
    db = ExportPagingDb([])
    db.autocommit = True
    app = make_app(tmp_path, db)
    result = continuing_result()
    context_before = EditableResultContext("DECISIONS", 0, {0: "ID"})
    result.editable_context = context_before
    continuation_before = result.continuation
    app.state.active_result = result
    destination = tmp_path / "cancelled-write.csv"
    destination.write_text("previous\n", encoding="utf-8")
    context = export_context(
        app,
        tmp_path,
        [str(destination), "yes"],
    )

    try:
        app.result_export("csv", context, CsvExportOptions())
        assert writer_started.wait(1)

        app.handle_key(ui.CTRL_C)
        app.wait_for_db_operation(timeout=2)

        assert db.cancel_calls == 0
        assert result.continuation is continuation_before
        assert result.editable_context is context_before
        assert result.detached_reason == ""
        assert destination.read_text(encoding="utf-8") == "previous\n"
        assert "Export cancelled; destination unchanged" in app.state.status
        assert "did not interrupt Oracle or change transaction state" in app.state.status
        assert "autocommit remains enabled" in app.state.status
    finally:
        app.shutdown_database_worker(timeout=2)


def test_full_export_cancelled_while_writer_queued_stops_before_snapshot_copy(
    tmp_path,
    monkeypatch,
):
    db = ExportPagingDb([])
    app = make_app(tmp_path, db)
    result = QueryResult(
        "Query",
        ["ID"],
        [["1"], ["2"]],
        "2 row(s)",
        original_rows=[[1], [2]],
    )
    app.state.active_result = result
    destination = tmp_path / "queued-writer.csv"
    context = export_context(app, tmp_path, [str(destination)], full=True)
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    snapshot_calls = 0
    original_snapshot_result = result_export_module.snapshot_result

    def counted_snapshot(query_result, *, cancelled=None):
        nonlocal snapshot_calls
        snapshot_calls += 1
        return original_snapshot_result(query_result, cancelled=cancelled)

    def fail_writer(*args, **kwargs):
        pytest.fail("cancelled queued export reached the file writer")

    def block_worker(workspace, progress):
        blocker_started.set()
        assert release_blocker.wait(2)

    monkeypatch.setattr(result_export_module, "snapshot_result", counted_snapshot)
    monkeypatch.setattr(result_export_module, "write_csv_snapshot", fail_writer)

    try:
        background = app.db_operations.submit_background(block_worker)
        assert blocker_started.wait(1)
        app.result_export("csv", context, CsvExportOptions())

        app.handle_key(ui.CTRL_C)
        release_blocker.set()
        app.wait_for_db_operation(timeout=2)

        assert background.done.is_set()
        assert snapshot_calls == 1
        assert db.cancel_calls == 0
        assert not destination.exists()
        assert "Export cancelled; destination unchanged" in app.state.status
    finally:
        release_blocker.set()
        app.shutdown_database_worker(timeout=2)


def test_full_export_cancel_race_keeps_completed_page_but_writes_no_file(tmp_path):
    class RacingPagingDb(ExportPagingDb):
        def __init__(self):
            super().__init__([])
            self.fetch_started = threading.Event()
            self.cancel_seen = threading.Event()

        def fetch_more_rows(
            self,
            continuation: QueryResultContinuation,
            loaded_rows: int,
            page_rows: int | None = None,
        ) -> QueryResultPage:
            self.calls.append((loaded_rows, page_rows))
            self.fetch_started.set()
            assert self.cancel_seen.wait(2)
            return QueryResultPage(
                [["3"]],
                "3 row(s); more rows available",
                [[3]],
                continuation=continuation,
                has_more_rows=True,
            )

        def cancel_current_operation(self) -> bool:
            self.cancel_calls += 1
            self.cancel_seen.set()
            return True

    db = RacingPagingDb()
    app = make_app(tmp_path, db)
    result = continuing_result()
    result.editable_context = EditableResultContext("DECISIONS", 0, {0: "ID"})
    app.state.active_result = result
    destination = tmp_path / "cancel-race.csv"
    context = export_context(app, tmp_path, [str(destination)], full=True)

    try:
        app.result_export("csv", context, CsvExportOptions())
        assert db.fetch_started.wait(1)

        app.handle_key(ui.CTRL_C)
        app.wait_for_db_operation(timeout=2)

        assert db.calls == [(2, 2)]
        assert result.rows == [["1"], ["2"]]
        assert result.original_rows == [[1], [2]]
        assert result.continuation is None
        assert result.editable_context is None
        assert not destination.exists()
        assert "Export cancelled during row fetch after 3 row(s)" in app.state.status
    finally:
        app.shutdown_database_worker(timeout=2)


def test_full_export_connection_loss_keeps_page_and_stops_chain(tmp_path):
    class LosingConnectionPagingDb(ExportPagingDb):
        def __init__(self):
            super().__init__([([["3"]], True)])
            self.connection = object()

        def fetch_more_rows(
            self,
            continuation: QueryResultContinuation,
            loaded_rows: int,
            page_rows: int | None = None,
        ) -> QueryResultPage:
            page = super().fetch_more_rows(
                continuation,
                loaded_rows,
                page_rows,
            )
            self.connection = None
            return page

    db = LosingConnectionPagingDb()
    app = make_app(tmp_path, db)
    result = continuing_result()
    result.editable_context = EditableResultContext("DECISIONS", 0, {0: "ID"})
    app.state.active_result = result
    destination = tmp_path / "lost-connection.csv"
    context = export_context(app, tmp_path, [str(destination)], full=True)

    try:
        app.result_export("csv", context, CsvExportOptions())
        app.wait_for_db_operation(timeout=2)

        assert db.calls == [(2, 2)]
        assert result.rows == [["1"], ["2"]]
        assert result.original_rows == [[1], [2]]
        assert result.continuation is None
        assert result.editable_context is None
        assert not destination.exists()
        assert "connection was lost; no file was written" in app.state.status
        assert "no pending transaction was tracked" in app.state.status
    finally:
        app.shutdown_database_worker(timeout=2)
