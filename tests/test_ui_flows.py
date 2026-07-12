from __future__ import annotations

import curses
from datetime import datetime
from pathlib import Path

import pytest

import plsqlwks.ui as ui
import plsqlwks.ui.app_files as ui_app_files
from plsqlwks.config import AppConfig, SessionTab
from plsqlwks.db import (
    CellUpdateResult,
    EditableResultContext,
    ExplainPlanResult,
    ExplainPlanStep,
    NULL_DISPLAY_TOKEN,
    OracleExecutionError,
    QueryResult,
    QueryResultContinuation,
    QueryResultPage,
    RowInsertResult,
)
from plsqlwks.sqlsplit import split_script
from plsqlwks.ui import (
    CTRL_E,
    CTRL_N,
    CTRL_R,
    CTRL_T,
    CTRL_W,
    ESC,
    FileTab,
    FOCUS_BROWSER,
    FOCUS_EDITOR,
    FOCUS_RESULTS,
    KEY_CTRL_ALT_C,
    KEY_CTRL_PAGEDOWN,
    KEY_CTRL_PAGEUP,
    RESULT_ROW_DETAIL,
    App,
    Buffer,
    ResultCell,
    UIState,
    alt_digit_key,
    browser_entry_text,
    display_width,
    explain_plan_lines,
    explain_plan_tree_lines,
    file_source_key,
    flatten_browser_entries,
    format_elapsed_hhmmss,
)


def test_enter_results_focus_and_escape_back_to_editor():
    app = make_app()
    app.enter_results_focus()
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.status == "No table result is available"

    app.state.active_result = QueryResult("data", ["A"], [["1"]], "1 row")
    app.enter_results_focus()
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.status.startswith("Results grid")

    app.handle_results_key(ESC)
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.status == "Editor focus"


def test_tab_focuses_text_results_without_a_table_result():
    app = make_app()
    app.state.results = ["diagnostic line"]

    App.handle_key(app, ui.TAB)

    assert app.state.active_result is None
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.show_dbms_output is False


def test_tab_focuses_visible_dbms_output_without_a_table_result():
    app = make_app()
    app.state.results = ["[Block] 2 dbms_output line(s)"]
    app.state.dbms_output = ["first", "second"]
    app.state.show_dbms_output = True

    App.handle_key(app, ui.TAB)

    assert app.state.active_result is None
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.show_dbms_output is True


def test_result_navigation_updates_selection_and_mode():
    app = make_app()
    app.state.active_result = QueryResult("data", ["A", "B"], [["1", "2"], ["3", "4"]], "2 rows")
    app.enter_results_focus()

    app.handle_results_key(curses.KEY_DOWN)
    app.handle_results_key(curses.KEY_RIGHT)
    assert (app.state.result_row, app.state.result_col) == (1, 1)

    app.handle_results_key(curses.KEY_F8)
    assert app.state.result_mode == RESULT_ROW_DETAIL
    assert "row detail" in app.state.status


def test_result_page_down_inside_loaded_rows_does_not_fetch_more():
    app = make_app(db=PagingDb())
    app.state.active_result = continuing_result([["0"], ["1"], ["2"], ["3"], ["4"]])
    app.state.focus = FOCUS_RESULTS
    app.state.result_page_size = 2
    app.state.result_row = 1

    app.handle_results_key(curses.KEY_NPAGE)

    assert app.state.db.calls == []
    assert app.state.result_row == 3


def test_result_page_down_at_loaded_end_fetches_and_appends_more_rows():
    db = PagingDb()
    app = make_app(db=db)
    result = continuing_result([["1"], ["2"]])
    continuation = result.continuation
    assert continuation is not None
    app.state.active_result = result
    app.state.results = ["[data] 2 row(s) (limited to 2 rows) in 0.01s"]
    app.state.focus = FOCUS_RESULTS
    app.state.result_page_size = 2

    app.handle_results_key(curses.KEY_NPAGE)
    app.wait_for_db_operation(timeout=1)

    assert db.calls == [(continuation, 2)]
    assert result.rows == [["1"], ["2"], ["3"], ["4"]]
    assert result.original_rows == [["1"], ["2"], [3], [4]]
    assert result.continuation is None
    assert result.message == "4 row(s) in 0.02s"
    assert app.state.results[0] == "[data] 4 row(s) in 0.02s"
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.result_row == 2
    assert "4 row(s) in 0.02s" in app.state.status


def test_result_page_down_fetch_failure_preserves_current_grid():
    app = make_app(db=FailingPagingDb())
    result = continuing_result([["1"], ["2"]])
    app.state.active_result = result
    app.state.results = ["[data] 2 row(s) (limited to 2 rows) in 0.01s"]
    app.state.focus = FOCUS_RESULTS
    app.state.result_page_size = 2

    app.handle_results_key(curses.KEY_NPAGE)
    app.wait_for_db_operation(timeout=1)

    assert result.rows == [["1"], ["2"]]
    assert result.continuation is None
    assert app.state.active_result is result
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.status == "Fetch rows failed: RuntimeError: fetch failed"
    assert any("fetch failed" in line for line in app.state.results)


def test_explain_plan_tree_lines_use_ascii_connectors():
    lines = explain_plan_tree_lines(sample_plan_steps())

    assert [line.text for line in lines] == [
        "SELECT STATEMENT  [cost=3]",
        "+- TABLE ACCESS FULL  HR.DECISIONS (TABLE)  [cost=2, rows=10, bytes=120]",
        "\\- NESTED LOOPS",
        "   \\- INDEX RANGE SCAN  DECISION_PK (INDEX)  [cost=1]",
    ]


def test_explain_plan_lines_use_raw_text_when_steps_are_not_available():
    result = ExplainPlanResult(
        "Current statement",
        [],
        "Explain plan: 2 line(s) in 0.01s",
        ["Plan hash value: 123", "| Id | Operation        |"],
    )

    lines = explain_plan_lines(result)

    assert [line.text for line in lines] == ["Plan hash value: 123", "| Id | Operation        |"]
    assert [line.segments[0].kind for line in lines] == [ui.PLAN_TEXT, ui.PLAN_TEXT]


def test_ctrl_e_explains_current_statement_in_results_pane():
    db = FakeExplainDb()
    app = make_app(db=db)
    app.state.buffer = Buffer(lines=["select 1 from dual; select * from decisions;"], row=0, col=27)

    app.handle_key(CTRL_E)
    app.wait_for_db_operation(timeout=1)

    assert db.statements == ["select * from decisions"]
    assert app.state.explain_result is not None
    assert app.state.active_result is None
    assert app.state.status.startswith("Explain plan: 4 step(s)")
    assert app.state.focus == FOCUS_EDITOR

    app.enter_results_focus()
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.status == "Explain plan: lines 1-4/4"
    app.handle_results_key(curses.KEY_NPAGE)
    assert app.state.explain_scroll == 0
    app.handle_results_key(ESC)
    assert app.state.focus == FOCUS_EDITOR


def test_ctrl_e_prompts_for_bind_values_before_explain():
    db = FakeExplainDb()
    app = make_app(db=db)
    app.state.buffer = Buffer(lines=["select * from decisions where id = :id"], row=0, col=0)
    prompts: list[tuple[str, str, bool]] = []

    def prompt_text_box(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default, strip))
        return "42"

    app.prompt_text_box = prompt_text_box

    app.handle_key(CTRL_E)
    app.wait_for_db_operation(timeout=1)

    assert prompts == [("Value for :id", "", False)]
    assert db.statements == ["select * from decisions where id = :id"]
    assert db.bind_values == [{"id": "42"}]


def test_ctrl_e_explain_failure_prints_location_and_moves_cursor():
    app = make_app(db=FailingExplainDb())
    app.state.buffer = Buffer(lines=["select * from missing_table"], row=0, col=0)

    app.handle_key(CTRL_E)
    app.wait_for_db_operation(timeout=1)

    assert (app.state.buffer.row, app.state.buffer.col) == (0, 7)
    assert app.state.status.startswith("Explain failed at line 1, column 8")
    assert "Error location: line 1, column 8" in app.state.results
    assert app.state.focus == FOCUS_EDITOR


def test_explain_plan_draws_colored_segments(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=FakeScreen(height=8, width=90))
    app.show_explain_result(sample_plan_result())
    app.enter_results_focus()

    app.draw_results(0, 6, 90)

    assert any("+- " in call.text for call in app.screen.calls)
    assert any("TABLE ACCESS FULL" in call.text and call.attr & curses.A_BOLD for call in app.screen.calls)
    assert any("HR.DECISIONS" in call.text and call.attr == (5 << 8) for call in app.screen.calls)
    assert any("[cost=2" in call.text and call.attr & curses.A_DIM for call in app.screen.calls)


def test_explain_plan_draws_configured_colored_segments(monkeypatch):
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda pair, fg, bg: calls.append((pair, fg, bg)))
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    monkeypatch.setattr(curses, "COLORS", 256, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 64, raising=False)
    app = make_app(
        screen=FakeScreen(height=8, width=90),
        config=make_config(
            explain_colors={
                "connector": 160,
                "operation": 45,
                "object": 200,
                "metrics": 244,
                "text": 15,
            }
        ),
    )

    app.init_colors()
    app.show_explain_result(sample_plan_result())
    app.enter_results_focus()
    app.draw_results(0, 6, 90)

    assert (ui.COLOR_PLAN_CONNECTOR, 160, -1) in calls
    assert any(
        "+- " in call.text and call.attr == ((ui.COLOR_PLAN_CONNECTOR << 8) | curses.A_DIM)
        for call in app.screen.calls
    )
    assert any(
        "TABLE ACCESS FULL" in call.text
        and call.attr == ((ui.COLOR_PLAN_OPERATION << 8) | curses.A_BOLD)
        for call in app.screen.calls
    )
    assert any(
        "HR.DECISIONS" in call.text and call.attr == (ui.COLOR_PLAN_OBJECT << 8)
        for call in app.screen.calls
    )
    assert any(
        "[cost=2" in call.text and call.attr == ((ui.COLOR_PLAN_METRICS << 8) | curses.A_DIM)
        for call in app.screen.calls
    )

    app.screen.calls.clear()
    app.show_explain_result(
        ExplainPlanResult("Current statement", [], "Explain plan: 1 line(s) in 0.01s", ["Plan hash value: 123"])
    )
    app.draw_results(0, 3, 90)

    assert any(
        "Plan hash value" in call.text and call.attr == (ui.COLOR_PLAN_TEXT << 8)
        for call in app.screen.calls
    )


def test_browser_toggle_refreshes_and_loads_object_definition():
    app = make_app(db=FakeBrowserDb())

    app.toggle_browser()
    app.wait_for_db_operation(timeout=1)
    assert app.state.focus == FOCUS_BROWSER
    assert app.state.browser_visible is True
    assert app.state.browser_objects["TABLE"] == ["DECISIONS"]

    app.state.browser_expanded.add("TABLE")
    app.state.browser_row = 1
    app.activate_browser_entry()
    app.wait_for_db_operation(timeout=1)

    assert app.state.focus == FOCUS_EDITOR
    assert app.state.buffer.text() == "create table decisions (id number);"
    assert app.state.buffer.title == "schema://HR/TABLE/DECISIONS.sql"
    assert app.state.buffer.dirty is False


def test_browser_filter_matches_names_case_insensitively_and_expands_matches():
    expanded: set[str] = set()
    objects = {
        "TABLE": ["DECISIONS", "Straße_LOG"],
        "VIEW": ["DECISION_SUMMARY"],
    }

    decision_entries = flatten_browser_entries(objects, expanded, "decision")
    unicode_entries = flatten_browser_entries(objects, expanded, "STRASSE")

    assert [(entry.kind, entry.label) for entry in decision_entries] == [
        ("group", "Tables (1)"),
        ("object", "DECISIONS"),
        ("group", "Views (1)"),
        ("object", "DECISION_SUMMARY"),
    ]
    assert browser_entry_text(decision_entries[0], expanded, "decision") == "Tables (1)"
    assert browser_entry_text(decision_entries[1], expanded, "decision") == "    DECISIONS"
    assert [(entry.kind, entry.object_name) for entry in unicode_entries] == [
        ("group", ""),
        ("object", "Straße_LOG"),
    ]
    assert flatten_browser_entries(objects, expanded, "missing") == []
    assert expanded == set()


def test_browser_filter_keys_select_first_match_and_use_two_stage_escape():
    app = make_app()
    app.state.focus = FOCUS_BROWSER
    app.state.browser_scroll = 7
    app.state.browser_objects = {object_type: [] for object_type in ui.SCHEMA_OBJECT_TYPES}
    app.state.browser_objects["TABLE"] = ["DECISIONS", "DECISION_LOG", "PROJECTS"]

    app.handle_key("d")
    app.handle_key(ord("e"))
    app.handle_key("c")

    assert app.state.browser_filter == "dec"
    assert app.state.browser_scroll == 0
    assert app.state.browser_row == 1
    assert app.active_browser_entry().object_name == "DECISIONS"

    app.state.browser_row = 0
    app.activate_browser_entry()
    assert app.state.browser_expanded == set()

    app.handle_key(" ")
    assert app.state.browser_filter == "dec "
    assert app.browser_entries() == []

    app.handle_key(curses.KEY_BACKSPACE)
    assert app.state.browser_filter == "dec"
    assert app.state.browser_row == 1

    app.handle_key(ESC)
    assert app.state.browser_filter == ""
    assert app.state.focus == FOCUS_BROWSER
    assert app.state.browser_row == 0

    app.handle_key(ESC)
    assert app.state.focus == FOCUS_EDITOR


def test_browser_space_toggles_group_only_when_filter_is_empty():
    app = make_app()
    app.state.focus = FOCUS_BROWSER
    app.state.browser_objects = {object_type: [] for object_type in ui.SCHEMA_OBJECT_TYPES}
    app.state.browser_objects["TABLE"] = ["DECISIONS"]

    app.handle_browser_key(" ")
    assert app.state.browser_expanded == {"TABLE"}

    app.set_browser_filter("dec")
    app.state.browser_row = 0
    app.toggle_browser_group_at_cursor()
    assert app.state.browser_expanded == {"TABLE"}


def test_browser_filter_persists_across_refresh_and_hide_show():
    app = make_app(db=FakeBrowserDb())
    app.state.browser_filter = "dec"

    app.toggle_browser()
    app.wait_for_db_operation(timeout=1)

    assert app.state.browser_filter == "dec"
    assert app.state.browser_row == 1
    assert app.active_browser_entry().object_name == "DECISIONS"

    app.state.browser_objects["TABLE"] = ["STALE_TABLE"]
    app.handle_key(CTRL_R)
    app.wait_for_db_operation(timeout=1)

    assert app.state.browser_filter == "dec"
    assert app.state.browser_objects["TABLE"] == ["DECISIONS"]
    assert app.state.browser_row == 1

    app.toggle_browser()
    app.toggle_browser()

    assert app.state.browser_filter == "dec"
    assert app.state.browser_visible is True
    assert app.state.focus == FOCUS_BROWSER


def test_load_schema_object_failure_leaves_buffer_unchanged():
    app = make_app(db=FailingBrowserDb())
    app.state.buffer = Buffer(lines=["select 1 from dual"], row=0, col=0)

    app.load_schema_object("TABLE", "BROKEN")
    app.wait_for_db_operation(timeout=1)

    assert app.state.buffer.text() == "select 1 from dual"
    assert app.state.status == "Load definition failed"
    assert any(line.startswith("ERROR loading TABLE BROKEN") for line in app.state.results)


def test_schema_object_load_opens_and_reuses_tab_without_replacing_current_buffer():
    app = make_app(db=FakeBrowserDb())
    app.state.buffer = Buffer(lines=["select 1 from dual"], row=0, col=8, title="scratch.sql", dirty=True)

    app.load_schema_object("TABLE", "DECISIONS")
    app.wait_for_db_operation(timeout=1)

    assert len(app.state.tabs) == 2
    assert app.state.buffer.text() == "create table decisions (id number);"
    assert app.state.buffer.title == "schema://HR/TABLE/DECISIONS.sql"
    assert app.state.buffer.dirty is False
    assert app.state.active_tab.source_key == "schema://HR/TABLE/DECISIONS.sql"

    app.switch_to_tab(0)
    assert app.state.buffer.text() == "select 1 from dual"
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 8)
    assert app.state.buffer.title == "scratch.sql"
    assert app.state.buffer.dirty is True

    app.load_schema_object("TABLE", "DECISIONS")
    assert len(app.state.tabs) == 2
    assert app.state.buffer.text() == "create table decisions (id number);"
    assert app.state.status == "Switched to TABLE DECISIONS"


def test_template_tabs_reuse_clean_matching_tab_and_preserve_dirty_template_work():
    app = make_app()
    app.state.buffer = Buffer(lines=["select 1 from dual"], row=0, col=8, title="scratch.sql", dirty=True)
    app.pick = lambda title, options: 0

    app.new_template()

    assert len(app.state.tabs) == 2
    assert app.state.buffer.text() == "select *\nfrom dual;"
    assert app.state.buffer.title == "SQL select.sql"
    assert app.state.buffer.dirty is False
    first_template_idx = app.state.active_tab_idx

    app.switch_to_tab(0)
    app.new_template()
    assert len(app.state.tabs) == 2
    assert app.state.active_tab_idx == first_template_idx

    app.state.buffer.insert_char(" ")
    app.switch_to_tab(0)
    app.new_template()
    assert len(app.state.tabs) == 3
    assert app.state.buffer.text() == "select *\nfrom dual;"
    assert app.state.buffer.dirty is False


def test_open_file_uses_reusable_file_tabs_and_save_updates_only_active_tab(tmp_path):
    config = make_config(tmp_path)
    config.sql_dir.mkdir(parents=True)
    config.plsql_dir.mkdir(parents=True)
    config.results_dir.mkdir(parents=True)
    first = config.sql_dir / "a.sql"
    second = config.sql_dir / "b.sql"
    first.write_text("select 1 from dual;\n", encoding="utf-8")
    second.write_text("select 2 from dual;\n", encoding="utf-8")
    app = make_app(config=config)
    picks = iter([0, 1, 0])
    app.pick = lambda title, options: next(picks)

    app.open_file()
    assert len(app.state.tabs) == 2
    assert app.state.buffer.text() == "select 1 from dual;"
    assert app.state.active_tab.source_key == file_source_key(first)

    app.open_file()
    assert len(app.state.tabs) == 3
    assert app.state.buffer.text() == "select 2 from dual;"

    app.state.buffer.col = len(app.state.buffer.lines[0])
    app.state.buffer.insert_text("\n-- changed")
    app.save_buffer()
    assert second.read_text(encoding="utf-8") == "select 2 from dual;\n-- changed\n"
    assert first.read_text(encoding="utf-8") == "select 1 from dual;\n"

    app.open_file()
    assert len(app.state.tabs) == 3
    assert app.state.buffer.path == first
    assert app.state.status == f"Switched to {first}"


def test_open_save_reuses_and_parses_long_special_sql_file(tmp_path, long_special_sql_case):
    config = make_config(tmp_path)
    config.sql_dir.mkdir(parents=True)
    config.plsql_dir.mkdir(parents=True)
    config.results_dir.mkdir(parents=True)
    long_file = config.sql_dir / "long_special.sql"
    other_file = config.sql_dir / "other.sql"
    long_file.write_text(long_special_sql_case.script, encoding="utf-8")
    other_file.write_text("select 0 from dual;\n", encoding="utf-8")
    app = make_app(config=config)
    picks = iter([0, 1, 0])
    app.pick = lambda title, options: next(picks)

    app.open_file()
    assert len(app.state.tabs) == 2
    assert app.state.buffer.path == long_file
    assert app.state.buffer.text() == long_special_sql_case.editor_text
    assert [statement.text for statement in split_script(app.state.buffer.text())] == (
        long_special_sql_case.expected_statements
    )

    app.open_file()
    assert len(app.state.tabs) == 3
    assert app.state.buffer.path == other_file
    app.state.buffer.col = len(app.state.buffer.lines[0])
    app.state.buffer.insert_text("\n-- changed active tab")
    app.save_buffer()

    assert other_file.read_text(encoding="utf-8") == "select 0 from dual;\n-- changed active tab\n"
    assert long_file.read_text(encoding="utf-8") == long_special_sql_case.script

    app.open_file()
    assert len(app.state.tabs) == 3
    assert app.state.buffer.path == long_file
    assert app.state.buffer.text() == long_special_sql_case.editor_text
    assert [statement.text for statement in split_script(app.state.buffer.text())] == (
        long_special_sql_case.expected_statements
    )
    assert app.state.status == f"Switched to {long_file}"


def test_open_file_failure_keeps_current_tab_and_reports_error(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    missing = config.sql_dir / "missing.sql"
    app = make_app(config=config)
    app.state.buffer = Buffer(lines=["select 1 from dual"], row=0, col=8, title="current.sql", dirty=True)
    app.state.active_tab.source_key = "current"
    app.pick = lambda title, options: 0
    monkeypatch.setattr(ui, "list_workspace_files", lambda config: [missing])

    app.open_file()

    assert len(app.state.tabs) == 1
    assert app.state.active_tab_idx == 0
    assert app.state.buffer.text() == "select 1 from dual"
    assert app.state.buffer.title == "current.sql"
    assert app.state.buffer.dirty is True
    assert app.state.active_tab.source_key == "current"
    assert app.state.status == "Open failed"
    assert any(line.startswith("ERROR opening file:") for line in app.state.results)


def test_restore_session_tabs_replaces_initial_tab_and_restores_positions(tmp_path):
    first = tmp_path / "first.sql"
    second = tmp_path / "second.sql"
    first.write_text("select\nfirst from dual;\n", encoding="utf-8")
    second.write_text("select second\nfrom dual;\n", encoding="utf-8")
    config = make_config(
        tmp_path,
        session_tabs=(
            SessionTab(first, row=1, col=5),
            SessionTab(second, row=0, col=len("select second")),
        ),
        active_session_tab=0,
    )
    app = make_app(config=config)
    initial_tab = app.state.active_tab

    app.restore_session_tabs()

    assert len(app.state.tabs) == 2
    assert all(tab is not initial_tab for tab in app.state.tabs)
    assert [tab.buffer.path for tab in app.state.tabs] == [first.resolve(), second.resolve()]
    assert [(tab.buffer.row, tab.buffer.col) for tab in app.state.tabs] == [
        (1, 5),
        (0, len("select second")),
    ]
    assert [tab.source_key for tab in app.state.tabs] == [file_source_key(first), file_source_key(second)]
    assert all(tab.buffer.dirty is False for tab in app.state.tabs)
    assert app.state.active_tab_idx == 0
    assert app.state.focus == FOCUS_EDITOR


def test_restore_session_tabs_skips_missing_unreadable_and_duplicate_paths(monkeypatch, tmp_path):
    first = tmp_path / "first.sql"
    unreadable = tmp_path / "unreadable.sql"
    second = tmp_path / "second.sql"
    missing = tmp_path / "missing.sql"
    first.write_text("first\n", encoding="utf-8")
    unreadable.write_text("unreadable\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    config = make_config(
        tmp_path,
        session_tabs=(
            SessionTab(missing),
            SessionTab(Path("invalid\0path.sql")),
            SessionTab(first),
            SessionTab(unreadable),
            SessionTab(first.parent / "." / first.name),
            SessionTab(second, col=3),
        ),
        active_session_tab=5,
    )
    app = make_app(config=config)
    original_load = Buffer.load

    def load(buffer, path, record_undo=True):
        if Path(path).resolve() == unreadable.resolve():
            raise UnicodeError("cannot decode")
        original_load(buffer, path, record_undo=record_undo)

    monkeypatch.setattr(Buffer, "load", load)

    app.restore_session_tabs()

    assert [tab.buffer.path for tab in app.state.tabs] == [first.resolve(), second.resolve()]
    assert app.state.active_tab_idx == 1
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 3)
    assert app.state.status == "Ready"


def test_restore_session_tabs_with_no_loadable_files_keeps_initial_tab(tmp_path):
    config = make_config(
        tmp_path,
        session_tabs=(SessionTab(tmp_path / "missing.sql", row=4, col=2),),
    )
    app = make_app(config=config)
    initial_tab = app.state.active_tab
    initial_tab.buffer = Buffer(lines=["scratch"], row=0, col=4, title="scratch.sql", dirty=True)

    app.restore_session_tabs()

    assert app.state.tabs == [initial_tab]
    assert app.state.active_tab_idx == 0
    assert app.state.buffer.text() == "scratch"
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 4)
    assert app.state.buffer.dirty is True
    assert app.state.status == "Ready"


@pytest.mark.parametrize(("row", "col"), [(2, 0), (0, 4), (-1, 0), (0, -1)])
def test_restore_session_tabs_invalid_position_starts_at_beginning(tmp_path, row, col):
    path = tmp_path / "position.sql"
    path.write_text("abc\nde\n", encoding="utf-8")
    config = make_config(tmp_path, session_tabs=(SessionTab(path, row=row, col=col),))
    app = make_app(config=config)

    app.restore_session_tabs()

    assert app.state.buffer.path == path.resolve()
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 0)


def test_tab_switching_preserves_buffer_undo_and_result_state():
    app = make_app()
    app.state.buffer.insert_char("a")
    app.state.active_result = QueryResult("first", ["A"], [["1"]], "1 row")
    app.state.result_row = 3

    app.new_tab()
    app.state.buffer.insert_char("b")
    app.state.active_result = QueryResult("second", ["B"], [["2"]], "1 row")
    app.state.result_col = 2

    app.handle_key(KEY_CTRL_PAGEUP)
    assert app.state.buffer.text() == "a"
    assert app.state.active_result.title == "first"
    assert app.state.result_row == 3
    app.undo_buffer()
    assert app.state.buffer.text() == ""

    app.handle_key(KEY_CTRL_PAGEDOWN)
    assert app.state.buffer.text() == "b"
    assert app.state.active_result.title == "second"
    assert app.state.result_col == 2


def test_alt_digit_and_ctrl_w_tab_shortcuts_work_across_focuses():
    app = make_app()
    app.new_tab(status="second")
    app.new_tab(status="third")
    app.state.focus = FOCUS_BROWSER

    app.handle_key(alt_digit_key(1))
    assert app.state.active_tab_idx == 0
    assert app.state.focus == FOCUS_BROWSER

    app.state.focus = FOCUS_RESULTS
    app.state.active_result = None
    app.handle_key(KEY_CTRL_PAGEDOWN)
    assert app.state.active_tab_idx == 1
    assert app.state.focus == FOCUS_EDITOR

    app.prompt = lambda label, default="", strip=True: "n"
    app.state.buffer.insert_char("x")
    app.handle_key(CTRL_W)
    assert len(app.state.tabs) == 2
    assert app.state.status.startswith("Closed")


def test_ctrl_t_creates_new_tab_and_ctrl_n_searches_active_tab():
    app = make_app()
    app.state.buffer.insert_text("select 1")

    app.handle_key(CTRL_T)
    assert len(app.state.tabs) == 2
    assert app.state.active_tab_idx == 1
    assert app.state.status == "New tab"

    app.state.buffer.insert_text("select 2")
    app.prompt = lambda label, default="", strip=True: "select"
    app.handle_key(CTRL_N)
    assert len(app.state.tabs) == 2
    assert app.state.buffer.text() == "select 2"
    assert app.state.buffer.selection_anchor == (0, 0)
    assert app.state.status == 'Found "select" 1/1 (wrapped)'


def test_close_last_tab_creates_empty_tab_and_dirty_cancel_keeps_tab():
    app = make_app()
    app.state.buffer.insert_char("x")
    app.prompt = lambda label, default="", strip=True: "c"

    app.close_active_tab()
    assert len(app.state.tabs) == 1
    assert app.state.buffer.text() == "x"
    assert app.state.status == "Close cancelled"

    app.prompt = lambda label, default="", strip=True: "n"
    app.close_active_tab()
    assert len(app.state.tabs) == 1
    assert app.state.buffer.text() == ""
    assert app.state.buffer.dirty is False
    assert app.state.status == "Closed tab; new empty tab"


def test_clean_tab_close_does_not_prompt_and_selects_neighbor():
    app = make_app()
    app.state.buffer = Buffer(title="first.sql")
    app.new_tab(FileTab(buffer=Buffer(title="second.sql")), status="second")
    app.prompt = lambda label, default="", strip=True: (_ for _ in ()).throw(AssertionError("unexpected prompt"))

    app.close_active_tab()

    assert len(app.state.tabs) == 1
    assert app.state.buffer.title == "first.sql"
    assert app.state.status == "Closed second.sql"


def test_dirty_tab_close_save_as_success_writes_file_and_closes(tmp_path):
    app = make_app(config=make_config(tmp_path))
    app.state.buffer.insert_text("select 42 from dual;")
    saved = tmp_path / "sql" / "saved.sql"
    answers = iter(["y", str(saved)])
    app.prompt = lambda label, default="", strip=True: next(answers)

    app.close_active_tab()

    assert saved.read_text(encoding="utf-8") == "select 42 from dual;\n"
    assert len(app.state.tabs) == 1
    assert app.state.buffer.text() == ""
    assert app.state.status == "Closed tab; new empty tab"


def test_dirty_existing_file_close_saves_without_save_as_prompt(tmp_path):
    config = make_config(tmp_path)
    path = config.sql_dir / "saved.sql"
    path.parent.mkdir(parents=True)
    path.write_text("select 1 from dual;\n", encoding="utf-8")
    app = make_app(config=config)
    app.state.buffer = Buffer(lines=["select 2 from dual;"], path=path, title="saved.sql", dirty=True)
    prompts: list[tuple[str, str]] = []

    def confirm_save(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return "y"

    app.prompt = confirm_save

    app.close_active_tab()

    assert prompts == [("Save changes to saved.sql? y/n/c", "")]
    assert path.read_text(encoding="utf-8") == "select 2 from dual;\n"
    assert len(app.state.tabs) == 1
    assert app.state.buffer.text() == ""
    assert app.state.status == "Closed tab; new empty tab"


def test_save_buffer_defaults_unsaved_sql_and_plsql_to_matching_workspace_dirs(tmp_path):
    config = make_config(tmp_path)
    app = make_app(config=config)
    prompts: list[tuple[str, str]] = []

    def accept_default(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return default

    app.prompt = accept_default
    app.state.buffer.insert_text("select 1 from dual;")

    assert app.save_buffer() is True
    assert prompts[0] == ("Save as", str(config.sql_dir / "scratch.sql"))
    assert (config.sql_dir / "scratch.sql").read_text(encoding="utf-8") == "select 1 from dual;\n"
    assert app.state.active_tab.source_key == file_source_key(config.sql_dir / "scratch.sql")

    app.new_tab()
    app.state.buffer.insert_text("begin\n  null;\nend;\n/")

    assert app.save_buffer() is True
    assert prompts[1] == ("Save as", str(config.plsql_dir / "scratch.sql"))
    assert (config.plsql_dir / "scratch.sql").read_text(encoding="utf-8") == "begin\n  null;\nend;\n/\n"
    assert app.state.active_tab.source_key == file_source_key(config.plsql_dir / "scratch.sql")


def test_save_buffer_asks_before_overwriting_new_target(tmp_path):
    config = make_config(tmp_path)
    target = config.sql_dir / "scratch.sql"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    app = make_app(config=config)
    app.state.buffer.insert_text("select 1 from dual;")
    answers = iter([str(target), "n"])
    app.prompt = lambda label, default="", strip=True: next(answers)

    assert app.save_buffer() is False
    assert target.read_text(encoding="utf-8") == "old\n"
    assert app.state.buffer.path is None
    assert app.state.buffer.dirty is True
    assert app.state.active_tab.source_key is None
    assert app.state.status == "Overwrite cancelled"

    answers = iter([str(target), "y"])
    app.prompt = lambda label, default="", strip=True: next(answers)

    assert app.save_buffer() is True
    assert target.read_text(encoding="utf-8") == "select 1 from dual;\n"
    assert app.state.active_tab.source_key == file_source_key(target)


def test_save_buffer_rejects_save_as_target_open_in_another_tab(tmp_path):
    config = make_config(tmp_path)
    target = config.sql_dir / "open.sql"
    target.parent.mkdir(parents=True)
    target.write_text("select 2 from dual;\n", encoding="utf-8")
    app = make_app(config=config)
    app.state.tabs = [
        FileTab(buffer=Buffer(lines=["select 1"], dirty=True)),
        FileTab(buffer=Buffer(lines=["select 2"], path=target, title="open.sql"), source_key=file_source_key(target)),
    ]
    app.state.active_tab_idx = 0
    prompts: list[tuple[str, str]] = []

    def choose_open_file(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return str(target)

    app.prompt = choose_open_file

    assert app.save_buffer() is False
    assert prompts == [("Save as", str(config.sql_dir / "scratch.sql"))]
    assert target.read_text(encoding="utf-8") == "select 2 from dual;\n"
    assert app.state.buffer.path is None
    assert app.state.status == "Save failed: file is already open in another tab"


def test_rename_current_buffer_defaults_unsaved_sql_and_plsql_to_matching_workspace_dirs(tmp_path):
    config = make_config(tmp_path)
    app = make_app(config=config)
    prompts: list[tuple[str, str]] = []

    def accept_default(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return default

    app.prompt = accept_default
    app.state.buffer.insert_text("select 1 from dual;")

    assert app.rename_current_buffer() is True
    assert prompts[0] == ("Rename as", str(config.sql_dir / "scratch.sql"))
    assert (config.sql_dir / "scratch.sql").read_text(encoding="utf-8") == "select 1 from dual;\n"
    assert app.state.buffer.path == config.sql_dir / "scratch.sql"
    assert app.state.buffer.title == "scratch.sql"
    assert app.state.buffer.dirty is False
    assert app.state.active_tab.source_key == file_source_key(config.sql_dir / "scratch.sql")
    assert app.state.status == f"Renamed buffer to {config.sql_dir / 'scratch.sql'}"

    app.new_tab()
    app.state.buffer.insert_text("begin\n  null;\nend;\n/")

    assert app.rename_current_buffer() is True
    assert prompts[1] == ("Rename as", str(config.plsql_dir / "scratch.sql"))
    assert (config.plsql_dir / "scratch.sql").read_text(encoding="utf-8") == "begin\n  null;\nend;\n/\n"
    assert app.state.active_tab.source_key == file_source_key(config.plsql_dir / "scratch.sql")


def test_rename_current_buffer_saved_file_writes_new_path_and_keeps_old_file(tmp_path):
    config = make_config(tmp_path)
    old_path = config.sql_dir / "saved.sql"
    new_path = config.sql_dir / "renamed.sql"
    old_path.parent.mkdir(parents=True)
    old_path.write_text("select 1 from dual;\n", encoding="utf-8")
    app = make_app(config=config)
    app.state.buffer = Buffer(lines=["select 2 from dual;"], path=old_path, title="saved.sql", dirty=True)
    app.state.active_tab.source_key = file_source_key(old_path)
    app.prompt = lambda label, default="", strip=True: str(new_path)

    assert app.rename_current_buffer() is True
    assert old_path.read_text(encoding="utf-8") == "select 1 from dual;\n"
    assert new_path.read_text(encoding="utf-8") == "select 2 from dual;\n"
    assert app.state.buffer.path == new_path
    assert app.state.buffer.title == "renamed.sql"
    assert app.state.buffer.dirty is False
    assert app.state.active_tab.source_key == file_source_key(new_path)


def test_rename_current_buffer_asks_before_overwriting_new_target(tmp_path):
    config = make_config(tmp_path)
    old_path = config.sql_dir / "saved.sql"
    target = config.sql_dir / "target.sql"
    old_path.parent.mkdir(parents=True)
    old_path.write_text("old source\n", encoding="utf-8")
    target.write_text("old target\n", encoding="utf-8")
    app = make_app(config=config)
    app.state.buffer = Buffer(lines=["select 2 from dual;"], path=old_path, title="saved.sql", dirty=True)
    app.state.active_tab.source_key = file_source_key(old_path)
    answers = iter([str(target), "n"])
    app.prompt = lambda label, default="", strip=True: next(answers)

    assert app.rename_current_buffer() is False
    assert target.read_text(encoding="utf-8") == "old target\n"
    assert app.state.buffer.path == old_path
    assert app.state.buffer.title == "saved.sql"
    assert app.state.buffer.dirty is True
    assert app.state.active_tab.source_key == file_source_key(old_path)
    assert app.state.status == "Overwrite cancelled"

    answers = iter([str(target), "y"])
    app.prompt = lambda label, default="", strip=True: next(answers)

    assert app.rename_current_buffer() is True
    assert old_path.read_text(encoding="utf-8") == "old source\n"
    assert target.read_text(encoding="utf-8") == "select 2 from dual;\n"
    assert app.state.buffer.path == target
    assert app.state.buffer.title == "target.sql"
    assert app.state.buffer.dirty is False
    assert app.state.active_tab.source_key == file_source_key(target)


def test_rename_current_buffer_same_current_path_does_not_prompt_for_overwrite(tmp_path):
    config = make_config(tmp_path)
    path = config.sql_dir / "saved.sql"
    path.parent.mkdir(parents=True)
    path.write_text("select 1 from dual;\n", encoding="utf-8")
    app = make_app(config=config)
    app.state.buffer = Buffer(lines=["select 2 from dual;"], path=path, title="saved.sql", dirty=True)
    app.state.active_tab.source_key = file_source_key(path)
    prompts: list[tuple[str, str]] = []

    def choose_current_path(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return str(path)

    app.prompt = choose_current_path

    assert app.rename_current_buffer() is True
    assert prompts == [("Rename as", str(path))]
    assert path.read_text(encoding="utf-8") == "select 2 from dual;\n"
    assert app.state.buffer.path == path
    assert app.state.buffer.dirty is False


def test_rename_current_buffer_cancel_preserves_buffer_state(tmp_path):
    config = make_config(tmp_path)
    path = config.sql_dir / "saved.sql"
    app = make_app(config=config)
    app.state.buffer = Buffer(lines=["select 1"], path=path, title="saved.sql", dirty=True)
    app.state.active_tab.source_key = file_source_key(path)
    app.prompt = lambda label, default="", strip=True: ""

    assert app.rename_current_buffer() is False
    assert app.state.buffer.path == path
    assert app.state.buffer.title == "saved.sql"
    assert app.state.buffer.dirty is True
    assert app.state.active_tab.source_key == file_source_key(path)
    assert app.state.status == "Rename cancelled"


def test_rename_current_buffer_rejects_path_open_in_another_tab(tmp_path):
    config = make_config(tmp_path)
    first = config.sql_dir / "first.sql"
    second = config.sql_dir / "second.sql"
    second.parent.mkdir(parents=True)
    second.write_text("select 2 from dual;\n", encoding="utf-8")
    app = make_app(config=config)
    app.state.tabs = [
        FileTab(buffer=Buffer(lines=["select 1"], path=first, title="first.sql", dirty=True), source_key=file_source_key(first)),
        FileTab(buffer=Buffer(lines=["select 2"], path=second, title="second.sql"), source_key=file_source_key(second)),
    ]
    app.state.active_tab_idx = 0
    app.prompt = lambda label, default="", strip=True: str(second)

    assert app.rename_current_buffer() is False
    assert second.read_text(encoding="utf-8") == "select 2 from dual;\n"
    assert app.state.buffer.path == first
    assert app.state.buffer.title == "first.sql"
    assert app.state.buffer.dirty is True
    assert app.state.active_tab.source_key == file_source_key(first)
    assert app.state.status == "Rename failed: file is already open in another tab"


def test_rename_current_buffer_failure_restores_active_tab_state(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    old_path = config.sql_dir / "saved.sql"
    new_path = config.sql_dir / "renamed.sql"
    source_key = file_source_key(old_path)
    app = make_app(config=config)
    app.state.buffer = Buffer(lines=["select 2 from dual;"], path=old_path, title="saved.sql", dirty=True)
    app.state.active_tab.source_key = source_key
    app.prompt = lambda label, default="", strip=True: str(new_path)

    def fail_save(self, path_arg=None):
        self.path = path_arg
        self.title = "renamed.sql"
        self.dirty = False
        raise OSError("disk full")

    monkeypatch.setattr(Buffer, "save", fail_save)

    assert app.rename_current_buffer() is False
    assert app.state.buffer.path == old_path
    assert app.state.buffer.title == "saved.sql"
    assert app.state.buffer.dirty is True
    assert app.state.active_tab.source_key == source_key
    assert app.state.status == "Rename failed"
    assert any(line.startswith("ERROR renaming buffer:") for line in app.state.results)


def test_save_buffer_failure_keeps_active_tab_and_dirty_buffer(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    path = config.sql_dir / "saved.sql"
    path.parent.mkdir(parents=True)
    path.write_text("select 1 from dual;\n", encoding="utf-8")
    app = make_app(config=config)
    source_key = file_source_key(path)
    app.state.tabs = [
        FileTab(buffer=Buffer(lines=["select 2 from dual;"], path=path, title="saved.sql", dirty=True), source_key=source_key),
        FileTab(buffer=Buffer(title="other.sql"), source_key="other"),
    ]
    app.state.active_tab_idx = 0

    def fail_save(self, path_arg=None):
        assert path_arg == path
        raise OSError("disk full")

    monkeypatch.setattr(Buffer, "save", fail_save)

    assert app.save_buffer() is False
    assert app.state.active_tab_idx == 0
    assert app.state.buffer.text() == "select 2 from dual;"
    assert app.state.buffer.path == path
    assert app.state.buffer.title == "saved.sql"
    assert app.state.buffer.dirty is True
    assert app.state.active_tab.source_key == source_key
    assert app.state.status == "Save failed"
    assert any(line.startswith("ERROR saving file:") for line in app.state.results)


def test_persist_session_tabs_writes_only_file_buffers_and_maps_active(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    first = tmp_path / "first.sql"
    second = tmp_path / "second.sql"
    app = make_app(config=config)
    app.state.tabs = [
        FileTab(buffer=Buffer(lines=["first", "line"], row=1, col=2, path=first, title=first.name)),
        FileTab(buffer=Buffer(lines=["scratch"], row=0, col=4, title="scratch.sql")),
        FileTab(buffer=Buffer(lines=["second"], row=0, col=3, path=second, title=second.name)),
    ]
    app.state.active_tab_idx = 2
    writes: list[tuple[AppConfig, list[SessionTab], int]] = []

    def write_session_tabs(config_arg, tabs, active_index):
        writes.append((config_arg, list(tabs), active_index))

    monkeypatch.setattr(ui_app_files, "write_session_tabs", write_session_tabs)

    assert app.persist_session_tabs() is True
    assert writes == [
        (
            config,
            [SessionTab(first, row=1, col=2), SessionTab(second, row=0, col=3)],
            1,
        )
    ]


def test_cancelled_quit_does_not_persist_session(monkeypatch):
    app = make_app()
    app.state.buffer.insert_char("x")
    app.prompt = lambda label, default="", strip=True: "c"
    writes: list[object] = []
    monkeypatch.setattr(ui_app_files, "write_session_tabs", lambda *args: writes.append(args))

    app.request_quit()

    assert writes == []
    assert app.running is True
    assert app.state.status == "Quit cancelled"


def test_successful_dirty_quit_persists_original_active_tab(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    first = tmp_path / "first.sql"
    second = tmp_path / "second.sql"
    app = make_app(config=config)
    app.state.tabs = [
        FileTab(buffer=Buffer(lines=["first"], path=first, title=first.name, dirty=True)),
        FileTab(buffer=Buffer(lines=["second"], path=second, title=second.name, dirty=True)),
    ]
    app.state.active_tab_idx = 0
    answers = iter(["n", "n"])
    app.prompt = lambda label, default="", strip=True: next(answers)
    writes: list[tuple[list[SessionTab], int]] = []

    def write_session_tabs(config_arg, tabs, active_index):
        assert config_arg is config
        writes.append((list(tabs), active_index))

    monkeypatch.setattr(ui_app_files, "write_session_tabs", write_session_tabs)

    app.request_quit()

    assert app.running is False
    assert app.state.active_tab_idx == 0
    assert writes == [([SessionTab(first), SessionTab(second)], 0)]


def test_successful_quit_stops_when_session_persistence_fails(monkeypatch):
    app = make_app()

    def fail_write(config, tabs, active_index):
        raise OSError("disk full")

    monkeypatch.setattr(ui_app_files, "write_session_tabs", fail_write)

    app.request_quit()

    assert app.running is False


def test_confirm_quit_accepts_discard_for_multiple_dirty_tabs():
    app = make_app()
    app.state.buffer.insert_char("a")
    app.new_tab()
    app.state.buffer.insert_char("b")
    answers = iter(["n", "n"])
    app.prompt = lambda label, default="", strip=True: next(answers)

    assert app.confirm_quit() is True
    assert len(app.state.tabs) == 2
    assert app.state.tabs[0].buffer.dirty is True
    assert app.state.tabs[1].buffer.dirty is True


def test_confirm_quit_cancel_restores_original_active_tab():
    app = make_app()
    app.state.buffer = Buffer(lines=["first"], title="first.sql", dirty=True)
    app.new_tab(FileTab(buffer=Buffer(lines=["second"], title="second.sql", dirty=True)), status="second")
    app.prompt = lambda label, default="", strip=True: "c"

    assert app.confirm_quit() is False
    assert app.state.active_tab_idx == 1
    assert app.state.buffer.title == "second.sql"
    assert app.state.status == "Quit cancelled"


def test_quit_prompts_dirty_tabs_and_save_as_cancel_aborts(tmp_path):
    config = make_config(tmp_path)
    app = make_app(config=config)
    app.state.buffer.insert_char("x")
    app.new_tab()
    app.state.buffer.insert_char("y")
    answers = iter(["n", "y", ""])
    app.prompt = lambda label, default="", strip=True: next(answers)

    assert app.confirm_quit() is False
    assert len(app.state.tabs) == 2
    assert app.state.active_tab_idx == 1
    assert app.state.status == "Quit cancelled"


def test_quit_commits_pending_transaction_before_stopping():
    db = PendingTransactionDb()
    app = make_app(db=db)
    app.prompt = lambda label, default="", strip=True: "c"

    app.request_quit()
    app.wait_for_db_operation(timeout=1)

    assert db.calls == ["commit"]
    assert db.has_uncommitted_changes is False
    assert app.running is False


def test_quit_rolls_back_pending_transaction_before_stopping():
    db = PendingTransactionDb()
    app = make_app(db=db)
    app.prompt = lambda label, default="", strip=True: "r"

    app.request_quit()
    app.wait_for_db_operation(timeout=1)

    assert db.calls == ["rollback"]
    assert db.has_uncommitted_changes is False
    assert app.running is False


def test_quit_transaction_cancel_leaves_application_running():
    db = PendingTransactionDb()
    app = make_app(db=db)
    app.prompt = lambda label, default="", strip=True: "x"

    app.request_quit()

    assert db.calls == []
    assert db.has_uncommitted_changes is True
    assert app.running is True
    assert app.state.status == "Quit cancelled"


def test_quit_transaction_failure_leaves_application_running():
    db = PendingTransactionDb(failing_action="commit")
    app = make_app(db=db)
    app.prompt = lambda label, default="", strip=True: "c"

    app.request_quit()
    app.wait_for_db_operation(timeout=1)

    assert db.calls == ["commit"]
    assert db.has_uncommitted_changes is True
    assert app.running is True
    assert app.state.status == "Commit failed"
    assert any(line.startswith("ERROR committing transaction:") for line in app.state.results)


def test_quit_pending_transaction_blocks_edits_until_commit_finishes():
    class BlockingCommitDb(PendingTransactionDb):
        def __init__(self):
            super().__init__()
            self.started = ui.threading.Event()
            self.release = ui.threading.Event()
            self.cancel_calls = 0

        def commit(self) -> ui.TransactionReport:
            self.started.set()
            assert self.release.wait(1)
            return super().commit()

        def cancel_current_operation(self) -> bool:
            self.cancel_calls += 1
            return True

    db = BlockingCommitDb()
    app = make_app(db=db)
    app.prompt = lambda label, default="", strip=True: "c"

    app.request_quit()
    assert db.started.wait(1)
    app.handle_key(ui.CTRL_C)
    app.handle_key("x")
    app.handle_key(ui.CTRL_T)

    assert app.state.buffer.text() == ""
    assert len(app.state.tabs) == 1
    assert db.cancel_calls == 1
    assert app.state.status == "Quit transaction resolution in progress"

    db.release.set()
    app.wait_for_db_operation(timeout=1)

    assert app.running is False
    assert db.calls == ["commit"]


def test_successful_rollback_with_cursor_cleanup_failure_still_invalidates_results():
    class CleanupFailingRollbackDb(PendingTransactionDb):
        def close_all_result_continuations(self) -> None:
            raise RuntimeError("cursor close failed")

    db = CleanupFailingRollbackDb()
    app = make_app(db=db)
    app.state.active_result = QueryResult("data", ["A"], [["1"]], "1 row")
    app.state.last_result = app.state.active_result
    app.state.focus = FOCUS_RESULTS

    app.rollback_transaction()
    app.wait_for_db_operation(timeout=1)

    assert db.calls == ["rollback"]
    assert app.state.active_result is None
    assert app.state.last_result is None
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.status.startswith("Rollback transaction,")
    assert "warning: result cleanup failed: RuntimeError: cursor close failed" in app.state.status


def test_quit_rejects_active_database_operation_without_prompting():
    app = make_app(db=PendingTransactionDb())
    app.state.db_operation = pending_db_operation(app)
    app.prompt = lambda label, default="", strip=True: (_ for _ in ()).throw(
        AssertionError("unexpected prompt")
    )

    app.request_quit()

    assert app.running is True
    assert app.state.status == "Quit unavailable while database operation is running"


def test_dirty_quit_cancel_does_not_resolve_pending_transaction():
    db = PendingTransactionDb()
    app = make_app(db=db)
    app.state.buffer.insert_char("x")
    app.prompt = lambda label, default="", strip=True: "c"

    app.request_quit()

    assert db.calls == []
    assert db.has_uncommitted_changes is True
    assert app.running is True
    assert app.state.buffer.dirty is True
    assert app.state.status == "Quit cancelled"


def test_browser_refresh_failure_keeps_focus_and_reports_error():
    app = make_app(db=FailingRefreshDb())
    app.state.focus = FOCUS_BROWSER
    app.state.buffer = Buffer(lines=["select 1 from dual"], row=0, col=0)

    app.refresh_browser()
    app.wait_for_db_operation(timeout=1)

    assert app.state.buffer.text() == "select 1 from dual"
    assert app.state.status == "Schema refresh failed"
    assert any(line.startswith("ERROR refreshing schema browser") for line in app.state.results)


def test_draw_result_grid_highlights_selected_cell(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=FakeScreen(height=12, width=60))
    app.state.active_result = QueryResult("data", ["A", "B"], [["one", "two"]], "1 row")
    app.state.focus = FOCUS_RESULTS
    app.state.result_col = 1

    app.draw_result_grid(0, 6, 40)

    assert any(call.text.startswith("two") and call.attr & curses.A_REVERSE for call in app.screen.calls)


def test_draw_result_grid_escapes_embedded_null(monkeypatch):
    class RejectingNullScreen(FakeScreen):
        def addstr(self, y: int, x: int, text: str, attr: int = 0):
            if "\x00" in text:
                raise ValueError("embedded null character")
            super().addstr(y, x, text, attr)

    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=RejectingNullScreen(height=12, width=60))
    app.state.active_result = QueryResult("data", ["VALUE"], [["A\x00B"]], "1 row")

    app.draw_result_grid(0, 6, 40)

    assert any("A\\x00B" in call.text for call in app.screen.calls)
    assert all("\x00" not in call.text for call in app.screen.calls)


def test_draw_result_grid_displays_data_line_break_as_space(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=FakeScreen(height=12, width=60))
    app.state.active_result = QueryResult("data", ["VALUE"], [["A\nB"]], "1 row")

    app.draw_result_grid(0, 6, 40)

    assert any("A B" in call.text for call in app.screen.calls)
    assert all("\\x0a" not in call.text and "\n" not in call.text for call in app.screen.calls)


def test_draw_editor_highlights_matching_brackets(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    line = "select (a) from dual"
    app = make_app(screen=FakeScreen(height=6, width=60))
    app.state.buffer = Buffer(lines=[line], row=0, col=line.index("("))

    app.draw_editor(0, 3, 60)

    highlighted = {
        call.text
        for call in app.screen.calls
        if call.text in {"(", ")"} and call.attr & curses.A_REVERSE and call.attr & curses.A_BOLD
    }
    assert highlighted == {"(", ")"}


def test_draw_editor_bracket_overlay_preserves_syntax_highlighting(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    line = "select (n'ok') from dual -- comment"
    app = make_app(screen=FakeScreen(height=6, width=80))
    app.syntax_colors_enabled = True
    app.state.buffer = Buffer(lines=[line], row=0, col=line.index("("))

    app.draw_editor(0, 3, 80)

    keyword = next(call for call in app.screen.calls if call.text == "select")
    string = next(call for call in app.screen.calls if call.text == "n'ok'")
    comment = next(call for call in app.screen.calls if call.text == "-- comment")
    highlighted = [
        call
        for call in app.screen.calls
        if call.text in {"(", ")"} and call.attr & curses.A_REVERSE and call.attr & curses.A_BOLD
    ]

    assert keyword.attr == curses.color_pair(ui.COLOR_SYNTAX_KEYWORD) | curses.A_BOLD
    assert string.attr == curses.color_pair(ui.COLOR_SYNTAX_STRING) | curses.A_BOLD
    assert comment.attr == curses.color_pair(ui.COLOR_SYNTAX_COMMENT) | curses.A_DIM
    assert {call.text for call in highlighted} == {"(", ")"}
    assert all(call.attr & curses.color_pair(ui.COLOR_SYNTAX_OPERATOR) for call in highlighted)


def test_init_colors_uses_256_color_syntax_palette(monkeypatch):
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda pair, fg, bg: calls.append((pair, fg, bg)))
    monkeypatch.setattr(curses, "COLORS", 256, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 64, raising=False)
    app = make_app()

    app.init_colors()

    assert app.syntax_colors_enabled is True
    assert (ui.COLOR_SYNTAX_KEYWORD, 33, -1) in calls
    assert (ui.COLOR_SYNTAX_STRING, 114, -1) in calls
    assert (ui.COLOR_SYNTAX_NUMBER, 214, -1) in calls
    assert (ui.COLOR_SYNTAX_BIND, 177, -1) in calls
    assert (ui.COLOR_SYNTAX_OPERATOR, 250, -1) in calls


def test_init_colors_applies_configured_editor_colors(monkeypatch):
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda pair, fg, bg: calls.append((pair, fg, bg)))
    monkeypatch.setattr(curses, "COLORS", 256, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 64, raising=False)
    app = make_app(
        config=make_config(
            editor_colors={
                "keyword": 160,
                "string": 45,
                "bind": 210,
                "operator": 200,
            }
        )
    )

    app.init_colors()

    assert app.syntax_colors_enabled is True
    assert (ui.COLOR_SYNTAX_KEYWORD, 160, -1) in calls
    assert (ui.COLOR_SYNTAX_STRING, 45, -1) in calls
    assert (ui.COLOR_SYNTAX_BIND, 210, -1) in calls
    assert (ui.COLOR_SYNTAX_OPERATOR, 200, -1) in calls
    assert (ui.COLOR_SYNTAX_NUMBER, 214, -1) in calls


def test_init_colors_applies_configured_explain_plan_colors(monkeypatch):
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda pair, fg, bg: calls.append((pair, fg, bg)))
    monkeypatch.setattr(curses, "COLORS", 256, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 64, raising=False)
    app = make_app(
        config=make_config(
            explain_colors={
                "connector": 160,
                "operation": 45,
                "object": 200,
                "metrics": 244,
                "text": 15,
            }
        )
    )

    app.init_colors()

    assert app.explain_color_kinds_enabled == {"connector", "operation", "object", "metrics", "text"}
    assert (ui.COLOR_PLAN_CONNECTOR, 160, -1) in calls
    assert (ui.COLOR_PLAN_OPERATION, 45, -1) in calls
    assert (ui.COLOR_PLAN_OBJECT, 200, -1) in calls
    assert (ui.COLOR_PLAN_METRICS, 244, -1) in calls
    assert (ui.COLOR_PLAN_TEXT, 15, -1) in calls


def test_init_colors_ignores_editor_colors_unsupported_by_terminal(monkeypatch):
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda pair, fg, bg: calls.append((pair, fg, bg)))
    monkeypatch.setattr(curses, "COLORS", 8, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 64, raising=False)
    app = make_app(config=make_config(editor_colors={"keyword": 14, "string": curses.COLOR_CYAN}))

    app.init_colors()

    assert app.syntax_colors_enabled is True
    assert (ui.COLOR_SYNTAX_KEYWORD, curses.COLOR_YELLOW, -1) in calls
    assert (ui.COLOR_SYNTAX_STRING, curses.COLOR_CYAN, -1) in calls


def test_init_colors_ignores_explain_colors_unsupported_by_terminal(monkeypatch):
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda pair, fg, bg: calls.append((pair, fg, bg)))
    monkeypatch.setattr(curses, "COLORS", 8, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 64, raising=False)
    app = make_app(config=make_config(explain_colors={"connector": 14, "operation": curses.COLOR_CYAN}))

    app.init_colors()

    assert app.explain_color_kinds_enabled == {"operation"}
    assert (ui.COLOR_PLAN_CONNECTOR, 14, -1) not in calls
    assert (ui.COLOR_PLAN_OPERATION, curses.COLOR_CYAN, -1) in calls


def test_init_colors_uses_bright_syntax_palette_on_16_color_terminal(monkeypatch):
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda pair, fg, bg: calls.append((pair, fg, bg)))
    monkeypatch.setattr(curses, "COLORS", 16, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 64, raising=False)
    app = make_app()

    app.init_colors()

    assert app.syntax_colors_enabled is True
    assert (ui.COLOR_SYNTAX_KEYWORD, 14, -1) in calls
    assert (ui.COLOR_SYNTAX_STRING, 10, -1) in calls
    assert (ui.COLOR_SYNTAX_NUMBER, 11, -1) in calls
    assert (ui.COLOR_SYNTAX_COMMENT, 12, -1) in calls
    assert (ui.COLOR_SYNTAX_BIND, 13, -1) in calls
    assert (ui.COLOR_SYNTAX_OPERATOR, 15, -1) in calls


def test_init_colors_uses_standard_syntax_palette_on_8_color_terminal(monkeypatch):
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda pair, fg, bg: calls.append((pair, fg, bg)))
    monkeypatch.setattr(curses, "COLORS", 8, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 64, raising=False)
    app = make_app()

    app.init_colors()

    assert app.syntax_colors_enabled is True
    assert (ui.COLOR_SYNTAX_KEYWORD, curses.COLOR_YELLOW, -1) in calls
    assert (ui.COLOR_SYNTAX_STRING, curses.COLOR_GREEN, -1) in calls
    assert (ui.COLOR_SYNTAX_NUMBER, curses.COLOR_CYAN, -1) in calls
    assert (ui.COLOR_SYNTAX_COMMENT, curses.COLOR_BLUE, -1) in calls
    assert (ui.COLOR_SYNTAX_BIND, getattr(curses, "COLOR_MAGENTA", curses.COLOR_RED), -1) in calls
    assert (ui.COLOR_SYNTAX_OPERATOR, curses.COLOR_WHITE, -1) in calls


def test_init_colors_falls_back_to_black_background_when_default_colors_fail(monkeypatch):
    calls: list[tuple[int, int, int]] = []

    def fail_default_colors() -> None:
        raise curses.error("default colors unavailable")

    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", fail_default_colors)
    monkeypatch.setattr(curses, "init_pair", lambda pair, fg, bg: calls.append((pair, fg, bg)))
    monkeypatch.setattr(curses, "COLORS", 8, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 64, raising=False)
    app = make_app()

    app.init_colors()

    assert app.syntax_colors_enabled is True
    assert (2, curses.COLOR_CYAN, curses.COLOR_BLACK) in calls
    assert (ui.COLOR_SYNTAX_STRING, curses.COLOR_GREEN, curses.COLOR_BLACK) in calls


def test_init_colors_failed_syntax_pairs_use_visible_fallback_attrs(monkeypatch):
    def fail_syntax_pairs(pair: int, fg: int, bg: int) -> None:
        if pair >= ui.COLOR_SYNTAX_KEYWORD:
            raise curses.error("no pair")

    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", fail_syntax_pairs)
    monkeypatch.setattr(curses, "COLORS", 8, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 64, raising=False)
    app = make_app()

    app.init_colors()

    assert app.syntax_colors_enabled is False
    assert app.syntax_attr(ui.SYNTAX_KEYWORD) == curses.A_BOLD
    assert app.syntax_attr(ui.SYNTAX_STRING) == getattr(curses, "A_UNDERLINE", 0)
    assert app.syntax_attr(ui.SYNTAX_NUMBER) == curses.A_BOLD
    assert app.syntax_attr(ui.SYNTAX_OPERATOR) == curses.A_BOLD


def test_draw_editor_uses_syntax_colors_after_color_init(monkeypatch):
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda pair, fg, bg: calls.append((pair, fg, bg)))
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    monkeypatch.setattr(curses, "COLORS", 8, raising=False)
    monkeypatch.setattr(curses, "COLOR_PAIRS", 64, raising=False)
    line = "select rowid, active_flag, jobs.comm from jobs where active_flag = 'Y';"
    app = make_app(screen=FakeScreen(height=6, width=160))
    app.state.buffer = Buffer(lines=[line], row=0, col=0)

    app.init_colors()
    app.draw_editor(0, 3, 160)

    select_call = next(call for call in app.screen.calls if call.text == "select")
    from_call = next(call for call in app.screen.calls if call.text == "from")
    string_call = next(call for call in app.screen.calls if call.text == "'Y'")
    operator_call = next(call for call in app.screen.calls if call.text == ".")

    assert app.syntax_colors_enabled is True
    assert (ui.COLOR_SYNTAX_KEYWORD, curses.COLOR_YELLOW, -1) in calls
    assert select_call.attr == curses.color_pair(ui.COLOR_SYNTAX_KEYWORD) | curses.A_BOLD
    assert from_call.attr == curses.color_pair(ui.COLOR_SYNTAX_KEYWORD) | curses.A_BOLD
    assert string_call.attr == curses.color_pair(ui.COLOR_SYNTAX_STRING) | curses.A_BOLD
    assert operator_call.attr == curses.color_pair(ui.COLOR_SYNTAX_OPERATOR) | curses.A_BOLD
    assert all(call.attr for call in (select_call, from_call, string_call, operator_call))


def test_draw_text_results_and_dbms_output_honor_scroll_offsets(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=FakeScreen(height=8, width=60))
    app.state.results = [f"result {idx}" for idx in range(5)]
    app.state.active_tab.results_scroll = 1

    app.draw_text_results(0, 4, 40)

    assert [call.text.strip() for call in app.screen.calls if call.y in (1, 2, 3)] == [
        "result 1",
        "result 2",
        "result 3",
    ]

    app.screen.calls.clear()
    app.state.dbms_output = [f"output {idx}" for idx in range(5)]
    app.state.active_tab.dbms_output_scroll = 2

    app.draw_dbms_output(0, 4, 40)

    assert [call.text.strip() for call in app.screen.calls if call.y in (1, 2, 3)] == [
        "output 2",
        "output 3",
        "output 4",
    ]


def test_draw_help_results_uses_ascii_layout_and_color_segments(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=FakeScreen(height=28, width=90))
    app.show_help()

    app.draw_results(0, 24, 90)

    assert any("PLSQLWKS HELP" in call.text and call.attr == ((3 << 8) | curses.A_BOLD) for call in app.screen.calls)
    assert any(" GLOBAL " in call.text and call.attr == ((3 << 8) | curses.A_BOLD) for call in app.screen.calls)
    assert any("F1" in call.text and call.attr == ((2 << 8) | curses.A_BOLD) for call in app.screen.calls)
    assert any(call.text.startswith("+") and call.attr & curses.A_DIM for call in app.screen.calls)
    assert all(0 <= call.x < app.screen.width for call in app.screen.calls)
    assert all(display_width(call.text) <= app.screen.width - call.x for call in app.screen.calls)


def test_show_help_can_include_workspace_diagnostics():
    app = make_app(screen=FakeScreen(height=28, width=90))

    app.show_help(["Password file is missing: /tmp/orapass", "Will create: /tmp/workspace/sql"])

    assert app.state.results_style == ui.RESULT_STYLE_HELP
    assert any(" WORKSPACE " in line for line in app.state.results)
    assert any("Password file is missing" in line for line in app.state.results)
    assert any("Will create: /tmp/workspace/sql" in line for line in app.state.results)
    assert any(" WORKSPACE " in line.text for line in app.state.active_tab.help_lines)


def test_run_initializes_colorized_help_with_startup_warnings_and_workspace_health(monkeypatch, tmp_path):
    restored_path = tmp_path / "restored.sql"
    restored_path.write_text("select 1 from dual;\n", encoding="utf-8")
    startup_warnings = (
        "Using legacy source workspace: /old/workspace",
        "Password file permissions are 0644, expected 0600: /old/orapass",
    )
    config = make_config(
        tmp_path,
        session_tabs=(SessionTab(restored_path, row=0, col=7),),
        startup_warnings=startup_warnings,
    )
    db = ClosingDb()
    app = make_app(screen=FakeScreen(height=12, width=80), db=db, config=config)
    app.screen.keypad = lambda enabled: None
    app.screen.leaveok = lambda enabled: None
    app.screen.timeout = lambda delay: None
    app.show_cursor = lambda: None
    app.init_colors = lambda: None
    app.wait_for_db_operation = lambda: None
    app.close_all_result_continuations = lambda: None
    app.shutdown_database_worker = db.close
    curses_modes: list[str] = []
    monkeypatch.setattr(curses, "raw", lambda: None)
    monkeypatch.setattr(curses, "nonl", lambda: curses_modes.append("nonl"))
    monkeypatch.setattr(curses, "nl", lambda: curses_modes.append("nl"))
    monkeypatch.setattr(curses, "noraw", lambda: curses_modes.append("noraw"))
    monkeypatch.setattr(ui, "enable_extended_keyboard_reporting", lambda: False)

    original_show_help = app.show_help
    seen: dict[str, object] = {}

    def show_help(messages=None, *, focus_results=True):
        seen["messages"] = messages
        seen["focus_results"] = focus_results
        original_show_help(messages, focus_results=focus_results)

    def try_connect():
        seen["style_after_help"] = app.state.results_style
        app.running = False
        app.state.status = "Connected as hr"

    app.show_help = show_help
    app.try_connect = try_connect

    App.run(app)

    assert seen["messages"] == [*startup_warnings, *ui.workspace_health(config)]
    assert seen["focus_results"] is False
    assert seen["style_after_help"] == ui.RESULT_STYLE_HELP
    assert app.state.buffer.path == restored_path.resolve()
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 7)
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.results_style == ui.RESULT_STYLE_HELP
    assert any(" WORKSPACE " in line for line in app.state.results)
    assert db.closed is True
    assert curses_modes == ["nonl", "nl", "noraw"]


def test_help_results_scroll_with_focused_results_pane():
    app = make_app(screen=FakeScreen(height=12, width=80))
    app.show_help()
    app.state.focus = FOCUS_RESULTS

    App.handle_key(app, ui.KEY_CTRL_DOWN)

    assert app.state.active_tab.results_scroll == 1
    assert app.state.status.startswith("Results: lines 2-")

    App.handle_key(app, ui.KEY_CTRL_UP)

    assert app.state.active_tab.results_scroll == 0


def test_draw_tab_bar_highlights_active_tab_and_dirty_marker(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=FakeScreen(height=8, width=50))
    app.state.tabs = [
        FileTab(buffer=Buffer(title="one.sql")),
        FileTab(buffer=Buffer(title="two.sql", dirty=True)),
    ]
    app.state.active_tab_idx = 1

    app.draw_tab_bar(1, 50)

    assert any("1 one.sql" in call.text for call in app.screen.calls)
    assert any("2 two.sql*" in call.text and call.attr & curses.A_REVERSE for call in app.screen.calls)


def test_draw_header_shows_active_tab_count_title_and_dirty_marker(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=FakeScreen(height=8, width=80))
    app.state.tabs = [
        FileTab(buffer=Buffer(title="one.sql")),
        FileTab(buffer=Buffer(title="two.sql", dirty=True)),
    ]
    app.state.active_tab_idx = 1

    app.draw_header(80)

    assert any("tab 2/2" in call.text and "two.sql*" in call.text for call in app.screen.calls)


def test_draw_with_browser_offsets_tab_bar_and_editor(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=FakeScreen(height=18, width=80))
    app.state.browser_visible = True
    app.state.tabs = [
        FileTab(buffer=Buffer(title="one.sql", lines=["select 1 from dual"])),
        FileTab(buffer=Buffer(title="two.sql", lines=["select 2 from dual"])),
    ]
    app.state.active_tab_idx = 1

    app.draw()

    assert any(call.y == 1 and call.x == 25 and "1 one.sql" in call.text for call in app.screen.calls)
    assert any(call.y == 2 and call.x == 25 and "1 " in call.text for call in app.screen.calls)


def test_draw_browser_shows_filter_no_matches_and_keeps_cursor_in_header(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=FakeScreen(height=8, width=30))
    app.state.focus = FOCUS_BROWSER
    app.state.browser_filter = "MISSING"
    app.state.browser_objects["TABLE"] = ["DECISIONS"]

    cursor = app.draw_browser(1, 5, 30)

    assert any(call.y == 1 and call.text.startswith(" Schema | Filter: MISSING") for call in app.screen.calls)
    assert any(call.y == 2 and call.text.strip() == "No matches" for call in app.screen.calls)
    assert cursor == (1, display_width(" Schema | Filter: MISSING"))

    app.screen.calls.clear()
    cursor = app.draw_browser(1, 5, 12)

    assert cursor == (1, 11)
    assert all(display_width(call.text) <= 12 - call.x for call in app.screen.calls)


def test_draw_fullscreen_results_hides_tab_bar_and_editor(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=FakeScreen(height=12, width=80))
    app.state.result_ratio = ui.RESULT_RATIO_FULLSCREEN
    app.state.focus = FOCUS_RESULTS
    app.state.buffer = Buffer(title="query.sql", lines=["select 1 from dual"])
    app.state.active_result = QueryResult("data", ["A"], [["1"], ["2"]], "2 rows")

    app.draw()

    assert any(call.y == 1 and call.x == 0 and call.text.startswith(" Results > grid") for call in app.screen.calls)
    assert not any("[1 query.sql]" in call.text for call in app.screen.calls)
    assert all(0 <= call.y < app.screen.height for call in app.screen.calls)


def test_draw_grid_only_fullscreen_starts_with_data_header_and_uses_last_line(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=FakeScreen(height=6, width=40))
    app.state.result_grid_fullscreen = True
    app.state.result_ratio = ui.RESULT_RATIO_FULLSCREEN
    app.state.focus = FOCUS_RESULTS
    app.state.buffer = Buffer(title="query.sql", lines=["select 1 from dual"])
    app.state.active_result = QueryResult(
        "data",
        ["ID", "NAME"],
        [["1", "one"], ["2", "two"], ["3", "three"], ["4", "four"]],
        "4 rows",
    )

    app.draw()

    assert any(call.y == 0 and call.x == 0 and call.text.startswith("ID") for call in app.screen.calls)
    assert any(call.y == 1 and "-+-" in call.text for call in app.screen.calls)
    assert any(call.y == 5 and "four" in call.text for call in app.screen.calls)
    assert not any(call.text.startswith(" Results > grid") for call in app.screen.calls)
    assert not any("[1 query.sql]" in call.text for call in app.screen.calls)
    assert not any(call.text.startswith("[ ]") or call.text.startswith("[*]") for call in app.screen.calls)
    assert all(0 <= call.y < app.screen.height for call in app.screen.calls)


def test_draw_editor_fullscreen_starts_on_first_line_and_uses_last_line(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=FakeScreen(height=5, width=40))
    app.state.result_ratio = ui.RESULT_RATIO_EDITOR_FULLSCREEN
    app.state.focus = FOCUS_EDITOR
    app.state.buffer = Buffer(title="query.sql", lines=["one", "two", "three", "four", "five"], row=4, col=4)
    app.state.active_result = QueryResult("data", ["A"], [["1"]], "1 row")

    app.draw()

    assert any(call.y == 0 and call.text.strip() == "1" for call in app.screen.calls)
    assert any(call.y == 0 and call.text == "one" for call in app.screen.calls)
    assert any(call.y == 4 and call.text == "five" for call in app.screen.calls)
    assert not any(call.text.startswith("[O] plsqlwks") for call in app.screen.calls)
    assert not any(call.text.startswith(" Results") for call in app.screen.calls)
    assert not any(call.text.startswith("[ ]") or call.text.startswith("[*]") for call in app.screen.calls)
    assert all(0 <= call.y < app.screen.height for call in app.screen.calls)


def test_draw_narrow_terminal_with_browser_many_tabs_and_long_unicode_result(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    app = make_app(screen=FakeScreen(height=9, width=32))
    app.state.tabs = [
        FileTab(buffer=Buffer(title=f"tab{idx}_Příliš_žluťoučký.sql", lines=["select q'[semi;]' from dual"]))
        for idx in range(12)
    ]
    app.state.active_tab_idx = 8
    app.state.browser_visible = True
    app.state.browser_expanded.add("TABLE")
    app.state.browser_objects["TABLE"] = ["DECISIONS", "ŽLUTOUCKY"]
    app.state.active_result = QueryResult(
        "wide",
        ["ROWID", "LONG_NAME", "NULLABLE"],
        [["AAABBBCCC", "Příliš žluťoučký kůň; with semicolon", "NULL"], ["SHORT"]],
        "2 rows",
    )
    app.state.focus = FOCUS_RESULTS
    app.state.result_col = 1

    app.draw()

    assert app.screen.calls
    assert app.screen.moves
    assert app.state.active_tab_idx == 8
    assert all(0 <= call.y < app.screen.height for call in app.screen.calls)
    assert all(0 <= call.x < app.screen.width for call in app.screen.calls)
    assert all(display_width(call.text) <= app.screen.width - call.x for call in app.screen.calls)


def test_rendered_results_are_isolated_per_tab():
    app = make_app()

    app.render_results([QueryResult("first", ["A"], [["1"]], "first ok")])
    first_results = list(app.state.results)
    app.new_tab()
    app.render_results([QueryResult("second", ["B"], [["2"]], "second ok")])

    assert app.state.active_result.title == "second"
    assert any("[second] second ok" in line for line in app.state.results)

    app.switch_to_tab(0)
    assert app.state.active_result.title == "first"
    assert app.state.results == first_results
    assert any("[first] first ok" in line for line in app.state.results)


def test_async_db_operation_finishes_into_originating_tab():
    app = make_app()
    release = ui.threading.Event()

    def worker(db, progress):
        release.wait(1)
        return [QueryResult("async", ["A"], [["1"]], "1 row")]

    app.start_db_operation("execute", "Running current statement", worker)
    app.new_tab(status="second")

    release.set()
    app.wait_for_db_operation(timeout=1)

    assert app.state.active_tab_idx == 1
    assert app.state.active_result is None
    app.switch_to_tab(0)
    assert app.state.active_result is not None
    assert app.state.active_result.title == "async"


def test_script_worker_closes_superseded_paged_results_before_completion():
    class MultiPagingDb:
        autocommit = True

        def __init__(self):
            self.next_token = 1
            self.closed_tokens: list[str] = []

        def execute_statement(self, statement: str, title: str = "Statement") -> QueryResult:
            token = f"cursor-{self.next_token}"
            self.next_token += 1
            return QueryResult(
                title,
                ["VALUE"],
                [[str(self.next_token)]],
                "1 row (limited)",
                continuation=QueryResultContinuation(token),
            )

        def close_result_continuation(self, continuation: QueryResultContinuation) -> None:
            self.closed_tokens.append(continuation.token)

    db = MultiPagingDb()
    app = make_app(db=db)
    app.state.buffer = Buffer(
        lines=[
            "select 1 from dual;",
            "select 2 from dual;",
            "select 3 from dual;",
        ]
    )

    app.run_script()
    app.wait_for_db_operation(timeout=1)

    assert db.closed_tokens == ["cursor-1", "cursor-2"]
    assert app.state.active_result is not None
    assert app.state.active_result.title.startswith("Statement 3")
    assert app.state.active_result.continuation == QueryResultContinuation("cursor-3")


def test_f6_toggles_dbms_output_for_table_results(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair)
    app = make_app(screen=FakeScreen(height=8, width=60))
    app.render_results(
        [
            QueryResult("Select", ["TITLE"], [["table row"]], "1 row"),
            QueryResult("Block", ["DBMS_OUTPUT"], [["from output"]], "1 dbms_output line(s)"),
        ]
    )

    assert app.state.show_dbms_output is False
    assert app.state.dbms_output == ["from output"]
    app.draw_results(0, 5, 60)
    assert any("TITLE" in call.text for call in app.screen.calls)
    assert not any("from output" in call.text for call in app.screen.calls)

    app.enter_results_focus()
    App.handle_key(app, curses.KEY_F6)

    assert app.state.show_dbms_output is True
    assert app.state.focus == FOCUS_EDITOR
    app.screen.erase()
    app.draw_results(0, 5, 60)
    assert any("from output" in call.text for call in app.screen.calls)

    App.handle_key(app, ui.TAB)

    assert app.state.show_dbms_output is False
    assert app.state.focus == FOCUS_RESULTS


def test_dbms_output_only_result_is_visible_in_small_result_pane(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair)
    app = make_app(screen=FakeScreen(height=4, width=50))

    app.render_results([QueryResult("Block", ["DBMS_OUTPUT"], [["visible output"]], "summary")])
    app.draw_results(0, 2, 50)

    assert app.state.active_result is None
    assert app.state.show_dbms_output is True
    assert any("visible output" in call.text for call in app.screen.calls)
    assert not any("summary" in call.text for call in app.screen.calls)


def test_dbms_output_state_is_isolated_per_tab():
    app = make_app()

    app.render_results(
        [
            QueryResult("first", ["A"], [["1"]], "first ok"),
            QueryResult("first output", ["DBMS_OUTPUT"], [["one"]], "1 dbms_output line(s)"),
        ]
    )
    app.new_tab()
    app.render_results([QueryResult("second output", ["DBMS_OUTPUT"], [["two"]], "1 dbms_output line(s)")])

    assert app.state.dbms_output == ["two"]
    assert app.state.show_dbms_output is True

    app.switch_to_tab(0)

    assert app.state.active_result.title == "first"
    assert app.state.dbms_output == ["one"]
    assert app.state.show_dbms_output is False


def test_run_script_failure_shows_dbms_output_diagnostics_and_moves_cursor():
    app = make_app(db=DiagnosticScriptDb())
    app.state.buffer = Buffer(
        lines=[
            "select 1 from dual;",
            "begin",
            "  raise_application_error(-20000, 'boom');",
            "exception",
            "  when others then",
            "    dbms_output.put_line('Error raised in: <anonymous> at line 2 - ' || sqlerrm);",
            "    dbms_output.put_line(dbms_utility.format_error_backtrace);",
            "    raise;",
            "end;",
            "/",
        ],
        row=0,
        col=0,
    )

    App.run_script(app)
    App.wait_for_db_operation(app, timeout=1)

    assert (app.state.buffer.row, app.state.buffer.col) == (2, 0)
    assert app.state.status.startswith("Execution failed at line 3, column 1: ORA-20000: boom")
    assert "Error location: line 3, column 1" in app.state.results
    assert any("[Statement 1 lines 1-1] 1 row" in line for line in app.state.results)
    assert any(line == "Diagnostics:" for line in app.state.results)
    assert any(line == "DBMS_OUTPUT:" for line in app.state.results)
    assert any("Error raised in: <anonymous> at line 2 - ORA-20000: boom" in line for line in app.state.results)


def test_rendered_dbms_output_wraps_as_text_instead_of_grid():
    app = make_app(screen=FakeScreen(height=12, width=32))
    message = "Příliš žluťoučký kůň " + "x" * 80 + " END"

    app.render_results([QueryResult("Block", ["DBMS_OUTPUT"], [[message]], "1 dbms_output line(s)")])

    assert app.state.active_result is None
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.dbms_output == [message]
    assert app.state.show_dbms_output is True
    assert app.state.results[0] == "[Block] 1 dbms_output line(s)"
    assert "".join(app.state.results[1:]).replace(" ", "") == message.replace(" ", "")
    assert not any("Tab opens the result grid" in line for line in app.state.results)


def test_status_bar_shows_transaction_pending_indicator(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair)
    app = make_app(screen=FakeScreen(height=5, width=24), db=FakeEditingDb())
    app.state.status = "Ready"

    app.draw_status(4, 24)

    assert app.screen.calls[-1].text.startswith("[ ] Ready")
    assert display_width(app.screen.calls[-1].text) == 24

    app.state.db.has_uncommitted_changes = True
    app.draw_status(4, 10)

    assert app.screen.calls[-1].text.startswith("[*] Ready")
    assert display_width(app.screen.calls[-1].text) == 10


def test_format_elapsed_hhmmss_uses_total_elapsed_hours():
    assert format_elapsed_hhmmss(0) == "00:00:00"
    assert format_elapsed_hhmmss(9.9) == "00:00:09"
    assert format_elapsed_hhmmss(65) == "00:01:05"
    assert format_elapsed_hhmmss(3661) == "01:01:01"
    assert format_elapsed_hhmmss(25 * 3600) == "25:00:00"


def test_status_bar_shows_active_db_operation_timer(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair)
    monkeypatch.setattr(ui.time, "monotonic", lambda: 3761.0)
    app = make_app(screen=FakeScreen(height=5, width=60))
    app.state.db_operation = pending_db_operation(app, started_at=100.0)

    app.draw_status(4, 60)

    assert app.screen.calls[-1].text.startswith("[ ] Running current statement 01:01:01")


def test_ctrl_c_interrupts_active_db_operation(monkeypatch):
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair)
    monkeypatch.setattr(ui.time, "monotonic", lambda: 15.0)
    db = InterruptibleDb()
    app = make_app(screen=FakeScreen(height=5, width=80), db=db)
    release = start_blocking_db_operation(app)
    assert app.state.db_operation is not None
    app.state.db_operation.started_at = 5.0

    app.handle_key(ui.CTRL_C)

    assert db.cancel_calls == 1
    assert app.state.db_operation is not None
    assert app.state.db_operation.cancel_requested is True
    assert app.state.status == "Database interrupt requested"

    app.draw_status(4, 80)

    assert app.screen.calls[-1].text.startswith("[ ] Running current statement (interrupt requested) 00:00:10")

    release.set()
    app.wait_for_db_operation(timeout=1)
    app.shutdown_database_worker()


def test_repeated_ctrl_c_does_not_repeat_database_interrupt():
    db = InterruptibleDb()
    app = make_app(db=db)
    release = start_blocking_db_operation(app)

    app.handle_key(ui.CTRL_C)
    app.handle_key(ui.CTRL_C)

    assert db.cancel_calls == 1
    assert app.state.status == "Database interrupt already requested"

    release.set()
    app.wait_for_db_operation(timeout=1)
    app.shutdown_database_worker()


def test_cancelled_oracle_operation_reports_interrupted_status():
    app = make_app()
    exc = OracleExecutionError(
        RuntimeError("ORA-01013: user requested cancel of current operation"),
        "Block",
    )

    app.handle_execution_error(exc)

    assert app.state.status.startswith("Execution interrupted: ORA-01013")


def test_active_db_operation_rejects_second_run():
    app = make_app(db=FakeExplainDb())
    app.state.buffer = Buffer(lines=["select * from decisions"], row=0, col=0)
    app.state.db_operation = pending_db_operation(app)

    app.handle_key(curses.KEY_F5)

    assert app.state.status == "Database operation already running"


def test_set_results_wraps_text_and_clears_table_result():
    app = make_app(screen=FakeScreen(height=10, width=18))
    app.state.active_result = QueryResult("data", ["A"], [["1"]], "1 row")
    app.state.focus = FOCUS_RESULTS

    app.set_results(["ERROR " + "x" * 40])

    assert app.state.active_result is None
    assert app.state.focus == FOCUS_EDITOR
    assert len(app.state.results) > 1
    assert app.state.results[0].startswith("ERROR")


def test_edit_selected_result_cell_updates_displayed_value():
    db = FakeEditingDb()
    app = make_app(db=db)
    app.state.active_result = QueryResult(
        "data",
        ["ROWID", "NAME"],
        [["AAABBBCCC", "old"]],
        "1 row",
        editable_context=EditableResultContext("DECISIONS", 0, {1: "NAME"}),
        original_rows=[["AAABBBCCC", "old"]],
    )
    app.state.result_col = 1
    prompts: list[tuple[str, str, bool]] = []

    def prompt(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default, strip))
        return "new"

    app.prompt = prompt

    app.edit_selected_result_cell()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [("Set NAME", "old", False)]
    assert db.updates == [("AAABBBCCC", 1, "old", "new")]
    assert app.state.active_result.rows[0][1] == "refreshed"
    assert app.state.active_result.original_rows[0][1] == "refreshed"
    assert app.state.status == "Updated NAME"


def test_edit_selected_result_cell_uses_empty_prompt_default_for_database_null():
    app = make_app(db=FakeEditingDb())
    app.state.active_result = QueryResult(
        "data",
        ["ROWID", "NAME"],
        [["AAABBBCCC", NULL_DISPLAY_TOKEN]],
        "1 row",
        editable_context=EditableResultContext("DECISIONS", 0, {1: "NAME"}),
    )
    app.state.result_col = 1
    prompts: list[tuple[str, str, bool]] = []

    def cancel_prompt(label: str, default: str = "", strip: bool = True) -> None:
        prompts.append((label, default, strip))
        return None

    app.prompt = cancel_prompt

    app.edit_selected_result_cell()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [("Set NAME", "", False)]
    assert app.state.active_result.rows[0][1] == NULL_DISPLAY_TOKEN
    assert app.state.status == "Edit cancelled"


def test_edit_selected_result_cell_in_manual_mode_reports_pending_commit():
    db = FakeEditingDb(autocommit=False)
    app = make_app(db=db)
    app.state.active_result = editable_result()
    app.state.result_col = 1
    app.prompt = lambda label, default="", strip=True: "manual"

    app.edit_selected_result_cell()
    app.wait_for_db_operation(timeout=1)

    assert db.updates == [("AAABBBCCC", 1, "old", "manual")]
    assert app.state.active_result.rows[0][1] == "refreshed"
    assert app.state.status == "Updated NAME (pending commit)"


def test_edit_selected_result_cell_cancel_and_failure_leave_displayed_value():
    app = make_app(db=FakeEditingDb())
    app.state.active_result = editable_result()
    app.state.result_col = 1
    app.prompt = lambda label, default="", strip=True: None

    app.edit_selected_result_cell()

    assert app.state.active_result.rows[0][1] == "old"
    assert app.state.status == "Edit cancelled"

    failing = make_app(db=FailingEditingDb())
    failing.state.active_result = editable_result()
    failing.state.result_col = 1
    failing.prompt = lambda label, default="", strip=True: "new"

    failing.edit_selected_result_cell()
    failing.wait_for_db_operation(timeout=1)

    assert failing.state.active_result.rows[0][1] == "old"
    assert failing.state.status.startswith("Cell update failed:")


def test_insert_draft_row_edits_and_commits_from_result_grid():
    db = FakeEditingDb()
    app = make_app(db=db)
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = editable_result()
    app.state.result_col = 1

    app.handle_results_key(curses.KEY_IC)

    assert app.state.active_result.rows == [["<new>", "<NULL>"], ["AAABBBCCC", "old"]]
    assert app.state.result_row == 0
    assert app.state.result_row_scroll == 0
    assert app.state.result_col == 1
    assert app.state.active_tab.result_insert_draft is not None
    assert app.state.status == "Insert draft active: Enter edits cell, Ctrl-Alt-C inserts, Esc cancels"

    app.prompt = lambda label, default="", strip=True: "new"
    app.handle_results_key(10)

    assert app.state.active_result.rows[0] == ["<new>", "new"]
    assert app.state.status == "Set NAME in insert draft"

    App.handle_key(app, KEY_CTRL_ALT_C)
    app.wait_for_db_operation(timeout=1)

    assert db.inserts == [({1: "new"}, 2)]
    assert app.state.active_result.rows == [["AAANEW", "inserted"], ["AAABBBCCC", "old"]]
    assert app.state.active_tab.result_insert_draft is None
    assert app.state.status == "Inserted row"


def test_insert_draft_row_starts_at_visible_grid_top_when_scrolled():
    app = make_app(db=FakeEditingDb())
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = QueryResult(
        "data",
        ["ROWID", "NAME"],
        [["ROW1", "one"], ["ROW2", "two"], ["ROW3", "three"]],
        "3 rows",
        editable_context=EditableResultContext("DECISIONS", 0, {1: "NAME"}),
    )
    app.state.result_row = 2
    app.state.result_row_scroll = 2

    app.handle_results_key(curses.KEY_IC)

    assert app.state.active_result.rows[0] == ["<new>", "<NULL>"]
    assert app.state.result_row == 0
    assert app.state.result_row_scroll == 0
    assert app.state.status == "Insert draft active: Enter edits cell, Ctrl-Alt-C inserts, Esc cancels"


def test_insert_draft_manual_mode_reports_pending_commit():
    db = FakeEditingDb(autocommit=False)
    app = make_app(db=db)
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = editable_result()

    app.handle_results_key(curses.KEY_IC)
    App.handle_key(app, KEY_CTRL_ALT_C)
    app.wait_for_db_operation(timeout=1)

    assert db.inserts == [({1: "<NULL>"}, 2)]
    assert app.state.status == "Inserted row (pending commit)"


def test_short_insert_draft_uses_database_null_token_for_missing_cells():
    db = FakeEditingDb()
    app = make_app(db=db)
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = QueryResult(
        "data",
        ["ROWID", "NAME", "NOTE"],
        [["AAABBBCCC", "old", "note"]],
        "1 row",
        editable_context=EditableResultContext("DECISIONS", 0, {1: "NAME", 2: "NOTE"}),
    )

    app.handle_results_key(curses.KEY_IC)
    draft = app.state.active_tab.result_insert_draft
    assert draft is not None
    draft.row.pop()
    app.state.result_col = 2
    defaults: list[str] = []

    def cancel_prompt(label: str, default: str = "", strip: bool = True) -> None:
        defaults.append(default)
        return None

    app.prompt = cancel_prompt
    app.edit_insert_draft_cell()
    App.handle_key(app, KEY_CTRL_ALT_C)
    app.wait_for_db_operation(timeout=1)

    assert defaults == [NULL_DISPLAY_TOKEN]
    assert db.inserts == [({1: NULL_DISPLAY_TOKEN, 2: NULL_DISPLAY_TOKEN}, 3)]


def test_insert_draft_cancel_and_failure_keep_or_remove_draft():
    app = make_app(db=FakeEditingDb())
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = editable_result()

    app.handle_results_key(curses.KEY_IC)
    app.handle_results_key(ESC)

    assert app.state.active_result.rows == [["AAABBBCCC", "old"]]
    assert app.state.active_tab.result_insert_draft is None
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.status == "Insert draft cancelled"

    failing = make_app(db=FailingInsertDb())
    failing.state.focus = FOCUS_RESULTS
    failing.state.active_result = editable_result()
    failing.handle_results_key(curses.KEY_IC)
    failing.prompt = lambda label, default="", strip=True: "new"
    failing.handle_results_key(10)

    App.handle_key(failing, KEY_CTRL_ALT_C)
    failing.wait_for_db_operation(timeout=1)

    assert failing.state.active_result.rows == [["<new>", "new"], ["AAABBBCCC", "old"]]
    assert failing.state.active_tab.result_insert_draft is not None
    assert failing.state.result_row == 0
    assert failing.state.status.startswith("Insert failed:")


def test_insert_draft_blocks_row_movement_and_row_detail_toggle():
    app = make_app(db=FakeEditingDb())
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = editable_result()

    app.handle_results_key(curses.KEY_IC)
    app.handle_results_key(curses.KEY_UP)

    assert app.state.result_row == 0
    assert app.state.status == "Insert draft active: Enter edits cell, Ctrl-Alt-C inserts, Esc cancels"

    App.handle_key(app, curses.KEY_F8)

    assert app.state.result_mode != RESULT_ROW_DETAIL
    assert app.state.status == "Insert draft active: Enter edits cell, Ctrl-Alt-C inserts, Esc cancels"


def test_insert_draft_rejects_readonly_non_grid_and_uneditable_results():
    readonly_db = FakeEditingDb()
    readonly_db.read_only = True
    app = make_app(db=readonly_db)
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = editable_result()

    app.handle_results_key(curses.KEY_IC)

    assert app.state.active_result.rows == [["AAABBBCCC", "old"]]
    assert app.state.status == "Row inserts are disabled in read-only mode"

    app = make_app(db=FakeEditingDb())
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = editable_result()
    app.state.result_mode = RESULT_ROW_DETAIL

    app.handle_results_key(curses.KEY_IC)

    assert app.state.status == "Insert row is only available in result grid"

    app = make_app(db=FakeEditingDb())
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = QueryResult("data", ["NAME"], [["old"]], "1 row")

    app.handle_results_key(curses.KEY_IC)

    assert app.state.status == "Result is not ROWID-editable"


def test_view_selected_result_cell_opens_modal_and_sets_status():
    app = make_app()
    app.state.active_result = QueryResult("data", ["VALUE"], [["long value"]], "1 row")
    seen: list[ResultCell] = []
    app.show_cell_viewer = lambda cell: seen.append(cell)

    app.view_selected_result_cell()

    assert seen == [ResultCell("VALUE", 0, 0, "long value")]
    assert app.state.status == "Viewed VALUE"


def test_cell_viewer_scrolls_and_closes(monkeypatch):
    windows: list[FakeWindow] = []

    def fake_newwin(height, width, top, left):
        window = FakeWindow(height, width)
        windows.append(window)
        return window

    monkeypatch.setattr(curses, "newwin", fake_newwin)
    app = make_app(screen=FakeScreen(height=12, width=50))
    keys = iter([curses.KEY_NPAGE, curses.KEY_END, ESC])
    app.read_key = lambda window=None, idle_timeout=200: next(keys)

    app.show_cell_viewer(ResultCell("VALUE", 0, 0, " ".join(f"word{i}" for i in range(40))))

    assert len(windows) == 3
    assert any("/" in call.text for window in windows for call in window.calls)


def make_app(screen: FakeScreen | None = None, db: object | None = None, config: AppConfig | None = None) -> App:
    app = object.__new__(App)
    app.screen = screen or FakeScreen()
    app.state = UIState(config=config or make_config(), db=db or object())
    app.running = True
    app.draw_offset_x = 0
    app.syntax_colors_enabled = False
    app.explain_color_kinds_enabled = set()
    return app


def pending_db_operation(app: App, started_at: float = 0.0) -> ui.DbOperation:
    return ui.DbOperation(
        kind="execute",
        label="Running current statement",
        started_at=started_at,
        handle=ui.DbCommandHandle(
            command_id=1,
            events=ui.queue.Queue(),
            done=ui.threading.Event(),
        ),
        tab=app.state.active_tab,
    )


def start_blocking_db_operation(app: App) -> ui.threading.Event:
    started = ui.threading.Event()
    release = ui.threading.Event()

    def block(db, progress):
        started.set()
        release.wait()
        return []

    assert app.start_db_operation("execute", "Running current statement", block)
    assert started.wait(1)
    return release


def make_config(
    root: Path | None = None,
    editor_colors: dict[str, int] | None = None,
    explain_colors: dict[str, int] | None = None,
    session_tabs: tuple[SessionTab, ...] = (),
    active_session_tab: int = 0,
    startup_warnings: tuple[str, ...] = (),
) -> AppConfig:
    workspace = root or Path("/tmp/plsqlwks-tests")
    return AppConfig(
        user="hr",
        dsn="db",
        password_file=workspace / "orapass" if root else Path("/tmp/orapass"),
        workspace_dir=workspace,
        editor_colors=editor_colors or {},
        explain_colors=explain_colors or {},
        session_tabs=session_tabs,
        active_session_tab=active_session_tab,
        startup_warnings=startup_warnings,
    )


class ClosingDb:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class PendingTransactionDb:
    def __init__(self, failing_action: str | None = None):
        self.autocommit = False
        self.has_uncommitted_changes = True
        self.failing_action = failing_action
        self.calls: list[str] = []

    def commit(self):
        self.calls.append("commit")
        if self.failing_action == "commit":
            raise RuntimeError("commit failed")
        self.has_uncommitted_changes = False
        return ui.TransactionReport(datetime.now(), rows_changed=1)

    def rollback(self):
        self.calls.append("rollback")
        if self.failing_action == "rollback":
            raise RuntimeError("rollback failed")
        self.has_uncommitted_changes = False
        return ui.TransactionReport(datetime.now(), rows_changed=1)


class FakeBrowserDb:
    def list_schema_objects(self):
        return {"TABLE": ["DECISIONS"], "VIEW": [], "PROCEDURE": [], "FUNCTION": [], "PACKAGE": []}

    def get_object_definition(self, object_type: str, object_name: str) -> str:
        return "create table decisions (id number);"


class FailingBrowserDb(FakeBrowserDb):
    def get_object_definition(self, object_type: str, object_name: str) -> str:
        raise RuntimeError("metadata failed")


class FailingRefreshDb(FakeBrowserDb):
    def list_schema_objects(self):
        raise RuntimeError("refresh failed")


class InterruptibleDb:
    def __init__(self):
        self.cancel_calls = 0

    def cancel_current_operation(self) -> bool:
        self.cancel_calls += 1
        return True

    def close(self) -> None:
        pass


class FakeExplainDb:
    def __init__(self):
        self.statements: list[str] = []
        self.bind_values: list[dict[str, object]] = []

    def explain_statement(
        self,
        statement: str,
        title: str = "Explain plan",
        bind_values: dict[str, object] | None = None,
    ) -> ExplainPlanResult:
        self.statements.append(statement)
        self.bind_values.append(dict(bind_values or {}))
        return sample_plan_result(title)


class FailingExplainDb:
    def explain_statement(self, statement: str, title: str = "Explain plan") -> ExplainPlanResult:
        raise RuntimeError("ORA-06550: line 1, column 8:\nORA-00942: table or view does not exist")


class PagingDb:
    def __init__(self):
        self.autocommit = True
        self.calls: list[tuple[QueryResultContinuation, int]] = []

    def fetch_more_rows(
        self,
        continuation: QueryResultContinuation,
        loaded_rows: int,
    ) -> QueryResultPage:
        self.calls.append((continuation, loaded_rows))
        return QueryResultPage([["3"], ["4"]], "4 row(s) in 0.02s", [[3], [4]])


class FailingPagingDb:
    def __init__(self):
        self.autocommit = True

    def fetch_more_rows(
        self,
        continuation: QueryResultContinuation,
        loaded_rows: int,
    ) -> QueryResultPage:
        raise RuntimeError("fetch failed")


class DiagnosticScriptDb:
    def execute_statement(self, statement: str, title: str = "Statement") -> QueryResult:
        if title.startswith("Statement 2"):
            raise OracleExecutionError(
                RuntimeError("ORA-20000: boom"),
                title,
                [
                    "Error raised in: <anonymous> at line 2 - ORA-20000: boom",
                    "ORA-06512: at line 2",
                ],
            )
        return QueryResult(title, [], [], "1 row")


class FakeEditingDb:
    def __init__(self, autocommit: bool = True):
        self.autocommit = autocommit
        self.updates: list[tuple[str, int, object, str]] = []
        self.inserts: list[tuple[dict[int, str], int]] = []

    def update_cell_by_rowid(
        self,
        context,
        rowid: str,
        column_index: int,
        original_value: object,
        value_text: str,
    ) -> CellUpdateResult:
        self.updates.append((rowid, column_index, original_value, value_text))
        return CellUpdateResult("refreshed", "refreshed")

    def insert_row_for_result(
        self,
        context,
        values_by_column_index: dict[int, str],
        result_column_count: int,
    ) -> RowInsertResult:
        self.inserts.append((dict(values_by_column_index), result_column_count))
        return RowInsertResult(["AAANEW", "inserted"], ["AAANEW", "inserted"])


class FailingEditingDb(FakeEditingDb):
    def update_cell_by_rowid(
        self,
        context,
        rowid: str,
        column_index: int,
        original_value: object,
        value_text: str,
    ) -> CellUpdateResult:
        raise RuntimeError("update failed")


class FailingInsertDb(FakeEditingDb):
    def insert_row_for_result(self, context, values_by_column_index: dict[int, str], result_column_count: int) -> list[str]:
        raise RuntimeError("insert failed")


def editable_result() -> QueryResult:
    return QueryResult(
        "data",
        ["ROWID", "NAME"],
        [["AAABBBCCC", "old"]],
        "1 row",
        editable_context=EditableResultContext("DECISIONS", 0, {1: "NAME"}),
    )


def continuing_result(rows: list[list[str]]) -> QueryResult:
    return QueryResult(
        "data",
        ["A"],
        rows,
        f"{len(rows)} row(s) (limited to {len(rows)} rows) in 0.01s",
        continuation=QueryResultContinuation("continuation-token"),
        original_rows=[list(row) for row in rows],
    )


def sample_plan_result(title: str = "Current statement") -> ExplainPlanResult:
    steps = sample_plan_steps()
    return ExplainPlanResult(title, steps, f"Explain plan: {len(steps)} step(s) in 0.01s")


def sample_plan_steps() -> list[ExplainPlanStep]:
    return [
        ExplainPlanStep(0, None, 0, "SELECT STATEMENT", "", "", "", "", "", "", "3", ""),
        ExplainPlanStep(1, 0, 1, "TABLE ACCESS", "FULL", "HR", "DECISIONS", "TABLE", "10", "120", "2", ""),
        ExplainPlanStep(2, 0, 1, "NESTED LOOPS", "", "", "", "", "", "", "", ""),
        ExplainPlanStep(3, 2, 2, "INDEX", "RANGE SCAN", "", "DECISION_PK", "INDEX", "", "", "1", ""),
    ]


class DrawCall:
    def __init__(self, y: int, x: int, text: str, attr: int = 0):
        self.y = y
        self.x = x
        self.text = text
        self.attr = attr


class FakeScreen:
    def __init__(self, height: int = 24, width: int = 120):
        self.height = height
        self.width = width
        self.calls: list[DrawCall] = []
        self.moves: list[tuple[int, int]] = []

    def getmaxyx(self):
        return self.height, self.width

    def addstr(self, y: int, x: int, text: str, attr: int = 0):
        self.calls.append(DrawCall(y, x, text, attr))

    def move(self, y: int, x: int):
        self.moves.append((y, x))

    def erase(self):
        self.calls.clear()

    def refresh(self):
        pass


class FakeWindow(FakeScreen):
    def keypad(self, enabled: bool):
        self.keypad_enabled = enabled

    def box(self):
        self.calls.append(DrawCall(0, 0, "BOX", 0))
