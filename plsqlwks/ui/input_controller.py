from __future__ import annotations

import curses
from typing import Protocol

from .command_dispatcher import CommandDispatcher
from .commands import CommandMenuItem
from .constants import (
    CTRL_B,
    CTRL_C,
    CTRL_E,
    CTRL_F,
    CTRL_G,
    CTRL_L,
    CTRL_N,
    CTRL_O,
    CTRL_P,
    CTRL_Q,
    CTRL_R,
    CTRL_S,
    CTRL_T,
    CTRL_U,
    CTRL_V,
    CTRL_W,
    CTRL_X,
    CTRL_Y,
    CTRL_Z,
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
    KEY_CTRL_DOWN,
    KEY_CTRL_ENTER,
    KEY_CTRL_EQUALS,
    KEY_CTRL_PAGEDOWN,
    KEY_CTRL_PAGEUP,
    KEY_CTRL_UP,
    KEY_SHIFT_TAB,
    TAB,
    curses_function_key,
)
from .keys import alt_digit_from_key
from .ports import DbOperationsPort
from .results import result_pane_is_fullscreen
from .state import UIState


class ApplicationInputPort(Protocol):
    quit_pending: bool


class CommandDialogPort(Protocol):
    def pick_command_menu(
        self,
        commands: tuple[CommandMenuItem, ...],
    ) -> CommandMenuItem | None: ...

    def refresh_modal_background(self) -> None: ...


class DocumentInputPort(Protocol):
    def switch_tab(self, delta: int) -> None: ...

    def switch_to_visible_tab_number(self, number: int) -> None: ...


class BrowserInputPort(Protocol):
    def handle_browser_key(self, key: int | str) -> None: ...


class ResultInputPort(Protocol):
    def handle_results_key(self, key: int | str) -> None: ...


class EditorInputPort(Protocol):
    def edit_key(self, key: int | str) -> None: ...


class ViewportInputPort(Protocol):
    def toggle_result_pane_size(self) -> None: ...

    def scroll_results_page(self, direction: int) -> None: ...

    def scroll_focused_window(self, delta: int) -> None: ...


class InputController:
    def __init__(
        self,
        state: UIState,
        operations: DbOperationsPort,
        application: ApplicationInputPort,
        dispatcher: CommandDispatcher,
        dialogs: CommandDialogPort,
        documents: DocumentInputPort,
        viewport: ViewportInputPort,
        browser: BrowserInputPort,
        results: ResultInputPort,
        editor: EditorInputPort,
        command_menu_items: tuple[CommandMenuItem, ...],
    ) -> None:
        self.state = state
        self.operations = operations
        self.application = application
        self.dispatcher = dispatcher
        self.dialogs = dialogs
        self.documents = documents
        self.viewport = viewport
        self.browser = browser
        self.results = results
        self.editor = editor
        self.command_menu_items = command_menu_items

    def _execute(self, handler_id: str) -> None:
        self.dispatcher.execute(handler_id)

    def open_commands_menu(self) -> None:
        command = self.dialogs.pick_command_menu(self.command_menu_items)
        if command is None:
            self.state.status = "Command menu cancelled"
            return
        self.dialogs.refresh_modal_background()
        self.dispatcher.execute(command.handler)

    def _handle_global_key(self, key: int | str) -> bool:
        if key == CTRL_C and self.operations.active:
            self.operations.interrupt()
            return True
        if key == CTRL_Q:
            self._execute("request_quit")
            return True
        if key == KEY_ALT_O:
            self.open_commands_menu()
            return True
        if key == curses.KEY_F1:
            self._execute("show_help")
            return True
        if key == curses.KEY_F7:
            self.viewport.toggle_result_pane_size()
            return True
        if key == curses.KEY_F6:
            self._execute("toggle_dbms_output_view")
            return True
        if key == curses.KEY_F8:
            self._execute("toggle_result_mode")
            return True
        if key == curses.KEY_F9:
            self._execute("toggle_browser")
            return True
        if key == curses_function_key(12):
            self._execute("choose_transaction_mode")
            return True
        if key == KEY_CTRL_ALT_C:
            self._execute("commit_or_insert_draft")
            return True
        if key == KEY_CTRL_ALT_R:
            self._execute("rollback_transaction")
            return True
        if key == CTRL_W:
            self._execute("close_active_tab")
            return True
        if key == KEY_CTRL_PAGEUP:
            if self.focused_results_are_scrollable():
                self.viewport.scroll_results_page(-1)
            else:
                self.documents.switch_tab(-1)
            return True
        if key == KEY_CTRL_PAGEDOWN:
            if self.focused_results_are_scrollable():
                self.viewport.scroll_results_page(1)
            else:
                self.documents.switch_tab(1)
            return True
        alt_digit = alt_digit_from_key(key)
        if alt_digit is not None:
            self.documents.switch_to_visible_tab_number(alt_digit)
            return True
        if key == KEY_CTRL_UP:
            self.viewport.scroll_focused_window(-1)
            return True
        if key == KEY_CTRL_DOWN:
            self.viewport.scroll_focused_window(1)
            return True
        if key == KEY_ALT_G:
            self._execute("generate_sql_with_columns")
            return True
        if key == KEY_ALT_PLUS:
            self._execute("refresh_autocomplete_cache")
            return True
        return False

    def focused_results_are_scrollable(self) -> bool:
        if self.state.focus != FOCUS_RESULTS:
            return False
        if self.state.show_dbms_output and self.state.dbms_output:
            return True
        return bool(
            self.state.explain_result is not None
            or self.state.active_result is not None
            or self.state.results
        )

    def _redirect_fullscreen_editor_focus(self) -> None:
        if result_pane_is_fullscreen(self.state.result_ratio) and self.state.focus == FOCUS_EDITOR:
            self.state.focus = FOCUS_RESULTS

    def _handle_editor_command_key(self, key: int | str) -> bool:
        handlers: dict[int | str, str] = {
            KEY_SHIFT_TAB: "autocomplete_editor",
            CTRL_F: "prompt_search",
            CTRL_G: "prompt_go_to_line",
            CTRL_N: "repeat_search_forward",
            CTRL_P: "repeat_search_backward",
            CTRL_E: "explain_current_statement",
            CTRL_B: "toggle_current_line_comment",
            CTRL_U: "uppercase_selection",
            CTRL_L: "lowercase_selection",
            CTRL_Z: "undo_buffer",
            CTRL_Y: "redo_buffer",
            CTRL_C: "copy_selection",
            CTRL_X: "cut_selection",
            CTRL_V: "paste_clipboard",
            TAB: "enter_results_focus",
            CTRL_S: "save_buffer",
            curses.KEY_F2: "save_buffer",
            KEY_ALT_R: "rename_current_buffer",
            CTRL_O: "open_file",
            curses.KEY_F3: "open_file",
            curses.KEY_F4: "new_template",
            curses.KEY_F5: "run_current_statement",
            KEY_CTRL_ENTER: "run_current_statement",
            KEY_ALT_X: "run_current_statement",
            10: "run_current_statement",
            curses_function_key(11): "run_script",
            CTRL_T: "new_tab",
            KEY_CTRL_EQUALS: "reconnect_database",
            CTRL_R: "refresh_workspace_file_list",
        }
        handler = handlers.get(key)
        if handler is None:
            return False
        self._execute(handler)
        return True

    def handle_key(self, key: int | str) -> None:
        if self.application.quit_pending:
            if key == CTRL_C and self.operations.active:
                self.operations.interrupt()
                return
            self.state.status = "Quit transaction resolution in progress"
            return
        if self._handle_global_key(key):
            return
        self._redirect_fullscreen_editor_focus()
        if self.state.focus == FOCUS_BROWSER:
            self.browser.handle_browser_key(key)
            return
        if self.state.focus == FOCUS_RESULTS:
            self.results.handle_results_key(key)
            return
        if self._handle_editor_command_key(key):
            return
        self.editor.edit_key(key)
