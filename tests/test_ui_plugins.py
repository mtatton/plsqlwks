from __future__ import annotations

import curses
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

import plsqlwks.ui.app as app_module
from plsqlwks.config import AppConfig
from plsqlwks.db import QueryResult, QueryResultContinuation
from plsqlwks.plugins import (
    PLUGIN_API_VERSION,
    Plugin,
    PluginCommand,
    PluginContext,
    ResultSnapshot,
)
from plsqlwks.plugins.csv_export import CsvExportOptions
from plsqlwks.plugins.csv_export import create_plugin as create_csv_export_plugin
from plsqlwks.plugins.html_export import create_plugin as create_html_export_plugin
from plsqlwks.plugins.loader import PluginRegistry, load_plugin_registry
from plsqlwks.plugins.xlsx_export import create_plugin as create_xlsx_export_plugin
from plsqlwks.ui.app import App
from plsqlwks.ui.commands import COMMAND_MENU_ITEMS, filtered_command_indexes
from plsqlwks.ui.constants import FOCUS_RESULTS
from plsqlwks.ui.plugin_host import PluginHost, UIPluginContext, snapshot_result
from plsqlwks.ui.state import UIState


pytestmark = pytest.mark.plugin


class FakeScreen:
    def __init__(self) -> None:
        self.height = 24
        self.width = 120

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def keypad(self, enabled: bool) -> None:
        pass

    def leaveok(self, enabled: bool) -> None:
        pass

    def timeout(self, delay: int) -> None:
        pass


def make_config(root: Path, *, read_only: bool = False) -> AppConfig:
    return AppConfig(
        user="hr",
        dsn="db",
        password_file=root / "orapass",
        workspace_dir=root,
        read_only=read_only,
    )


def make_context(
    results_dir: Path,
    *,
    result: QueryResult | None = None,
    insert_draft: bool = False,
    statuses: list[str] | None = None,
    result_messages: list[tuple[list[str], bool]] | None = None,
) -> UIPluginContext:
    status_messages = statuses if statuses is not None else []
    messages = result_messages if result_messages is not None else []
    captured_result = None if insert_draft else snapshot_result(result)
    return UIPluginContext(
        results_dir,
        result_snapshot=captured_result,
        insert_draft=insert_draft,
        prompt=lambda label, default, strip: None,
        set_status=status_messages.append,
        set_results=lambda lines, clear_table: messages.append((lines, clear_table)),
    )


def test_context_returns_an_immutable_copy_of_the_loaded_result(tmp_path):
    result = QueryResult(
        "Current statement",
        ["A", "B"],
        [["one", "two"]],
        "1 row",
        continuation=QueryResultContinuation("opaque-token"),
        original_rows=[[1, 2]],
    )
    context = make_context(tmp_path, result=result)

    snapshot = context.get_active_result()

    assert snapshot is not None
    assert snapshot.title == "Current statement"
    assert snapshot.columns == ("A", "B")
    assert snapshot.rows == (("one", "two"),)
    assert snapshot.has_more is True
    assert isinstance(snapshot.columns, tuple)
    assert isinstance(snapshot.rows, tuple)
    assert isinstance(snapshot.rows[0], tuple)
    assert not hasattr(context, "state")
    assert not hasattr(context, "active_result")
    assert not hasattr(context, "query_result")
    assert "_get_active_query_result" not in UIPluginContext.__slots__
    result.columns[0] = "CHANGED"
    result.rows[0][0] = "changed"
    result.continuation = None
    assert snapshot.columns == ("A", "B")
    assert snapshot.rows == (("one", "two"),)
    assert snapshot.has_more is True


def test_context_captures_insert_draft_without_a_result(tmp_path):
    context = UIPluginContext(
        tmp_path,
        result_snapshot=None,
        insert_draft=True,
        prompt=lambda label, default, strip: None,
        set_status=lambda message: None,
        set_results=lambda lines, clear_table: None,
    )

    assert context.has_active_insert_draft() is True
    assert context.get_active_result() is None


def test_context_delegates_modal_prompts_and_overwrite_confirmation(tmp_path):
    calls: list[tuple[str, str, bool]] = []
    answers = iter(["  keep spaces  ", "yes"])

    def prompt(label: str, default: str, strip: bool) -> str:
        calls.append((label, default, strip))
        return next(answers)

    context = UIPluginContext(
        tmp_path,
        result_snapshot=None,
        insert_draft=False,
        prompt=prompt,
        set_status=lambda message: None,
        set_results=lambda lines, clear_table: None,
    )

    assert context.prompt_text("Filename", "default.csv", strip=False) == "  keep spaces  "
    destination = tmp_path / "existing.csv"
    assert context.confirm_overwrite(destination) is True
    assert calls == [
        ("Filename", "default.csv", False),
        (f"Overwrite {destination}? y/n", "", True),
    ]


def test_context_report_error_preserves_active_result_and_continuation(tmp_path):
    app = object.__new__(App)
    app.screen = FakeScreen()
    app.state = UIState(config=make_config(tmp_path), db=object())
    continuation = QueryResultContinuation("opaque-token")
    result = QueryResult(
        "data",
        ["A"],
        [["1"]],
        "1 row",
        continuation=continuation,
    )
    app.state.active_result = result
    context = UIPluginContext(
        tmp_path,
        result_snapshot=snapshot_result(result),
        insert_draft=False,
        prompt=lambda label, default, strip: None,
        set_status=lambda message: setattr(app.state, "status", message),
        set_results=lambda lines, clear_table: app.set_results(
            lines,
            clear_table=clear_table,
        ),
    )

    context.report_error("CSV export", OSError("disk full"))

    assert app.state.active_result is result
    assert result.continuation is continuation
    assert app.state.results[0] == "ERROR CSV export:"
    assert any("disk full" in line for line in app.state.results)


def test_plugin_host_adds_csv_html_xlsx_without_mutating_builtin_commands(tmp_path):
    original_commands = COMMAND_MENU_ITEMS
    registry = load_plugin_registry(entry_points=())
    context = make_context(tmp_path)
    host = PluginHost(registry, lambda: context)

    assert COMMAND_MENU_ITEMS is original_commands
    assert host.command_menu_items[: len(original_commands)] == original_commands
    assert len(host.command_menu_items) == len(original_commands) + 3
    csv_command, html_command, xlsx_command = host.command_menu_items[-3:]
    assert csv_command.section == "Results"
    assert csv_command.title == "Export loaded rows to CSV"
    assert csv_command.shortcut == ""
    assert html_command.section == "Results"
    assert html_command.title == "Export loaded rows to HTML"
    assert html_command.shortcut == ""
    assert html_command.handler == "__plugin__:html-export:export-loaded-rows"
    assert not hasattr(App, html_command.handler)
    assert not hasattr(App, "export_loaded_rows_to_html")
    assert xlsx_command.section == "Results"
    assert xlsx_command.title == "Export loaded rows to XLSX"
    assert xlsx_command.shortcut == ""
    assert xlsx_command.handler == "__plugin__:xlsx-export:export-loaded-rows"
    assert not hasattr(App, xlsx_command.handler)
    assert not hasattr(App, "export_loaded_rows_to_xlsx")
    assert PLUGIN_API_VERSION == 1
    assert filtered_command_indexes(host.command_menu_items, "csv") == [
        len(host.command_menu_items) - 3
    ]
    assert filtered_command_indexes(host.command_menu_items, "html") == [
        len(host.command_menu_items) - 2
    ]
    assert filtered_command_indexes(host.command_menu_items, "xlsx") == [
        len(host.command_menu_items) - 1
    ]


def test_open_commands_menu_dispatches_html_through_plugin_mapping(tmp_path):
    statuses: list[str] = []
    context = make_context(tmp_path, statuses=statuses)
    host = PluginHost(
        PluginRegistry((create_html_export_plugin(),), ()),
        lambda: context,
    )
    app = object.__new__(App)
    app.state = UIState(config=make_config(tmp_path), db=object())
    app._plugin_host = host
    app.command_menu_items = host.command_menu_items
    html_command = host.command_menu_items[-1]
    app.pick_command_menu = lambda commands: html_command
    app.refresh_modal_background = lambda: None

    App.open_commands_menu(app)

    assert statuses == ["No table result is available for export"]


def test_open_commands_menu_dispatches_xlsx_through_plugin_mapping(tmp_path):
    statuses: list[str] = []
    context = make_context(tmp_path, statuses=statuses)
    host = PluginHost(
        PluginRegistry((create_xlsx_export_plugin(),), ()),
        lambda: context,
    )
    app = object.__new__(App)
    app.state = UIState(config=make_config(tmp_path), db=object())
    app._plugin_host = host
    app.command_menu_items = host.command_menu_items
    xlsx_command = host.command_menu_items[-1]
    app.pick_command_menu = lambda commands: xlsx_command
    app.refresh_modal_background = lambda: None

    App.open_commands_menu(app)

    assert statuses == ["No table result is available for export"]


def test_open_commands_menu_dispatches_plugin_without_app_getattr(tmp_path):
    calls: list[object] = []
    plugin = Plugin(
        id="example",
        name="Example",
        commands=(
            PluginCommand(
                id="run",
                section="Results",
                title="Run example",
                handler=calls.append,
            ),
        ),
    )
    context = make_context(tmp_path)
    host = PluginHost(PluginRegistry((plugin,), ()), lambda: context)
    app = object.__new__(App)
    app.state = UIState(config=make_config(tmp_path), db=object())
    app._plugin_host = host
    app.command_menu_items = host.command_menu_items
    selected = host.command_menu_items[-1]
    app.pick_command_menu = lambda commands: selected
    app.refresh_modal_background = lambda: None

    App.open_commands_menu(app)

    assert calls == [context]


def test_plugin_handler_exception_is_reported_without_changing_grid(tmp_path):
    def fail(context: object) -> None:
        raise RuntimeError("plugin exploded")

    plugin = Plugin(
        id="broken-command",
        name="Broken command",
        commands=(
            PluginCommand("explode", "Results", "Explode", fail),
        ),
    )
    app = object.__new__(App)
    app.screen = FakeScreen()
    app.state = UIState(config=make_config(tmp_path), db=object())
    continuation = QueryResultContinuation("opaque-token")
    result = QueryResult("data", ["A"], [["1"]], "1 row", continuation=continuation)
    app.state.active_result = result
    app.state.result_row = 0
    app.state.result_col = 0
    context = UIPluginContext(
        tmp_path,
        result_snapshot=snapshot_result(result),
        insert_draft=False,
        prompt=lambda label, default, strip: None,
        set_status=lambda message: setattr(app.state, "status", message),
        set_results=lambda lines, clear_table: app.set_results(
            lines,
            clear_table=clear_table,
        ),
    )
    host = PluginHost(PluginRegistry((plugin,), ()), lambda: context)

    assert host.execute(host.command_menu_items[-1].handler) is True

    assert app.state.status == "Explode failed: RuntimeError: plugin exploded"
    assert app.state.active_result is result
    assert result.continuation is continuation
    assert (app.state.result_row, app.state.result_col) == (0, 0)
    assert app.state.results[0] == "ERROR Plugin command failed: Explode:"
    assert any("plugin exploded" in line for line in app.state.results)


def test_plugin_host_does_not_catch_base_exceptions(tmp_path):
    def interrupt(context: object) -> None:
        raise KeyboardInterrupt

    plugin = Plugin(
        id="interrupt",
        name="Interrupt",
        commands=(PluginCommand("run", "Results", "Interrupt", interrupt),),
    )
    context = make_context(tmp_path)
    host = PluginHost(PluginRegistry((plugin,), ()), lambda: context)

    with pytest.raises(KeyboardInterrupt):
        host.execute(host.command_menu_items[-1].handler)


def test_plugin_command_remains_available_in_read_only_mode(tmp_path):
    calls: list[object] = []
    plugin = Plugin(
        id="readonly-export",
        name="Read-only export",
        commands=(
            PluginCommand("run", "Results", "Export", calls.append),
        ),
    )
    config = make_config(tmp_path, read_only=True)
    app = object.__new__(App)
    app.state = UIState(config=config, db=SimpleNamespace(read_only=True))
    context = make_context(tmp_path)
    host = PluginHost(PluginRegistry((plugin,), ()), lambda: context)
    app._plugin_host = host
    app.command_menu_items = host.command_menu_items
    selected = host.command_menu_items[-1]
    app.pick_command_menu = lambda commands: selected
    app.refresh_modal_background = lambda: None

    App.open_commands_menu(app)

    assert calls == [context]


def test_csv_plugin_through_app_preserves_ui_and_never_uses_database_worker(tmp_path):
    class NoDatabaseWorkerAccess:
        def __getattr__(self, name: str) -> object:
            pytest.fail(f"CSV export accessed database worker attribute {name!r}")

    config = make_config(tmp_path, read_only=True)
    database_state = SimpleNamespace(
        autocommit=False,
        has_uncommitted_changes=True,
        read_only=True,
    )
    app = object.__new__(App)
    app.screen = FakeScreen()
    app.state = UIState(config=config, db=database_state)
    app.db_worker = NoDatabaseWorkerAccess()
    continuation = QueryResultContinuation("opaque-token")
    original_rows = [[1, "original"]]
    result = QueryResult(
        "Current result",
        ["ID", "VALUE"],
        [["1", "display value"]],
        "1 row",
        continuation=continuation,
        original_rows=original_rows,
    )
    app.state.active_result = result
    app.state.result_row = 0
    app.state.result_col = 1
    app.state.buffer.lines = ["select * from current_result"]
    app.state.buffer.row = 0
    app.state.buffer.col = 7
    active_tab = app.state.active_tab
    destination = tmp_path / "results" / "current.csv"
    app.active_insert_draft = lambda: None
    app.prompt_text_box = lambda label, default="", strip=True: str(destination)
    app._plugin_host = PluginHost(
        PluginRegistry((create_csv_export_plugin(),), ()),
        app._create_plugin_context,
    )

    assert app._plugin_host.execute(app._plugin_host.command_menu_items[-1].handler) is True

    assert destination.read_text(encoding="utf-8") == "ID,VALUE\n1,display value\n"
    assert app.state.status == f"Exported 1 loaded row(s) to {destination}; additional rows are available"
    assert app.state.active_result is result
    assert result.rows == [["1", "display value"]]
    assert result.original_rows == original_rows
    assert result.continuation is continuation
    assert (app.state.result_row, app.state.result_col) == (0, 1)
    assert app.state.active_tab is active_tab
    assert app.state.active_tab_idx == 0
    assert app.state.buffer.lines == ["select * from current_result"]
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 7)
    assert database_state.autocommit is False
    assert database_state.has_uncommitted_changes is True


def test_html_plugin_through_app_preserves_ui_and_never_uses_database_worker(tmp_path):
    class NoDatabaseWorkerAccess:
        def __getattr__(self, name: str) -> object:
            pytest.fail(f"HTML export accessed database worker attribute {name!r}")

    config = make_config(tmp_path, read_only=True)
    database_state = SimpleNamespace(
        autocommit=False,
        has_uncommitted_changes=True,
        read_only=True,
    )
    app = object.__new__(App)
    app.screen = FakeScreen()
    app.state = UIState(config=config, db=database_state)
    app.db_worker = NoDatabaseWorkerAccess()
    continuation = QueryResultContinuation("opaque-token")
    original_rows = [[1, "original"]]
    result = QueryResult(
        "Current result",
        ["ID", "VALUE"],
        [["1", "display value"]],
        "1 row",
        continuation=continuation,
        original_rows=original_rows,
    )
    app.state.active_result = result
    app.state.result_row = 0
    app.state.result_col = 1
    app.state.result_row_scroll = 0
    app.state.result_col_scroll = 1
    app.state.active_tab.results_scroll = 3
    app.state.focus = FOCUS_RESULTS
    app.state.buffer.lines = ["select * from current_result"]
    app.state.buffer.row = 0
    app.state.buffer.col = 7
    active_tab = app.state.active_tab
    destination = tmp_path / "results" / "current.html"
    app.active_insert_draft = lambda: None
    app.prompt_text_box = lambda label, default="", strip=True: str(destination)
    app._plugin_host = PluginHost(
        PluginRegistry((create_html_export_plugin(),), ()),
        app._create_plugin_context,
    )

    assert app._plugin_host.execute(app._plugin_host.command_menu_items[-1].handler) is True

    document = destination.read_text(encoding="utf-8")
    assert document.startswith("<!doctype html>\n")
    assert "1 loaded row(s)" in document
    assert "Additional rows are available in PLSQLWKS and were not exported." in document
    assert app.state.active_result is result
    assert result.rows == [["1", "display value"]]
    assert result.original_rows == original_rows
    assert result.continuation is continuation
    assert (app.state.result_row, app.state.result_col) == (0, 1)
    assert (app.state.result_row_scroll, app.state.result_col_scroll) == (0, 1)
    assert app.state.active_tab.results_scroll == 3
    assert app.state.active_tab is active_tab
    assert app.state.active_tab_idx == 0
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.buffer.lines == ["select * from current_result"]
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 7)
    assert app.state.db_operation is None
    assert database_state.autocommit is False
    assert database_state.has_uncommitted_changes is True


def test_xlsx_plugin_through_app_preserves_ui_and_never_uses_database_worker(tmp_path):
    class NoDatabaseWorkerAccess:
        def __getattr__(self, name: str) -> object:
            pytest.fail(f"XLSX export accessed database worker attribute {name!r}")

    config = make_config(tmp_path, read_only=True)
    database_state = SimpleNamespace(
        autocommit=False,
        has_uncommitted_changes=True,
        read_only=True,
    )
    app = object.__new__(App)
    app.screen = FakeScreen()
    app.state = UIState(config=config, db=database_state)
    app.db_worker = NoDatabaseWorkerAccess()
    continuation = QueryResultContinuation("opaque-token")
    original_rows = [[1, "original"]]
    result = QueryResult(
        "Current result",
        ["ID", "VALUE"],
        [["1", "display value"]],
        "1 row",
        continuation=continuation,
        original_rows=original_rows,
    )
    app.state.active_result = result
    app.state.result_row = 0
    app.state.result_col = 1
    app.state.result_row_scroll = 0
    app.state.result_col_scroll = 1
    app.state.active_tab.results_scroll = 3
    app.state.focus = FOCUS_RESULTS
    app.state.buffer.lines = ["select * from current_result"]
    app.state.buffer.row = 0
    app.state.buffer.col = 7
    active_tab = app.state.active_tab
    destination = tmp_path / "results" / "current.xlsx"
    app.active_insert_draft = lambda: None
    app.prompt_text_box = lambda label, default="", strip=True: str(destination)
    app._plugin_host = PluginHost(
        PluginRegistry((create_xlsx_export_plugin(),), ()),
        app._create_plugin_context,
    )

    assert app._plugin_host.execute(app._plugin_host.command_menu_items[-1].handler) is True

    with zipfile.ZipFile(destination) as workbook:
        assert "[Content_Types].xml" in workbook.namelist()
        assert "xl/worksheets/sheet1.xml" in workbook.namelist()
    assert app.state.status == (
        f"Exported 1 loaded row(s) to {destination}; additional rows are available"
    )
    assert app.state.active_result is result
    assert result.rows == [["1", "display value"]]
    assert result.original_rows == original_rows
    assert result.continuation is continuation
    assert (app.state.result_row, app.state.result_col) == (0, 1)
    assert (app.state.result_row_scroll, app.state.result_col_scroll) == (0, 1)
    assert app.state.active_tab.results_scroll == 3
    assert app.state.active_tab is active_tab
    assert app.state.active_tab_idx == 0
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.buffer.lines == ["select * from current_result"]
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 7)
    assert app.state.db_operation is None
    assert database_state.autocommit is False
    assert database_state.has_uncommitted_changes is True


def test_app_initialization_owns_combined_commands_and_plugin_warnings(monkeypatch, tmp_path):
    registry = PluginRegistry(
        (
            create_csv_export_plugin(),
            create_html_export_plugin(),
            create_xlsx_export_plugin(),
        ),
        ("Plugin warning",),
    )
    loaded_options: list[CsvExportOptions | None] = []

    class FakeWorker:
        def __init__(self, workspace: object) -> None:
            self.session_state = object()

    monkeypatch.setattr(app_module, "OracleWorkspace", lambda config: object())
    monkeypatch.setattr(app_module, "DatabaseWorker", FakeWorker)
    def load_registry(*, csv_export_options=None):
        loaded_options.append(csv_export_options)
        return registry

    monkeypatch.setattr(app_module, "load_plugin_registry", load_registry)
    monkeypatch.setattr(app_module, "list_workspace_files", lambda config: [])

    config = replace(
        make_config(tmp_path),
        csv_export_separator=";",
        csv_export_null_value="NULL",
        csv_export_date_format="%d.%m.%Y",
    )
    app = App(FakeScreen(), config)

    assert app.command_menu_items[: len(COMMAND_MENU_ITEMS)] == COMMAND_MENU_ITEMS
    assert [command.title for command in app.command_menu_items[-3:]] == [
        "Export loaded rows to CSV",
        "Export loaded rows to HTML",
        "Export loaded rows to XLSX",
    ]
    assert app._plugin_startup_warnings == ("Plugin warning",)
    assert loaded_options == [
        CsvExportOptions(
            separator=";",
            null_value="NULL",
            date_format="%d.%m.%Y",
        )
    ]


def test_app_configures_the_builtin_csv_handler(monkeypatch, tmp_path):
    class FakeWorker:
        def __init__(self, workspace: object) -> None:
            self.session_state = object()

    monkeypatch.setattr(app_module, "OracleWorkspace", lambda config: object())
    monkeypatch.setattr(app_module, "DatabaseWorker", FakeWorker)
    monkeypatch.setattr(
        app_module,
        "load_plugin_registry",
        lambda **kwargs: load_plugin_registry(entry_points=(), **kwargs),
    )
    monkeypatch.setattr(app_module, "list_workspace_files", lambda config: [])
    config = replace(
        make_config(tmp_path),
        csv_export_separator=";",
        csv_export_null_value="NULL",
        csv_export_date_format="%d.%m.%Y",
    )
    app = App(FakeScreen(), config)
    destination = config.results_dir / "configured.csv"
    app.state.active_result = QueryResult(
        "data",
        ["MISSING", "CREATED"],
        [["<NULL>", "2026-07-12"]],
        "1 row",
    )
    app.active_insert_draft = lambda: None
    app.prompt_text_box = lambda label, default="", strip=True: str(destination)

    csv_command = next(
        command
        for command in app.command_menu_items
        if command.title == "Export loaded rows to CSV"
    )
    assert app._plugin_host.execute(csv_command.handler) is True

    assert destination.read_text(encoding="utf-8") == (
        "MISSING;CREATED\nNULL;12.07.2026\n"
    )


def test_app_context_factory_checks_draft_before_snapshot(monkeypatch, tmp_path):
    registry = PluginRegistry((create_csv_export_plugin(),), ())

    class FakeWorker:
        def __init__(self, workspace: object) -> None:
            self.session_state = object()

    monkeypatch.setattr(app_module, "OracleWorkspace", lambda config: object())
    monkeypatch.setattr(app_module, "DatabaseWorker", FakeWorker)
    monkeypatch.setattr(app_module, "load_plugin_registry", lambda **kwargs: registry)
    monkeypatch.setattr(app_module, "list_workspace_files", lambda config: [])
    app = App(FakeScreen(), make_config(tmp_path))
    app.state.active_result = QueryResult("data", ["A"], [["draft"]], "1 row")
    app.active_insert_draft = lambda: object()
    monkeypatch.setattr(
        app_module,
        "snapshot_result",
        lambda result: pytest.fail("draft result must not be snapshotted"),
    )

    assert app._plugin_host.execute(app.command_menu_items[-1].handler) is True

    assert app.state.status.startswith("Export unavailable while an insert draft is active")


def test_app_snapshots_result_immediately_before_plugin_handler(monkeypatch, tmp_path):
    snapshots: list[ResultSnapshot | None] = []
    result = QueryResult("data", ["A"], [["before"]], "1 row")

    def mutate_after_command_starts(context: PluginContext) -> None:
        result.rows[0][0] = "after"
        snapshots.append(context.get_active_result())

    plugin = Plugin(
        id="snapshot",
        name="Snapshot",
        commands=(
            PluginCommand(
                "capture",
                "Results",
                "Capture result",
                mutate_after_command_starts,
            ),
        ),
    )
    registry = PluginRegistry((plugin,), ())

    class FakeWorker:
        def __init__(self, workspace: object) -> None:
            self.session_state = object()

    monkeypatch.setattr(app_module, "OracleWorkspace", lambda config: object())
    monkeypatch.setattr(app_module, "DatabaseWorker", FakeWorker)
    monkeypatch.setattr(app_module, "load_plugin_registry", lambda **kwargs: registry)
    monkeypatch.setattr(app_module, "list_workspace_files", lambda config: [])
    app = App(FakeScreen(), make_config(tmp_path))
    app.state.active_result = result
    app.active_insert_draft = lambda: None

    assert app._plugin_host.execute(app.command_menu_items[-1].handler) is True

    assert snapshots[0] is not None
    assert snapshots[0].rows == (("before",),)
    assert result.rows == [["after"]]


def test_run_includes_plugin_warnings_in_startup_help(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    app = object.__new__(App)
    app.screen = FakeScreen()
    app.state = UIState(config=config, db=object())
    app.running = False
    app._plugin_startup_warnings = ("External plugin skipped",)
    app.show_cursor = lambda: None
    app.init_colors = lambda: None
    app.restore_session_tabs = lambda: None
    app.try_connect = lambda: None
    app.wait_for_db_operation = lambda timeout=None: None
    app.close_all_result_continuations = lambda: None
    app.shutdown_database_worker = lambda timeout=None: None
    seen: list[tuple[list[str], bool]] = []
    app.show_help = lambda messages, focus_results=True: seen.append(
        (messages, focus_results)
    )
    monkeypatch.setattr(app_module, "workspace_health", lambda config: ["Workspace health"])
    monkeypatch.setattr(app_module, "enable_extended_keyboard_reporting", lambda: False)
    monkeypatch.setattr(curses, "raw", lambda: None)
    monkeypatch.setattr(curses, "nonl", lambda: None)
    monkeypatch.setattr(curses, "nl", lambda: None)
    monkeypatch.setattr(curses, "noraw", lambda: None)

    App.run(app)

    assert seen == [
        (["External plugin skipped", "Workspace health"], False),
    ]
