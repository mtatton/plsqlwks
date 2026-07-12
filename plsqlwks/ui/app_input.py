from __future__ import annotations

import curses
from pathlib import Path
import queue
import sys
import time
import threading
from typing import Any, Callable

from ..config import save_autocommit
from ..db import (
    ExplainPlanResult,
    OracleWorkspace,
    QueryResult,
    QueryResultPage,
    TransactionReport,
    workspace_health,
)
from ..sqlsplit import split_script, statement_at_cursor
from ..workspace import list_workspace_files
from .constants import *
from .display import *
from .keys import *
from .buffer import *
from .help import *
from .errors import *
from .sql import *
from .completion import *
from .syntax import *
from .clipboard import *
from .browser import *
from .menu import *
from .commands import *
from .results import *
from .state import *

def _legacy_ui_attr(name: str, fallback):
    facade = sys.modules.get("plsqlwks.ui")
    return getattr(facade, name, fallback)


class AppInputMixin:
    def read_key(self, window: curses.window | None = None, idle_timeout: int = 200) -> int | str:
        target = window or self.screen
        try:
            key = target.get_wch()
        except curses.error:
            return -1
        if not is_escape_key(key):
            return normalize_key(key)

        sequence = [key]
        target.timeout(ESC_SEQUENCE_TIMEOUT_MS)
        try:
            while len(sequence) < 16:
                try:
                    next_key = target.get_wch()
                except curses.error:
                    break
                if next_key == -1:
                    break
                sequence.append(next_key)
                decoded = decode_key_sequence(sequence)
                if decoded is not None:
                    return decoded
        finally:
            target.timeout(idle_timeout)
        return ESC

    def _handle_global_key(self, key: int | str) -> bool:
        if key == CTRL_C and self.db_operation_active():
            self.interrupt_db_operation()
            return True
        if key in (CTRL_Q,):
            self.request_quit()
            return True
        if key == KEY_ALT_O:
            self.open_commands_menu()
            return True
        if key == curses.KEY_F1:
            self.show_help()
            return True
        if key == curses.KEY_F7:
            self.toggle_result_pane_size()
            return True
        if key == curses.KEY_F6:
            self.toggle_dbms_output_view()
            return True
        if key == curses.KEY_F8:
            self.toggle_result_mode()
            return True
        if key == curses.KEY_F9:
            self.toggle_browser()
            return True
        if key == curses_function_key(12):
            self.choose_transaction_mode()
            return True
        if key == KEY_CTRL_ALT_C:
            self.commit_or_insert_draft()
            return True
        if key == KEY_CTRL_ALT_R:
            self.rollback_transaction()
            return True
        if key == CTRL_W:
            self.close_active_tab()
            return True
        if key == KEY_CTRL_PAGEUP:
            self.switch_tab(-1)
            return True
        if key == KEY_CTRL_PAGEDOWN:
            self.switch_tab(1)
            return True
        alt_digit = alt_digit_from_key(key)
        if alt_digit is not None:
            self.switch_to_visible_tab_number(alt_digit)
            return True
        if key == KEY_CTRL_UP:
            self.scroll_focused_window(-1)
            return True
        if key == KEY_CTRL_DOWN:
            self.scroll_focused_window(1)
            return True
        if key == KEY_ALT_G:
            self.generate_sql_with_columns()
            return True
        if key == KEY_ALT_PLUS:
            self.refresh_autocomplete_cache()
            return True
        return False

    def request_quit(self) -> None:
        if not self.confirm_quit():
            return
        resolution = self.prompt_pending_transaction("Quit cancelled")
        if resolution is None:
            return

        self._quit_pending = True

        def finish_quit() -> None:
            self._quit_pending = False
            self.persist_session_tabs()
            self.running = False

        def cancel_quit() -> None:
            self._quit_pending = False

        if resolution == "commit":
            if not self.commit_transaction(finish_quit, cancel_quit):
                cancel_quit()
        elif resolution == "rollback":
            if not self.rollback_transaction(finish_quit, cancel_quit):
                cancel_quit()
        else:
            finish_quit()

    def commit_or_insert_draft(self) -> None:
        if self.commit_insert_draft_if_active():
            return
        self.commit_transaction()

    def reconnect_database(self) -> None:
        if self.reject_if_db_operation_active():
            return
        resolution = self.prompt_pending_transaction(
            "Reconnect cancelled",
            allow_discard=True,
        )
        if resolution is None:
            return

        def reconnect_after_resolution() -> None:
            self._start_connect(force=True)

        if resolution == "commit":
            self.commit_transaction(
                after_success=reconnect_after_resolution,
                preserve_results_on_error=True,
            )
        elif resolution == "rollback":
            self.rollback_transaction(
                after_success=reconnect_after_resolution,
                preserve_results_on_error=True,
            )
        elif resolution in {"discard", "none"}:
            reconnect_after_resolution()
        else:
            self.state.status = "Reconnect cancelled"

    def refresh_workspace_file_list(self) -> None:
        self.state.files = _legacy_ui_attr("list_workspace_files", list_workspace_files)(self.state.config)
        self.state.status = "File list refreshed"

    def repeat_search_forward(self) -> None:
        self.repeat_search(1)

    def repeat_search_backward(self) -> None:
        self.repeat_search(-1)

    def uppercase_selection(self) -> None:
        self.transform_selection_sql_code_case(str.upper, "Uppercased selection")

    def lowercase_selection(self) -> None:
        self.transform_selection_sql_code_case(str.lower, "Lowercased selection")

    def open_commands_menu(self) -> None:
        command = self.pick_command_menu(COMMAND_MENU_ITEMS)
        if command is None:
            self.state.status = "Command menu cancelled"
            return
        self.refresh_modal_background()
        getattr(self, command.handler)()

    def pick_command_menu(self, commands: tuple[CommandMenuItem, ...]) -> CommandMenuItem | None:
        if not commands:
            return None
        filter_text = ""
        previous_geometry: tuple[int, int, int, int] | None = None
        sections = tree_menu_sections(commands)
        expanded_sections = {section.name for section in sections}
        shortcut_width = max(display_width(command.shortcut) for command in commands)
        rows = tree_menu_rows(commands, filter_text, expanded_sections)
        selected = first_tree_menu_row_index(rows)
        while True:
            rows = tree_menu_rows(commands, filter_text, expanded_sections)
            selected = clamp_picker_selection(selected, len(rows))
            height, width = self.screen.getmaxyx()
            if height < 4 or width < 20:
                self.state.status = "Terminal too small for command menu"
                return None
            visible_rows = max(1, min(len(rows) or 1, height - 4))
            box_h = min(height - 1, visible_rows + 3)
            labels = [tree_menu_row_label(row, commands, shortcut_width) for row in rows]
            max_text_width = max(display_width(text) for text in [*labels, f"Filter: {filter_text}", "No matches"])
            box_w = min(width, max(32, max_text_width + 4))
            top = 1 if height > box_h else 0
            left = 0
            geometry = (top, left, box_h, box_w)
            if previous_geometry is not None and geometry != previous_geometry:
                self.refresh_modal_background()
            previous_geometry = geometry
            try:
                win = curses.newwin(box_h, box_w, top, left)
            except curses.error:
                self.state.status = "Command menu unavailable"
                return None
            try:
                win.keypad(True)
            except curses.error:
                pass
            try:
                win.box()
            except curses.error:
                pass
            safe_window_addstr(win, 0, 2, clip_text(" Commands ", max(0, box_w - 4)), curses.A_BOLD)
            safe_window_addstr(win, 1, 1, fit_text(f"Filter: {filter_text}", box_w - 2))
            visible = max(1, box_h - 3)
            start = max(0, selected - visible + 1)
            if rows:
                for idx in range(visible):
                    row_idx = start + idx
                    if row_idx >= len(rows):
                        break
                    row = rows[row_idx]
                    attr = curses.A_BOLD if row.item_index is None else 0
                    if row_idx == selected:
                        attr |= curses.A_REVERSE
                    text = tree_menu_row_label(row, commands, shortcut_width)
                    safe_window_addstr(win, idx + 2, 1, fit_text(text, box_w - 2), attr)
            else:
                safe_window_addstr(win, 2, 1, fit_text("No matches", box_w - 2))
            safe_window_refresh = getattr(win, "refresh", None)
            if callable(safe_window_refresh):
                try:
                    safe_window_refresh()
                except curses.error:
                    pass
            key = self.read_key(win, idle_timeout=-1)
            if key == getattr(curses, "KEY_RESIZE", None):
                self.refresh_modal_background()
                continue
            if key in (ESC, CTRL_Q):
                return None
            if key in (10, 13):
                if rows:
                    row = rows[selected]
                    if row.item_index is not None:
                        return commands[row.item_index]
                    if filter_text:
                        selected = min(selected + 1, len(rows) - 1)
                    elif row.section in expanded_sections:
                        expanded_sections.remove(row.section)
                    else:
                        expanded_sections.add(row.section)
                continue
            if key == curses.KEY_UP:
                selected = clamp_picker_selection(selected - 1, len(rows))
            elif key == curses.KEY_DOWN:
                selected = clamp_picker_selection(selected + 1, len(rows))
            elif key == curses.KEY_PPAGE:
                selected = clamp_picker_selection(selected - visible, len(rows))
            elif key == curses.KEY_NPAGE:
                selected = clamp_picker_selection(selected + visible, len(rows))
            elif key == curses.KEY_LEFT and rows:
                row = rows[selected]
                if row.item_index is None:
                    if row.section in expanded_sections and not filter_text:
                        expanded_sections.remove(row.section)
                else:
                    section_idx = tree_menu_section_row_index(rows, row.section)
                    if section_idx is not None:
                        selected = section_idx
                    if not filter_text:
                        expanded_sections.discard(row.section)
            elif key == curses.KEY_RIGHT and rows:
                row = rows[selected]
                if row.item_index is None and not filter_text:
                    expanded_sections.add(row.section)
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                if filter_text:
                    filter_text = filter_text[:-1]
                    selected = first_tree_menu_row_index(
                        tree_menu_rows(commands, filter_text, expanded_sections)
                    )
            elif isinstance(key, str) and is_printable_text(key):
                filter_text += key
                selected = first_tree_menu_row_index(
                    tree_menu_rows(commands, filter_text, expanded_sections)
                )

    def _redirect_fullscreen_editor_focus(self) -> None:
        if result_pane_is_fullscreen(self.state.result_ratio) and self.state.focus == FOCUS_EDITOR:
            self.state.focus = FOCUS_RESULTS

    def _handle_editor_command_key(self, key: int | str) -> bool:
        if key == KEY_SHIFT_TAB:
            self.autocomplete_editor()
            return True
        if key == CTRL_F:
            self.prompt_search()
            return True
        if key == CTRL_G:
            self.prompt_go_to_line()
            return True
        if key == CTRL_N:
            self.repeat_search(1)
            return True
        if key == CTRL_P:
            self.repeat_search(-1)
            return True
        if key == CTRL_E:
            self.explain_current_statement()
            return True
        if key == CTRL_B:
            self.toggle_current_line_comment()
            return True
        if key == CTRL_U:
            self.transform_selection_sql_code_case(str.upper, "Uppercased selection")
            return True
        if key == CTRL_L:
            self.transform_selection_sql_code_case(str.lower, "Lowercased selection")
            return True
        if key == CTRL_Z:
            self.undo_buffer()
            return True
        if key == CTRL_Y:
            self.redo_buffer()
            return True
        if key == CTRL_C:
            self.copy_selection()
            return True
        if key == CTRL_X:
            self.cut_selection()
            return True
        if key == CTRL_V:
            self.paste_clipboard()
            return True
        if key == TAB:
            self.enter_results_focus()
            return True
        if key in (CTRL_S, curses.KEY_F2):
            self.save_buffer()
            return True
        if key == KEY_ALT_R:
            self.rename_current_buffer()
            return True
        if key in (CTRL_O, curses.KEY_F3):
            self.open_file()
            return True
        if key == curses.KEY_F4:
            self.new_template()
            return True
        if key in (curses.KEY_F5, KEY_CTRL_ENTER, KEY_ALT_X, 10):
            self.run_current_statement()
            return True
        if key == curses_function_key(11):
            self.run_script()
            return True
        if key == CTRL_T:
            self.new_tab()
            return True
        if key == KEY_CTRL_EQUALS:
            self.reconnect_database()
            return True
        if key == CTRL_R:
            self.refresh_workspace_file_list()
            return True
        return False

    def handle_key(self, key: int | str) -> None:
        if getattr(self, "_quit_pending", False):
            if key == CTRL_C and self.db_operation_active():
                self.interrupt_db_operation()
                return
            self.state.status = "Quit transaction resolution in progress"
            return
        if self._handle_global_key(key):
            return
        self._redirect_fullscreen_editor_focus()
        if self.state.focus == FOCUS_BROWSER:
            self.handle_browser_key(key)
            return
        if self.state.focus == FOCUS_RESULTS:
            self.handle_results_key(key)
            return
        if self._handle_editor_command_key(key):
            return
        self.edit_key(key)

    def toggle_result_pane_size(self) -> None:
        if self.state.result_grid_fullscreen:
            self.state.result_grid_fullscreen = False
            self.set_result_pane_ratio(RESULT_RATIO_EDITOR_FULLSCREEN)
            return
        if result_pane_is_editor_fullscreen(self.state.result_ratio):
            self.state.result_mode = RESULT_GRID
            self.set_result_pane_ratio(RESULT_RATIO_GRID_SPLIT)
            return
        if self.enter_result_grid_fullscreen():
            return
        self.set_result_pane_ratio(RESULT_RATIO_EDITOR_FULLSCREEN)

    def set_result_pane_ratio(self, ratio: float) -> None:
        self.state.result_ratio = ratio
        if result_pane_is_editor_fullscreen(self.state.result_ratio):
            self.state.focus = FOCUS_EDITOR
        elif result_pane_is_fullscreen(self.state.result_ratio) and self.state.focus == FOCUS_EDITOR:
            self.state.focus = FOCUS_RESULTS
        self.state.status = result_pane_status(self.state.result_ratio)

    def enter_result_grid_fullscreen(self) -> bool:
        if self.state.active_result is None:
            self.state.status = "No table result is available"
            return False
        self.state.result_grid_fullscreen_previous_ratio = self.state.result_ratio
        self.state.result_grid_fullscreen = True
        self.state.show_dbms_output = False
        self.state.result_mode = RESULT_GRID
        self.state.focus = FOCUS_RESULTS
        self.set_result_pane_ratio(RESULT_RATIO_FULLSCREEN)
        self.state.status = "Data grid fullscreen"
        return True

    def scroll_focused_window(self, delta: int) -> None:
        if delta == 0:
            return
        if self.state.focus == FOCUS_BROWSER:
            self.scroll_browser_window(delta)
            return
        if self.state.focus == FOCUS_RESULTS or (self.state.show_dbms_output and self.state.dbms_output):
            self.scroll_results_window(delta)
            return
        self.scroll_editor_window(delta)

    def current_pane_sizes(self) -> tuple[int, int, int, int]:
        try:
            height, width = self.screen.getmaxyx()
        except Exception:
            height, width = 24, 120
        if self.state.result_grid_fullscreen and self.state.active_result is not None:
            return 0, max(0, height), max(1, width), 0
        if result_pane_is_editor_fullscreen(self.state.result_ratio):
            return max(0, height), 0, max(1, width), 0
        status_h = 1
        header_h = 1
        tab_h = result_pane_tab_height(self.state.result_ratio)
        usable_h = max(3, height - status_h - header_h)
        content_usable_h = max(3, usable_h - tab_h)
        browser_w = browser_panel_width(width) if self.state.browser_visible else 0
        content_x = browser_w + 1 if self.state.browser_visible else 0
        content_w = max(1, width - content_x)
        editor_h, result_h = editor_result_pane_heights(content_usable_h, self.state.result_ratio)
        browser_body_h = max(0, usable_h - 1)
        return max(0, editor_h), max(0, result_h), content_w, browser_body_h

    def scroll_editor_window(self, delta: int) -> None:
        editor_h, _, _, _ = self.current_pane_sizes()
        if editor_h <= 0:
            self.state.focus = FOCUS_RESULTS
            self.state.status = result_pane_status(self.state.result_ratio)
            return
        buf = self.state.buffer
        buf.scroll = clamp_cell_view_scroll(buf.scroll + delta, len(buf.lines), editor_h)
        if buf.row < buf.scroll:
            buf.row = buf.scroll
            buf.col = min(buf.col, len(buf.lines[buf.row]))
            buf.clear_selection()
        elif buf.row >= buf.scroll + editor_h:
            buf.row = min(len(buf.lines) - 1, buf.scroll + editor_h - 1)
            buf.col = min(buf.col, len(buf.lines[buf.row]))
            buf.clear_selection()

    def scroll_browser_window(self, delta: int) -> None:
        entries = self.browser_entries()
        if not entries:
            self.state.browser_row = 0
            self.state.browser_scroll = 0
            return
        visible = max(1, self.state.browser_page_size)
        self.state.browser_scroll = clamp_cell_view_scroll(self.state.browser_scroll + delta, len(entries), visible)
        self.state.browser_row = clamp_browser_row(self.state.browser_row, entries)
        if self.state.browser_row < self.state.browser_scroll:
            self.state.browser_row = self.state.browser_scroll
        elif self.state.browser_row >= self.state.browser_scroll + visible:
            self.state.browser_row = min(len(entries) - 1, self.state.browser_scroll + visible - 1)

    def scroll_results_window(self, delta: int) -> None:
        if self.state.show_dbms_output and self.state.dbms_output:
            self.scroll_dbms_output_window(delta)
            return
        if self.state.explain_result is not None:
            self.scroll_explain_window(delta)
            return
        if self.state.active_result is not None:
            if self.state.result_mode == RESULT_ROW_DETAIL:
                self.scroll_result_detail_window(delta)
            else:
                self.scroll_result_grid_window(delta)
            return
        self.scroll_text_results_window(delta)

    def scroll_explain_window(self, delta: int) -> None:
        result = self.state.explain_result
        if result is None:
            return
        page = max(1, self.state.explain_page_size)
        total = len(explain_plan_lines(result))
        self.state.explain_scroll = clamp_cell_view_scroll(self.state.explain_scroll + delta, total, page)
        self.update_explain_status()

    def scroll_result_grid_window(self, delta: int) -> None:
        result = self.state.active_result
        if result is None:
            return
        page = max(1, self.state.result_page_size)
        total = len(result.rows)
        self.state.result_row_scroll = clamp_cell_view_scroll(self.state.result_row_scroll + delta, total, page)
        if total:
            self.state.result_row = min(max(self.state.result_row, 0), total - 1)
            if self.state.result_row < self.state.result_row_scroll:
                self.state.result_row = self.state.result_row_scroll
            elif self.state.result_row >= self.state.result_row_scroll + page:
                self.state.result_row = min(total - 1, self.state.result_row_scroll + page - 1)
        else:
            self.state.result_row = 0
        self.update_result_status()

    def scroll_result_detail_window(self, delta: int) -> None:
        result = self.state.active_result
        if result is None:
            return
        page = max(1, self.state.result_page_size)
        total = len(result.columns)
        self.state.result_col_scroll = clamp_cell_view_scroll(self.state.result_col_scroll + delta, total, page)
        if total:
            self.state.result_col = min(max(self.state.result_col, 0), total - 1)
            if self.state.result_col < self.state.result_col_scroll:
                self.state.result_col = self.state.result_col_scroll
            elif self.state.result_col >= self.state.result_col_scroll + page:
                self.state.result_col = min(total - 1, self.state.result_col_scroll + page - 1)
        else:
            self.state.result_col = 0
        self.update_result_status()

    def scroll_text_results_window(self, delta: int) -> None:
        _, result_h, _, _ = self.current_pane_sizes()
        visible = max(0, result_h - 1)
        current = scroll_start(self.state.active_tab.results_scroll, len(self.state.results), visible)
        scroll = clamp_cell_view_scroll(current + delta, len(self.state.results), visible)
        self.state.active_tab.results_scroll = scroll
        self.update_line_scroll_status("Results", scroll, len(self.state.results), visible)

    def scroll_dbms_output_window(self, delta: int) -> None:
        _, result_h, content_w, _ = self.current_pane_sizes()
        visible = max(0, result_h - 1)
        lines = wrapped_dbms_output_lines(self.state.dbms_output, content_w)
        current = scroll_start(self.state.active_tab.dbms_output_scroll, len(lines), visible)
        scroll = clamp_cell_view_scroll(current + delta, len(lines), visible)
        self.state.active_tab.dbms_output_scroll = scroll
        self.update_line_scroll_status("DBMS_OUTPUT", scroll, len(lines), visible)

    def update_line_scroll_status(self, label: str, scroll: int, total: int, visible: int) -> None:
        start = min(scroll + 1, total) if total else 0
        end = min(scroll + max(0, visible), total)
        self.state.status = f"{label}: lines {start}-{end}/{total}"
