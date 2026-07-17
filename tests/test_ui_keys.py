import configparser
import curses
from datetime import datetime
from pathlib import Path

import pytest

import plsqlwks.ui as ui
from plsqlwks.config import AppConfig
from plsqlwks.db import (
    EditableResultContext,
    ExplainPlanResult,
    ExplainPlanStep,
    OracleCompilationError,
    OracleExecutionError,
    PlsqlCompileDiagnostic,
    PlsqlObject,
    QueryResult,
    QueryResultContinuation,
    TransactionReport,
    assemble_package_definition,
    ensure_sql_terminator,
    normalize_identifier,
    terminate_plsql_ddl,
)
from plsqlwks.sqlsplit import split_script
from plsqlwks.ui import (
    COMMAND_MENU_ITEMS,
    CTRL_B,
    CTRL_C,
    CTRL_F,
    CTRL_G,
    CTRL_L,
    CTRL_N,
    CTRL_P,
    CTRL_R,
    CTRL_U,
    CTRL_X,
    CTRL_Y,
    CTRL_Z,
    ESC,
    EXTENDED_KEYBOARD_ENABLE,
    EXTENDED_KEYBOARD_RESET,
    FOCUS_BROWSER,
    FOCUS_EDITOR,
    FOCUS_RESULTS,
    KEY_ALT_G,
    KEY_ALT_O,
    KEY_ALT_PLUS,
    KEY_ALT_R,
    KEY_ALT_X,
    KEY_CTRL_ALT_C,
    KEY_CTRL_ALT_R,
    KEY_CTRL_BACKSPACE,
    KEY_CTRL_DELETE,
    KEY_CTRL_DOWN,
    KEY_CTRL_END,
    KEY_CTRL_ENTER,
    KEY_CTRL_EQUALS,
    KEY_CTRL_HOME,
    KEY_CTRL_LEFT,
    KEY_CTRL_PAGEDOWN,
    KEY_CTRL_PAGEUP,
    KEY_CTRL_RIGHT,
    KEY_CTRL_SHIFT_END,
    KEY_CTRL_SHIFT_HOME,
    KEY_CTRL_SHIFT_LEFT,
    KEY_CTRL_SHIFT_RIGHT,
    KEY_CTRL_UP,
    KEY_SHIFT_END,
    KEY_SHIFT_HOME,
    KEY_SHIFT_LEFT,
    KEY_SHIFT_PAGEDOWN,
    KEY_SHIFT_PAGEUP,
    KEY_SHIFT_RIGHT,
    KEY_SHIFT_TAB,
    RESULT_ROW_DETAIL,
    SYNTAX_BIND,
    SYNTAX_COMMENT,
    SYNTAX_DEFAULT,
    SYNTAX_KEYWORD,
    SYNTAX_NUMBER,
    SYNTAX_OPERATOR,
    SYNTAX_STRING,
    UNDO_HISTORY_LIMIT,
    App,
    BrowserEntry,
    Buffer,
    ClipboardProvider,
    CommandMenuItem,
    CompletionCandidate,
    CompletionContext,
    ErrorLocation,
    FileTab,
    ResultCell,
    SearchMatch,
    SyntaxSegment,
    SyntaxToken,
    UIState,
    alt_digit_from_key,
    alt_digit_key,
    browser_entry_text,
    browser_panel_width,
    cell_view_lines,
    clamp_browser_row,
    clamp_cell_view_scroll,
    clamp_result_position,
    clamp_tab_index,
    clip_text,
    command_menu_label,
    completion_context_for_buffer,
    copy_to_system_clipboard,
    curses_function_key,
    decode_key_sequence,
    dedupe_completion_candidates,
    disable_extended_keyboard_reporting,
    display_width,
    editor_line_segments,
    enable_extended_keyboard_reporting,
    execution_error_diagnostics,
    execution_error_lines,
    file_source_key,
    filtered_command_indexes,
    filtered_picker_indexes,
    find_matching_bracket_positions,
    find_search_matches,
    first_document_error_location,
    fit_text,
    flatten_browser_entries,
    format_tab_label,
    format_table,
    generated_insert_sql,
    generated_select_sql,
    generated_sql_table_from_statement,
    generated_update_sql,
    move_buffer_to_error,
    normalize_clipboard_text,
    normalize_curses_keyname,
    normalize_key,
    object_completion_candidates,
    parse_error_locations,
    paste_from_clipboard,
    resolve_completion_qualifier,
    row_detail_lines,
    schema_object_title,
    search_match_index,
    search_status,
    selected_editable_cell,
    selected_result_cell,
    statement_table_references,
    syntax_line_segments,
    tab_display_title,
    table_column_widths,
    template_source_key,
    tokenize_sql_line,
    tokenize_sql_lines,
    transaction_mode_name,
    transaction_pending_indicator,
    transaction_report_status,
    transaction_rows_changed_text,
    transform_sql_code_in_selection,
    visible_tab_labels,
    visible_table_columns,
    wrap_display_text,
)
from plsqlwks.ui.menu import (
    first_tree_menu_row_index,
    tree_menu_row_label,
    tree_menu_rows,
)
from plsqlwks.ui.state import (
    BrowserState,
    DatabaseActivityState,
    begin_database_operation,
    finish_database_operation,
    replace_browser_filter,
    request_database_operation_cancel,
    update_database_operation_progress,
)
from plsqlwks.ui.viewport import reveal_selection
from tests.ui_harness import ServiceHarness


class FakeTerminalStream:
    def __init__(self):
        self.writes: list[bytes] = []
        self.flushes = 0

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        self.flushes += 1


class BrokenTerminalStream:
    def write(self, data: bytes) -> None:
        raise OSError("closed")

    def flush(self) -> None:
        raise OSError("closed")


class FakeOracleOffsetInfo:
    def __init__(self, offset: int, message: str = "ORA-00933: SQL command not properly ended"):
        self.offset = offset
        self.message = message

    def __str__(self) -> str:
        return self.message


class FakeOracleOffsetError(Exception):
    pass


class CompletionDb:
    def __init__(
        self,
        objects: dict[str, list[str]] | None = None,
        columns: dict[str, list[str]] | None = None,
    ):
        self.objects = objects or {"TABLE": [], "VIEW": [], "PROCEDURE": [], "FUNCTION": [], "PACKAGE": []}
        self.columns = columns or {}
        self.object_calls = 0
        self.column_calls: list[str] = []

    def list_schema_objects(self) -> dict[str, list[str]]:
        self.object_calls += 1
        return self.objects

    def list_object_columns(self, object_name: str) -> list[str]:
        normalized = normalize_identifier(object_name) or object_name
        self.column_calls.append(normalized)
        return self.columns.get(normalized, [])


def test_ui_facade_reexports_split_module_symbols():
    import argparse
    import queue
    import threading
    import time

    from plsqlwks.ui import (
        app as ui_app,
    )
    from plsqlwks.ui import (
        browser as ui_browser,
    )
    from plsqlwks.ui import (
        buffer as ui_buffer,
    )
    from plsqlwks.ui import (
        clipboard as ui_clipboard,
    )
    from plsqlwks.ui import (
        completion as ui_completion,
    )
    from plsqlwks.ui import (
        display as ui_display,
    )
    from plsqlwks.ui import (
        errors as ui_errors,
    )
    from plsqlwks.ui import (
        help as ui_help,
    )
    from plsqlwks.ui import (
        keys as ui_keys,
    )
    from plsqlwks.ui import (
        results as ui_results,
    )
    from plsqlwks.ui import (
        sql as ui_sql,
    )
    from plsqlwks.ui import (
        state as ui_state,
    )
    from plsqlwks.ui import (
        syntax as ui_syntax,
    )

    assert ui.App is ui_app.App
    assert ui.main is ui_app.main
    assert ui.parse_args is ui_app.parse_args
    assert ui.Buffer is ui_buffer.Buffer
    assert ui.UIState is ui_state.UIState
    assert ui.BrowserEntry is ui_browser.BrowserEntry
    assert ui.ClipboardProvider is ui_clipboard.ClipboardProvider
    assert ui.CompletionCandidate is ui_completion.CompletionCandidate
    assert ui.SyntaxToken is ui_syntax.SyntaxToken
    assert ui.ResultCell is ui_results.ResultCell
    assert ui.HelpLine is ui_help.HelpLine
    assert ui.decode_key_sequence is ui_keys.decode_key_sequence
    assert ui.display_width is ui_display.display_width
    assert ui.parse_error_locations is ui_errors.parse_error_locations
    assert ui.generated_select_sql is ui_sql.generated_select_sql
    assert ui.argparse is argparse
    assert ui.queue is queue
    assert ui.threading is threading
    assert ui.time is time


def test_ui_facade_export_contract_matches_pre_package_surface():
    expected_exports = set(
        (Path(__file__).parent / "fixtures" / "ui_exports.txt").read_text(encoding="utf-8").splitlines()
    )
    expected_exports.update(
        {
            "DatabaseWorker",
            "DbCommandHandle",
            "DbSessionState",
            "DbWorkerEvent",
            "DbWorkerFinished",
            "DbWorkerProgress",
        }
    )

    assert len(expected_exports) == 459
    assert len(ui.__all__) == len(expected_exports)
    assert set(ui.__all__) == expected_exports
    assert all(hasattr(ui, name) for name in expected_exports)


def test_ui_package_contains_composed_service_modules():
    import importlib.util

    implementation_modules = (
        "app",
        "application_controller",
        "browser",
        "buffer",
        "catalog",
        "clipboard",
        "command_dispatcher",
        "commands",
        "completion",
        "constants",
        "db_operations",
        "db_session",
        "db_worker",
        "dialogs",
        "display",
        "documents",
        "editor_controller",
        "errors",
        "help",
        "input_controller",
        "key_reader",
        "keys",
        "menu",
        "plugin_host",
        "ports",
        "query_controller",
        "renderer",
        "result_controller",
        "result_presenter",
        "results",
        "sql",
        "state",
        "syntax",
        "viewport",
    )

    for module_name in implementation_modules:
        assert importlib.util.find_spec(f"plsqlwks.ui.{module_name}") is not None
        assert importlib.util.find_spec(f"plsqlwks.ui_{module_name}") is None

    legacy_mixin_modules = (
        "app_db",
        "app_editor",
        "app_files",
        "app_input",
        "app_render",
        "app_results",
        "app_tabs_browser",
    )
    for module_name in legacy_mixin_modules:
        assert importlib.util.find_spec(f"plsqlwks.ui.{module_name}") is None


def test_app_is_plain_composition_root_and_services_own_ui_behavior():
    from plsqlwks.ui.catalog import BrowserController
    from plsqlwks.ui.documents import DocumentController
    from plsqlwks.ui.editor_controller import CompletionController
    from plsqlwks.ui.input_controller import InputController
    from plsqlwks.ui.query_controller import QueryController
    from plsqlwks.ui.renderer import Renderer
    from plsqlwks.ui.result_controller import ResultController

    assert App.__bases__ == (object,)
    assert set(App.__dict__) >= {
        "__init__",
        "_built_in_actions",
        "_create_plugin_context",
        "run",
    }
    assert not {
        "draw",
        "handle_key",
        "run_current_statement",
        "toggle_browser",
        "handle_results_key",
        "autocomplete_editor",
        "open_file",
    }.intersection(App.__dict__)

    expected_method_owners = {
        Renderer.draw: "plsqlwks.ui.renderer",
        InputController.handle_key: "plsqlwks.ui.input_controller",
        QueryController.run_current_statement: "plsqlwks.ui.query_controller",
        BrowserController.toggle_browser: "plsqlwks.ui.catalog",
        ResultController.handle_results_key: "plsqlwks.ui.result_controller",
        CompletionController.autocomplete_editor: "plsqlwks.ui.editor_controller",
        DocumentController.open_file: "plsqlwks.ui.documents",
    }
    for method, module_name in expected_method_owners.items():
        assert method.__module__ == module_name


def test_extended_keyboard_reporting_helpers_emit_sequences():
    stream = FakeTerminalStream()

    assert enable_extended_keyboard_reporting(stream) is True
    assert disable_extended_keyboard_reporting(stream) is True

    assert stream.writes == [EXTENDED_KEYBOARD_ENABLE, EXTENDED_KEYBOARD_RESET]
    assert stream.flushes == 2


def test_extended_keyboard_reporting_write_failures_are_swallowed():
    assert enable_extended_keyboard_reporting(BrokenTerminalStream()) is False
    assert disable_extended_keyboard_reporting(BrokenTerminalStream()) is False


def test_decodes_csi_u_ctrl_enter():
    assert decode_key_sequence([ord(ch) for ch in "\x1b[13;5u"]) == KEY_CTRL_ENTER


def test_decodes_xterm_modify_other_keys_ctrl_enter():
    assert decode_key_sequence([ord(ch) for ch in "\x1b[27;5;13~"]) == KEY_CTRL_ENTER


def test_decodes_ctrl_enter_ctrl_m_j_extended_variants():
    assert decode_key_sequence([ord(ch) for ch in "\x1b[10;5u"]) == KEY_CTRL_ENTER
    assert decode_key_sequence([ord(ch) for ch in "\x1b[77;5u"]) == KEY_CTRL_ENTER
    assert decode_key_sequence([ord(ch) for ch in "\x1b[109;5u"]) == KEY_CTRL_ENTER
    assert decode_key_sequence([ord(ch) for ch in "\x1b[74;5u"]) == KEY_CTRL_ENTER
    assert decode_key_sequence([ord(ch) for ch in "\x1b[106;5u"]) == KEY_CTRL_ENTER
    assert decode_key_sequence([ord(ch) for ch in "\x1b[27;5;10~"]) == KEY_CTRL_ENTER
    assert decode_key_sequence([ord(ch) for ch in "\x1b[27;5;77~"]) == KEY_CTRL_ENTER
    assert decode_key_sequence([ord(ch) for ch in "\x1b[27;5;109~"]) == KEY_CTRL_ENTER
    assert decode_key_sequence([ord(ch) for ch in "\x1b[27;5;74~"]) == KEY_CTRL_ENTER
    assert decode_key_sequence([ord(ch) for ch in "\x1b[27;5;106~"]) == KEY_CTRL_ENTER
    assert decode_key_sequence([ord(ch) for ch in "\x1b[13;5~"]) == KEY_CTRL_ENTER
    assert decode_key_sequence([ord(ch) for ch in "\x1b[10;5~"]) == KEY_CTRL_ENTER


def test_decodes_ctrl_equals_extended_variants():
    assert decode_key_sequence(list("\x1b[61;5u")) == KEY_CTRL_EQUALS
    assert decode_key_sequence(list("\x1b[27;5;61~")) == KEY_CTRL_EQUALS
    assert decode_key_sequence([ord("=")]) is None


def test_decodes_ctrl_g_extended_variants():
    assert decode_key_sequence(list("\x1b[103;5u")) == CTRL_G
    assert decode_key_sequence(list("\x1b[27;5;103~")) == CTRL_G
    assert normalize_key("\x07") == CTRL_G


def test_decodes_alt_x_execute_shortcut():
    assert decode_key_sequence([ord(ch) for ch in "\x1bx"]) == KEY_ALT_X
    assert decode_key_sequence([ord(ch) for ch in "\x1bX"]) == KEY_ALT_X


def test_decodes_alt_g_generate_sql_shortcut():
    assert decode_key_sequence([ord(ch) for ch in "\x1bg"]) == KEY_ALT_G
    assert decode_key_sequence([ord(ch) for ch in "\x1bG"]) == KEY_ALT_G


def test_decodes_alt_plus_refresh_autocomplete_shortcut():
    assert decode_key_sequence(list("\x1b+")) == KEY_ALT_PLUS
    assert decode_key_sequence(list("\x1b=")) == KEY_ALT_PLUS
    assert decode_key_sequence(list("\x1b[43;3u")) == KEY_ALT_PLUS
    assert decode_key_sequence(list("\x1b[27;3;43~")) == KEY_ALT_PLUS


def test_unknown_alt_sequence_is_not_execute_shortcut():
    assert decode_key_sequence([ord(ch) for ch in "\x1by"]) is None
    assert decode_key_sequence([ord(ch) for ch in "\x1bf"]) is None


def test_raw_lf_is_ctrl_enter_but_raw_cr_is_plain_enter():
    assert decode_key_sequence([10]) == KEY_CTRL_ENTER
    assert decode_key_sequence([13]) is None


def test_decodes_common_raw_function_key_sequences():
    assert decode_key_sequence(list("\x1bOP")) == curses_function_key(1)
    assert decode_key_sequence(list("\x1bOQ")) == curses_function_key(2)
    assert decode_key_sequence(list("\x1bOR")) == curses_function_key(3)
    assert decode_key_sequence(list("\x1bOS")) == curses_function_key(4)
    assert decode_key_sequence(list("\x1b[13~")) == curses.KEY_F3
    assert decode_key_sequence(list("\x1b[15~")) == curses_function_key(5)
    assert decode_key_sequence(list("\x1b[21~")) == curses_function_key(10)
    assert decode_key_sequence(list("\x1b[23~")) == curses_function_key(11)
    assert decode_key_sequence(list("\x1b[24~")) == curses_function_key(12)
    assert decode_key_sequence(list("\x1b[[C")) == curses.KEY_F3
    assert decode_key_sequence(list("\x1b[2~")) == curses.KEY_IC


def test_normalizes_function_key_keynames():
    assert normalize_curses_keyname("KEY_F3") == curses.KEY_F3
    assert normalize_curses_keyname("KEY_F(3)") == curses.KEY_F3
    assert normalize_curses_keyname("kf3") == curses.KEY_F3


def test_alt_x_routes_to_current_statement_execution():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    calls: list[str] = []
    app.run_current_statement = lambda: calls.append("run")

    app.handle_key(KEY_ALT_X)

    assert calls == ["run"]


def test_decodes_alt_o_commands_menu_shortcut():
    assert decode_key_sequence(list("\x1bo")) == KEY_ALT_O
    assert decode_key_sequence(list("\x1b[111;3u")) == KEY_ALT_O
    assert decode_key_sequence(list("\x1b[27;3;111~")) == KEY_ALT_O


def test_alt_o_opens_top_left_command_menu_and_executes_selection(monkeypatch):
    app = ServiceHarness()
    app.screen = FakeScreen(height=12, width=80)
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    windows: list[FakePickerWindow] = []

    def fake_newwin(height: int, width: int, top: int, left: int):
        window = FakePickerWindow(height, width, top, left)
        windows.append(window)
        return window

    monkeypatch.setattr(curses, "newwin", fake_newwin)
    keys = iter(["h", "e", 10])
    app.read_key = lambda window=None, idle_timeout=200: next(keys)
    calls: list[str] = []
    app.show_help = lambda: calls.append("help")

    app.handle_key(KEY_ALT_O)

    assert calls == ["help"]
    assert windows
    assert windows[0].top == 1
    assert windows[0].left == 0
    assert any("[-] Application" in call.text for window in windows for call in window.calls)
    assert any("Filter: he" in call.text for window in windows for call in window.calls)
    assert any("Show help" in call.text for window in windows for call in window.calls)


def test_command_menu_filters_by_terms_and_formats_labels():
    commands = (
        CommandMenuItem("File", "Open file", "Ctrl-O", "open_file", "workspace"),
        CommandMenuItem("Editor", "Go to line", "Ctrl-G", "prompt_go_to_line", "jump"),
    )

    assert filtered_command_indexes(commands, "file work") == [0]
    assert filtered_command_indexes(commands, "jump") == [1]
    assert command_menu_label(commands[0], 6, 6) == "File    Open file  Ctrl-O"


def test_command_menu_tree_groups_sections_and_collapses():
    commands = (
        CommandMenuItem("File", "Open file", "Ctrl-O", "open_file", "workspace"),
        CommandMenuItem("File", "Save buffer", "Ctrl-S", "save_buffer", "write"),
        CommandMenuItem("Editor", "Go to line", "Ctrl-G", "prompt_go_to_line", "jump"),
    )

    rows = tree_menu_rows(commands, "", {"File"})

    assert [(row.kind, row.section, row.item_index) for row in rows] == [
        ("section", "File", None),
        ("item", "File", 0),
        ("item", "File", 1),
        ("section", "Editor", None),
    ]
    assert tree_menu_row_label(rows[0], commands, 6) == "[-] File (2)"
    assert tree_menu_row_label(rows[1], commands, 6) == "    Open file  Ctrl-O"
    assert tree_menu_row_label(rows[3], commands, 6) == "[+] Editor (1)"


def test_command_menu_tree_filter_expands_matches_and_selects_first_command():
    commands = (
        CommandMenuItem("File", "Open file", "Ctrl-O", "open_file", "workspace"),
        CommandMenuItem("Editor", "Go to line", "Ctrl-G", "prompt_go_to_line", "jump"),
    )

    rows = tree_menu_rows(commands, "jump", set())

    assert [(row.kind, row.section, row.item_index) for row in rows] == [
        ("section", "Editor", None),
        ("item", "Editor", 1),
    ]
    assert tree_menu_row_label(rows[0], commands, 6) == "[-] Editor (1)"
    assert first_tree_menu_row_index(rows) == 1


def test_command_menu_can_collapse_section_with_enter(monkeypatch):
    app = ServiceHarness()
    app.screen = FakeScreen(height=12, width=80)
    app.state = UIState(config=make_config(), db=object())
    windows: list[FakePickerWindow] = []

    def fake_newwin(height: int, width: int, top: int, left: int):
        window = FakePickerWindow(height, width, top, left)
        windows.append(window)
        return window

    monkeypatch.setattr(curses, "newwin", fake_newwin)
    keys = iter([curses.KEY_UP, 10, ESC])
    app.read_key = lambda window=None, idle_timeout=200: next(keys)

    assert app.pick_command_menu(COMMAND_MENU_ITEMS) is None
    assert any("[+] Application" in call.text for window in windows for call in window.calls)


def test_alt_r_routes_to_buffer_rename():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    calls: list[str] = []
    app.rename_current_buffer = lambda: calls.append("rename")

    app.handle_key(KEY_ALT_R)

    assert calls == ["rename"]


def test_f1_routes_to_styled_help_and_clears_result_views():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.active_result = QueryResult("data", ["A"], [["1"]], "1 row")
    app.state.dbms_output = ["line"]
    app.state.show_dbms_output = True
    app.state.focus = FOCUS_RESULTS

    app.handle_key(curses.KEY_F1)

    assert app.state.active_result is None
    assert app.state.explain_result is None
    assert app.state.dbms_output == []
    assert app.state.show_dbms_output is False
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.results_style == ui.RESULT_STYLE_HELP
    assert app.state.results == ui.HELP
    assert app.state.status == "Help"


def test_f7_cycles_grid_fullscreen_editor_fullscreen_and_split_layout():
    app = ServiceHarness()
    app.screen = FakeScreen(height=24, width=120)
    app.state = UIState(config=make_config(), db=object())
    app.state.active_result = QueryResult("data", ["A"], [["1"]], "1 row")
    app.state.focus = FOCUS_EDITOR

    app.handle_key(curses.KEY_F7)
    assert app.state.result_grid_fullscreen is True
    assert app.state.result_ratio == ui.RESULT_RATIO_FULLSCREEN
    assert app.state.result_mode == ui.RESULT_GRID
    assert app.state.status == "Data grid fullscreen"
    assert app.state.focus == FOCUS_RESULTS
    assert app.current_pane_sizes()[:2] == (0, 24)

    app.handle_key(curses.KEY_F7)
    assert app.state.result_grid_fullscreen is False
    assert app.state.result_ratio == ui.RESULT_RATIO_EDITOR_FULLSCREEN
    assert app.state.status == "Results pane: editor fullscreen"
    assert app.state.focus == FOCUS_EDITOR
    assert app.current_pane_sizes()[:2] == (24, 0)

    app.handle_key(curses.KEY_F7)
    assert app.state.result_grid_fullscreen is False
    assert app.state.result_ratio == ui.RESULT_RATIO_GRID_SPLIT
    assert app.state.result_mode == ui.RESULT_GRID
    assert app.state.status == "Results pane: 2/3 editor, 1/3 data grid"
    assert app.current_pane_sizes()[:2] == (14, 7)


def test_f7_moves_from_grid_only_fullscreen_to_editor_fullscreen():
    app = ServiceHarness()
    app.screen = FakeScreen(height=24, width=120)
    app.state = UIState(config=make_config(), db=object())
    app.state.active_result = QueryResult("data", ["A"], [["1"]], "1 row")

    app.handle_key(curses.KEY_F7)
    app.handle_key(curses.KEY_F7)

    assert app.state.result_grid_fullscreen is False
    assert app.state.result_ratio == ui.RESULT_RATIO_EDITOR_FULLSCREEN
    assert app.state.status == "Results pane: editor fullscreen"


def test_result_pane_ratio_helpers_support_editor_and_split_layouts():
    assert ui.result_pane_is_editor_fullscreen(ui.RESULT_RATIO_EDITOR_FULLSCREEN)
    assert ui.editor_result_pane_heights(21, ui.RESULT_RATIO_EDITOR_FULLSCREEN) == (21, 0)
    assert ui.editor_result_pane_heights(21, ui.RESULT_RATIO_GRID_SPLIT) == (14, 7)


def test_fullscreen_results_do_not_scroll_or_edit_hidden_editor():
    app = ServiceHarness()
    app.screen = FakeScreen(height=24, width=120)
    app.state = UIState(config=make_config(), db=object())
    app.state.result_ratio = ui.RESULT_RATIO_FULLSCREEN
    app.state.focus = FOCUS_EDITOR
    app.state.buffer = Buffer(lines=["select"], row=0, col=0, scroll=0, dirty=False)

    app.handle_key(KEY_CTRL_DOWN)

    assert (app.state.buffer.row, app.state.buffer.col, app.state.buffer.scroll) == (0, 0, 0)
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.status == "Results pane: fullscreen"

    app.handle_key("x")

    assert app.state.buffer.text() == "select"


def test_decodes_ctrl_alt_commit_and_rollback_shortcuts():
    assert decode_key_sequence(list("\x1br")) == KEY_ALT_R
    assert decode_key_sequence(list("\x1bR")) == KEY_ALT_R
    assert decode_key_sequence([ESC, CTRL_C]) == KEY_CTRL_ALT_C
    assert decode_key_sequence([ESC, CTRL_R]) == KEY_CTRL_ALT_R
    assert decode_key_sequence(list("\x1b[99;7u")) == KEY_CTRL_ALT_C
    assert decode_key_sequence(list("\x1b[114;7u")) == KEY_CTRL_ALT_R
    assert decode_key_sequence(list("\x1b[27;7;99~")) == KEY_CTRL_ALT_C
    assert decode_key_sequence(list("\x1b[27;7;114~")) == KEY_CTRL_ALT_R


def test_transaction_shortcuts_commit_and_rollback_from_any_focus():
    for focus in (FOCUS_BROWSER, FOCUS_RESULTS, "editor"):
        db = TransactionDb()
        app = ServiceHarness()
        app.state = UIState(config=make_config(), db=db)
        app.state.focus = focus
        app.state.active_result = QueryResult("data", ["A"], [["1"]], "1 row")

        app.handle_key(KEY_CTRL_ALT_C)
        app.wait_for_db_operation(timeout=1)
        app.handle_key(KEY_CTRL_ALT_R)
        app.wait_for_db_operation(timeout=1)

        assert db.calls == ["commit", "rollback"]
        assert app.state.active_result is None
        assert app.state.status == "Rollback transaction, 2026-06-12 10:12:15, unknown row(s) changed"


def test_transaction_report_status_formats_exact_and_unknown_rows():
    timestamp = datetime(2026, 6, 12, 10, 12, 15)

    exact = TransactionReport(timestamp, rows_changed=7)
    unknown_only = TransactionReport(timestamp, rows_changed=0, has_unknown_changes=True)
    mixed = TransactionReport(timestamp, rows_changed=7, has_unknown_changes=True)

    assert transaction_rows_changed_text(exact) == "7 row(s) changed"
    assert transaction_rows_changed_text(unknown_only) == "unknown row(s) changed"
    assert transaction_rows_changed_text(mixed) == "7+ row(s) changed"
    assert transaction_report_status("Committed transaction", exact) == (
        "Committed transaction, 2026-06-12 10:12:15, 7 row(s) changed"
    )


def test_transaction_pending_indicator_uses_db_state():
    db = TransactionDb()
    assert transaction_pending_indicator(db) == "[ ]"

    db.has_uncommitted_changes = True

    assert transaction_pending_indicator(db) == "[*]"


def test_f12_chooses_transaction_mode(tmp_path):
    db = TransactionDb()
    app = ServiceHarness()
    config = make_config(tmp_path)
    app.state = UIState(config=config, db=db)
    answers = iter(["m", "a"])
    app.prompt = lambda label, default="", strip=True: next(answers)

    app.handle_key(curses.KEY_F12)
    app.wait_for_db_operation(timeout=1)
    assert db.autocommit is False
    assert db.modes == [False]
    assert transaction_mode_name(db) == "manual"
    assert app.state.status == "Transaction mode: manual"
    parser = configparser.ConfigParser()
    assert config.config_file is not None
    parser.read(config.config_file, encoding="utf-8")
    assert parser.get("database", "autocommit") == "no"

    app.handle_key(curses.KEY_F12)
    app.wait_for_db_operation(timeout=1)
    assert db.autocommit is True
    assert db.modes == [False, True]
    assert transaction_mode_name(db) == "autocommit"
    assert app.state.status == "Transaction mode: autocommit"
    parser = configparser.ConfigParser()
    parser.read(config.config_file, encoding="utf-8")
    assert parser.get("database", "autocommit") == "yes"


def test_transaction_mode_persistence_failure_keeps_live_mode_and_ui_responsive(
    monkeypatch,
    tmp_path,
):
    db = TransactionDb()
    app = ServiceHarness()
    app.state = UIState(config=make_config(tmp_path), db=db)
    app.prompt = lambda label, default="", strip=True: "m"
    monkeypatch.setattr(
        "plsqlwks.ui.db_session.save_autocommit",
        lambda config, enabled: (_ for _ in ()).throw(OSError("disk full")),
    )

    app.choose_transaction_mode()
    app.wait_for_db_operation(timeout=1)

    assert db.autocommit is False
    assert app.state.db_operation is None
    assert app.state.status.startswith("Transaction mode: manual")
    assert "preference was not saved: disk full" in app.state.status


@pytest.mark.parametrize(
    "interruption",
    [KeyboardInterrupt("interrupted"), SystemExit(23)],
    ids=["keyboard-interrupt", "system-exit"],
)
def test_transaction_mode_persistence_interruption_reports_live_mode_then_propagates(
    monkeypatch,
    tmp_path,
    interruption,
):
    db = TransactionDb()
    app = ServiceHarness()
    app.state = UIState(config=make_config(tmp_path), db=db)
    app.prompt = lambda label, default="", strip=True: "m"

    def interrupt_persistence(config, enabled):
        raise interruption

    monkeypatch.setattr(
        "plsqlwks.ui.db_session.save_autocommit",
        interrupt_persistence,
    )

    app.choose_transaction_mode()
    with pytest.raises(type(interruption)) as raised:
        app.wait_for_db_operation(timeout=1)

    assert raised.value is interruption
    assert db.autocommit is False
    assert app.state.db_operation is None
    assert app.state.status.startswith("Transaction mode: manual")
    assert "live mode changed, preference save was interrupted" in app.state.status
    assert type(interruption).__name__ in app.state.status or str(interruption) in app.state.status


def test_manual_to_autocommit_commits_pending_transaction_before_switch(tmp_path):
    db = TransactionDb()
    db.autocommit = False
    db.has_uncommitted_changes = True
    app = ServiceHarness()
    config = make_config(tmp_path, autocommit=False)
    app.state = UIState(config=config, db=db)
    answers = iter(["a", "c"])
    app.prompt = lambda label, default="", strip=True: next(answers)

    app.choose_transaction_mode()
    app.wait_for_db_operation(timeout=1)

    assert db.calls == ["commit"]
    assert db.modes == [True]
    assert db.autocommit is True
    assert db.has_uncommitted_changes is False
    assert app.state.status == "Transaction mode: autocommit"
    parser = configparser.ConfigParser()
    parser.read(config.config_file, encoding="utf-8")
    assert parser.get("database", "autocommit") == "yes"


def test_manual_to_autocommit_rollback_invalidates_stale_grid_before_switch(tmp_path):
    db = TransactionDb()
    db.autocommit = False
    db.has_uncommitted_changes = True
    app = ServiceHarness()
    config = make_config(tmp_path, autocommit=False)
    app.state = UIState(config=config, db=db)
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = QueryResult(
        "data",
        ["ROWID", "NAME"],
        [["AAABBBCCC", "uncommitted"]],
        "1 row",
        editable_context=EditableResultContext("DECISIONS", 0, {1: "NAME"}),
        original_rows=[["AAABBBCCC", "uncommitted"]],
    )
    app.state.last_result = app.state.active_result
    answers = iter(["a", "r"])
    app.prompt = lambda label, default="", strip=True: next(answers)

    app.choose_transaction_mode()
    app.wait_for_db_operation(timeout=1)

    assert db.calls == ["rollback"]
    assert db.modes == [True]
    assert db.autocommit is True
    assert app.state.active_result is None
    assert app.state.last_result is None
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.status == "Transaction mode: autocommit"


def test_manual_to_autocommit_cancel_keeps_pending_transaction_and_mode(tmp_path):
    db = TransactionDb()
    db.autocommit = False
    db.has_uncommitted_changes = True
    app = ServiceHarness()
    config = make_config(tmp_path, autocommit=False)
    app.state = UIState(config=config, db=db)
    prompts: list[tuple[str, str]] = []
    answers = iter(["a", "x"])

    def prompt(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return next(answers)

    app.prompt = prompt

    app.choose_transaction_mode()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [
        ("Transaction mode (a=autocommit, m=manual)", "m"),
        ("Pending transaction: c=commit, r=rollback, x=cancel", ""),
    ]
    assert db.calls == []
    assert db.modes == []
    assert db.autocommit is False
    assert db.has_uncommitted_changes is True
    assert app.state.status == "Transaction mode unchanged"
    assert config.config_file is not None
    assert config.config_file.exists() is False


def test_manual_to_autocommit_resolution_failure_keeps_mode(tmp_path):
    db = FailingTransactionDb("rollback")
    db.autocommit = False
    db.has_uncommitted_changes = True
    app = ServiceHarness()
    app.screen = FakeScreen()
    config = make_config(tmp_path, autocommit=False)
    app.state = UIState(config=config, db=db)
    answers = iter(["a", "r"])
    app.prompt = lambda label, default="", strip=True: next(answers)

    app.choose_transaction_mode()
    app.wait_for_db_operation(timeout=1)

    assert db.calls == ["rollback"]
    assert db.modes == []
    assert db.autocommit is False
    assert db.has_uncommitted_changes is True
    assert app.state.status == "Rollback failed"
    assert any(line.startswith("ERROR rolling back transaction:") for line in app.state.results)
    assert config.config_file is not None
    assert config.config_file.exists() is False


def test_manual_to_autocommit_mode_failure_does_not_misreport_successful_commit(tmp_path):
    class ModeFailingTransactionDb(TransactionDb):
        def set_autocommit(self, enabled: bool) -> None:
            raise RuntimeError("autocommit switch failed")

    db = ModeFailingTransactionDb()
    db.autocommit = False
    db.has_uncommitted_changes = True
    app = ServiceHarness()
    app.screen = FakeScreen()
    config = make_config(tmp_path, autocommit=False)
    app.state = UIState(config=config, db=db)
    answers = iter(["a", "c"])
    app.prompt = lambda label, default="", strip=True: next(answers)

    app.choose_transaction_mode()
    app.wait_for_db_operation(timeout=1)

    assert db.calls == ["commit"]
    assert db.has_uncommitted_changes is False
    assert db.autocommit is False
    assert app.state.db.autocommit is False
    assert app.state.db.has_uncommitted_changes is False
    assert app.state.status == "Transaction mode change failed after commit"
    assert app.state.results[0] == "ERROR changing transaction mode:"
    assert config.config_file is not None
    assert config.config_file.exists() is False


def test_plain_cr_enter_in_editor_inserts_newline_not_execute():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["select 1 from dual"], row=0, col=6)
    calls: list[str] = []
    app.run_current_statement = lambda: calls.append("run")

    app.handle_key(13)

    assert calls == []
    assert app.state.buffer.lines == ["select", " 1 from dual"]


def test_ctrl_enter_lf_in_editor_executes_statement_not_newline():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["select 1 from dual"], row=0, col=6)
    calls: list[str] = []
    app.run_current_statement = lambda: calls.append("run")

    app.handle_key(10)

    assert calls == ["run"]
    assert app.state.buffer.lines == ["select 1 from dual"]


def test_run_current_statement_shortcuts_use_cursor_column_for_same_line_sql():
    for key in (curses.KEY_F5, KEY_CTRL_ENTER, KEY_ALT_X):
        app = ServiceHarness()
        db = RecordingDb()
        app.screen = FakeScreen()
        app.draw_offset_x = 0
        app.state = UIState(config=make_config(), db=db)
        app.state.buffer = Buffer(lines=["select 1 from dual; select 2 from dual;"], row=0, col=25)

        app.handle_key(key)
        app.wait_for_db_operation(timeout=1)

        assert db.statements == ["select 2 from dual"]
        assert app.state.status == "ok"


def test_run_current_statement_executes_commented_declare_block_as_one_statement():
    app = ServiceHarness()
    db = RecordingDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=db)
    lines = [
        "-- setup before declaration",
        "declare",
        "  x number;",
        "begin",
        "  null;",
        "end;",
        "/",
    ]
    app.state.buffer = Buffer(lines=lines, row=1, col=1)

    app.run_current_statement()
    app.wait_for_db_operation(timeout=1)

    assert db.statements == ["\n".join(lines[:6])]
    assert db.titles == ["Current statement"]
    assert app.state.status == "ok"


def test_failed_sql_offset_moves_cursor_to_same_line_statement_error():
    app = ServiceHarness()
    db = OffsetFailingDb(offset=len("select * "))
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=db)
    prefix = "select 1 from dual;   "
    bad = "select * frm dual;"
    app.state.buffer = Buffer(lines=[prefix + bad], row=0, col=len(prefix) + 1)

    app.run_current_statement()
    app.wait_for_db_operation(timeout=1)

    expected_col = len(prefix) + len("select * ")
    assert db.statements == ["select * frm dual"]
    assert (app.state.buffer.row, app.state.buffer.col) == (0, expected_col)
    assert app.state.status.startswith(f"Execution failed at line 1, column {expected_col + 1}")
    assert f"Error location: line 1, column {expected_col + 1}" in app.state.results


def test_run_current_statement_executes_selected_sql_only():
    app = ServiceHarness()
    db = RecordingDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=db)
    selected = "select 2 from dual;"
    app.state.buffer = Buffer(
        lines=["select 1 from dual;", selected, "select 3 from dual;"],
        row=1,
        col=len(selected),
        selection_anchor=(1, 0),
    )

    app.handle_key(curses.KEY_F5)
    app.wait_for_db_operation(timeout=1)

    assert db.statements == ["select 2 from dual"]
    assert db.titles == ["Selection lines 2-2"]
    assert app.state.status == "ok"


def test_run_current_statement_prompts_for_bind_values():
    app = ServiceHarness()
    db = RecordingDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=db)
    statement = "select * from decisions where id = :id and name = :name"
    app.state.buffer = Buffer(lines=[statement], row=0, col=0)
    prompts: list[tuple[str, str, bool]] = []
    answers = iter(["42", "Ada"])

    def prompt_text_box(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default, strip))
        return next(answers)

    app.prompt_text_box = prompt_text_box

    app.run_current_statement()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [
        ("Value for :id", "", False),
        ("Value for :name", "", False),
    ]
    assert db.statements == [statement]
    assert db.bind_values == [{"id": "42", "name": "Ada"}]


def test_run_current_statement_prefills_remembered_bind_values_when_enabled():
    app = ServiceHarness()
    db = RecordingDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(remember_bind_values=True), db=db)
    statement = "select * from decisions where id = :id"
    app.state.buffer = Buffer(lines=[statement], row=0, col=0)
    prompts: list[tuple[str, str, bool]] = []
    answers = iter(["42", "84"])

    def prompt_text_box(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default, strip))
        return next(answers)

    app.prompt_text_box = prompt_text_box

    app.run_current_statement()
    app.wait_for_db_operation(timeout=1)
    app.run_current_statement()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [
        ("Value for :id", "", False),
        ("Value for :id", "42", False),
    ]
    assert db.bind_values == [{"id": "42"}, {"id": "84"}]
    assert app.state.remembered_bind_values == {"id": "84"}


def test_remembered_unquoted_bind_value_is_reused_across_case_variants():
    app = ServiceHarness()
    db = RecordingDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(remember_bind_values=True), db=db)
    app.state.buffer = Buffer(lines=["select :id from dual"], row=0, col=0)
    prompts: list[tuple[str, str, bool]] = []
    answers = iter(["42", "84"])

    def prompt_text_box(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default, strip))
        return next(answers)

    app.prompt_text_box = prompt_text_box

    app.run_current_statement()
    app.wait_for_db_operation(timeout=1)
    app.state.buffer = Buffer(lines=["select :ID from dual"], row=0, col=0)
    app.run_current_statement()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [
        ("Value for :id", "", False),
        ("Value for :ID", "42", False),
    ]
    assert db.bind_values == [{"id": "42"}, {"ID": "84"}]
    assert app.state.remembered_bind_values == {"ID": "84"}


def test_run_current_statement_cancelled_bind_prompt_does_not_execute():
    app = ServiceHarness()
    db = RecordingDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=db)
    app.state.buffer = Buffer(lines=["select * from decisions where id = :id"], row=0, col=0)
    app.prompt_text_box = lambda label, default="", strip=True: None

    app.run_current_statement()

    assert db.statements == []
    assert app.state.db_operation is None
    assert app.state.status == "Execution cancelled"


def test_f6_toggles_dbms_output_and_f11_runs_script():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    calls: list[str] = []
    app.toggle_dbms_output_view = lambda: calls.append("toggle")
    app.run_script = lambda: calls.append("script")

    app.handle_key(curses.KEY_F6)
    app.handle_key(curses_function_key(11))

    assert calls == ["toggle", "script"]


def test_decodes_ctrl_home_end():
    assert decode_key_sequence(list("\x1b[1;5H")) == KEY_CTRL_HOME
    assert decode_key_sequence(list("\x1b[1;5F")) == KEY_CTRL_END
    assert decode_key_sequence(list("\x1b[5H")) == KEY_CTRL_HOME
    assert decode_key_sequence(list("\x1b[5F")) == KEY_CTRL_END
    assert decode_key_sequence(list("\x1b[27;5;72~")) == KEY_CTRL_HOME
    assert decode_key_sequence(list("\x1b[27;5;70~")) == KEY_CTRL_END


def test_decodes_shift_home_end_sequences():
    assert decode_key_sequence(list("\x1b[1;2H")) == KEY_SHIFT_HOME
    assert decode_key_sequence(list("\x1b[2H")) == KEY_SHIFT_HOME
    assert decode_key_sequence(list("\x1b[7;2~")) == KEY_SHIFT_HOME
    assert decode_key_sequence(list("\x1b[27;2;72~")) == KEY_SHIFT_HOME
    assert decode_key_sequence(list("\x1b[1;2F")) == KEY_SHIFT_END
    assert decode_key_sequence(list("\x1b[2F")) == KEY_SHIFT_END
    assert decode_key_sequence(list("\x1b[8;2~")) == KEY_SHIFT_END
    assert decode_key_sequence(list("\x1b[27;2;70~")) == KEY_SHIFT_END


def test_decodes_ctrl_shift_home_end_sequences():
    assert decode_key_sequence(list("\x1b[1;6H")) == KEY_CTRL_SHIFT_HOME
    assert decode_key_sequence(list("\x1b[6H")) == KEY_CTRL_SHIFT_HOME
    assert decode_key_sequence(list("\x1b[7;6~")) == KEY_CTRL_SHIFT_HOME
    assert decode_key_sequence(list("\x1b[27;6;72~")) == KEY_CTRL_SHIFT_HOME
    assert decode_key_sequence(list("\x1b[1;6F")) == KEY_CTRL_SHIFT_END
    assert decode_key_sequence(list("\x1b[6F")) == KEY_CTRL_SHIFT_END
    assert decode_key_sequence(list("\x1b[8;6~")) == KEY_CTRL_SHIFT_END
    assert decode_key_sequence(list("\x1b[27;6;70~")) == KEY_CTRL_SHIFT_END


def test_decodes_ctrl_arrow_sequences():
    assert decode_key_sequence(list("\x1b[1;5A")) == KEY_CTRL_UP
    assert decode_key_sequence(list("\x1b[1;5B")) == KEY_CTRL_DOWN
    assert decode_key_sequence(list("\x1b[5A")) == KEY_CTRL_UP
    assert decode_key_sequence(list("\x1b[5B")) == KEY_CTRL_DOWN
    assert decode_key_sequence(list("\x1bOa")) == KEY_CTRL_UP
    assert decode_key_sequence(list("\x1bOb")) == KEY_CTRL_DOWN
    assert decode_key_sequence(list("\x1b[27;5;65~")) == KEY_CTRL_UP
    assert decode_key_sequence(list("\x1b[27;5;66~")) == KEY_CTRL_DOWN
    assert decode_key_sequence(list("\x1b[1;5D")) == KEY_CTRL_LEFT
    assert decode_key_sequence(list("\x1b[1;5C")) == KEY_CTRL_RIGHT
    assert decode_key_sequence(list("\x1b[5D")) == KEY_CTRL_LEFT
    assert decode_key_sequence(list("\x1b[5C")) == KEY_CTRL_RIGHT
    assert decode_key_sequence(list("\x1bOd")) == KEY_CTRL_LEFT
    assert decode_key_sequence(list("\x1bOc")) == KEY_CTRL_RIGHT
    assert decode_key_sequence(list("\x1b[27;5;68~")) == KEY_CTRL_LEFT
    assert decode_key_sequence(list("\x1b[27;5;67~")) == KEY_CTRL_RIGHT


def test_decodes_ctrl_shift_arrow_sequences():
    assert decode_key_sequence(list("\x1b[1;6D")) == KEY_CTRL_SHIFT_LEFT
    assert decode_key_sequence(list("\x1b[1;6C")) == KEY_CTRL_SHIFT_RIGHT
    assert decode_key_sequence(list("\x1b[6D")) == KEY_CTRL_SHIFT_LEFT
    assert decode_key_sequence(list("\x1b[6C")) == KEY_CTRL_SHIFT_RIGHT
    assert decode_key_sequence(list("\x1b[27;6;68~")) == KEY_CTRL_SHIFT_LEFT
    assert decode_key_sequence(list("\x1b[27;6;67~")) == KEY_CTRL_SHIFT_RIGHT


def test_decodes_ctrl_page_and_alt_digit_sequences():
    assert decode_key_sequence(list("\x1b[5;5~")) == KEY_CTRL_PAGEUP
    assert decode_key_sequence(list("\x1b[6;5~")) == KEY_CTRL_PAGEDOWN
    assert decode_key_sequence(list("\x1b[27;5;53~")) == KEY_CTRL_PAGEUP
    assert decode_key_sequence(list("\x1b[27;5;54~")) == KEY_CTRL_PAGEDOWN
    assert decode_key_sequence(list("\x1b1")) == alt_digit_key(1)
    assert decode_key_sequence(list("\x1b9")) == alt_digit_key(9)
    assert alt_digit_from_key(alt_digit_key(4)) == 4
    assert alt_digit_from_key(KEY_ALT_X) is None


def test_decodes_ctrl_delete_sequence():
    assert decode_key_sequence(list("\x1b[3;5~")) == KEY_CTRL_DELETE


def test_decodes_ctrl_backspace_sequences():
    assert decode_key_sequence(list("\x1b[127;5u")) == KEY_CTRL_BACKSPACE
    assert decode_key_sequence(list("\x1b[8;5u")) == KEY_CTRL_BACKSPACE
    assert decode_key_sequence(list("\x1b[27;5;127~")) == KEY_CTRL_BACKSPACE
    assert decode_key_sequence(list("\x1b[27;5;8~")) == KEY_CTRL_BACKSPACE
    assert decode_key_sequence(list("\x1b[127;5~")) == KEY_CTRL_BACKSPACE
    assert decode_key_sequence([127]) is None
    assert decode_key_sequence([8]) is None


def test_decodes_shift_page_sequences():
    assert decode_key_sequence(list("\x1b[5;2~")) == KEY_SHIFT_PAGEUP
    assert decode_key_sequence(list("\x1b[27;2;53~")) == KEY_SHIFT_PAGEUP
    assert decode_key_sequence(list("\x1b[6;2~")) == KEY_SHIFT_PAGEDOWN
    assert decode_key_sequence(list("\x1b[27;2;54~")) == KEY_SHIFT_PAGEDOWN


def test_decodes_shift_tab_sequence():
    assert decode_key_sequence(list("\x1b[Z")) == KEY_SHIFT_TAB


def test_normalizes_terminfo_ctrl_arrow_keynames():
    assert normalize_curses_keyname("kHOM") == KEY_SHIFT_HOME
    assert normalize_curses_keyname("kEND") == KEY_SHIFT_END
    assert normalize_curses_keyname("KEY_SHOME") == KEY_SHIFT_HOME
    assert normalize_curses_keyname("KEY_SEND") == KEY_SHIFT_END
    assert normalize_curses_keyname("kHOM6") == KEY_CTRL_SHIFT_HOME
    assert normalize_curses_keyname("kEND6") == KEY_CTRL_SHIFT_END
    assert normalize_curses_keyname("KEY_CS_HOME") == KEY_CTRL_SHIFT_HOME
    assert normalize_curses_keyname("KEY_CS_END") == KEY_CTRL_SHIFT_END
    assert normalize_curses_keyname("kHOM5") == KEY_CTRL_HOME
    assert normalize_curses_keyname("kEND5") == KEY_CTRL_END
    assert normalize_curses_keyname("KEY_CHOME") == KEY_CTRL_HOME
    assert normalize_curses_keyname("KEY_CEND") == KEY_CTRL_END
    assert normalize_curses_keyname("kUP5") == KEY_CTRL_UP
    assert normalize_curses_keyname("kDN5") == KEY_CTRL_DOWN
    assert normalize_curses_keyname("KEY_CTRL_UP") == KEY_CTRL_UP
    assert normalize_curses_keyname("KEY_CTRL_DOWN") == KEY_CTRL_DOWN
    assert normalize_curses_keyname("kLFT5") == KEY_CTRL_LEFT
    assert normalize_curses_keyname("kRIT5") == KEY_CTRL_RIGHT
    assert normalize_curses_keyname("KEY_CLEFT") == KEY_CTRL_LEFT
    assert normalize_curses_keyname("KEY_CRIGHT") == KEY_CTRL_RIGHT
    assert normalize_curses_keyname("kLFT6") == KEY_CTRL_SHIFT_LEFT
    assert normalize_curses_keyname("kRIT6") == KEY_CTRL_SHIFT_RIGHT
    assert normalize_curses_keyname("KEY_CSLEFT") == KEY_CTRL_SHIFT_LEFT
    assert normalize_curses_keyname("KEY_CSRIGHT") == KEY_CTRL_SHIFT_RIGHT
    assert normalize_curses_keyname("kPRV5") == KEY_CTRL_PAGEUP
    assert normalize_curses_keyname("kNXT5") == KEY_CTRL_PAGEDOWN
    assert normalize_curses_keyname("KEY_CTRL_PAGEUP") == KEY_CTRL_PAGEUP
    assert normalize_curses_keyname("KEY_CTRL_PAGEDOWN") == KEY_CTRL_PAGEDOWN
    assert normalize_curses_keyname("kDC5") == KEY_CTRL_DELETE
    assert normalize_curses_keyname("KEY_CDC") == KEY_CTRL_DELETE
    assert normalize_curses_keyname("KEY_CTRL_DELETE") == KEY_CTRL_DELETE
    assert normalize_curses_keyname("KEY_CONTROL_DELETE") == KEY_CTRL_DELETE
    assert normalize_curses_keyname("kBS5") == KEY_CTRL_BACKSPACE
    assert normalize_curses_keyname("KEY_CBACKSPACE") == KEY_CTRL_BACKSPACE
    assert normalize_curses_keyname("KEY_CTRL_BACKSPACE") == KEY_CTRL_BACKSPACE
    assert normalize_curses_keyname("KEY_CONTROL_BACKSPACE") == KEY_CTRL_BACKSPACE
    assert normalize_curses_keyname("kPRV") == KEY_SHIFT_PAGEUP
    assert normalize_curses_keyname("kNXT") == KEY_SHIFT_PAGEDOWN
    assert normalize_curses_keyname("KEY_SPREVIOUS") == KEY_SHIFT_PAGEUP
    assert normalize_curses_keyname("KEY_SNEXT") == KEY_SHIFT_PAGEDOWN
    assert normalize_curses_keyname("KEY_SHIFT_PPAGE") == KEY_SHIFT_PAGEUP
    assert normalize_curses_keyname("KEY_SHIFT_NPAGE") == KEY_SHIFT_PAGEDOWN
    assert normalize_curses_keyname("KEY_SHIFT_PAGEUP") == KEY_SHIFT_PAGEUP
    assert normalize_curses_keyname("KEY_SHIFT_PAGEDOWN") == KEY_SHIFT_PAGEDOWN
    assert normalize_curses_keyname("KEY_BTAB") == KEY_SHIFT_TAB
    assert normalize_curses_keyname("kBTab") == KEY_SHIFT_TAB
    assert normalize_curses_keyname("KEY_IC") == curses.KEY_IC
    assert normalize_curses_keyname("KEY_INSERT") == curses.KEY_IC
    assert normalize_curses_keyname("kich1") == curses.KEY_IC
    assert normalize_curses_keyname("KEY_HOME") is None
    assert normalize_curses_keyname("KEY_END") is None
    assert normalize_curses_keyname("kend") is None
    assert normalize_curses_keyname("KEY_LEFT") is None


def test_decodes_shift_arrow_sequences():
    assert decode_key_sequence(list("\x1b[1;2D")) == KEY_SHIFT_LEFT
    assert decode_key_sequence(list("\x1b[1;2C")) == KEY_SHIFT_RIGHT


def test_ignores_unknown_escape_sequence():
    assert decode_key_sequence([ord(ch) for ch in "\x1b[A"]) is None


def test_read_key_returns_escape_when_sequence_times_out():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    window = FakeInputWindow(["\x1b"])

    assert app.read_key(window, idle_timeout=200) == ESC
    assert window.timeouts == [100, 200]


def test_read_key_decodes_alt_x_sequence():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    window = FakeInputWindow(["\x1b", "x"])

    assert app.read_key(window, idle_timeout=200) == KEY_ALT_X
    assert window.timeouts == [100, 200]


def test_read_key_decodes_raw_f3_sequence():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    window = FakeInputWindow(["\x1b", "[", "1", "3", "~"])

    assert app.read_key(window, idle_timeout=200) == curses.KEY_F3
    assert window.timeouts == [100, 200]


def test_read_key_normalizes_raw_lf_to_ctrl_enter():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    window = FakeInputWindow(["\n"])

    assert app.read_key(window, idle_timeout=200) == KEY_CTRL_ENTER
    assert window.timeouts == []


def test_read_key_normalizes_raw_cr_to_plain_enter():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    window = FakeInputWindow(["\r"])

    assert app.read_key(window, idle_timeout=200) == 13
    assert window.timeouts == []


def test_decodes_wide_string_sequence():
    assert decode_key_sequence(list("\x1b[13;5u")) == KEY_CTRL_ENTER


def test_normalizes_control_character_from_get_wch():
    assert normalize_key("\x11") == 17


def test_normalizes_lf_to_ctrl_enter_and_keeps_cr_plain_enter():
    assert normalize_key("\n") == KEY_CTRL_ENTER
    assert normalize_key(10) == KEY_CTRL_ENTER
    assert normalize_key("\r") == 13
    assert normalize_key(13) == 13
    assert normalize_key(curses.KEY_ENTER) == 13


def test_counts_and_clips_utf8_display_cells():
    assert display_width("kůň") == 3
    assert display_width("表") == 2
    assert clip_text("ab表c", 4) == "ab表"


def test_escapes_control_characters_for_display():
    fitted = fit_text("A\x00B", 8)

    assert "\x00" not in fitted
    assert fitted == "A\\x00B  "
    assert display_width("A\x00B") == 6
    assert clip_text("A\x00B", 6) == "A\\x00B"
    assert display_width("A\nB") == 3
    assert clip_text("A\nB", 6) == "A B"
    assert clip_text("A\r\nB", 6) == "A B"
    assert wrap_display_text("A\nB", 6) == ["A", "B"]
    assert wrap_display_text("A\r\nB", 6) == ["A", "B"]


def test_pads_by_display_cells():
    fitted = fit_text("表a", 5)
    assert fitted == "表a  "
    assert display_width(fitted) == 5


def test_buffer_selection_extracts_and_deletes_single_line_text():
    buffer = Buffer(lines=["select one from dual"], row=0, col=10, selection_anchor=(0, 7))
    assert buffer.selection_range() == ((0, 7), (0, 10))
    assert buffer.selected_text() == "one"
    assert buffer.delete_selection() is True
    assert buffer.lines == ["select  from dual"]
    assert (buffer.row, buffer.col) == (0, 7)
    assert buffer.selection_range() is None


def test_buffer_selection_extracts_and_replaces_multiline_text():
    buffer = Buffer(lines=["select one", "from dual"], row=1, col=4, selection_anchor=(0, 7))
    assert buffer.selected_text() == "one\nfrom"
    buffer.insert_text("two\nthree")
    assert buffer.lines == ["select two", "three dual"]
    assert (buffer.row, buffer.col) == (1, 5)


def test_buffer_transform_selection_uppercases_single_line_and_keeps_selection():
    buffer = Buffer(lines=["select one from dual"], row=0, col=10, selection_anchor=(0, 7), dirty=False)

    assert buffer.transform_selection(str.upper) is True

    assert buffer.lines == ["select ONE from dual"]
    assert buffer.selection_range() == ((0, 7), (0, 10))
    assert buffer.selected_text() == "ONE"
    assert buffer.dirty is True


def test_buffer_transform_selection_lowercases_multiline_and_undo_restores_selection():
    buffer = Buffer(lines=["SELECT One", "FROM Dual"], row=1, col=4, selection_anchor=(0, 7), dirty=False)

    assert buffer.transform_selection(str.lower) is True

    assert buffer.lines == ["SELECT one", "from Dual"]
    assert buffer.selection_range() == ((0, 7), (1, 4))
    assert buffer.selected_text() == "one\nfrom"
    assert buffer.dirty is True

    assert buffer.undo() is True
    assert buffer.lines == ["SELECT One", "FROM Dual"]
    assert buffer.selection_anchor == (0, 7)
    assert (buffer.row, buffer.col) == (1, 4)
    assert buffer.dirty is False


def test_buffer_transform_selection_without_selection_leaves_state_unchanged():
    buffer = Buffer(lines=["select one"], row=0, col=6, dirty=False)

    assert buffer.transform_selection(str.upper) is False

    assert buffer.lines == ["select one"]
    assert (buffer.row, buffer.col) == (0, 6)
    assert buffer.dirty is False
    assert buffer.undo_stack == []


def test_buffer_editing_replaces_active_selection():
    buffer = Buffer(lines=["abcdef"], row=0, col=4, selection_anchor=(0, 1))
    buffer.insert_char("X")
    assert buffer.lines == ["aXef"]
    assert (buffer.row, buffer.col) == (0, 2)


def test_buffer_undo_redo_typed_character_and_newline():
    buffer = Buffer(lines=["select"], row=0, col=6, dirty=False)

    buffer.insert_char("ž")
    assert buffer.lines == ["selectž"]
    assert (buffer.row, buffer.col) == (0, 7)
    assert buffer.dirty is True

    assert buffer.undo() is True
    assert buffer.lines == ["select"]
    assert (buffer.row, buffer.col) == (0, 6)
    assert buffer.dirty is False

    assert buffer.redo() is True
    assert buffer.lines == ["selectž"]
    assert (buffer.row, buffer.col) == (0, 7)
    assert buffer.dirty is True

    buffer.newline()
    assert buffer.lines == ["selectž", ""]
    assert (buffer.row, buffer.col) == (1, 0)

    assert buffer.undo() is True
    assert buffer.lines == ["selectž"]
    assert (buffer.row, buffer.col) == (0, 7)

    assert buffer.redo() is True
    assert buffer.lines == ["selectž", ""]
    assert (buffer.row, buffer.col) == (1, 0)


def test_buffer_undo_redo_backspace_delete_and_multiline_utf8():
    buffer = Buffer(lines=["kůň", "dual"], row=0, col=3, dirty=False)

    buffer.backspace()
    assert buffer.lines == ["ků", "dual"]

    assert buffer.undo() is True
    assert buffer.lines == ["kůň", "dual"]
    assert (buffer.row, buffer.col) == (0, 3)
    assert buffer.dirty is False

    assert buffer.redo() is True
    assert buffer.lines == ["ků", "dual"]
    assert (buffer.row, buffer.col) == (0, 2)
    assert buffer.dirty is True

    buffer = Buffer(lines=["abc", "def"], row=0, col=3, dirty=False)
    buffer.delete()
    assert buffer.lines == ["abcdef"]

    assert buffer.undo() is True
    assert buffer.lines == ["abc", "def"]
    assert (buffer.row, buffer.col) == (0, 3)

    assert buffer.redo() is True
    assert buffer.lines == ["abcdef"]
    assert (buffer.row, buffer.col) == (0, 3)


def test_buffer_delete_word_right_uses_next_word_boundary_and_undo_redo():
    buffer = Buffer(lines=["select name from dual"], row=0, col=7, dirty=False)

    buffer.delete_word_right()

    assert buffer.lines == ["select from dual"]
    assert (buffer.row, buffer.col) == (0, 7)
    assert buffer.dirty is True

    assert buffer.undo() is True
    assert buffer.lines == ["select name from dual"]
    assert (buffer.row, buffer.col) == (0, 7)
    assert buffer.dirty is False

    assert buffer.redo() is True
    assert buffer.lines == ["select from dual"]
    assert (buffer.row, buffer.col) == (0, 7)
    assert buffer.dirty is True


def test_buffer_delete_word_right_crosses_lines():
    buffer = Buffer(lines=["select", "  from dual"], row=0, col=0, dirty=False)

    buffer.delete_word_right()

    assert buffer.lines == ["from dual"]
    assert (buffer.row, buffer.col) == (0, 0)


def test_buffer_delete_word_right_deletes_selection_first():
    buffer = Buffer(lines=["select name from dual"], row=0, col=11, selection_anchor=(0, 7), dirty=False)

    buffer.delete_word_right()

    assert buffer.lines == ["select  from dual"]
    assert (buffer.row, buffer.col) == (0, 7)
    assert buffer.selection_range() is None


def test_buffer_delete_word_right_at_eof_is_noop():
    buffer = Buffer(lines=["select"], row=0, col=6, dirty=False)

    buffer.delete_word_right()

    assert buffer.lines == ["select"]
    assert (buffer.row, buffer.col) == (0, 6)
    assert buffer.dirty is False
    assert buffer.undo_stack == []


def test_buffer_delete_word_left_uses_previous_word_boundary_and_undo_redo():
    buffer = Buffer(lines=["select name from dual"], row=0, col=7, dirty=False)

    buffer.delete_word_left()

    assert buffer.lines == ["name from dual"]
    assert (buffer.row, buffer.col) == (0, 0)
    assert buffer.dirty is True

    assert buffer.undo() is True
    assert buffer.lines == ["select name from dual"]
    assert (buffer.row, buffer.col) == (0, 7)
    assert buffer.dirty is False

    assert buffer.redo() is True
    assert buffer.lines == ["name from dual"]
    assert (buffer.row, buffer.col) == (0, 0)
    assert buffer.dirty is True


def test_buffer_delete_word_left_crosses_lines():
    buffer = Buffer(lines=["select", "  from dual"], row=1, col=2, dirty=False)

    buffer.delete_word_left()

    assert buffer.lines == ["from dual"]
    assert (buffer.row, buffer.col) == (0, 0)


def test_buffer_delete_word_left_deletes_selection_first():
    buffer = Buffer(lines=["select name from dual"], row=0, col=11, selection_anchor=(0, 7), dirty=False)

    buffer.delete_word_left()

    assert buffer.lines == ["select  from dual"]
    assert (buffer.row, buffer.col) == (0, 7)
    assert buffer.selection_range() is None


def test_buffer_delete_word_left_at_start_is_noop():
    buffer = Buffer(lines=["select"], row=0, col=0, dirty=False)

    buffer.delete_word_left()

    assert buffer.lines == ["select"]
    assert (buffer.row, buffer.col) == (0, 0)
    assert buffer.dirty is False
    assert buffer.undo_stack == []


def test_buffer_undo_redo_selection_replacement_as_one_step():
    buffer = Buffer(lines=["select one", "from dual"], row=1, col=4, selection_anchor=(0, 7), dirty=False)

    buffer.insert_text("two\nthree")

    assert buffer.lines == ["select two", "three dual"]
    assert (buffer.row, buffer.col) == (1, 5)
    assert buffer.selection_range() is None
    assert len(buffer.undo_stack) == 1

    assert buffer.undo() is True
    assert buffer.lines == ["select one", "from dual"]
    assert (buffer.row, buffer.col) == (1, 4)
    assert buffer.selection_anchor == (0, 7)
    assert buffer.selected_text() == "one\nfrom"
    assert buffer.dirty is False

    assert buffer.redo() is True
    assert buffer.lines == ["select two", "three dual"]
    assert (buffer.row, buffer.col) == (1, 5)
    assert buffer.selection_range() is None
    assert buffer.dirty is True


def test_buffer_undo_redo_whole_buffer_replacement_restores_metadata():
    path = Path("/tmp/scratch.sql")
    buffer = Buffer(
        lines=["select 1", "from dual"],
        row=1,
        col=4,
        scroll=3,
        path=path,
        title="scratch.sql",
        dirty=True,
        selection_anchor=(0, 2),
    )

    buffer.set_text(
        "create table decisions (id number);",
        title=schema_object_title("hr", "TABLE", "DECISIONS"),
        dirty=False,
    )

    assert buffer.text() == "create table decisions (id number);"
    assert buffer.title == "schema://HR/TABLE/DECISIONS.sql"
    assert buffer.path is None
    assert buffer.dirty is False
    assert buffer.selection_range() is None

    assert buffer.undo() is True
    assert buffer.lines == ["select 1", "from dual"]
    assert (buffer.row, buffer.col, buffer.scroll) == (1, 4, 3)
    assert buffer.path == path
    assert buffer.title == "scratch.sql"
    assert buffer.dirty is True
    assert buffer.selection_anchor == (0, 2)

    assert buffer.redo() is True
    assert buffer.text() == "create table decisions (id number);"
    assert buffer.title == "schema://HR/TABLE/DECISIONS.sql"
    assert buffer.path is None
    assert buffer.dirty is False


def test_buffer_file_load_is_undoable(tmp_path):
    opened = tmp_path / "opened.sql"
    opened.write_text("select 2 from dual;\n", encoding="utf-8")
    buffer = Buffer(lines=["select 1 from dual"], row=0, col=8, title="scratch.sql", dirty=True)

    buffer.load(opened)

    assert buffer.text() == "select 2 from dual;"
    assert buffer.path == opened
    assert buffer.title == "opened.sql"
    assert buffer.dirty is False

    assert buffer.undo() is True
    assert buffer.text() == "select 1 from dual"
    assert (buffer.row, buffer.col) == (0, 8)
    assert buffer.path is None
    assert buffer.title == "scratch.sql"
    assert buffer.dirty is True

    assert buffer.redo() is True
    assert buffer.text() == "select 2 from dual;"
    assert buffer.path == opened
    assert buffer.title == "opened.sql"
    assert buffer.dirty is False


def test_buffer_save_is_not_undoable_and_undo_keeps_saved_identity(tmp_path):
    path = tmp_path / "saved.sql"
    buffer = Buffer(lines=["select"], row=0, col=6, dirty=False)

    buffer.insert_char("1")
    buffer.save(path)

    assert buffer.text() == "select1"
    assert buffer.dirty is False

    assert buffer.undo() is True
    assert buffer.text() == "select"
    assert buffer.path == path
    assert buffer.title == "saved.sql"
    assert buffer.dirty is True

    assert buffer.redo() is True
    assert buffer.text() == "select1"
    assert buffer.path == path
    assert buffer.dirty is False


def test_buffer_undo_and_redo_compare_content_with_clean_checkpoint(tmp_path):
    path = tmp_path / "saved.sql"
    path.write_text("select\n", encoding="utf-8")
    buffer = Buffer()
    buffer.load(path, record_undo=False)

    buffer.insert_char("1")
    assert buffer.dirty is True

    assert buffer.undo() is True
    assert buffer.text() == "select"
    assert buffer.dirty is False

    assert buffer.redo() is True
    assert buffer.text() == "1select"
    assert buffer.dirty is True


def test_buffer_failed_atomic_save_as_preserves_destination_and_identity(
    monkeypatch,
    tmp_path,
):
    import plsqlwks.exporting as exporting_module

    source = tmp_path / "source.sql"
    destination = tmp_path / "destination.sql"
    destination.write_text("external\n", encoding="utf-8")
    buffer = Buffer(lines=["select"], row=0, col=6, path=source, title=source.name)
    buffer.insert_char("1")

    monkeypatch.setattr(
        exporting_module.os,
        "replace",
        lambda source_path, destination_path: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        buffer.save(destination)

    assert destination.read_text(encoding="utf-8") == "external\n"
    assert buffer.path == source
    assert buffer.title == source.name
    assert buffer.dirty is True
    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []

    assert buffer.undo() is True
    assert buffer.text() == "select"
    assert buffer.path == source
    assert buffer.dirty is False


def test_buffer_save_load_long_special_sql_round_trip(tmp_path, long_special_sql_case):
    path = tmp_path / "long_special.sql"
    buffer = Buffer(
        lines=long_special_sql_case.editor_text.splitlines(),
        row=4,
        col=12,
        path=path,
        title="long_special.sql",
        dirty=True,
    )

    saved = buffer.save()

    assert saved == path
    assert path.read_text(encoding="utf-8") == long_special_sql_case.script
    assert buffer.path == path
    assert buffer.title == "long_special.sql"
    assert buffer.dirty is False

    loaded = Buffer(lines=["select 0 from dual"], row=0, col=8, title="scratch.sql", dirty=True)
    loaded.load(path)

    assert loaded.text() == long_special_sql_case.editor_text
    assert loaded.path == path
    assert loaded.title == "long_special.sql"
    assert loaded.dirty is False
    assert [statement.text for statement in split_script(loaded.text())] == long_special_sql_case.expected_statements


def test_buffer_round_trip_preserves_non_crlf_line_separator_characters(tmp_path):
    path = tmp_path / "literal_separators.sql"
    source = "select q'[left\u2028middle\u0085vertical\vform\fright]' from dual;\n"
    path.write_text(source, encoding="utf-8")
    buffer = Buffer()

    buffer.load(path, record_undo=False)
    saved = buffer.save()

    assert saved == path
    assert buffer.text() == source.rstrip("\n")
    assert path.read_text(encoding="utf-8") == source


def test_buffer_redo_stack_clears_after_new_edit_following_undo():
    buffer = Buffer()
    buffer.insert_char("a")
    assert buffer.undo() is True

    buffer.insert_char("b")

    assert buffer.text() == "b"
    assert buffer.redo() is False
    assert buffer.redo_stack == []


def test_buffer_undo_history_respects_snapshot_cap():
    buffer = Buffer()
    for _ in range(UNDO_HISTORY_LIMIT + 5):
        buffer.insert_char("x")

    assert len(buffer.undo_stack) == UNDO_HISTORY_LIMIT

    for _ in range(UNDO_HISTORY_LIMIT):
        assert buffer.undo() is True

    assert buffer.text() == "x" * 5
    assert buffer.undo() is False


def test_editor_undo_redo_shortcuts_route_only_in_editor_focus():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer.insert_char("x")

    app.handle_key(CTRL_Z)
    assert app.state.buffer.text() == ""
    assert app.state.status == "Undo"

    app.handle_key(CTRL_Y)
    assert app.state.buffer.text() == "x"
    assert app.state.status == "Redo"

    app.state.focus = FOCUS_RESULTS
    app.state.active_result = QueryResult("data", ["A"], [["1"]], "1 row")
    app.handle_key(CTRL_Z)
    assert app.state.buffer.text() == "x"
    assert app.state.status == "Redo"

    app.state.focus = FOCUS_BROWSER
    app.handle_key(CTRL_Z)
    assert app.state.buffer.text() == "x"
    assert app.state.status == "Redo"


def test_editor_undo_redo_shortcuts_report_empty_history():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["select 1"], row=0, col=8)

    app.handle_key(CTRL_Z)
    assert app.state.buffer.text() == "select 1"
    assert app.state.status == "Nothing to undo"

    app.handle_key(CTRL_Y)
    assert app.state.buffer.text() == "select 1"
    assert app.state.status == "Nothing to redo"


def test_find_search_matches_are_literal_case_insensitive_and_unicode():
    matches = find_search_matches(["select Příliš", "příliš low"], "PŘÍLIŠ")

    assert matches == [
        SearchMatch(0, 7, 0, 13, 7, 13),
        SearchMatch(1, 0, 1, 6, 14, 20),
    ]
    assert find_search_matches(["a.b a-b"], "a.b") == [SearchMatch(0, 0, 0, 3, 0, 3)]
    assert find_search_matches(["abc"], "") == []


def test_search_match_index_moves_forward_backward_and_wraps():
    matches = find_search_matches(["one two one"], "one")

    assert search_match_index(matches, 0, 1) == (0, False)
    assert search_match_index(matches, 3, 1) == (1, False)
    assert search_match_index(matches, 12, 1) == (0, True)
    assert search_match_index(matches, 8, -1) == (0, False)
    assert search_match_index(matches, 0, -1) == (1, True)


def test_search_status_formats_match_count_and_wrap_marker():
    assert search_status("select", 1, 3, False) == 'Found "select" 2/3'
    assert search_status("select", 0, 3, True) == 'Found "select" 1/3 (wrapped)'


def test_ctrl_f_prompts_and_selects_first_match_without_dirtying_buffer():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["select one", "select two"], row=0, col=0, dirty=False)
    prompts: list[tuple[str, str, bool]] = []

    def find_prompt(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default, strip))
        return "select"

    app.prompt = find_prompt

    app.handle_key(CTRL_F)

    assert prompts == [("Find", "", False)]
    assert app.state.active_tab.search_query == "select"
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 6)
    assert app.state.buffer.selection_anchor == (0, 0)
    assert app.state.buffer.dirty is False
    assert app.state.buffer.undo_stack == []
    assert app.state.status == 'Found "select" 1/2'


def test_ctrl_n_and_ctrl_p_repeat_search_and_wrap():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["one two one"], row=0, col=0, dirty=False)
    app.state.active_tab.search_query = "one"

    app.handle_key(CTRL_N)
    assert (app.state.buffer.selection_anchor, app.state.buffer.row, app.state.buffer.col) == ((0, 0), 0, 3)
    assert app.state.status == 'Found "one" 1/2'

    app.handle_key(CTRL_N)
    assert (app.state.buffer.selection_anchor, app.state.buffer.row, app.state.buffer.col) == ((0, 8), 0, 11)
    assert app.state.status == 'Found "one" 2/2'

    app.handle_key(CTRL_N)
    assert (app.state.buffer.selection_anchor, app.state.buffer.row, app.state.buffer.col) == ((0, 0), 0, 3)
    assert app.state.status == 'Found "one" 1/2 (wrapped)'

    app.handle_key(CTRL_P)
    assert (app.state.buffer.selection_anchor, app.state.buffer.row, app.state.buffer.col) == ((0, 8), 0, 11)
    assert app.state.status == 'Found "one" 2/2 (wrapped)'
    assert app.state.buffer.dirty is False
    assert app.state.buffer.undo_stack == []


def test_ctrl_n_without_query_prompts_for_search_text():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["alpha beta"], row=0, col=0)
    app.prompt = lambda label, default="", strip=True: "beta"

    app.handle_key(CTRL_N)

    assert app.state.active_tab.search_query == "beta"
    assert (app.state.buffer.selection_anchor, app.state.buffer.row, app.state.buffer.col) == ((0, 6), 0, 10)
    assert app.state.status == 'Found "beta" 1/1'


def test_search_reports_no_match_and_clear_or_cancel_status():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["alpha"], row=0, col=0, selection_anchor=(0, 0))
    app.state.active_tab.search_query = "missing"

    app.handle_key(CTRL_N)
    assert app.state.buffer.selection_range() is None
    assert app.state.status == 'No matches for "missing"'

    app.prompt = lambda label, default="", strip=True: ""
    app.handle_key(CTRL_F)
    assert app.state.active_tab.search_query == ""
    assert app.state.status == "Search cleared"

    app.state.active_tab.search_query = "alpha"
    app.dialogs.prompt = lambda label, default="", strip=True: None
    app.handle_key(CTRL_F)
    assert app.state.active_tab.search_query == "alpha"
    assert app.state.status == "Search cancelled"


def test_ctrl_g_prompts_with_dialog_and_moves_to_line_without_dirtying_buffer():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(
        lines=["select one", "select two", "select three"],
        row=0,
        col=6,
        selection_anchor=(0, 0),
        dirty=False,
    )
    prompts: list[tuple[str, str, bool]] = []

    def go_to_line_prompt(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default, strip))
        return "3"

    app.prompt_text_box = go_to_line_prompt

    app.handle_key(CTRL_G)

    assert prompts == [("Go to line", "1", True)]
    assert (app.state.buffer.row, app.state.buffer.col) == (2, 0)
    assert app.state.buffer.selection_anchor is None
    assert app.state.buffer.dirty is False
    assert app.state.buffer.undo_stack == []
    assert app.state.status == "Moved to line 3/3"


def test_ctrl_g_reports_cancel_invalid_and_out_of_range_without_moving():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["one", "two"], row=1, col=2, dirty=False)

    app.prompt_text_box = lambda label, default="", strip=True: None
    app.handle_key(CTRL_G)
    assert (app.state.buffer.row, app.state.buffer.col) == (1, 2)
    assert app.state.status == "Go to line cancelled"

    app.dialogs.prompt_text_box = lambda label, default="", strip=True: "two"
    app.handle_key(CTRL_G)
    assert (app.state.buffer.row, app.state.buffer.col) == (1, 2)
    assert app.state.status == "Invalid line number"

    app.dialogs.prompt_text_box = lambda label, default="", strip=True: "3"
    app.handle_key(CTRL_G)
    assert (app.state.buffer.row, app.state.buffer.col) == (1, 2)
    assert app.state.status == "Line number must be 1-2"


def test_completion_context_detects_prefix_and_qualifier():
    buffer = Buffer(lines=["select e.na from employees e"], row=0, col=11)

    assert completion_context_for_buffer(buffer, "select e.na from employees e") == CompletionContext(
        row=0,
        start_col=9,
        end_col=11,
        prefix="na",
        qualifier="e",
        statement="select e.na from employees e",
    )

    buffer = Buffer(lines=["select employees."], row=0, col=17)
    context = completion_context_for_buffer(buffer)
    assert context.prefix == ""
    assert context.qualifier == "employees"
    assert context.start_col == 17


def test_completion_context_parses_quoted_prefix_and_qualifier():
    line = 'select "Alias"."Na'
    buffer = Buffer(lines=[line], row=0, col=len(line))

    assert completion_context_for_buffer(buffer, line) == CompletionContext(
        row=0,
        start_col=15,
        end_col=len(line),
        prefix="Na",
        qualifier="Alias",
        statement=line,
        prefix_quoted=True,
        qualifier_quoted=True,
    )


def test_apply_completion_recomputes_exact_clean_checkpoint():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    buffer = Buffer(lines=["select"], row=0, col=6, dirty=False)
    buffer.backspace()
    app.state.buffer = buffer

    assert buffer.text() == "selec"
    assert buffer.dirty is True

    app.apply_completion(
        CompletionContext(0, 0, 5, "selec", statement="selec"),
        CompletionCandidate("select", "select [keyword]", "keyword"),
    )

    assert buffer.text() == "select"
    assert buffer.dirty is False
    assert app.state.status == "Completed keyword: select"


def test_statement_table_references_extract_tables_and_aliases():
    references = statement_table_references("select d.name from decisions d join projects p on p.id = d.project_id")

    assert references == {
        "DECISIONS": "DECISIONS",
        "D": "DECISIONS",
        "PROJECTS": "PROJECTS",
        "P": "PROJECTS",
    }


def test_statement_table_references_preserve_quoted_exact_names():
    references = statement_table_references('select "d"."Name" from "Mixed Table" "d" join "Foo" "Where" on 1 = 1')

    assert references == {
        "Mixed Table": "Mixed Table",
        "d": "Mixed Table",
        "Foo": "Foo",
        "Where": "Foo",
    }


def test_statement_table_references_ignore_literals_and_comments():
    references = statement_table_references(
        'select a."Name" from "Actual" a where note = \'from "String table" a\' -- from "Comment table" a\n'
    )

    assert references == {"Actual": "Actual", "A": "Actual"}


def test_completion_qualifier_respects_quoted_and_unquoted_aliases():
    references = statement_table_references('select 1 from "First" "a" join "Second" a on 1 = 1')

    assert references == {
        "First": "First",
        "a": "First",
        "Second": "Second",
        "A": "Second",
    }
    assert (
        resolve_completion_qualifier(
            "a",
            references,
            {},
            quoted=False,
        )
        == "Second"
    )
    assert (
        resolve_completion_qualifier(
            "a",
            references,
            {},
            quoted=True,
        )
        == "First"
    )


def test_generated_sql_table_from_statement_uses_first_table_reference():
    assert (
        generated_sql_table_from_statement("select d.name from decisions d join projects p on p.id = d.project_id")
        == "DECISIONS"
    )
    assert generated_sql_table_from_statement("begin null; end;") == ""


def test_generated_sql_with_columns_formats_statement_templates():
    columns = ["ID", "FIRST_NAME"]

    assert generated_select_sql("EMPLOYEES", columns) == ("select\n  ID,\n  FIRST_NAME\nfrom EMPLOYEES;\n")
    assert generated_insert_sql("EMPLOYEES", columns) == (
        "insert into EMPLOYEES (\n  ID,\n  FIRST_NAME\n) values (\n  :id,\n  :first_name\n);\n"
    )
    assert generated_update_sql("EMPLOYEES", columns) == (
        "update EMPLOYEES\nset\n  ID = :id,\n  FIRST_NAME = :first_name\nwhere <condition>;\n"
    )


def test_generated_sql_quotes_exact_names_and_deduplicates_bind_names():
    columns = ["Display Name", "Display-Name", "SELECT"]

    assert generated_insert_sql("Mixed Table", columns) == (
        'insert into "Mixed Table" (\n'
        '  "Display Name",\n'
        '  "Display-Name",\n'
        '  "SELECT"\n'
        ") values (\n"
        "  :display_name,\n"
        "  :display_name_2,\n"
        "  :select\n"
        ");\n"
    )


def test_generated_sql_bind_names_stay_unique_after_real_suffixes():
    assert generated_update_sql("CaseTable", ["FOO", "Foo", "FOO_2"]) == (
        'update "CaseTable"\nset\n  FOO = :foo,\n  "Foo" = :foo_2,\n  FOO_2 = :foo_2_2\nwhere <condition>;\n'
    )


def test_dedupe_completion_candidates_orders_by_kind_and_name():
    candidates = dedupe_completion_candidates(
        [
            CompletionCandidate("SELECT", "SELECT [keyword]", "keyword"),
            CompletionCandidate("NAME", "NAME [column DECISIONS]", "column", "DECISIONS"),
            CompletionCandidate("DECISIONS", "DECISIONS [table]", "table"),
            CompletionCandidate("NAME", "NAME [column PROJECTS]", "column", "PROJECTS"),
        ]
    )

    assert candidates == [
        CompletionCandidate("NAME", "NAME [column DECISIONS]", "column", "DECISIONS"),
        CompletionCandidate("DECISIONS", "DECISIONS [table]", "table"),
        CompletionCandidate("SELECT", "SELECT [keyword]", "keyword"),
    ]


def test_filtered_picker_indexes_matches_case_insensitive_substrings():
    options = ["Alpha", "beta", "Report Alpha", "Gamma"]

    assert filtered_picker_indexes(options, "") == [0, 1, 2, 3]
    assert filtered_picker_indexes(options, "ALP") == [0, 2]
    assert filtered_picker_indexes(options, "ta") == [1]
    assert filtered_picker_indexes(options, "missing") == []


def run_prompt(
    monkeypatch,
    keys: list[int | str],
    label: str = "Set NAME",
    default: str = "",
    strip: bool = False,
    width: int = 80,
) -> tuple[str | None, "FakePickerWindow"]:
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    screen = FakePickerWindow(3, width, 0, 0)
    app.screen = screen

    monkeypatch.setattr(curses, "curs_set", lambda visibility: None)
    monkeypatch.setattr(curses, "color_pair", lambda pair: 0)
    key_iter = iter(keys)
    app.read_key = lambda window=None, idle_timeout=200: next(key_iter)

    return app.prompt(label, default, strip), screen


def test_prompt_inserts_printable_text_at_cursor(monkeypatch):
    value, _screen = run_prompt(
        monkeypatch,
        [curses.KEY_LEFT, curses.KEY_LEFT, "X", 10],
        default="abcd",
    )

    assert value == "abXcd"


def test_prompt_backspace_delete_home_and_end_edit_around_cursor(monkeypatch):
    value, _screen = run_prompt(
        monkeypatch,
        [
            curses.KEY_LEFT,
            curses.KEY_BACKSPACE,
            curses.KEY_HOME,
            curses.KEY_DC,
            curses.KEY_END,
            curses.KEY_BACKSPACE,
            10,
        ],
        default="abcd",
    )

    assert value == "b"


def test_prompt_navigation_and_deletion_stop_at_text_boundaries(monkeypatch):
    value, _screen = run_prompt(
        monkeypatch,
        [
            curses.KEY_HOME,
            curses.KEY_LEFT,
            curses.KEY_BACKSPACE,
            curses.KEY_END,
            curses.KEY_RIGHT,
            curses.KEY_DC,
            10,
        ],
        default="ab",
    )

    assert value == "ab"


def test_prompt_scrolls_long_text_and_places_cursor_by_display_width(monkeypatch):
    value, screen = run_prompt(
        monkeypatch,
        [curses.KEY_HOME, curses.KEY_RIGHT, curses.KEY_RIGHT, 10],
        label="Set",
        default="a界bcdefghijklmnop",
        width=16,
    )

    assert value == "a界bcdefghijklmnop"
    assert screen.calls[0].text == "Set: ghijklmnop "
    assert screen.calls[-1].text == "Set: a界bcdefghi"
    assert screen.moves[0] == (2, 15)
    assert screen.moves[-1] == (2, 8)


def test_prompt_preserves_enter_strip_and_escape_behavior(monkeypatch):
    value, _screen = run_prompt(monkeypatch, [10], default="  value  ", strip=True)
    cancelled, _screen = run_prompt(monkeypatch, [ESC], default="unchanged")

    assert value == "value"
    assert cancelled is None


def run_prompt_text_box(
    monkeypatch,
    keys: list[int | str],
    label: str = "Value for :id",
    default: str = "",
    strip: bool = True,
    screen: "FakeScreen | None" = None,
) -> tuple[str | None, list["FakePickerWindow"]]:
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.screen = screen or FakeScreen(height=12, width=80)
    windows: list[FakePickerWindow] = []

    def fake_newwin(height: int, width: int, top: int, left: int):
        window = FakePickerWindow(height, width, top, left)
        windows.append(window)
        return window

    monkeypatch.setattr(curses, "curs_set", lambda visibility: None)
    monkeypatch.setattr(curses, "newwin", fake_newwin)
    key_iter = iter(keys)
    app.read_key = lambda window=None, idle_timeout=200: next(key_iter)

    return app.prompt_text_box(label, default, strip), windows


def test_prompt_text_box_accepts_entered_value(monkeypatch):
    value, windows = run_prompt_text_box(monkeypatch, ["4", "2", 10], strip=False)

    assert value == "42"
    assert any("Value for :id" in call.text for window in windows for call in window.calls)
    assert windows[-1].moves[-1] == (2, 4)


def test_prompt_text_box_backspace_and_preserves_spaces(monkeypatch):
    value, _windows = run_prompt_text_box(
        monkeypatch,
        [" ", "x", curses.KEY_BACKSPACE, " ", 10],
        strip=False,
    )

    assert value == "  "


def test_prompt_text_box_escape_cancels(monkeypatch):
    value, _windows = run_prompt_text_box(monkeypatch, [ESC])

    assert value is None


def test_prompt_text_box_redraws_after_resize_key(monkeypatch):
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    screen = FakeScreen(height=12, width=80)
    app.screen = screen
    windows: list[FakePickerWindow] = []

    def fake_newwin(height: int, width: int, top: int, left: int):
        window = FakePickerWindow(height, width, top, left)
        windows.append(window)
        return window

    monkeypatch.setattr(curses, "curs_set", lambda visibility: None)
    monkeypatch.setattr(curses, "newwin", fake_newwin)
    keys = iter([curses.KEY_RESIZE, "7", 10])

    def read_key(window=None, idle_timeout=200):
        key = next(keys)
        if key == curses.KEY_RESIZE:
            screen.height = 10
            screen.width = 30
        return key

    app.read_key = read_key

    value = app.prompt_text_box("Value for :id", "", strip=False)

    assert value == "7"
    assert [window.width for window in windows] == [36, 26, 26]


def test_prompt_text_box_survives_resize_window_draw_errors(monkeypatch):
    class FailingWindow(FakePickerWindow):
        def box(self):
            raise curses.error

        def addstr(self, y: int, x: int, text: str, attr: int = 0):
            raise curses.error

        def move(self, y: int, x: int):
            raise curses.error

        def refresh(self):
            raise curses.error

    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.screen = FakeScreen(height=12, width=80)
    windows: list[FakePickerWindow] = []

    def fake_newwin(height: int, width: int, top: int, left: int):
        window: FakePickerWindow
        window = FailingWindow(height, width, top, left) if not windows else FakePickerWindow(height, width, top, left)
        windows.append(window)
        return window

    monkeypatch.setattr(curses, "curs_set", lambda visibility: None)
    monkeypatch.setattr(curses, "newwin", fake_newwin)
    keys = iter([curses.KEY_RESIZE, "4", "2", 10])
    app.read_key = lambda window=None, idle_timeout=200: next(keys)

    value = app.prompt_text_box("Value for :id", "", strip=False)

    assert value == "42"
    assert len(windows) == 4


def test_prompt_text_box_clips_long_value_inside_field(monkeypatch):
    long_value = "abcdefghijklmnopqrstuvwxyz0123456789"

    value, windows = run_prompt_text_box(
        monkeypatch,
        [*long_value, 10],
        strip=False,
        screen=FakeScreen(height=12, width=40),
    )

    assert value == long_value
    field_calls = [(window, call) for window in windows for call in window.calls if call.attr == curses.A_REVERSE]
    assert field_calls
    assert all(display_width(call.text) <= window.width - 4 for window, call in field_calls)
    assert field_calls[-1][1].text.endswith(long_value[-1])
    assert not field_calls[-1][1].text.startswith(long_value[:3])


def test_pick_filters_options_and_returns_original_index(monkeypatch):
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.screen = FakeScreen(height=12, width=80)
    windows = []

    def fake_newwin(height: int, width: int, top: int, left: int):
        window = FakePickerWindow(height, width, top, left)
        windows.append(window)
        return window

    monkeypatch.setattr(curses, "newwin", fake_newwin)
    keys = iter(["l", "o", 10])
    app.read_key = lambda window=None, idle_timeout=200: next(keys)

    choice = app.pick("Complete", ["DECISIONS [table]", "DECISION_LOG [table]", "SELECT [keyword]"])

    assert choice == 1
    assert any("Filter: lo" in call.text for window in windows for call in window.calls)
    assert any("DECISION_LOG [table]" in call.text for window in windows for call in window.calls)


def test_pick_ignores_enter_without_matches_and_recovers_with_backspace(monkeypatch):
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.screen = FakeScreen(height=12, width=80)
    windows = []

    def fake_newwin(height: int, width: int, top: int, left: int):
        window = FakePickerWindow(height, width, top, left)
        windows.append(window)
        return window

    monkeypatch.setattr(curses, "newwin", fake_newwin)
    keys = iter(["z", 10, curses.KEY_BACKSPACE, "g", 10])
    app.read_key = lambda window=None, idle_timeout=200: next(keys)

    choice = app.pick("Complete", ["Alpha", "Beta", "Gamma"])

    assert choice == 2
    assert any("No matches" in call.text for window in windows for call in window.calls)


def test_pick_refreshes_background_when_filter_resizes_popup(monkeypatch):
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.screen = FakeScreen(height=12, width=80)
    windows = []
    refresh_counts_before_newwin: list[tuple[int, int]] = []

    def fake_newwin(height: int, width: int, top: int, left: int):
        refresh_counts_before_newwin.append((app.screen.touchwin_count, app.screen.refresh_count))
        window = FakePickerWindow(height, width, top, left)
        windows.append(window)
        return window

    monkeypatch.setattr(curses, "newwin", fake_newwin)
    keys = iter(["z", ESC])
    app.read_key = lambda window=None, idle_timeout=200: next(keys)

    choice = app.pick("Complete", ["Alpha", "Beta", "Gamma", "Delta"])

    assert choice is None
    assert [(window.height, window.width, window.top, window.left) for window in windows] == [
        (7, 30, 2, 25),
        (4, 30, 4, 25),
    ]
    assert refresh_counts_before_newwin == [(0, 0), (1, 1)]


def test_shift_tab_completes_unique_keyword_and_undo_restores_prefix():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["SEL"], row=0, col=3, dirty=False)

    app.handle_key(KEY_SHIFT_TAB)
    app.wait_for_db_operation(timeout=1)

    assert app.state.buffer.text() == "SELECT"
    assert app.state.buffer.dirty is True
    assert app.state.status == "Completed keyword: SELECT"

    app.handle_key(CTRL_Z)
    assert app.state.buffer.text() == "SEL"
    assert app.state.buffer.dirty is False


def test_shift_tab_completes_left_and_right_join_keywords():
    for prefix, expected in [("LEF", "LEFT"), ("RIG", "RIGHT")]:
        app = ServiceHarness()
        app.state = UIState(config=make_config(), db=object())
        app.running = True
        app.state.buffer = Buffer(lines=[prefix], row=0, col=len(prefix), dirty=False)

        app.handle_key(KEY_SHIFT_TAB)

        assert app.state.buffer.text() == expected
        assert app.state.status == f"Completed keyword: {expected}"


def test_shift_tab_uses_picker_for_multiple_object_matches():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["dec"], row=0, col=3, dirty=False)
    app.state.browser_loaded = True
    app.state.browser_objects = {
        "TABLE": ["DECISIONS", "DECISION_LOG"],
        "VIEW": [],
        "PROCEDURE": [],
        "FUNCTION": [],
        "PACKAGE": [],
    }
    seen_options: list[list[str]] = []

    def choose_first(title: str, options: list[str]) -> int:
        seen_options.append(options)
        return 0

    app.pick = choose_first

    app.handle_key(KEY_SHIFT_TAB)
    app.wait_for_db_operation(timeout=1)

    assert seen_options
    assert "DECISIONS [table]" in seen_options[0]
    assert app.state.buffer.text() == "DECISIONS"
    assert app.state.status == "Completed table: DECISIONS"


def test_object_completion_excludes_browser_only_object_types():
    schema_objects = {
        "TABLE": ["APP_TABLE"],
        "VIEW": ["APP_VIEW"],
        "PROCEDURE": ["APP_PROCEDURE"],
        "FUNCTION": ["APP_FUNCTION"],
        "PACKAGE": ["APP_PACKAGE"],
        "TRIGGER": ["APP_TRIGGER"],
        "SEQUENCE": ["APP_SEQUENCE"],
        "INDEX": ["APP_INDEX"],
        "SYNONYM": ["APP_SYNONYM"],
    }

    candidates = object_completion_candidates(schema_objects, "APP_")

    assert {candidate.kind for candidate in candidates} == {
        "table",
        "view",
        "procedure",
        "function",
        "package",
    }
    assert {candidate.insert_text for candidate in candidates} == {
        "APP_TABLE",
        "APP_VIEW",
        "APP_PROCEDURE",
        "APP_FUNCTION",
        "APP_PACKAGE",
    }


def test_quoted_completion_matches_case_insensitively_and_inserts_exact_names():
    schema_objects = {
        "TABLE": ["FOO", "Foo", "Mixed Table"],
        "VIEW": [],
        "PROCEDURE": [],
        "FUNCTION": [],
        "PACKAGE": [],
    }

    candidates = object_completion_candidates(schema_objects, "fo", quoted=True)

    assert [candidate.insert_text for candidate in candidates] == ['"FOO"', '"Foo"']


def test_shift_tab_picker_filter_selects_completion_candidate(monkeypatch):
    app = ServiceHarness()
    app.screen = FakeScreen(height=12, width=80)
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["dec"], row=0, col=3, dirty=False)
    app.state.browser_loaded = True
    app.state.browser_objects = {
        "TABLE": ["DECISIONS", "DECISION_LOG"],
        "VIEW": [],
        "PROCEDURE": [],
        "FUNCTION": [],
        "PACKAGE": [],
    }
    windows = []

    def fake_newwin(height: int, width: int, top: int, left: int):
        window = FakePickerWindow(height, width, top, left)
        windows.append(window)
        return window

    monkeypatch.setattr(curses, "newwin", fake_newwin)
    keys = iter(["l", "o", "g", 10])
    app.read_key = lambda window=None, idle_timeout=200: next(keys)

    app.handle_key(KEY_SHIFT_TAB)

    assert app.state.buffer.text() == "DECISION_LOG"
    assert app.state.status == "Completed table: DECISION_LOG"
    assert any("Filter: log" in call.text for window in windows for call in window.calls)


def test_shift_tab_lazily_loads_schema_objects_for_object_completion():
    db = CompletionDb(objects={"TABLE": ["EMPLOYEES"], "VIEW": [], "PROCEDURE": [], "FUNCTION": [], "PACKAGE": []})
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.running = True
    app.state.buffer = Buffer(lines=["emp"], row=0, col=3, dirty=False)

    app.handle_key(KEY_SHIFT_TAB)
    app.wait_for_db_operation(timeout=1)

    assert db.object_calls == 1
    assert app.state.browser_loaded is True
    assert app.state.buffer.text() == "EMPLOYEES"
    assert app.state.status == "Completed table: EMPLOYEES"


def test_shift_tab_completes_qualified_columns_and_caches_metadata():
    db = CompletionDb(columns={"EMPLOYEES": ["EMPLOYEE_ID", "FIRST_NAME"]})
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.running = True
    app.state.buffer = Buffer(lines=["employees.em"], row=0, col=len("employees.em"), dirty=False)

    app.handle_key(KEY_SHIFT_TAB)
    app.wait_for_db_operation(timeout=1)

    assert db.object_calls == 1
    assert db.column_calls == ["EMPLOYEES"]
    assert app.state.schema_columns == {"EMPLOYEES": ["EMPLOYEE_ID", "FIRST_NAME"]}
    assert app.state.buffer.text() == "employees.EMPLOYEE_ID"
    assert app.state.status == "Completed column EMPLOYEES: EMPLOYEE_ID"


def test_alt_plus_refreshes_autocomplete_objects_and_clears_column_cache():
    db = CompletionDb(objects={"TABLE": ["FRESH_TABLE"], "VIEW": [], "PROCEDURE": [], "FUNCTION": [], "PACKAGE": []})
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.running = True
    app.state.browser_loaded = True
    app.state.browser_objects = {"TABLE": ["STALE_TABLE"], "VIEW": [], "PROCEDURE": [], "FUNCTION": [], "PACKAGE": []}
    app.state.schema_columns = {"STALE_TABLE": ["STALE_COLUMN"]}

    app.handle_key(KEY_ALT_PLUS)
    app.wait_for_db_operation(timeout=1)

    assert db.object_calls == 1
    assert app.state.browser_loaded is True
    assert app.state.browser_objects["TABLE"] == ["FRESH_TABLE"]
    assert app.state.schema_columns == {}
    assert app.state.status == "Refreshed autocomplete cache: 1 schema object(s)"


def test_alt_plus_refresh_failure_preserves_autocomplete_cache():
    class FailingCompletionDb(CompletionDb):
        def list_schema_objects(self) -> dict[str, list[str]]:
            self.object_calls += 1
            raise RuntimeError("metadata down")

    db = FailingCompletionDb()
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.running = True
    app.state.browser_loaded = True
    app.state.browser_objects = {"TABLE": ["STALE_TABLE"], "VIEW": [], "PROCEDURE": [], "FUNCTION": [], "PACKAGE": []}
    app.state.schema_columns = {"STALE_TABLE": ["STALE_COLUMN"]}
    app.set_results = lambda lines, clear_table=True: setattr(app.state, "results", lines)

    app.handle_key(KEY_ALT_PLUS)
    app.wait_for_db_operation(timeout=1)

    assert db.object_calls == 1
    assert app.state.browser_objects["TABLE"] == ["STALE_TABLE"]
    assert app.state.schema_columns == {"STALE_TABLE": ["STALE_COLUMN"]}
    assert app.state.status == "Autocomplete cache refresh failed"
    assert app.state.results[0] == "ERROR refreshing autocomplete cache:"


def test_shift_tab_completes_unqualified_columns_from_current_statement():
    db = CompletionDb(
        objects={"TABLE": [], "VIEW": [], "PROCEDURE": [], "FUNCTION": [], "PACKAGE": []},
        columns={"EMPLOYEES": ["EMPLOYEE_ID", "NAME"]},
    )
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.running = True
    app.state.buffer = Buffer(lines=["select na from employees e"], row=0, col=len("select na"), dirty=False)

    app.handle_key(KEY_SHIFT_TAB)
    app.wait_for_db_operation(timeout=1)

    assert db.column_calls == ["EMPLOYEES"]
    assert app.state.buffer.text() == "select NAME from employees e"
    assert app.state.status == "Completed column EMPLOYEES: NAME"


def test_completion_metadata_changed_buffer_caches_without_applying_completion():
    started = ui.threading.Event()
    release = ui.threading.Event()

    class BlockingCompletionDb(CompletionDb):
        def list_schema_objects(self) -> dict[str, list[str]]:
            started.set()
            release.wait()
            return super().list_schema_objects()

    db = BlockingCompletionDb(
        objects={
            "TABLE": ["EMPLOYEES"],
            "VIEW": [],
            "PROCEDURE": [],
            "FUNCTION": [],
            "PACKAGE": [],
        }
    )
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.running = True
    app.state.buffer = Buffer(lines=["emp"], row=0, col=3, dirty=False)
    app.pick = lambda title, options: (_ for _ in ()).throw(
        AssertionError("completion picker must not open for stale context")
    )

    app.handle_key(KEY_SHIFT_TAB)
    assert started.wait(1)
    app.state.buffer.insert_char("x")
    release.set()
    app.wait_for_db_operation(timeout=1)

    assert app.state.browser_loaded is True
    assert app.state.browser_objects["TABLE"] == ["EMPLOYEES"]
    assert app.state.buffer.text() == "empx"
    assert app.state.status == "Completion metadata loaded; retry completion"


def test_completion_metadata_changed_focus_caches_without_applying_completion():
    started = ui.threading.Event()
    release = ui.threading.Event()

    class BlockingCompletionDb(CompletionDb):
        def list_schema_objects(self) -> dict[str, list[str]]:
            started.set()
            release.wait()
            return super().list_schema_objects()

    db = BlockingCompletionDb(
        objects={
            "TABLE": ["EMPLOYEES"],
            "VIEW": [],
            "PROCEDURE": [],
            "FUNCTION": [],
            "PACKAGE": [],
        }
    )
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.running = True
    app.state.buffer = Buffer(lines=["emp"], row=0, col=3, dirty=False)
    app.pick = lambda title, options: (_ for _ in ()).throw(
        AssertionError("completion picker must not open after focus changes")
    )

    app.handle_key(KEY_SHIFT_TAB)
    assert started.wait(1)
    app.state.focus = FOCUS_BROWSER
    release.set()
    app.wait_for_db_operation(timeout=1)

    assert app.state.browser_objects["TABLE"] == ["EMPLOYEES"]
    assert app.state.buffer.text() == "emp"
    assert app.state.focus == FOCUS_BROWSER
    assert app.state.status == "Completion metadata loaded; retry completion"


def test_alt_g_routes_to_sql_generator_from_editor_focus():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    called: list[bool] = []
    app.generate_sql_with_columns = lambda: called.append(True)

    app.handle_key(KEY_ALT_G)

    assert called == [True]


def test_generate_sql_with_columns_infers_table_and_replaces_selection():
    db = CompletionDb(columns={"EMPLOYEES": ["EMPLOYEE_ID", "FIRST_NAME"]})
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    statement = "select * from employees e"
    app.state.buffer = Buffer(lines=[statement], row=0, col=len(statement), selection_anchor=(0, 0), dirty=False)
    prompts: list[tuple[str, str, bool]] = []
    picks: list[tuple[str, list[str]]] = []

    def prompt(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default, strip))
        return default

    def pick(title: str, options: list[str]) -> int:
        picks.append((title, options))
        return 0

    app.prompt = prompt
    app.pick = pick

    app.generate_sql_with_columns()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [("Table or view", "EMPLOYEES", True)]
    assert picks == [("Generate SQL", ["SELECT with columns", "INSERT with columns", "UPDATE with columns"])]
    assert db.column_calls == ["EMPLOYEES"]
    assert app.state.buffer.text() == ("select\n  EMPLOYEE_ID,\n  FIRST_NAME\nfrom EMPLOYEES;\n")
    assert app.state.status == "Inserted SELECT with columns for EMPLOYEES"


def test_generate_sql_with_columns_uses_selected_browser_table_default():
    db = CompletionDb(columns={"DECISIONS": ["ID", "NAME"]})
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.state.focus = FOCUS_BROWSER
    app.state.buffer = Buffer(dirty=False)
    app.active_browser_entry = lambda: BrowserEntry("object", "DECISIONS", "TABLE", "DECISIONS")
    prompts: list[tuple[str, str]] = []

    def prompt(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return default

    app.prompt = prompt
    app.pick = lambda title, options: 2

    app.generate_sql_with_columns()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [("Table or view", "DECISIONS")]
    assert app.state.buffer.text() == ("update DECISIONS\nset\n  ID = :id,\n  NAME = :name\nwhere <condition>;\n")
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.status == "Inserted UPDATE with columns for DECISIONS"


def test_alt_g_generates_select_columns_for_selected_browser_view():
    db = CompletionDb(columns={"DECISION_VIEW": ["ID", "NAME"]})
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.state.focus = FOCUS_BROWSER
    app.state.buffer = Buffer(dirty=False)
    app.active_browser_entry = lambda: BrowserEntry(
        "object",
        "DECISION_VIEW",
        "VIEW",
        "DECISION_VIEW",
    )
    prompts: list[tuple[str, str]] = []

    def prompt(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return default

    app.prompt = prompt
    app.pick = lambda title, options: 0

    app.handle_key(KEY_ALT_G)
    app.wait_for_db_operation(timeout=1)

    assert prompts == [("Table or view", "DECISION_VIEW")]
    assert db.column_calls == ["DECISION_VIEW"]
    assert app.state.buffer.text() == ("select\n  ID,\n  NAME\nfrom DECISION_VIEW;\n")
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.status == "Inserted SELECT with columns for DECISION_VIEW"


def test_generate_sql_with_columns_cancelled_prompt_leaves_buffer_unchanged():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=CompletionDb(columns={"DECISIONS": ["ID"]}))
    app.state.buffer = Buffer(lines=["select 1"], row=0, col=0, dirty=False)
    app.prompt = lambda label, default="", strip=True: None

    app.generate_sql_with_columns()
    app.wait_for_db_operation(timeout=1)

    assert app.state.buffer.text() == "select 1"
    assert app.state.buffer.dirty is False
    assert app.state.status == "SQL generation cancelled"


def test_generate_sql_with_columns_handles_missing_columns_without_inserting():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=CompletionDb(columns={"DECISIONS": []}))
    app.state.buffer = Buffer(lines=[""], row=0, col=0, dirty=False)
    app.prompt = lambda label, default="", strip=True: "decisions"
    app.pick = lambda title, options: (_ for _ in ()).throw(AssertionError("unexpected picker"))

    app.generate_sql_with_columns()
    app.wait_for_db_operation(timeout=1)

    assert app.state.buffer.text() == ""
    assert app.state.buffer.dirty is False
    assert app.state.status == "No columns found for DECISIONS"


def test_generate_sql_with_columns_reports_metadata_failure_without_inserting():
    class FailingColumnsDb:
        def list_object_columns(self, object_name: str) -> list[str]:
            raise RuntimeError("metadata offline")

    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=FailingColumnsDb())
    app.state.buffer = Buffer(lines=[""], row=0, col=0, dirty=False)
    app.prompt = lambda label, default="", strip=True: "decisions"
    app.pick = lambda title, options: (_ for _ in ()).throw(AssertionError("unexpected picker"))

    app.generate_sql_with_columns()
    app.wait_for_db_operation(timeout=1)

    assert app.state.buffer.text() == ""
    assert app.state.buffer.dirty is False
    assert app.state.status == "Column metadata failed: RuntimeError: metadata offline"


def test_generate_sql_with_columns_cancelled_picker_leaves_buffer_unchanged():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=CompletionDb(columns={"DECISIONS": ["ID"]}))
    app.state.buffer = Buffer(lines=[""], row=0, col=0, dirty=False)
    app.prompt = lambda label, default="", strip=True: "decisions"
    app.pick = lambda title, options: None

    app.generate_sql_with_columns()
    app.wait_for_db_operation(timeout=1)

    assert app.state.buffer.text() == ""
    assert app.state.buffer.dirty is False
    assert app.state.status == "SQL generation cancelled"


def test_generate_sql_metadata_switched_tab_caches_without_opening_picker():
    started = ui.threading.Event()
    release = ui.threading.Event()

    class BlockingColumnsDb(CompletionDb):
        def list_object_columns(self, object_name: str) -> list[str]:
            started.set()
            release.wait()
            return super().list_object_columns(object_name)

    db = BlockingColumnsDb(columns={"DECISIONS": ["ID", "NAME"]})
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.state.buffer = Buffer(lines=["select * from decisions"], row=0, col=23, dirty=False)
    app.prompt = lambda label, default="", strip=True: default
    app.pick = lambda title, options: (_ for _ in ()).throw(
        AssertionError("SQL generator picker must not open for stale context")
    )

    app.generate_sql_with_columns()
    assert started.wait(1)
    app.new_tab()
    release.set()
    app.wait_for_db_operation(timeout=1)

    assert app.state.schema_columns == {"DECISIONS": ["ID", "NAME"]}
    assert app.state.active_tab_idx == 1
    assert app.state.buffer.text() == ""
    assert app.state.tabs[0].buffer.text() == "select * from decisions"
    assert app.state.status == "Loaded columns for DECISIONS; retry SQL generation"


def test_shift_tab_without_prefix_does_not_dirty_buffer():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["select "], row=0, col=len("select "), dirty=False)

    app.handle_key(KEY_SHIFT_TAB)

    assert app.state.buffer.text() == "select "
    assert app.state.buffer.dirty is False
    assert app.state.buffer.undo_stack == []
    assert app.state.status == "No completion prefix"


def test_ctrl_u_uppercases_selected_text_and_keeps_selection():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["select one from dual"], row=0, col=10, selection_anchor=(0, 7), dirty=False)

    app.handle_key(CTRL_U)

    assert app.state.buffer.lines == ["select ONE from dual"]
    assert app.state.buffer.selected_text() == "ONE"
    assert app.state.buffer.dirty is True
    assert app.state.status == "Uppercased selection"


def test_ctrl_u_uppercases_code_without_touching_string_literals():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    line = "x := 'Hello';"
    app.state.buffer = Buffer(lines=[line], row=0, col=len(line), selection_anchor=(0, 0), dirty=False)

    app.handle_key(CTRL_U)

    assert app.state.buffer.lines == ["X := 'Hello';"]
    assert app.state.buffer.selected_text() == "X := 'Hello';"
    assert app.state.buffer.dirty is True
    assert app.state.status == "Uppercased selection"


def test_ctrl_l_lowercases_selected_text_without_reconnecting():
    db = ReconnectDb()
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.running = True
    app.state.buffer = Buffer(lines=["SELECT ONE FROM DUAL"], row=0, col=10, selection_anchor=(0, 7), dirty=False)

    app.handle_key(CTRL_L)

    assert app.state.buffer.lines == ["SELECT one FROM DUAL"]
    assert app.state.buffer.selected_text() == "one"
    assert db.closed == 0
    assert db.connected == 0
    assert app.state.status == "Lowercased selection"


def test_ctrl_l_lowercases_code_without_touching_string_literals():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    line = "X := 'HELLO';"
    app.state.buffer = Buffer(lines=[line], row=0, col=len(line), selection_anchor=(0, 0), dirty=False)

    app.handle_key(CTRL_L)

    assert app.state.buffer.lines == ["x := 'HELLO';"]
    assert app.state.buffer.selected_text() == "x := 'HELLO';"
    assert app.state.buffer.dirty is True
    assert app.state.status == "Lowercased selection"


def test_ctrl_u_without_selection_reports_no_selection():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["select one"], row=0, col=6, dirty=False)

    app.handle_key(CTRL_U)

    assert app.state.buffer.text() == "select one"
    assert app.state.buffer.dirty is False
    assert app.state.status == "No selection"


def test_ctrl_l_without_selection_reports_no_selection_and_does_not_reconnect():
    db = ReconnectDb()
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.running = True
    app.state.buffer = Buffer(lines=["select one"], row=0, col=6, dirty=False)

    app.handle_key(CTRL_L)

    assert app.state.buffer.text() == "select one"
    assert db.closed == 0
    assert db.connected == 0
    assert app.state.status == "No selection"


def test_ctrl_equals_reconnects_without_touching_buffer():
    db = ReconnectDb()
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.running = True
    app.state.buffer = Buffer(lines=["select one"], row=0, col=6, dirty=False)

    app.handle_key(KEY_CTRL_EQUALS)
    app.wait_for_db_operation(timeout=1)

    assert app.state.buffer.text() == "select one"
    assert db.closed == 1
    assert db.connected == 1
    assert app.state.status == "Connected as hr"


def test_reconnect_invalidates_editable_and_paged_results_from_old_session():
    db = ReconnectDb()
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=db)
    app.running = True
    result = QueryResult(
        "data",
        ["ROWID", "NAME"],
        [["AAABBBCCC", "old"]],
        "1 row",
        editable_context=EditableResultContext("DECISIONS", 0, {1: "NAME"}),
        continuation=QueryResultContinuation("old-session-cursor"),
    )
    app.state.active_result = result
    app.state.last_result = result
    app.state.focus = FOCUS_RESULTS

    app.reconnect_database()
    app.wait_for_db_operation(timeout=1)

    assert db.closed == 1
    assert db.connected == 1
    assert result.continuation is None
    assert app.state.active_result is None
    assert app.state.last_result is None
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.status == "Connected as hr"


def test_reconnect_commits_pending_transaction_before_replacing_session():
    db = ReconnectDb(pending=True)
    app, result = make_reconnect_app(db)
    prompts: list[tuple[str, str]] = []

    def prompt(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return "c"

    app.prompt = prompt

    app.reconnect_database()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [("Pending transaction: c=commit, r=rollback, d=discard session, x=cancel", "")]
    assert db.calls == ["commit", "close", "connect"]
    assert db.connection is db.reconnected_connection
    assert db.has_uncommitted_changes is False
    assert result.continuation is None
    assert app.state.active_result is None
    assert app.state.last_result is None
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.status == "Connected as hr"


def test_reconnect_rolls_back_pending_transaction_before_replacing_session():
    db = ReconnectDb(pending=True)
    app, result = make_reconnect_app(db)
    prompts: list[tuple[str, str]] = []

    def prompt(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return "r"

    app.prompt = prompt

    app.reconnect_database()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [("Pending transaction: c=commit, r=rollback, d=discard session, x=cancel", "")]
    assert db.calls == ["rollback", "close", "connect"]
    assert db.connection is db.reconnected_connection
    assert db.has_uncommitted_changes is False
    assert result.continuation is None
    assert app.state.active_result is None
    assert app.state.last_result is None
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.status == "Connected as hr"


def test_reconnect_discards_pending_transaction_only_when_explicitly_requested():
    db = ReconnectDb(pending=True)
    app, result = make_reconnect_app(db)
    prompts: list[tuple[str, str]] = []

    def prompt(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return "d"

    app.prompt = prompt

    app.reconnect_database()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [("Pending transaction: c=commit, r=rollback, d=discard session, x=cancel", "")]
    assert db.calls == ["close", "connect"]
    assert db.connection is db.reconnected_connection
    assert db.has_uncommitted_changes is False
    assert result.continuation is None
    assert app.state.active_result is None
    assert app.state.last_result is None
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.status == "Connected as hr"


def test_reconnect_cancel_preserves_session_and_results():
    db = ReconnectDb(pending=True)
    app, result = make_reconnect_app(db)
    continuation = result.continuation
    prompts: list[tuple[str, str]] = []

    def prompt(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return "x"

    app.prompt = prompt

    app.reconnect_database()

    assert prompts == [("Pending transaction: c=commit, r=rollback, d=discard session, x=cancel", "")]
    assert db.calls == []
    assert db.closed == 0
    assert db.connected == 0
    assert db.connection is db.original_connection
    assert db.has_uncommitted_changes is True
    assert result.continuation is continuation
    assert app.state.active_result is result
    assert app.state.last_result is result
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.status == "Reconnect cancelled"


def test_direct_forced_connect_cancel_preserves_pending_session():
    db = ReconnectDb(pending=True)
    app, result = make_reconnect_app(db)
    continuation = result.continuation
    prompts: list[tuple[str, str]] = []

    def prompt(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return "x"

    app.prompt = prompt

    app.try_connect(force=True)

    assert prompts == [("Pending transaction: c=commit, r=rollback, d=discard session, x=cancel", "")]
    assert db.calls == []
    assert db.connection is db.original_connection
    assert db.has_uncommitted_changes is True
    assert result.continuation is continuation
    assert app.state.active_result is result
    assert app.state.last_result is result
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.status == "Reconnect cancelled"


def test_reconnect_commit_failure_preserves_session_and_results():
    db = ReconnectDb(pending=True, failing_action="commit")
    app, result = make_reconnect_app(db)
    continuation = result.continuation
    app.prompt = lambda label, default="", strip=True: "c"

    app.reconnect_database()
    app.wait_for_db_operation(timeout=1)

    assert db.calls == ["commit"]
    assert db.closed == 0
    assert db.connected == 0
    assert db.connection is db.original_connection
    assert db.has_uncommitted_changes is True
    assert result.continuation is continuation
    assert app.state.active_result is result
    assert app.state.last_result is result
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.status == "Commit failed"
    assert app.state.results[0] == "ERROR committing transaction:"


def test_reconnect_rollback_failure_preserves_session_and_results():
    db = ReconnectDb(pending=True, failing_action="rollback")
    app, result = make_reconnect_app(db)
    continuation = result.continuation
    app.prompt = lambda label, default="", strip=True: "r"

    app.reconnect_database()
    app.wait_for_db_operation(timeout=1)

    assert db.calls == ["rollback"]
    assert db.closed == 0
    assert db.connected == 0
    assert db.connection is db.original_connection
    assert db.has_uncommitted_changes is True
    assert result.continuation is continuation
    assert app.state.active_result is result
    assert app.state.last_result is result
    assert app.state.focus == FOCUS_RESULTS
    assert app.state.status == "Rollback failed"
    assert app.state.results[0] == "ERROR rolling back transaction:"


def test_reconnect_can_discard_dead_session_after_commit_failure():
    db = ReconnectDb(pending=True, failing_action="commit_and_close")
    app, result = make_reconnect_app(db)
    prompts: list[tuple[str, str]] = []
    answers = iter(["c", "d"])

    def prompt(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default))
        return next(answers)

    app.prompt = prompt

    app.reconnect_database()
    app.wait_for_db_operation(timeout=1)

    assert db.calls == ["commit"]
    assert db.closed == 0
    assert db.connected == 0
    assert db.connection is db.original_connection
    assert db.has_uncommitted_changes is True
    assert app.state.active_result is result

    app.reconnect_database()
    app.wait_for_db_operation(timeout=1)

    expected_prompt = (
        "Pending transaction: c=commit, r=rollback, d=discard session, x=cancel",
        "",
    )
    assert prompts == [expected_prompt, expected_prompt]
    assert db.calls == ["commit", "close", "connect"]
    assert db.connection is db.reconnected_connection
    assert db.has_uncommitted_changes is False
    assert result.continuation is None
    assert app.state.active_result is None
    assert app.state.last_result is None
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.status.startswith("Connected as hr (warning: old session close failed:")
    assert "already disconnected" in app.state.status


def test_reconnect_keeps_session_results_while_commit_is_in_progress():
    db = BlockingReconnectDb()
    app, result = make_reconnect_app(db)
    continuation = result.continuation
    app.prompt = lambda label, default="", strip=True: "c"

    try:
        app.reconnect_database()
        assert db.commit_started.wait(1)

        assert db.calls == ["commit"]
        assert db.closed == 0
        assert db.connected == 0
        assert db.connection is db.original_connection
        assert db.has_uncommitted_changes is True
        assert result.continuation is continuation
        assert app.state.active_result is result
        assert app.state.last_result is result
        assert app.state.focus == FOCUS_RESULTS

        db.commit_release.set()
        app.wait_for_db_operation(timeout=1)
    finally:
        db.commit_release.set()

    assert db.calls == ["commit", "close", "connect"]
    assert db.connection is db.reconnected_connection
    assert db.has_uncommitted_changes is False
    assert result.continuation is None
    assert app.state.active_result is None
    assert app.state.last_result is None
    assert app.state.focus == FOCUS_EDITOR
    assert app.state.status == "Connected as hr"


def test_reconnect_is_rejected_during_gated_worker_command_then_succeeds_in_order():
    db = ReconnectDb(pending=False)
    app, result = make_reconnect_app(db)
    app._wire()
    entered = ui.threading.Event()
    release = ui.threading.Event()
    completion = []

    def active_command(_db, progress):
        entered.set()
        assert release.wait(1)
        return "finished"

    try:
        assert app.db_operations.start(
            "execute",
            "Executing gated command",
            active_command,
            on_success=completion.append,
        )
        assert entered.wait(1)

        app.reconnect_database()

        assert app.state.status == "Database operation already running"
        assert db.calls == []
        assert db.connection is db.original_connection
        assert result.continuation == QueryResultContinuation("old-session-cursor")
        assert app.state.active_result is result

        release.set()
        app.wait_for_db_operation(timeout=1)
        assert completion == ["finished"]

        app.reconnect_database()
        app.wait_for_db_operation(timeout=1)

        assert db.calls == ["close", "connect"]
        assert db.connection is db.reconnected_connection
        assert result.continuation is None
        assert app.state.active_result is None
        assert app.state.last_result is None
        assert app.state.status == "Connected as hr"
    finally:
        release.set()
        app.shutdown_database_worker(timeout=1)


def test_ctrl_b_toggles_current_line_comment_with_indent():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["  select 1 from dual"], row=0, col=4, dirty=False)

    app.handle_key(CTRL_B)

    assert app.state.buffer.lines == ["  -- select 1 from dual"]
    assert app.state.buffer.dirty is True
    assert app.state.status == "Commented line"

    app.handle_key(CTRL_B)

    assert app.state.buffer.lines == ["  select 1 from dual"]
    assert app.state.status == "Uncommented line"


def test_ctrl_b_toggles_selected_line_block_and_leaves_blank_lines():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(
        lines=["begin", "  null;", "", "end;"],
        row=3,
        col=4,
        selection_anchor=(0, 0),
        dirty=False,
    )

    app.handle_key(CTRL_B)

    assert app.state.buffer.lines == ["-- begin", "  -- null;", "", "-- end;"]
    assert app.state.buffer.selection_range() is None
    assert app.state.status == "Commented 3 lines"

    app.handle_key(CTRL_Z)

    assert app.state.buffer.lines == ["begin", "  null;", "", "end;"]
    assert app.state.buffer.selection_anchor == (0, 0)
    assert app.state.buffer.dirty is False


def test_ctrl_b_uncomments_selected_commented_block_and_skips_final_column_zero_line():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(
        lines=["-- begin", "  -- null;", "end;"],
        row=2,
        col=0,
        selection_anchor=(0, 0),
        dirty=False,
    )

    app.handle_key(CTRL_B)

    assert app.state.buffer.lines == ["begin", "  null;", "end;"]
    assert app.state.status == "Uncommented 2 lines"


def test_ctrl_b_comments_mixed_selected_lines():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["-- begin", "  null;"], row=1, col=7, selection_anchor=(0, 0), dirty=False)

    app.handle_key(CTRL_B)

    assert app.state.buffer.lines == ["-- -- begin", "  -- null;"]
    assert app.state.status == "Commented 2 lines"


def test_ctrl_b_routes_only_in_editor_focus():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["select 1"], row=0, col=0, dirty=False)
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = QueryResult("data", ["A"], [["1"]], "1 row")

    app.handle_key(CTRL_B)

    assert app.state.buffer.lines == ["select 1"]
    assert app.state.status == "Ready"


def test_ctrl_c_copies_selected_result_cell_to_system_clipboard(monkeypatch):
    copied: list[str] = []
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.focus = FOCUS_RESULTS
    value = "Příliš\nžluťouč"
    app.state.active_result = QueryResult(
        "data",
        ["ID", "VALUE"],
        [["1", value]],
        "1 row",
    )
    app.state.result_col = 1
    monkeypatch.setattr(
        ui,
        "copy_to_system_clipboard",
        lambda text: copied.append(text) or "test clipboard",
    )

    app.handle_key(CTRL_C)

    assert copied == [value]
    assert app.state.internal_clipboard == value
    assert (app.state.result_row, app.state.result_col) == (0, 1)
    assert app.state.status == "Copied 14 char(s) to test clipboard"


def test_copy_selected_result_cell_command_falls_back_to_internal_clipboard(monkeypatch):
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.focus = FOCUS_RESULTS
    app.state.result_mode = RESULT_ROW_DETAIL
    app.state.active_result = QueryResult("data", ["VALUE"], [["<NULL>"]], "1 row")
    monkeypatch.setattr(ui, "copy_to_system_clipboard", lambda text: None)
    command = next(item for item in COMMAND_MENU_ITEMS if item.handler == "copy_selected_result_cell")
    app._wire()

    app.dispatcher.execute(command.handler)

    assert app.state.internal_clipboard == "<NULL>"
    assert app.state.status == "Copied 6 char(s) internally"


def test_ctrl_x_cuts_selected_text_to_clipboard(monkeypatch):
    copied: list[str] = []
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["select one from dual"], row=0, col=10, selection_anchor=(0, 7), dirty=False)
    monkeypatch.setattr(ui, "copy_to_system_clipboard", lambda text: copied.append(text) or "test clipboard")

    app.handle_key(CTRL_X)

    assert copied == ["one"]
    assert app.state.internal_clipboard == "one"
    assert app.state.buffer.lines == ["select  from dual"]
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 7)
    assert app.state.buffer.selection_range() is None
    assert app.state.buffer.dirty is True
    assert app.state.status == "Cut 3 char(s) to test clipboard"


def test_ctrl_x_cuts_multiline_utf8_selection_and_undo_restores_it(monkeypatch):
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(
        lines=["select Příliš", "žluťoučký kůň", "from dual"], row=1, col=7, selection_anchor=(0, 7), dirty=False
    )
    monkeypatch.setattr(ui, "copy_to_system_clipboard", lambda text: None)

    app.handle_key(CTRL_X)

    assert app.state.internal_clipboard == "Příliš\nžluťouč"
    assert app.state.buffer.lines == ["select ký kůň", "from dual"]
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 7)
    assert app.state.status == "Cut 14 char(s) internally"

    app.handle_key(CTRL_Z)

    assert app.state.buffer.lines == ["select Příliš", "žluťoučký kůň", "from dual"]
    assert (app.state.buffer.row, app.state.buffer.col) == (1, 7)
    assert app.state.buffer.selected_text() == "Příliš\nžluťouč"
    assert app.state.buffer.dirty is False


def test_ctrl_x_without_selection_leaves_buffer_unchanged(monkeypatch):
    copied: list[str] = []
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.running = True
    app.state.buffer = Buffer(lines=["select 1"], row=0, col=8, dirty=False)
    monkeypatch.setattr(ui, "copy_to_system_clipboard", lambda text: copied.append(text) or "test clipboard")

    app.handle_key(CTRL_X)

    assert copied == []
    assert app.state.internal_clipboard == ""
    assert app.state.buffer.text() == "select 1"
    assert app.state.buffer.dirty is False
    assert app.state.status == "No selection"


def test_buffer_word_movement_single_line_sql_tokens():
    buffer = Buffer(lines=["select a+b from dual"], row=0, col=0)
    buffer.move_word_right()
    assert (buffer.row, buffer.col) == (0, 7)
    buffer.move_word_right()
    assert (buffer.row, buffer.col) == (0, 8)
    buffer.move_word_right()
    assert (buffer.row, buffer.col) == (0, 9)
    buffer.move_word_right()
    assert (buffer.row, buffer.col) == (0, 11)
    buffer.move_word_left()
    assert (buffer.row, buffer.col) == (0, 9)


def test_buffer_word_movement_crosses_lines_and_clears_selection():
    buffer = Buffer(lines=["select", "  from dual"], row=0, col=6, selection_anchor=(0, 0))
    buffer.move_word_right()
    assert (buffer.row, buffer.col) == (1, 2)
    assert buffer.selection_range() is None
    buffer.move_word_left()
    assert (buffer.row, buffer.col) == (0, 0)


def test_buffer_word_movement_handles_utf8_words():
    line = "select žluťoučký from dual"
    buffer = Buffer(lines=[line], row=0, col=line.index("ž"))
    buffer.move_word_right()
    assert (buffer.row, buffer.col) == (0, line.index("from"))


def test_buffer_word_selection_extends_right_left_and_keeps_anchor():
    buffer = Buffer(lines=["select a+b from dual"], row=0, col=7)

    buffer.move_word_right(extend=True)
    assert (buffer.row, buffer.col) == (0, 8)
    assert buffer.selection_anchor == (0, 7)
    assert buffer.selected_text() == "a"

    buffer.move_word_right(extend=True)
    assert (buffer.row, buffer.col) == (0, 9)
    assert buffer.selection_anchor == (0, 7)
    assert buffer.selected_text() == "a+"

    buffer.move_word_left(extend=True)
    assert (buffer.row, buffer.col) == (0, 8)
    assert buffer.selection_anchor == (0, 7)
    assert buffer.selected_text() == "a"


def test_buffer_word_selection_crosses_lines_and_handles_utf8():
    line = "  žluťoučký"
    buffer = Buffer(lines=["select", line, "from dual"], row=0, col=6)

    buffer.move_word_right(extend=True)
    assert (buffer.row, buffer.col) == (1, 2)
    buffer.move_word_right(extend=True)
    assert (buffer.row, buffer.col) == (2, 0)
    assert buffer.selection_anchor == (0, 6)
    assert buffer.selected_text() == "\n  žluťoučký\n"


def test_buffer_plain_word_movement_still_clears_selection():
    buffer = Buffer(lines=["select name from dual"], row=0, col=7, selection_anchor=(0, 0))

    buffer.move_word_right()

    assert (buffer.row, buffer.col) == (0, 12)
    assert buffer.selection_range() is None


def test_buffer_file_start_end_movement_clears_selection():
    buffer = Buffer(lines=["select", "from dual"], row=0, col=2, scroll=1, selection_anchor=(0, 0))
    buffer.move_file_end()
    assert (buffer.row, buffer.col) == (1, len("from dual"))
    assert buffer.selection_range() is None

    buffer.selection_anchor = (1, 0)
    buffer.move_file_start()
    assert (buffer.row, buffer.col, buffer.scroll) == (0, 0, 0)
    assert buffer.selection_range() is None


def test_buffer_plain_page_movement_clears_selection():
    buffer = Buffer(lines=[f"line {idx:02d}" for idx in range(20)], row=5, col=3, selection_anchor=(0, 0))

    buffer.page(10)

    assert (buffer.row, buffer.col) == (15, 3)
    assert buffer.selection_range() is None


def test_buffer_page_selection_extends_by_page_amount():
    lines = [f"line {idx:02d}" for idx in range(20)]
    buffer = Buffer(lines=lines, row=3, col=5)

    buffer.page(10, extend=True)

    assert (buffer.row, buffer.col) == (13, 5)
    assert buffer.selection_anchor == (3, 5)
    expected = "\n".join([lines[3][5:], *lines[4:13], lines[13][:5]])
    assert buffer.selected_text() == expected


def test_buffer_repeated_page_selection_keeps_anchor_and_can_shrink():
    buffer = Buffer(lines=[f"line {idx:02d}" for idx in range(25)], row=5, col=4)

    buffer.page(10, extend=True)
    assert (buffer.row, buffer.col) == (15, 4)
    assert buffer.selection_anchor == (5, 4)
    assert buffer.selection_range() == ((5, 4), (15, 4))

    buffer.page(-10, extend=True)
    assert (buffer.row, buffer.col) == (5, 4)
    assert buffer.selection_anchor == (5, 4)
    assert buffer.selection_range() is None


def test_buffer_page_selection_clamps_and_preserves_valid_column():
    buffer = Buffer(lines=["longer text", "middle", "tiny"], row=0, col=8)

    buffer.page(10, extend=True)

    assert (buffer.row, buffer.col) == (2, len("tiny"))
    assert buffer.selection_anchor == (0, 8)
    assert buffer.selected_text() == "ext\nmiddle\ntiny"


def test_buffer_line_boundary_selection_helpers():
    buffer = Buffer(lines=["select name from dual"], row=0, col=11)

    buffer.move_line_start(extend=True)
    assert (buffer.row, buffer.col) == (0, 0)
    assert buffer.selected_text() == "select name"

    buffer = Buffer(lines=["select name from dual"], row=0, col=7)
    buffer.move_line_end(extend=True)
    assert (buffer.row, buffer.col) == (0, len("select name from dual"))
    assert buffer.selected_text() == "name from dual"


def test_buffer_file_boundary_selection_helpers_keep_anchor():
    buffer = Buffer(lines=["select", "from dual", "where id = 1"], row=1, col=4)

    buffer.move_file_start(extend=True)
    assert (buffer.row, buffer.col) == (0, 0)
    assert buffer.selected_text() == "select\nfrom"

    buffer = Buffer(lines=["select", "from dual", "where id = 1"], row=1, col=4)
    buffer.move_line_end(extend=True)
    buffer.move_file_end(extend=True)
    assert (buffer.row, buffer.col) == (2, len("where id = 1"))
    assert buffer.selection_anchor == (1, 4)
    assert buffer.selected_text() == " dual\nwhere id = 1"


def test_editor_shift_home_end_keys_select_line_boundaries():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=["select name from dual"], row=0, col=11)

    app.edit_key(KEY_SHIFT_HOME)
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 0)
    assert app.state.buffer.selected_text() == "select name"

    app.state.buffer = Buffer(lines=["select name from dual"], row=0, col=7)
    app.edit_key(KEY_SHIFT_END)
    assert (app.state.buffer.row, app.state.buffer.col) == (0, len("select name from dual"))
    assert app.state.buffer.selected_text() == "name from dual"


def test_editor_ctrl_shift_home_end_keys_select_document_boundaries():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=["select", "from dual", "where id = 1"], row=1, col=4)

    app.edit_key(KEY_CTRL_SHIFT_HOME)
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 0)
    assert app.state.buffer.selected_text() == "select\nfrom"

    app.state.buffer = Buffer(lines=["select", "from dual", "where id = 1"], row=1, col=4)
    app.edit_key(KEY_CTRL_SHIFT_END)
    assert (app.state.buffer.row, app.state.buffer.col) == (2, len("where id = 1"))
    assert app.state.buffer.selected_text() == " dual\nwhere id = 1"


def test_editor_ctrl_shift_end_selects_exact_utf8_text_to_file_end():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    start_col = len("Příliš ")
    app.state.buffer = Buffer(lines=["select 1", "Příliš žluťoučký", "from dual"], row=1, col=start_col)

    app.edit_key(KEY_CTRL_SHIFT_END)

    assert (app.state.buffer.row, app.state.buffer.col) == (2, len("from dual"))
    assert app.state.buffer.selection_anchor == (1, start_col)
    assert app.state.buffer.selected_text() == "žluťoučký\nfrom dual"


def test_editor_ctrl_shift_home_selects_exact_utf8_text_to_file_start():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    start_col = len("Příliš")
    app.state.buffer = Buffer(lines=["select 1", "Příliš žluťoučký", "from dual"], row=1, col=start_col)

    app.edit_key(KEY_CTRL_SHIFT_HOME)

    assert (app.state.buffer.row, app.state.buffer.col) == (0, 0)
    assert app.state.buffer.selection_anchor == (1, start_col)
    assert app.state.buffer.selected_text() == "select 1\nPříliš"


def test_editor_ctrl_shift_home_end_reuse_anchor_when_reversing_selection():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=["zero", "one two", "three"], row=1, col=4)

    app.edit_key(KEY_CTRL_SHIFT_END)
    assert (app.state.buffer.row, app.state.buffer.col) == (2, len("three"))
    assert app.state.buffer.selection_anchor == (1, 4)
    assert app.state.buffer.selected_text() == "two\nthree"

    app.edit_key(KEY_CTRL_SHIFT_HOME)
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 0)
    assert app.state.buffer.selection_anchor == (1, 4)
    assert app.state.buffer.selected_text() == "zero\none "


def test_editor_ctrl_shift_home_end_affect_only_active_tab():
    app = ServiceHarness()
    first = FileTab(buffer=Buffer(lines=["first tab"], row=0, col=5, title="first.sql"))
    second = FileTab(buffer=Buffer(lines=["select", "from dual"], row=0, col=3, title="second.sql"))
    app.state = UIState(config=make_config(), db=object(), tabs=[first, second], active_tab_idx=1)

    app.edit_key(KEY_CTRL_SHIFT_END)

    assert second.buffer.selection_anchor == (0, 3)
    assert second.buffer.selected_text() == "ect\nfrom dual"
    assert (first.buffer.row, first.buffer.col) == (0, 5)
    assert first.buffer.selection_range() is None


def test_editor_ctrl_shift_arrow_keys_select_by_word():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=["select name from dual"], row=0, col=7)

    app.edit_key(KEY_CTRL_SHIFT_RIGHT)
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 12)
    assert app.state.buffer.selected_text() == "name "

    app.edit_key(KEY_CTRL_SHIFT_RIGHT)
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 17)
    assert app.state.buffer.selection_anchor == (0, 7)
    assert app.state.buffer.selected_text() == "name from "

    app.edit_key(KEY_CTRL_SHIFT_LEFT)
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 12)
    assert app.state.buffer.selection_anchor == (0, 7)
    assert app.state.buffer.selected_text() == "name "


def test_editor_ctrl_backspace_deletes_previous_word():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=["select name from dual"], row=0, col=7)

    app.edit_key(KEY_CTRL_BACKSPACE)

    assert app.state.buffer.lines == ["name from dual"]
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 0)


def test_editor_ctrl_delete_deletes_next_word():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=["select name from dual"], row=0, col=7)

    app.edit_key(KEY_CTRL_DELETE)

    assert app.state.buffer.lines == ["select from dual"]
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 7)


def test_editor_shift_page_keys_select_by_page():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=[f"line {idx:02d}" for idx in range(20)], row=2, col=4)

    app.edit_key(KEY_SHIFT_PAGEDOWN)

    assert (app.state.buffer.row, app.state.buffer.col) == (12, 4)
    assert app.state.buffer.selection_anchor == (2, 4)
    assert app.state.buffer.selection_range() == ((2, 4), (12, 4))

    app.edit_key(KEY_SHIFT_PAGEUP)

    assert (app.state.buffer.row, app.state.buffer.col) == (2, 4)
    assert app.state.buffer.selection_anchor == (2, 4)
    assert app.state.buffer.selection_range() is None


def test_editor_plain_page_keys_clear_selection():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=[f"line {idx:02d}" for idx in range(20)], row=12, col=4, selection_anchor=(2, 4))

    app.edit_key(curses.KEY_PPAGE)

    assert (app.state.buffer.row, app.state.buffer.col) == (2, 4)
    assert app.state.buffer.selection_range() is None

    app.state.buffer = Buffer(lines=[f"line {idx:02d}" for idx in range(20)], row=2, col=4, selection_anchor=(2, 0))

    app.edit_key(curses.KEY_NPAGE)

    assert (app.state.buffer.row, app.state.buffer.col) == (12, 4)
    assert app.state.buffer.selection_range() is None


def test_ctrl_page_shortcuts_still_switch_tabs_outside_results_focus():
    app = ServiceHarness()
    app.state = UIState(
        config=make_config(),
        db=object(),
        tabs=[
            FileTab(buffer=Buffer(title="one.sql")),
            FileTab(buffer=Buffer(title="two.sql")),
            FileTab(buffer=Buffer(title="three.sql")),
        ],
        active_tab_idx=1,
    )

    app.handle_key(KEY_CTRL_PAGEUP)
    assert app.state.active_tab_idx == 0

    app.handle_key(KEY_CTRL_PAGEDOWN)
    assert app.state.active_tab_idx == 1

    app.state.focus = FOCUS_BROWSER
    app.handle_key(KEY_CTRL_PAGEUP)
    assert app.state.active_tab_idx == 0

    app.handle_key(KEY_CTRL_PAGEDOWN)
    assert app.state.active_tab_idx == 1


def test_ctrl_page_does_not_scroll_dbms_output_with_editor_focus():
    app = ServiceHarness()
    app.screen = FakeScreen(height=12, width=80)
    output_tab = FileTab(
        buffer=Buffer(title="output.sql"),
        dbms_output=[f"line {idx}" for idx in range(8)],
        show_dbms_output=True,
    )
    app.state = UIState(
        config=make_config(),
        db=object(),
        tabs=[FileTab(buffer=Buffer(title="one.sql")), output_tab],
        active_tab_idx=1,
    )
    app.state.focus = FOCUS_EDITOR

    app.handle_key(KEY_CTRL_PAGEUP)

    assert app.state.active_tab_idx == 0
    assert output_tab.dbms_output_scroll is None


def test_ctrl_page_scrolls_dbms_output_by_page_from_tail_and_clamps():
    app = ServiceHarness()
    app.screen = FakeScreen(height=12, width=80)
    app.state = UIState(
        config=make_config(),
        db=object(),
        tabs=[FileTab(buffer=Buffer(title="one.sql")), FileTab(buffer=Buffer(title="output.sql"))],
        active_tab_idx=1,
    )
    app.state.focus = FOCUS_RESULTS
    app.state.dbms_output = [f"line {idx}" for idx in range(8)]
    app.state.show_dbms_output = True

    app.handle_key(KEY_CTRL_PAGEUP)

    assert app.state.active_tab_idx == 1
    assert app.state.active_tab.dbms_output_scroll == 2
    assert app.state.status == "DBMS_OUTPUT: lines 3-5/8"

    app.handle_key(KEY_CTRL_PAGEUP)
    app.handle_key(KEY_CTRL_PAGEUP)
    assert app.state.active_tab.dbms_output_scroll == 0

    app.handle_key(KEY_CTRL_PAGEDOWN)
    app.handle_key(KEY_CTRL_PAGEDOWN)
    app.handle_key(KEY_CTRL_PAGEDOWN)
    assert app.state.active_tab_idx == 1
    assert app.state.active_tab.dbms_output_scroll == 5
    assert app.state.status == "DBMS_OUTPUT: lines 6-8/8"


def test_ctrl_page_scrolls_text_results_by_page_from_tail_and_clamps():
    app = ServiceHarness()
    app.screen = FakeScreen(height=12, width=80)
    app.state = UIState(
        config=make_config(),
        db=object(),
        tabs=[FileTab(buffer=Buffer(title="one.sql")), FileTab(buffer=Buffer(title="results.sql"))],
        active_tab_idx=1,
    )
    app.state.focus = FOCUS_RESULTS
    app.state.results = [f"line {idx}" for idx in range(8)]

    app.handle_key(KEY_CTRL_PAGEUP)

    assert app.state.active_tab_idx == 1
    assert app.state.active_tab.results_scroll == 2
    assert app.state.status == "Results: lines 3-5/8"

    app.handle_key(KEY_CTRL_PAGEUP)
    app.handle_key(KEY_CTRL_PAGEUP)
    assert app.state.active_tab.results_scroll == 0

    app.handle_key(KEY_CTRL_PAGEDOWN)
    app.handle_key(KEY_CTRL_PAGEDOWN)
    app.handle_key(KEY_CTRL_PAGEDOWN)
    assert app.state.active_tab_idx == 1
    assert app.state.active_tab.results_scroll == 5
    assert app.state.status == "Results: lines 6-8/8"


def test_ctrl_page_scrolls_explain_plan_by_page_and_clamps():
    app = ServiceHarness()
    app.screen = FakeScreen()
    app.state = UIState(
        config=make_config(),
        db=object(),
        tabs=[FileTab(buffer=Buffer(title="plan.sql")), FileTab(buffer=Buffer(title="other.sql"))],
    )
    app.state.focus = FOCUS_RESULTS
    app.state.explain_result = ExplainPlanResult("plan", sample_plan_steps(), "ok")
    app.state.explain_page_size = 2

    app.handle_key(KEY_CTRL_PAGEDOWN)

    assert app.state.active_tab_idx == 0
    assert app.state.explain_scroll == 2
    assert app.state.status == "Explain plan: lines 3-4/4"

    app.handle_key(KEY_CTRL_PAGEDOWN)
    assert app.state.explain_scroll == 2

    app.handle_key(KEY_CTRL_PAGEUP)
    app.handle_key(KEY_CTRL_PAGEUP)
    assert app.state.active_tab_idx == 0
    assert app.state.explain_scroll == 0


def test_ctrl_page_scrolls_result_grid_by_page_and_keeps_selection_visible():
    app = ServiceHarness()
    app.screen = FakeScreen()
    app.state = UIState(
        config=make_config(),
        db=object(),
        tabs=[FileTab(buffer=Buffer(title="grid.sql")), FileTab(buffer=Buffer(title="other.sql"))],
    )
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = QueryResult("data", ["A"], [[str(idx)] for idx in range(7)], "7 rows")
    app.state.result_page_size = 2

    app.handle_key(KEY_CTRL_PAGEDOWN)

    assert app.state.active_tab_idx == 0
    assert (app.state.result_row_scroll, app.state.result_row) == (2, 2)

    app.handle_key(KEY_CTRL_PAGEDOWN)
    app.handle_key(KEY_CTRL_PAGEDOWN)
    app.handle_key(KEY_CTRL_PAGEDOWN)
    assert (app.state.result_row_scroll, app.state.result_row) == (5, 5)

    app.handle_key(KEY_CTRL_PAGEUP)
    assert app.state.active_tab_idx == 0
    assert (app.state.result_row_scroll, app.state.result_row) == (3, 4)


def test_ctrl_page_scrolls_result_detail_by_page_and_keeps_selection_visible():
    app = ServiceHarness()
    app.screen = FakeScreen()
    app.state = UIState(
        config=make_config(),
        db=object(),
        tabs=[FileTab(buffer=Buffer(title="detail.sql")), FileTab(buffer=Buffer(title="other.sql"))],
    )
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = QueryResult(
        "data",
        ["A", "B", "C", "D", "E", "F", "G"],
        [["1", "2", "3", "4", "5", "6", "7"]],
        "1 row",
    )
    app.state.result_mode = RESULT_ROW_DETAIL
    app.state.result_page_size = 2

    app.handle_key(KEY_CTRL_PAGEDOWN)

    assert app.state.active_tab_idx == 0
    assert (app.state.result_col_scroll, app.state.result_col) == (2, 2)

    app.handle_key(KEY_CTRL_PAGEDOWN)
    app.handle_key(KEY_CTRL_PAGEDOWN)
    app.handle_key(KEY_CTRL_PAGEDOWN)
    assert (app.state.result_col_scroll, app.state.result_col) == (5, 5)

    app.handle_key(KEY_CTRL_PAGEUP)
    assert app.state.active_tab_idx == 0
    assert (app.state.result_col_scroll, app.state.result_col) == (3, 4)


def test_ctrl_up_down_scrolls_editor_window_without_dirtying_buffer():
    app = ServiceHarness()
    app.screen = FakeScreen(height=24, width=120)
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=[f"line {idx:02d}" for idx in range(30)], row=5, col=4, scroll=2, dirty=False)

    app.handle_key(KEY_CTRL_DOWN)

    assert (app.state.buffer.scroll, app.state.buffer.row, app.state.buffer.col) == (3, 5, 4)
    assert app.state.buffer.dirty is False
    assert app.state.buffer.undo_stack == []

    app.handle_key(KEY_CTRL_UP)

    assert (app.state.buffer.scroll, app.state.buffer.row, app.state.buffer.col) == (2, 5, 4)


def test_ctrl_down_editor_scroll_clamps_cursor_if_it_leaves_window():
    app = ServiceHarness()
    app.screen = FakeScreen(height=24, width=120)
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(
        lines=[f"line {idx:02d}" for idx in range(30)],
        row=2,
        col=4,
        scroll=2,
        dirty=False,
        selection_anchor=(2, 0),
    )

    app.handle_key(KEY_CTRL_DOWN)

    assert (app.state.buffer.scroll, app.state.buffer.row, app.state.buffer.col) == (3, 3, 4)
    assert app.state.buffer.selection_range() is None
    assert app.state.buffer.dirty is False


def test_ctrl_up_down_scrolls_schema_browser_window():
    app = ServiceHarness()
    app.screen = FakeScreen()
    app.state = UIState(config=make_config(), db=object())
    app.state.focus = FOCUS_BROWSER
    app.state.browser_page_size = 3
    app.state.browser_expanded.add("TABLE")
    app.state.browser_objects = {
        "TABLE": ["A", "B", "C", "D"],
        "VIEW": [],
        "PROCEDURE": [],
        "FUNCTION": [],
        "PACKAGE": [],
    }

    app.handle_key(KEY_CTRL_DOWN)

    assert (app.state.browser_scroll, app.state.browser_row) == (1, 1)

    app.handle_key(KEY_CTRL_UP)

    assert (app.state.browser_scroll, app.state.browser_row) == (0, 1)


def test_ctrl_up_down_scrolls_explain_plan_window():
    app = ServiceHarness()
    app.screen = FakeScreen()
    app.state = UIState(config=make_config(), db=object())
    app.state.focus = FOCUS_RESULTS
    app.state.explain_result = ExplainPlanResult("plan", sample_plan_steps(), "ok")
    app.state.explain_page_size = 2

    app.handle_key(KEY_CTRL_DOWN)

    assert app.state.explain_scroll == 1
    assert app.state.status == "Explain plan: lines 2-3/4"

    app.handle_key(KEY_CTRL_UP)

    assert app.state.explain_scroll == 0


def test_ctrl_up_down_scrolls_result_grid_window():
    app = ServiceHarness()
    app.screen = FakeScreen()
    app.state = UIState(config=make_config(), db=object())
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = QueryResult("data", ["A"], [[str(idx)] for idx in range(5)], "5 rows")
    app.state.result_page_size = 2

    app.handle_key(KEY_CTRL_DOWN)

    assert (app.state.result_row_scroll, app.state.result_row) == (1, 1)

    app.handle_key(KEY_CTRL_UP)

    assert (app.state.result_row_scroll, app.state.result_row) == (0, 1)


def test_ctrl_up_down_scrolls_result_detail_window():
    app = ServiceHarness()
    app.screen = FakeScreen()
    app.state = UIState(config=make_config(), db=object())
    app.state.focus = FOCUS_RESULTS
    app.state.active_result = QueryResult("data", ["A", "B", "C", "D"], [["1", "2", "3", "4"]], "1 row")
    app.state.result_mode = RESULT_ROW_DETAIL
    app.state.result_page_size = 2

    app.handle_key(KEY_CTRL_DOWN)

    assert (app.state.result_col_scroll, app.state.result_col) == (1, 1)

    app.handle_key(KEY_CTRL_UP)

    assert (app.state.result_col_scroll, app.state.result_col) == (0, 1)


def test_ctrl_up_scrolls_text_results_from_tail_position():
    app = ServiceHarness()
    app.screen = FakeScreen(height=12, width=80)
    app.state = UIState(config=make_config(), db=object())
    app.state.focus = FOCUS_RESULTS
    app.state.results = [f"line {idx}" for idx in range(6)]

    app.handle_key(KEY_CTRL_UP)

    assert app.state.active_tab.results_scroll == 2
    assert app.state.status == "Results: lines 3-5/6"


def test_ctrl_up_scrolls_dbms_output_from_tail_position():
    app = ServiceHarness()
    app.screen = FakeScreen(height=12, width=80)
    app.state = UIState(config=make_config(), db=object())
    app.state.focus = FOCUS_RESULTS
    app.state.dbms_output = [f"line {idx}" for idx in range(6)]
    app.state.show_dbms_output = True

    app.handle_key(KEY_CTRL_UP)

    assert app.state.active_tab.dbms_output_scroll == 2
    assert app.state.status == "DBMS_OUTPUT: lines 3-5/6"


def test_ctrl_up_scrolls_visible_dbms_output_when_editor_has_focus():
    app = ServiceHarness()
    app.screen = FakeScreen(height=12, width=80)
    app.state = UIState(config=make_config(), db=object())
    app.state.focus = FOCUS_EDITOR
    app.state.buffer = Buffer(lines=[f"editor {idx}" for idx in range(20)], row=5, scroll=4)
    app.state.dbms_output = [f"line {idx}" for idx in range(6)]
    app.state.show_dbms_output = True

    app.handle_key(KEY_CTRL_UP)

    assert app.state.active_tab.dbms_output_scroll == 2
    assert app.state.buffer.scroll == 4
    assert app.state.status == "DBMS_OUTPUT: lines 3-5/6"


def test_editor_plain_home_end_and_ctrl_home_end_clear_selection():
    app = ServiceHarness()
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=["select", "from dual"], row=1, col=4, selection_anchor=(0, 1))

    app.edit_key(curses.KEY_HOME)
    assert (app.state.buffer.row, app.state.buffer.col) == (1, 0)
    assert app.state.buffer.selection_range() is None

    app.state.buffer = Buffer(lines=["select", "from dual"], row=0, col=2, selection_anchor=(0, 0))
    app.edit_key(curses.KEY_END)
    assert (app.state.buffer.row, app.state.buffer.col) == (0, len("select"))
    assert app.state.buffer.selection_range() is None

    app.state.buffer = Buffer(lines=["select", "from dual"], row=1, col=4, selection_anchor=(0, 0))
    app.edit_key(KEY_CTRL_HOME)
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 0)
    assert app.state.buffer.selection_range() is None

    app.state.buffer = Buffer(lines=["select", "from dual"], row=0, col=2, selection_anchor=(0, 0))
    app.edit_key(KEY_CTRL_END)
    assert (app.state.buffer.row, app.state.buffer.col) == (1, len("from dual"))
    assert app.state.buffer.selection_range() is None


def test_parse_error_locations_from_oracle_errors():
    text = "ORA-06550: line 3, column 7:\nPLS-00103: bad\nORA-06512: at line 5"
    assert parse_error_locations(text) == [ErrorLocation(3, 7), ErrorLocation(5, 1)]


def test_parse_error_locations_from_sqlerrm_diagnostic_and_named_backtrace():
    text = 'Error raised in: SCOTT.PKG_RUN at line 42 - ORA-20000: boom\nORA-06512: at "SCOTT.PKG_RUN", line 17'

    assert parse_error_locations(text) == [ErrorLocation(42, 1), ErrorLocation(17, 1)]
    diagnostics = ui.parse_plsql_diagnostics(text)
    assert diagnostics[0].unit == "SCOTT.PKG_RUN"
    assert diagnostics[0].line == 42
    assert diagnostics[0].message == "ORA-20000: boom"


def test_first_document_error_location_maps_statement_offset_and_earliest_line():
    exc = RuntimeError("ORA-06550: line 4, column 2:\nORA-06550: line 2, column 9:")
    assert first_document_error_location(exc, statement_start_line=10) == ErrorLocation(11, 9)


def test_first_document_error_location_uses_ora_06512_fallback():
    exc = RuntimeError("ORA-06512: at line 5")
    assert first_document_error_location(exc, statement_start_line=3) == ErrorLocation(7, 1)


def test_first_document_error_location_ignores_named_external_units():
    exc = RuntimeError('ORA-06512: at "SCOTT.PKG_RUN", line 17\nORA-06512: at line 3')

    assert first_document_error_location(exc, statement_start_line=8) == ErrorLocation(10, 1)
    assert (
        first_document_error_location(
            RuntimeError('ORA-06512: at "SCOTT.PKG_RUN", line 17'),
            statement_start_line=8,
        )
        is None
    )


def test_first_document_error_location_returns_none_without_location():
    assert first_document_error_location(RuntimeError("ORA-00942: table or view does not exist")) is None


def test_first_document_error_location_uses_captured_dbms_output_diagnostic():
    exc = OracleExecutionError(
        RuntimeError("ORA-20000: boom"),
        "Block",
        ["Error raised in: <anonymous> at line 3 - ORA-20000: boom"],
    )

    assert first_document_error_location(exc, statement_start_line=8) == ErrorLocation(10, 1)


def test_first_document_error_location_prefers_exact_column_over_dbms_output_fallback():
    exc = OracleExecutionError(
        RuntimeError("ORA-06550: line 4, column 2:\nPLS-00103: bad"),
        "Block",
        ["Error raised in: <anonymous> at line 2 - ORA-20000: boom"],
    )

    assert first_document_error_location(exc, statement_start_line=10) == ErrorLocation(13, 2)


def test_first_document_error_location_uses_oracle_sql_offset_and_statement_column():
    exc = OracleExecutionError(
        FakeOracleOffsetError(FakeOracleOffsetInfo(len("select * "))),
        "SQL",
        statement="select * frm dual",
    )

    assert first_document_error_location(exc, statement_start_line=4, statement_start_col=2) == ErrorLocation(4, 12)


def test_first_document_error_location_maps_multiline_oracle_sql_offset():
    statement = "select\n    * frm dual"
    exc = OracleExecutionError(
        FakeOracleOffsetError(FakeOracleOffsetInfo(len("select\n    * "))),
        "SQL",
        statement=statement,
    )

    assert first_document_error_location(exc, statement_start_line=7, statement_start_col=9) == ErrorLocation(8, 7)


def test_first_document_error_location_prefers_line_column_over_oracle_sql_offset():
    exc = OracleExecutionError(
        FakeOracleOffsetError(FakeOracleOffsetInfo(1, "ORA-06550: line 2, column 3:\nPLS-00103: bad")),
        "Block",
        statement="begin\n  bad;\nend;",
    )

    assert first_document_error_location(exc, statement_start_line=10) == ErrorLocation(11, 3)


def test_execution_error_lines_include_diagnostics_and_dbms_output():
    exc = OracleExecutionError(
        RuntimeError("ORA-20000: boom"),
        "Block",
        ["Error raised in: PKG.RUN at line 3 - ORA-20000: boom"],
    )

    lines = execution_error_lines(exc)

    assert "Oracle error:" in lines
    assert "Diagnostics:" in lines
    assert "DBMS_OUTPUT:" in lines
    assert execution_error_diagnostics(exc)[0].message == "ORA-20000: boom"
    assert any("PKG.RUN line 3: ORA-20000: boom" in line for line in lines)


def test_execution_error_lines_include_non_masking_cleanup_warnings():
    exc = OracleExecutionError(
        RuntimeError("ORA-20000: primary failure"),
        "Block",
        warnings=["Statement cursor cleanup failed: close failed"],
    )

    lines = execution_error_lines(exc)

    assert "Warnings:" in lines
    assert "Statement cursor cleanup failed: close failed" in lines


def test_compilation_error_location_prefers_error_over_earlier_warning():
    compilation_error = OracleCompilationError(
        PlsqlObject("PROCEDURE", "BROKEN"),
        [
            PlsqlCompileDiagnostic(2, 3, "PLW-06002: warning", "WARNING"),
            PlsqlCompileDiagnostic(4, 5, "PLS-00103: error", "ERROR"),
        ],
    )
    exc = OracleExecutionError(compilation_error, "Compile")

    assert first_document_error_location(exc, statement_start_line=10) == ErrorLocation(
        13,
        5,
    )


def test_move_buffer_to_error_clamps_column_and_clears_selection():
    buffer = Buffer(lines=["select", "from dual"], row=0, col=2, selection_anchor=(0, 0))
    moved = move_buffer_to_error(buffer, ErrorLocation(2, 99))
    assert moved == ErrorLocation(2, len("from dual") + 1)
    assert (buffer.row, buffer.col) == (1, len("from dual"))
    assert buffer.selection_range() is None


def test_run_script_failure_moves_to_failing_statement_and_keeps_prior_results():
    app = ServiceHarness()
    db = FailingScriptDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=db)
    app.state.buffer = Buffer(lines=["select 1 from dual;", "begin", "  bad;", "end;", "/"], row=0, col=0)

    app.run_script()
    app.wait_for_db_operation(timeout=1)

    assert db.titles == ["Statement 1 lines 1-1", "Statement 2 lines 2-4"]
    assert (app.state.buffer.row, app.state.buffer.col) == (2, 2)
    assert app.state.status.startswith("Execution failed at line 3, column 3")
    assert "stopped at 2/2" in app.state.status
    assert "Error location: line 3, column 3" in app.state.results
    assert any("[Statement 1 lines 1-1] 1 row" in line for line in app.state.results)
    assert any("ERROR executing statement:" in line for line in app.state.results)


def test_run_script_status_reports_statement_progress_and_completion_count():
    app = ServiceHarness()
    db = RecordingDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=db)
    app.state.buffer = Buffer(
        lines=["select 1 from dual;", "select 2 from dual;"],
        row=0,
        col=0,
    )

    app.run_script()

    assert app.state.db_operation is not None
    assert "1/2" in app.state.db_operation.label
    app.wait_for_db_operation(timeout=1)
    assert app.state.status == "Completed 2/2: ok"


def test_changed_source_keeps_error_cursor_in_place_and_marks_diagnostics_stale():
    started = ui.threading.Event()
    release = ui.threading.Event()

    class BlockingFailureDb:
        def execute_statement(self, statement: str, title: str = "Statement") -> QueryResult:
            started.set()
            assert release.wait(1)
            raise RuntimeError("ORA-06550: line 1, column 3: bad source")

    app = ServiceHarness()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=BlockingFailureDb())
    app.state.buffer = Buffer(lines=["select bad from dual;"], row=0, col=0)

    app.run_current_statement()
    assert started.wait(1)
    app.state.buffer.lines.append("-- edited while running")
    app.state.buffer.row = 1
    app.state.buffer.col = 4
    release.set()
    app.wait_for_db_operation(timeout=1)

    assert (app.state.buffer.row, app.state.buffer.col) == (1, 4)
    assert "Source changed since execution started" in app.state.status
    assert any("earlier buffer revision" in line for line in app.state.results)
    assert len(app.state.active_tab.execution_diagnostics) == 1

    app.next_execution_diagnostic()

    assert (app.state.buffer.row, app.state.buffer.col) == (1, 4)
    assert "diagnostics are stale" in app.state.status


def test_execution_diagnostic_commands_cycle_local_locations_and_success_clears_them():
    app = ServiceHarness()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=["one", "two", "three", "four"], row=0, col=0)

    app.handle_execution_error(RuntimeError("ORA-06550: line 2, column 3: first\nORA-06550: line 4, column 2: second"))

    assert [(item.line, item.column) for item in app.state.active_tab.execution_diagnostics] == [
        (2, 3),
        (4, 2),
    ]
    assert [item.message for item in app.state.active_tab.execution_diagnostics] == ["first", "second"]
    assert (app.state.buffer.row, app.state.buffer.col) == (1, 2)

    app.next_execution_diagnostic()
    assert (app.state.buffer.row, app.state.buffer.col) == (3, 1)
    assert app.state.status.startswith("Diagnostic 2/2")
    assert app.state.status.endswith(": second")

    app.previous_execution_diagnostic()
    assert (app.state.buffer.row, app.state.buffer.col) == (1, 2)
    assert app.state.status.startswith("Diagnostic 1/2")
    assert app.state.status.endswith(": first")

    app.finish_execution([QueryResult("Statement", [], [], "ok")])
    assert app.state.active_tab.execution_diagnostics == []


def test_structured_compile_error_navigation_preserves_each_message():
    app = ServiceHarness()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=["one", "two", "three", "four"])
    compilation_error = OracleCompilationError(
        PlsqlObject("PROCEDURE", "BROKEN"),
        [
            PlsqlCompileDiagnostic(2, 3, "first compile error", "ERROR"),
            PlsqlCompileDiagnostic(4, 2, "second compile error", "ERROR"),
        ],
    )

    app.handle_execution_error(OracleExecutionError(compilation_error, "Compile", statement="create procedure"))

    assert [item.message for item in app.state.active_tab.execution_diagnostics] == [
        "Error: first compile error",
        "Error: second compile error",
    ]
    app.next_execution_diagnostic()
    assert app.state.status.endswith(": Error: second compile error")


def test_same_location_compile_error_message_beats_warning_message():
    app = ServiceHarness()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=["one", "two", "three"])
    compilation_error = OracleCompilationError(
        PlsqlObject("PROCEDURE", "BROKEN"),
        [
            PlsqlCompileDiagnostic(2, 3, "fatal compile error", "ERROR"),
            PlsqlCompileDiagnostic(2, 3, "secondary warning", "WARNING"),
        ],
    )

    app.handle_execution_error(OracleExecutionError(compilation_error, "Compile", statement="create procedure"))

    assert [item.message for item in app.state.active_tab.execution_diagnostics] == ["Error: fatal compile error"]


def test_successful_compile_diagnostics_are_rendered_and_navigable():
    class CompileWarning:
        line = 2
        position = 4
        text = "PLW-06009: procedure should raise"
        severity = "warning"

    app = ServiceHarness()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=object())
    app.state.buffer = Buffer(lines=[f"line {idx}" for idx in range(1, 10)], row=0, col=0)
    source_text = app.state.buffer.text()
    result = QueryResult("Statement 2 lines 5-8", [], [], "compiled with warnings")
    result.diagnostics = [CompileWarning()]

    app.finish_execution([result], source_text=source_text)

    assert any("Warning at line 2, column 4" in line for line in app.state.results)
    assert [(item.line, item.column) for item in app.state.active_tab.execution_diagnostics] == [(6, 4)]
    app.next_execution_diagnostic()
    assert (app.state.buffer.row, app.state.buffer.col) == (5, 3)


def test_selected_compile_warning_keeps_same_line_statement_column_origin():
    class CompileWarningDb:
        def execute_statement(
            self,
            statement: str,
            title: str = "Statement",
        ) -> QueryResult:
            result = QueryResult(title, [], [], "compiled with warnings")
            result.diagnostics = [PlsqlCompileDiagnostic(1, 3, "PLW-06002: warning", "WARNING")]
            return result

    app = ServiceHarness()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=CompileWarningDb())
    app.state.buffer = Buffer(lines=["skip: create procedure p as begin null; end;"])

    app.run_selected_script(
        "create procedure p as begin null; end;",
        first_line=1,
        first_col=6,
    )
    app.wait_for_db_operation(timeout=1)

    assert [(diagnostic.line, diagnostic.column) for diagnostic in app.state.active_tab.execution_diagnostics] == [
        (1, 9)
    ]


def test_run_script_executes_selected_sql_only_with_document_line_titles():
    app = ServiceHarness()
    db = RecordingDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=db)
    second = "select 2 from dual;"
    app.state.buffer = Buffer(
        lines=["select skip from dual;", "select 1 from dual;", second, "select after from dual;"],
        row=2,
        col=len(second),
        selection_anchor=(1, 0),
    )

    app.run_script()
    app.wait_for_db_operation(timeout=1)

    assert db.statements == ["select 1 from dual", "select 2 from dual"]
    assert db.titles == ["Selection 1 lines 2-2", "Selection 2 lines 3-3"]


def test_run_script_preflight_rejects_all_client_syntax_before_binds_or_db():
    app = ServiceHarness()
    db = RecordingDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=db)
    app.state.buffer = Buffer(
        lines=[
            "set serveroutput on",
            "select :id from dual;",
            "prompt finished",
            "select &name from dual;",
        ],
        row=0,
        col=0,
    )
    prompts: list[str] = []
    app.prompt_text_box = lambda label, default="", strip=True: prompts.append(label)

    app.run_script()

    assert prompts == []
    assert db.statements == []
    assert app.state.db_operation is None
    assert (app.state.buffer.row, app.state.buffer.col) == (0, 0)
    assert app.state.status.startswith("Execution blocked at line 1, column 1")
    assert any("line 3, column 1" in line for line in app.state.results)
    assert any("line 4, column 8" in line for line in app.state.results)


def test_run_script_prompts_once_per_bind_and_filters_values_per_statement():
    app = ServiceHarness()
    db = RecordingDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=db)
    app.state.buffer = Buffer(
        lines=[
            "select * from decisions where id = :id;",
            "select * from users where name = :name;",
            "select * from audit_log where decision_id = :id;",
        ],
        row=0,
        col=0,
    )
    prompts: list[tuple[str, str, bool]] = []
    answers = iter(["42", "Ada"])

    def prompt_text_box(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default, strip))
        return next(answers)

    app.prompt_text_box = prompt_text_box

    app.run_script()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [
        ("Value for :id", "", False),
        ("Value for :name", "", False),
    ]
    assert db.bind_values == [{"id": "42"}, {"name": "Ada"}, {"id": "42"}]


def test_run_script_prompts_once_for_unquoted_bind_case_variants():
    app = ServiceHarness()
    db = RecordingDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=db)
    app.state.buffer = Buffer(
        lines=[
            "select :id from dual;",
            "select :ID from dual;",
        ],
        row=0,
        col=0,
    )
    prompts: list[tuple[str, str, bool]] = []

    def prompt_text_box(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default, strip))
        return "42"

    app.prompt_text_box = prompt_text_box

    app.run_script()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [("Value for :id", "", False)]
    assert db.bind_values == [{"id": "42"}, {"ID": "42"}]


def test_run_current_statement_keeps_quoted_bind_names_case_sensitive():
    app = ServiceHarness()
    db = RecordingDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=db)
    statement = 'select :"Mixed Name", :"mixed name" from dual'
    app.state.buffer = Buffer(lines=[statement], row=0, col=0)
    prompts: list[tuple[str, str, bool]] = []
    answers = iter(["first", "second"])

    def prompt_text_box(label: str, default: str = "", strip: bool = True) -> str:
        prompts.append((label, default, strip))
        return next(answers)

    app.prompt_text_box = prompt_text_box

    app.run_current_statement()
    app.wait_for_db_operation(timeout=1)

    assert prompts == [
        ('Value for :"Mixed Name"', "", False),
        ('Value for :"mixed name"', "", False),
    ]
    assert db.bind_values == [{'"Mixed Name"': "first", '"mixed name"': "second"}]


def test_selected_script_failure_moves_to_original_document_line():
    app = ServiceHarness()
    db = FailingScriptDb()
    app.screen = FakeScreen()
    app.draw_offset_x = 0
    app.state = UIState(config=make_config(), db=db)
    app.state.buffer = Buffer(
        lines=["select skip from dual;", "select 1 from dual;", "begin", "  bad;", "end;", "/"],
        row=5,
        col=1,
        selection_anchor=(1, 0),
    )

    app.run_script()
    app.wait_for_db_operation(timeout=1)

    assert db.titles == ["Selection 1 lines 2-2", "Selection 2 lines 3-5"]
    assert (app.state.buffer.row, app.state.buffer.col) == (3, 2)
    assert app.state.status.startswith("Execution failed at line 4, column 3")
    assert any("[Selection 1 lines 2-2] 1 row" in line for line in app.state.results)


def test_editor_line_segments_mark_utf8_selection():
    segments = editor_line_segments("kůň表", 0, ((0, 1), (0, 3)))
    assert segments == [("k", False), ("ůň", True), ("表", False)]


def test_editor_line_segments_marks_empty_multiline_selection_edge():
    segments = editor_line_segments("", 1, ((0, 3), (2, 0)))
    assert segments == [("", True)]


def test_tokenize_sql_line_highlights_keywords_case_insensitively():
    assert tokenize_sql_line("SeLeCt name from dual") == [
        SyntaxToken("SeLeCt", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("name", SYNTAX_DEFAULT),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("from", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("dual", SYNTAX_DEFAULT),
    ]


def test_tokenize_sql_line_highlights_left_and_right_join_keywords():
    assert tokenize_sql_line("LeFt join departments RIGHT join jobs") == [
        SyntaxToken("LeFt", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("join", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("departments", SYNTAX_DEFAULT),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("RIGHT", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("join", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("jobs", SYNTAX_DEFAULT),
    ]


def test_tokenize_sql_line_strings_and_quoted_identifiers():
    assert tokenize_sql_line("select 'it''s ok', \"Mixed Name\" from dual") == [
        SyntaxToken("select", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("'it''s ok'", SYNTAX_STRING),
        SyntaxToken(",", SYNTAX_OPERATOR),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken('"Mixed Name"', SYNTAX_STRING),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("from", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("dual", SYNTAX_DEFAULT),
    ]


def test_tokenize_sql_line_comments_binds_and_numbers():
    assert tokenize_sql_line("where id = :id and score >= 10.5 -- comment") == [
        SyntaxToken("where", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("id", SYNTAX_DEFAULT),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("=", SYNTAX_OPERATOR),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken(":id", SYNTAX_BIND),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("and", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("score", SYNTAX_DEFAULT),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken(">", SYNTAX_OPERATOR),
        SyntaxToken("=", SYNTAX_OPERATOR),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("10.5", SYNTAX_NUMBER),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("-- comment", SYNTAX_COMMENT),
    ]


def test_tokenize_sql_line_block_comment_and_utf8_identifier():
    assert tokenize_sql_line("select kůň /* poznámka */ from dual") == [
        SyntaxToken("select", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("kůň", SYNTAX_DEFAULT),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("/* poznámka */", SYNTAX_COMMENT),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("from", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("dual", SYNTAX_DEFAULT),
    ]


def test_tokenize_sql_line_highlights_plsql_keywords_types_and_attributes():
    tokens = tokenize_sql_line("pragma autonomous_transaction; l_id employees.employee_id%type;")

    assert SyntaxToken("pragma", SYNTAX_KEYWORD) in tokens
    assert SyntaxToken("autonomous_transaction", SYNTAX_KEYWORD) in tokens
    assert SyntaxToken("%type", SYNTAX_KEYWORD) in tokens


def test_tokenize_sql_line_highlights_plsql_binds_and_inquiry_directives():
    tokens = tokenize_sql_line("dbms_output.put_line($$plsql_unit || :new.id || :old.id);")

    assert SyntaxToken("$$plsql_unit", SYNTAX_BIND) in tokens
    assert SyntaxToken(":new", SYNTAX_BIND) in tokens
    assert SyntaxToken(":old", SYNTAX_BIND) in tokens


def test_tokenize_sql_lines_carries_multiline_block_comment_state():
    tokens = tokenize_sql_lines(["select /* open", "still comment", "done */ from dual"])

    assert tokens[0][-1] == SyntaxToken("/* open", SYNTAX_COMMENT)
    assert tokens[1] == [SyntaxToken("still comment", SYNTAX_COMMENT)]
    assert tokens[2][:5] == [
        SyntaxToken("done */", SYNTAX_COMMENT),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("from", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("dual", SYNTAX_DEFAULT),
    ]


def test_tokenize_sql_lines_carries_multiline_q_quote_state():
    tokens = tokenize_sql_lines(["begin", "  v_text := q'[hello", "world]';", "end;"])

    assert SyntaxToken("begin", SYNTAX_KEYWORD) in tokens[0]
    assert tokens[1][-1] == SyntaxToken("q'[hello", SYNTAX_STRING)
    assert tokens[2] == [SyntaxToken("world]'", SYNTAX_STRING), SyntaxToken(";", SYNTAX_OPERATOR)]
    assert tokens[3] == [SyntaxToken("end", SYNTAX_KEYWORD), SyntaxToken(";", SYNTAX_OPERATOR)]


def test_transform_sql_code_in_selection_preserves_non_code_tokens():
    line = "SELECT \"Mixed Name\", 'HELLO', q'[WORLD]' FROM DUAL -- KEEP"

    assert transform_sql_code_in_selection([line], ((0, 0), (0, len(line))), str.lower) == (
        "select \"Mixed Name\", 'HELLO', q'[WORLD]' from dual -- KEEP"
    )


def test_transform_sql_code_in_selection_uppercasing_preserves_non_code_tokens():
    line = "select \"Mixed Name\", 'Hello', q'[World]' from dual -- keep"

    assert transform_sql_code_in_selection([line], ((0, 0), (0, len(line))), str.upper) == (
        "SELECT \"Mixed Name\", 'Hello', q'[World]' FROM DUAL -- keep"
    )


def test_transform_sql_code_in_selection_carries_multiline_string_state():
    lines = ["BEGIN", "  V_TEXT := q'[HELLO", "WORLD]';", "  X := 'BYE';", "END;"]

    assert transform_sql_code_in_selection(lines, ((0, 0), (4, len(lines[4]))), str.lower) == (
        "begin\n  v_text := q'[HELLO\nWORLD]';\n  x := 'BYE';\nend;"
    )


def test_tokenize_sql_line_highlights_national_and_custom_q_strings():
    assert tokenize_sql_line("select n'ok', nq'{fine}' from dual") == [
        SyntaxToken("select", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("n'ok'", SYNTAX_STRING),
        SyntaxToken(",", SYNTAX_OPERATOR),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("nq'{fine}'", SYNTAX_STRING),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("from", SYNTAX_KEYWORD),
        SyntaxToken(" ", SYNTAX_DEFAULT),
        SyntaxToken("dual", SYNTAX_DEFAULT),
    ]


def test_find_matching_bracket_positions_uses_cursor_or_previous_bracket():
    line = "select func(a, (b + c)) from dual"
    lines = [line]
    tokens = tokenize_sql_lines(lines)
    outer_open = line.index("(")
    outer_close = line.rindex(")")
    inner_open = line.index("(b")
    inner_close = line.index(")", inner_open)

    assert find_matching_bracket_positions(lines, tokens, 0, outer_open + 1) == {(0, outer_open), (0, outer_close)}
    assert find_matching_bracket_positions(lines, tokens, 0, inner_close) == {(0, inner_open), (0, inner_close)}


def test_find_matching_bracket_positions_ignores_strings_and_comments():
    line = "select '(' as text, (a) -- )"
    lines = [line]
    tokens = tokenize_sql_lines(lines)
    string_open = line.index("(")
    real_open = line.index("(a)")
    real_close = line.index(")", real_open)

    assert find_matching_bracket_positions(lines, tokens, 0, string_open) == set()
    assert find_matching_bracket_positions(lines, tokens, 0, real_open) == {(0, real_open), (0, real_close)}


def test_syntax_line_segments_keeps_brackets_as_regular_syntax_segments():
    segments = syntax_line_segments("select (x)", 0, None)

    assert segments == [
        SyntaxSegment("select", SYNTAX_KEYWORD, False),
        SyntaxSegment(" ", SYNTAX_DEFAULT, False),
        SyntaxSegment("(", SYNTAX_OPERATOR, False),
        SyntaxSegment("x", SYNTAX_DEFAULT, False),
        SyntaxSegment(")", SYNTAX_OPERATOR, False),
    ]


def test_syntax_line_segments_selection_overrides_syntax_class():
    segments = syntax_line_segments("select name", 0, ((0, 2), (0, 8)))
    assert segments == [
        SyntaxSegment("se", SYNTAX_KEYWORD, False),
        SyntaxSegment("lect", SYNTAX_KEYWORD, True),
        SyntaxSegment(" n", SYNTAX_DEFAULT, True),
        SyntaxSegment("ame", SYNTAX_DEFAULT, False),
    ]


def test_clipboard_copy_uses_first_successful_provider():
    seen: list[str] = []
    providers = [
        ClipboardProvider("bad", copy=lambda text: False),
        ClipboardProvider("ok", copy=lambda text: seen.append(text) is None),
    ]
    assert copy_to_system_clipboard("select 1", providers) == "ok"
    assert seen == ["select 1"]


def test_clipboard_paste_prefers_system_and_normalizes_newlines():
    providers = [ClipboardProvider("system", paste=lambda: "a\r\nb\r")]
    assert paste_from_clipboard("internal", providers) == ("a\nb\n", "system")


def test_clipboard_paste_falls_back_to_internal_clipboard():
    providers = [ClipboardProvider("empty", paste=lambda: "")]
    assert paste_from_clipboard("saved\r\ntext", providers) == ("saved\ntext", "internal clipboard")


def test_normalize_clipboard_text():
    assert normalize_clipboard_text("a\r\nb\rc") == "a\nb\nc"


def test_browser_entries_flatten_collapsed_and_expanded_groups():
    objects = {
        "TABLE": ["DECISIONS", "PROJECTS"],
        "VIEW": ["ACTIVE_PROJECTS"],
        "PROCEDURE": [],
        "FUNCTION": [],
        "PACKAGE": ["DEMO_PKG"],
    }
    collapsed = flatten_browser_entries(objects, set())
    assert collapsed[:2] == [
        BrowserEntry("group", "Tables (2)", "TABLE"),
        BrowserEntry("group", "Views (1)", "VIEW"),
    ]

    expanded = flatten_browser_entries(objects, {"TABLE"})
    assert expanded[:4] == [
        BrowserEntry("group", "Tables (2)", "TABLE"),
        BrowserEntry("object", "DECISIONS", "TABLE", "DECISIONS"),
        BrowserEntry("object", "PROJECTS", "TABLE", "PROJECTS"),
        BrowserEntry("group", "Views (1)", "VIEW"),
    ]


def test_browser_row_clamping_and_labels():
    entries = [BrowserEntry("group", "Tables (1)", "TABLE"), BrowserEntry("object", "DECISIONS", "TABLE", "DECISIONS")]
    assert clamp_browser_row(-10, entries) == 0
    assert clamp_browser_row(10, entries) == 1
    assert clamp_browser_row(10, []) == 0
    assert browser_entry_text(entries[0], set()) == "[+] Tables (1)"
    assert browser_entry_text(entries[0], {"TABLE"}) == "[-] Tables (1)"
    assert browser_entry_text(entries[1], set()) == "    DECISIONS"


def test_schema_object_title_and_browser_panel_width():
    assert schema_object_title("hr", "table", "DECISIONS") == "schema://HR/TABLE/DECISIONS.sql"
    assert schema_object_title("hr", "table", "Mixed/Table") == ("schema://HR/TABLE/Mixed%2FTable.sql")
    assert browser_panel_width(120) == 30
    assert browser_panel_width(200) == 38


def test_file_tab_labels_and_visible_tab_numbers_are_width_aware():
    tabs = [
        FileTab(buffer=Buffer(title="one.sql")),
        FileTab(buffer=Buffer(title="dva_žluťoučký.sql", dirty=True)),
        FileTab(buffer=Buffer(title="three.sql")),
    ]

    assert tab_display_title(tabs[0]) == "one.sql"
    assert format_tab_label(tabs[1]) == "dva_žluťoučký.sql*"
    assert clamp_tab_index(-1, tabs) == 0
    assert clamp_tab_index(99, tabs) == 2

    visible = visible_tab_labels(tabs, 0, 24)
    assert visible[0] == (0, 1, "one.sql")
    assert visible[1][0:2] == (1, 2)
    assert visible_tab_labels(tabs, 2, 80)[0] == (2, 1, "three.sql")


def test_ui_state_ensures_tab_and_clamps_active_tab_index():
    state = UIState(config=make_config(), db=object(), tabs=[], active_tab_idx=99, tab_scroll=99)

    assert len(state.tabs) == 1
    assert state.active_tab_idx == 0
    assert state.tab_scroll == 0
    assert state.buffer.text() == ""

    state.tabs.extend([FileTab(buffer=Buffer(title="two.sql")), FileTab(buffer=Buffer(title="three.sql"))])
    state.active_tab_idx = -10
    state.tab_scroll = 10
    state.ensure_tab()

    assert state.active_tab_idx == 0
    assert state.tab_scroll == 0


def test_ui_state_active_tab_properties_are_isolated():
    first_result = QueryResult("first", ["A"], [["1"]], "1 row")
    second_result = QueryResult("second", ["B"], [["2"]], "1 row")
    state = UIState(config=make_config(), db=object())
    state.buffer = Buffer(lines=["first"], row=0, col=3, title="one.sql", dirty=True, selection_anchor=(0, 0))
    state.results = ["first results"]
    state.last_result = first_result
    state.active_result = first_result
    state.result_mode = RESULT_ROW_DETAIL
    state.result_row = 4
    state.result_col = 5
    state.result_row_scroll = 2
    state.result_col_scroll = 3
    state.result_page_size = 7
    state.active_tab.search_query = "first"

    state.tabs.append(
        FileTab(buffer=Buffer(lines=["second"], title="two.sql"), active_result=second_result, search_query="second")
    )
    state.active_tab_idx = 1
    state.results = ["second results"]
    state.result_row = 1
    state.result_col = 2

    assert state.buffer.text() == "second"
    assert state.results == ["second results"]
    assert state.active_result == second_result
    assert (state.result_row, state.result_col) == (1, 2)
    assert state.active_tab.search_query == "second"

    state.active_tab_idx = 0
    assert state.buffer.text() == "first"
    assert state.buffer.selection_anchor == (0, 0)
    assert state.results == ["first results"]
    assert state.last_result == first_result
    assert state.active_result == first_result
    assert state.result_mode == RESULT_ROW_DETAIL
    assert (state.result_row, state.result_col) == (4, 5)
    assert (state.result_row_scroll, state.result_col_scroll, state.result_page_size) == (2, 3, 7)
    assert state.active_tab.search_query == "first"


def test_ui_state_legacy_properties_round_trip_through_nested_slices():
    state = UIState(config=make_config(), db=object())
    result = QueryResult("data", ["A"], [["1"]], "1 row")

    state.status = "Busy"
    state.browser_filter = "emp"
    state.active_result = result

    assert state.application.status == "Busy"
    assert state.browser.filter_text == "emp"
    assert state.active_tab.result_state.active_result is result

    state.application.status = "Ready"
    state.browser.filter_text = ""
    state.active_tab.result_state.lines = ["done"]

    assert state.status == "Ready"
    assert state.browser_filter == ""
    assert state.results == ["done"]


def test_browser_and_viewport_transitions_are_terminal_independent():
    browser = BrowserState(filter_text="old", row=8, scroll=5)

    filtered = replace_browser_filter(browser, "emp", selected_row=2)

    assert browser == BrowserState(filter_text="old", row=8, scroll=5)
    assert (filtered.filter_text, filtered.row, filtered.scroll) == ("emp", 2, 0)
    assert reveal_selection(8, 0, 3, 10) == (8, 6)
    assert reveal_selection(4, 9, 3, 0) == (0, 0)


def test_database_activity_transitions_return_replacement_state():
    handle = ui.DbCommandHandle(
        command_id=1,
        events=ui.queue.Queue(),
        done=ui.threading.Event(),
    )
    operation = ui.DbOperation(
        kind="execute",
        label="Running",
        started_at=1.0,
        handle=handle,
        tab=FileTab(),
    )
    activity = DatabaseActivityState(session=object())

    started = begin_database_operation(activity, operation)
    progressed = update_database_operation_progress(operation, "Executing 2/3", 2, 3)
    cancelled = request_database_operation_cancel(progressed, progressed.label)
    finished = finish_database_operation(started)

    assert activity.operation is None
    assert started.operation is operation
    assert (progressed.label, progressed.progress_current, progressed.progress_total) == (
        "Executing 2/3",
        2,
        3,
    )
    assert cancelled.cancel_requested is True
    assert operation.cancel_requested is False
    assert finished.operation is None


def test_tab_source_keys_are_stable_for_files_and_templates(tmp_path):
    path = tmp_path / "sql" / ".." / "query.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("select 1 from dual;\n", encoding="utf-8")

    assert file_source_key(path) == f"file:{path.expanduser().resolve()}"
    assert template_source_key("SQL select") == "template:SQL select"


def test_visible_tab_labels_limit_to_nine_and_clip_utf8():
    tabs = [FileTab(buffer=Buffer(title=f"tab{i}_žluťoučký.sql")) for i in range(12)]

    visible = visible_tab_labels(tabs, 0, 200)
    assert len(visible) == 9
    assert visible[0][0:2] == (0, 1)
    assert visible[-1][0:2] == (8, 9)

    clipped = visible_tab_labels(tabs, 0, 12)
    assert clipped == [(0, 1, "tab0_žlu")]


def test_loaded_object_buffer_title_and_clean_state():
    buffer = Buffer()
    buffer.set_text("create table t (id number);", title=schema_object_title("hr", "TABLE", "T"), dirty=False)
    assert buffer.title == "schema://HR/TABLE/T.sql"
    assert buffer.path is None
    assert buffer.dirty is False
    assert (buffer.row, buffer.col, buffer.scroll) == (0, 0, 0)


def test_ddl_termination_helpers():
    assert ensure_sql_terminator("create table t (id number)") == "create table t (id number);"
    assert terminate_plsql_ddl("create or replace procedure p as begin null; end;") == (
        "create or replace procedure p as begin null; end;\n/"
    )
    assert assemble_package_definition("create package p as end;", "create package body p as end;") == (
        "create package p as end;\n/\n\ncreate package body p as end;\n/"
    )


def test_clamps_result_position_for_empty_and_populated_results():
    empty = QueryResult("empty", ["A"], [], "0 rows")
    assert clamp_result_position(empty, 9, 9, 9, 9).row == 0
    assert clamp_result_position(empty, 9, 9, 9, 9).col == 0

    result = QueryResult("data", ["A", "B"], [["1", "2"], ["3", "4"]], "2 rows")
    pos = clamp_result_position(result, 9, -1, 9, 9)
    assert pos.row == 1
    assert pos.col == 0
    assert pos.row_scroll == 1
    assert pos.col_scroll == 1


def test_grid_column_helpers_are_utf8_width_aware():
    result = QueryResult("data", ["NAME", "SYMBOL"], [["kůň", "表value"]], "1 row")
    widths = table_column_widths(result)
    assert widths[0] == 4
    assert widths[1] == 7

    visible = visible_table_columns(widths, 0, 9)
    assert [(column.index, column.x, column.width) for column in visible] == [(0, 0, 4), (1, 7, 2)]
    assert fit_text("表value", widths[1]) == "表value"


def test_row_detail_lines_wrap_utf8_values():
    result = QueryResult("data", ["NAME", "VALUE"], [["kůň", "Příliš žluťoučký kůň"]], "1 row")
    lines = row_detail_lines(result, 0, 12)
    assert lines[0] == (0, "NAME = kůň")
    assert "".join(line[1] for line in lines if line[0] == 1) == "VALUE = Příliš žluťoučký kůň"


def test_row_detail_lines_handles_null_text_and_no_rows():
    result = QueryResult("data", ["VALUE"], [["NULL"]], "1 row")
    assert row_detail_lines(result, 0, 20) == [(0, "VALUE = NULL")]

    result_with_null_byte = QueryResult("data", ["VALUE"], [["A\x00B"]], "1 row")
    lines = row_detail_lines(result_with_null_byte, 0, 20)
    assert lines == [(0, "VALUE = A\\x00B")]
    assert "\x00" not in lines[0][1]

    result_with_line_break = QueryResult("data", ["VALUE"], [["A\nB"]], "1 row")
    lines = row_detail_lines(result_with_line_break, 0, 20)
    assert lines == [(0, "VALUE = A"), (0, "B")]
    assert "\\x0a" not in "\n".join(text for _, text in lines)

    no_rows = QueryResult("data", ["VALUE"], [], "0 rows")
    assert row_detail_lines(no_rows, 0, 20) == [(-1, "No rows.")]


def test_format_table_handles_ragged_rows():
    lines = format_table(["A", "B"], [["1"], ["2", "wide", "ignored"]])

    assert lines[2].startswith("1 | ")
    assert lines[3] == "2 | wide"
    assert "ignored" not in "\n".join(lines)


def test_selected_result_cell_and_cell_view_lines_wrap_utf8_values():
    result = QueryResult("data", ["NAME", "VALUE"], [["kůň", "Příliš žluťoučký kůň"]], "1 row")
    cell, message = selected_result_cell(result, 0, 1)

    assert message == ""
    assert cell == ResultCell("VALUE", 0, 1, "Příliš žluťoučký kůň")

    lines = cell_view_lines(cell, 12)
    assert lines[:3] == ["Column: VALUE", "Position: row 1, col 2", ""]
    assert "".join(lines[3:]) == "Příliš žluťoučký kůň"

    newline_cell = ResultCell("VALUE", 0, 0, "A\nB")
    lines = cell_view_lines(newline_cell, 12)
    assert lines[3:] == ["A", "B"]
    assert "\\x0a" not in "\n".join(lines)


def test_selected_result_cell_handles_missing_rows_and_columns():
    assert selected_result_cell(QueryResult("data", [], [], "0 rows"), 0, 0)[1] == "No table result is available"
    assert selected_result_cell(QueryResult("data", ["A"], [], "0 rows"), 0, 0)[1] == "No row selected"
    assert selected_result_cell(QueryResult("data", ["A"], [["1"]], "1 row"), 0, 2)[1] == "No column selected"


def test_clamp_cell_view_scroll():
    assert clamp_cell_view_scroll(-5, 20, 5) == 0
    assert clamp_cell_view_scroll(99, 20, 5) == 15
    assert clamp_cell_view_scroll(3, 4, 5) == 0


def test_selected_editable_cell_resolves_rowid_and_table_column():
    result = QueryResult(
        "data",
        ["ROWID", "NAME"],
        [["AAABBBCCC", "Příliš"]],
        "1 row",
        editable_context=EditableResultContext("DECISIONS", 0, {1: "NAME"}),
        original_rows=[["AAABBBCCC", "typed original"]],
    )

    cell, message = selected_editable_cell(result, 0, 1)

    assert message == ""
    assert cell is not None
    assert cell.table_name == "DECISIONS"
    assert cell.table_column == "NAME"
    assert cell.rowid == "AAABBBCCC"
    assert cell.current_value == "Příliš"
    assert cell.original_value == "typed original"


def test_selected_editable_cell_rejects_readonly_and_uneditable_cells():
    result = QueryResult(
        "data",
        ["ROWID", "NAME"],
        [["AAABBBCCC", "Příliš"]],
        "1 row",
        editable_context=EditableResultContext("DECISIONS", 0, {1: "NAME"}),
    )

    assert selected_editable_cell(result, 0, 0)[1] == "ROWID column is read-only"
    assert selected_editable_cell(QueryResult("data", ["NAME"], [], "0 rows"), 0, 0)[1] == (
        "Result is not ROWID-editable"
    )


def make_config(
    root: Path | None = None,
    autocommit: bool = True,
    remember_bind_values: bool = False,
) -> AppConfig:
    workspace = root or Path("/tmp/plsqlwks-tests")
    return AppConfig(
        user="hr",
        dsn="db",
        password_file=workspace / "orapass" if root else Path("/tmp/orapass"),
        workspace_dir=workspace,
        autocommit=autocommit,
        remember_bind_values=remember_bind_values,
    )


class FakeScreen:
    def __init__(self, height: int = 24, width: int = 120):
        self.height = height
        self.width = width
        self.touchwin_count = 0
        self.refresh_count = 0

    def getmaxyx(self):
        return self.height, self.width

    def touchwin(self):
        self.touchwin_count += 1

    def refresh(self):
        self.refresh_count += 1


def sample_plan_steps() -> list[ExplainPlanStep]:
    return [
        ExplainPlanStep(0, None, 0, "SELECT STATEMENT", "", "", "", "", "", "", "3", ""),
        ExplainPlanStep(1, 0, 1, "TABLE ACCESS", "FULL", "HR", "DECISIONS", "TABLE", "10", "120", "2", ""),
        ExplainPlanStep(2, 0, 1, "NESTED LOOPS", "", "", "", "", "", "", "", ""),
        ExplainPlanStep(3, 2, 2, "INDEX", "RANGE SCAN", "", "DECISION_PK", "INDEX", "", "", "1", ""),
    ]


class FakeInputWindow:
    def __init__(self, keys: list[int | str]):
        self.keys = keys
        self.timeouts: list[int] = []

    def get_wch(self):
        if not self.keys:
            raise curses.error
        return self.keys.pop(0)

    def timeout(self, value: int):
        self.timeouts.append(value)


class FakePickerCall:
    def __init__(self, y: int, x: int, text: str, attr: int = 0):
        self.y = y
        self.x = x
        self.text = text
        self.attr = attr


class FakePickerWindow:
    def __init__(self, height: int, width: int, top: int, left: int):
        self.height = height
        self.width = width
        self.top = top
        self.left = left
        self.calls: list[FakePickerCall] = []
        self.moves: list[tuple[int, int]] = []
        self.keypad_enabled = False
        self.refresh_count = 0

    def getmaxyx(self):
        return self.height, self.width

    def keypad(self, enabled: bool):
        self.keypad_enabled = enabled

    def box(self):
        self.calls.append(FakePickerCall(0, 0, "BOX"))

    def addstr(self, y: int, x: int, text: str, attr: int = 0):
        self.calls.append(FakePickerCall(y, x, text, attr))

    def move(self, y: int, x: int):
        self.moves.append((y, x))

    def refresh(self):
        self.refresh_count += 1


class FailingScriptDb:
    def __init__(self):
        self.titles: list[str] = []

    def execute_statement(self, statement: str, title: str = "Statement") -> QueryResult:
        self.titles.append(title)
        if title.startswith(("Statement 2", "Selection 2")):
            raise RuntimeError('ORA-06550: line 2, column 3:\nPLS-00103: Encountered the symbol "BAD"')
        return QueryResult(title, [], [], "1 row")


class RecordingDb:
    def __init__(self):
        self.statements: list[str] = []
        self.titles: list[str] = []
        self.bind_values: list[dict[str, object]] = []

    def execute_statement(
        self,
        statement: str,
        title: str = "Statement",
        bind_values: dict[str, object] | None = None,
    ) -> QueryResult:
        self.statements.append(statement)
        self.titles.append(title)
        self.bind_values.append(dict(bind_values or {}))
        return QueryResult(title, [], [], "ok")


class OffsetFailingDb:
    def __init__(self, offset: int):
        self.offset = offset
        self.statements: list[str] = []
        self.titles: list[str] = []

    def execute_statement(self, statement: str, title: str = "Statement") -> QueryResult:
        self.statements.append(statement)
        self.titles.append(title)
        raise OracleExecutionError(
            FakeOracleOffsetError(FakeOracleOffsetInfo(self.offset)),
            title,
            statement=statement,
        )


class ReconnectDb:
    def __init__(self, pending: bool = False, failing_action: str | None = None):
        self.autocommit = not pending
        self.has_uncommitted_changes = pending
        self.failing_action = failing_action
        self.original_connection = object()
        self.reconnected_connection = object()
        self.connection: object | None = self.original_connection
        self.closed = 0
        self.connected = 0
        self.calls: list[str] = []

    def commit(self) -> TransactionReport:
        self.calls.append("commit")
        if self.failing_action in {"commit", "commit_and_close"}:
            raise RuntimeError("connection is dead")
        self.has_uncommitted_changes = False
        return TransactionReport(datetime(2026, 6, 12, 10, 12, 15), rows_changed=1)

    def rollback(self) -> TransactionReport:
        self.calls.append("rollback")
        if self.failing_action == "rollback":
            raise RuntimeError("rollback failed")
        self.has_uncommitted_changes = False
        return TransactionReport(datetime(2026, 6, 12, 10, 12, 15), rows_changed=1)

    def close(self) -> None:
        self.calls.append("close")
        self.closed += 1
        self.connection = None
        self.has_uncommitted_changes = False
        if self.failing_action in {"close", "commit_and_close"}:
            raise RuntimeError("already disconnected")

    def ensure_connected(self) -> None:
        self.calls.append("connect")
        self.connected += 1
        self.connection = self.reconnected_connection


class BlockingReconnectDb(ReconnectDb):
    def __init__(self):
        super().__init__(pending=True)
        self.commit_started = ui.threading.Event()
        self.commit_release = ui.threading.Event()

    def commit(self) -> TransactionReport:
        self.calls.append("commit")
        self.commit_started.set()
        assert self.commit_release.wait(1)
        self.has_uncommitted_changes = False
        return TransactionReport(datetime(2026, 6, 12, 10, 12, 15), rows_changed=1)


def make_reconnect_app(db: ReconnectDb) -> tuple[ServiceHarness, QueryResult]:
    app = ServiceHarness()
    app.screen = FakeScreen()
    app.state = UIState(config=make_config(autocommit=False), db=db)
    app.running = True
    result = QueryResult(
        "data",
        ["ROWID", "NAME"],
        [["AAABBBCCC", "old"]],
        "1 row",
        editable_context=EditableResultContext("DECISIONS", 0, {1: "NAME"}),
        continuation=QueryResultContinuation("old-session-cursor"),
    )
    app.state.active_result = result
    app.state.last_result = result
    app.state.focus = FOCUS_RESULTS
    return app, result


class TransactionDb:
    def __init__(self):
        self.autocommit = True
        self.has_uncommitted_changes = False
        self.calls: list[str] = []
        self.modes: list[bool] = []
        self.timestamp = datetime(2026, 6, 12, 10, 12, 15)

    def set_autocommit(self, enabled: bool) -> None:
        self.autocommit = enabled
        self.modes.append(enabled)

    def commit(self) -> TransactionReport:
        self.calls.append("commit")
        self.has_uncommitted_changes = False
        return TransactionReport(self.timestamp, rows_changed=7)

    def rollback(self) -> TransactionReport:
        self.calls.append("rollback")
        self.has_uncommitted_changes = False
        return TransactionReport(self.timestamp, rows_changed=0, has_unknown_changes=True)


class FailingTransactionDb(TransactionDb):
    def __init__(self, failing_action: str):
        super().__init__()
        self.failing_action = failing_action

    def commit(self) -> TransactionReport:
        if self.failing_action == "commit":
            self.calls.append("commit")
            raise RuntimeError("commit failed")
        return super().commit()

    def rollback(self) -> TransactionReport:
        if self.failing_action == "rollback":
            self.calls.append("rollback")
            raise RuntimeError("rollback failed")
        return super().rollback()
