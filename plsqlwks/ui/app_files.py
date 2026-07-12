from __future__ import annotations

import configparser
import curses
from pathlib import Path
import queue
import sys
import time
import threading
from typing import Any, Callable

from ..config import SessionTab, save_autocommit, save_session_tabs as write_session_tabs
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
from .results import *
from .state import *

def _legacy_ui_attr(name: str, fallback):
    facade = sys.modules.get("plsqlwks.ui")
    return getattr(facade, name, fallback)


def right_clip_text(text: str, max_width: int) -> str:
    if display_width(text) <= max_width:
        return text
    used = 0
    clipped: list[str] = []
    for unit in reversed(display_units(text)):
        width = display_unit_width(unit)
        if used + width > max_width:
            break
        clipped.append(unit)
        used += width
    return "".join(reversed(clipped))


def safe_window_box(window: curses.window) -> None:
    try:
        window.box()
    except curses.error:
        pass


def safe_window_move(window: curses.window, y: int, x: int) -> None:
    height, width = window.getmaxyx()
    if height <= 0 or width <= 0:
        return
    try:
        window.move(min(max(y, 0), height - 1), min(max(x, 0), width - 1))
    except curses.error:
        pass


def safe_window_refresh(window: curses.window) -> None:
    try:
        window.refresh()
    except curses.error:
        pass


class AppFilesMixin:
    def restore_session_tabs(self) -> None:
        restored: list[FileTab] = []
        restored_by_source: dict[str, int] = {}
        restored_active: int | None = None
        for saved_index, saved_tab in enumerate(self.state.config.session_tabs):
            try:
                path = Path(saved_tab.path).expanduser().resolve()
                if not path.is_file():
                    continue
                source_key = file_source_key(path)
                duplicate_index = restored_by_source.get(source_key)
                if duplicate_index is not None:
                    if saved_index == self.state.config.active_session_tab:
                        restored_active = duplicate_index
                    continue
                buffer = Buffer()
                buffer.load(path, record_undo=False)
            except (OSError, RuntimeError, TypeError, UnicodeError, ValueError):
                continue
            if (
                isinstance(saved_tab.row, int)
                and not isinstance(saved_tab.row, bool)
                and isinstance(saved_tab.col, int)
                and not isinstance(saved_tab.col, bool)
                and 0 <= saved_tab.row < len(buffer.lines)
                and 0 <= saved_tab.col <= len(buffer.lines[saved_tab.row])
            ):
                buffer.row = saved_tab.row
                buffer.col = saved_tab.col
            restored_index = len(restored)
            restored_by_source[source_key] = restored_index
            restored.append(FileTab(buffer=buffer, source_key=source_key))
            if saved_index == self.state.config.active_session_tab:
                restored_active = restored_index
        if not restored:
            return
        self.state.tabs = restored
        self.state.active_tab_idx = restored_active if restored_active is not None else 0
        self.state.tab_scroll = 0
        self.state.focus = FOCUS_EDITOR

    def persist_session_tabs(self) -> bool:
        saved_tabs: list[SessionTab] = []
        active_tab = 0
        for tab_index, tab in enumerate(self.state.tabs):
            path = tab.buffer.path
            if path is None:
                continue
            if tab_index == self.state.active_tab_idx:
                active_tab = len(saved_tabs)
            saved_tabs.append(SessionTab(path=path, row=tab.buffer.row, col=tab.buffer.col))
        try:
            write_session_tabs(self.state.config, saved_tabs, active_tab)
        except (configparser.Error, OSError, RuntimeError, UnicodeError):
            return False
        return True

    def save_buffer(self) -> bool:
        if self.state.buffer.path is None:
            name = self.prompt("Save as", str(self.default_buffer_path()))
            if not name:
                self.state.status = "Save cancelled"
                return False
            path = Path(name).expanduser()
            source_key = file_source_key(path)
            existing = self.find_tab_by_source_key(source_key)
            if existing is not None and existing != self.state.active_tab_idx:
                self.state.status = "Save failed: file is already open in another tab"
                return False
            if not self.confirm_file_overwrite(path, self.state.buffer.path):
                return False
        else:
            path = self.state.buffer.path
        try:
            saved = self.state.buffer.save(path)
            self.state.active_tab.source_key = file_source_key(saved)
            self.state.files = _legacy_ui_attr("list_workspace_files", list_workspace_files)(self.state.config)
            self.state.status = f"Saved {saved}"
            return True
        except Exception as exc:
            self.state.status = "Save failed"
            self.set_results(["ERROR saving file:", *wrap_error(exc)])
            return False

    def default_buffer_path(self) -> Path:
        default_dir = self.state.config.plsql_dir if looks_like_plsql(self.state.buffer.text()) else self.state.config.sql_dir
        return default_dir / "scratch.sql"

    def confirm_file_overwrite(self, path: Path, current_path: Path | None) -> bool:
        if current_path is not None and file_source_key(path) == file_source_key(current_path):
            return True
        if not path.exists():
            return True
        answer = self.prompt("Overwrite existing file? y/n", "")
        if answer and answer.lower().startswith("y"):
            return True
        self.state.status = "Overwrite cancelled"
        return False

    def rename_current_buffer(self) -> bool:
        buffer = self.state.buffer
        default = str(buffer.path if buffer.path is not None else self.default_buffer_path())
        name = self.prompt("Rename as", default)
        if not name:
            self.state.status = "Rename cancelled"
            return False
        path = Path(name).expanduser()
        source_key = file_source_key(path)
        existing = self.find_tab_by_source_key(source_key)
        if existing is not None and existing != self.state.active_tab_idx:
            self.state.status = "Rename failed: file is already open in another tab"
            return False
        if not self.confirm_file_overwrite(path, buffer.path):
            return False

        old_path = buffer.path
        old_title = buffer.title
        old_dirty = buffer.dirty
        old_source_key = self.state.active_tab.source_key
        try:
            saved = buffer.save(path)
            self.state.active_tab.source_key = file_source_key(saved)
            self.state.files = _legacy_ui_attr("list_workspace_files", list_workspace_files)(self.state.config)
            self.state.status = f"Renamed buffer to {saved}"
            return True
        except Exception as exc:
            buffer.path = old_path
            buffer.title = old_title
            buffer.dirty = old_dirty
            self.state.active_tab.source_key = old_source_key
            self.state.status = "Rename failed"
            self.set_results(["ERROR renaming buffer:", *wrap_error(exc)])
            return False

    def open_file(self) -> None:
        self.state.files = _legacy_ui_attr("list_workspace_files", list_workspace_files)(self.state.config)
        if not self.state.files:
            self.state.status = "No workspace files"
            return
        choice = self.pick("Open file", [str(path) for path in self.state.files])
        if choice is None:
            self.state.status = "Open cancelled"
            return
        try:
            path = self.state.files[choice]
            source_key = file_source_key(path)
            existing = self.find_tab_by_source_key(source_key)
            if existing is not None:
                self.switch_to_tab(existing, f"Switched to {path}")
                self.state.focus = FOCUS_EDITOR
                return
            buffer = Buffer()
            buffer.load(path, record_undo=False)
            self.new_tab(FileTab(buffer=buffer, source_key=source_key), f"Opened {path}")
        except Exception as exc:
            self.state.status = "Open failed"
            self.set_results(["ERROR opening file:", *wrap_error(exc)])

    def new_template(self) -> None:
        names = list(TEMPLATES)
        choice = self.pick("Template", names)
        if choice is None:
            self.state.status = "Template cancelled"
            return
        name = names[choice]
        source_key = template_source_key(name)
        for idx, tab in enumerate(self.state.tabs):
            if tab.source_key == source_key and not tab.buffer.dirty:
                self.switch_to_tab(idx, f"Switched to {name} template")
                self.state.focus = FOCUS_EDITOR
                return
        buffer = Buffer()
        buffer.set_text(TEMPLATES[name], title=f"{name}.sql", dirty=False, record_undo=False)
        self.new_tab(FileTab(buffer=buffer, source_key=source_key), f"Inserted {name} template")

    def prompt(self, label: str, default: str = "", strip: bool = True) -> str | None:
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        text = default
        while True:
            self.draw_prompt_line(label, text)
            key = self.read_key()
            if key in (ESC, CTRL_Q):
                return None
            if key in (10, 13):
                return text.strip() if strip else text
            if key in (curses.KEY_BACKSPACE, 127, 8):
                text = text[:-1]
            elif isinstance(key, str) and is_printable_text(key):
                text += key

    def draw_prompt_line(self, label: str, text: str) -> None:
        height, width = self.screen.getmaxyx()
        if height <= 0 or width <= 0:
            return
        prompt_text = f"{label}: {text}"
        self.addstr(height - 1, 0, fit_text(prompt_text, width), curses.color_pair(1))
        try:
            self.screen.move(height - 1, min(width - 1, display_width(prompt_text)))
        except curses.error:
            pass
        try:
            self.screen.refresh()
        except curses.error:
            pass

    def prompt_text_box(self, label: str, default: str = "", strip: bool = True) -> str | None:
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        text = default
        previous_geometry: tuple[int, int, int, int] | None = None
        while True:
            height, width = self.screen.getmaxyx()
            key: int | str
            if height < 5 or width < 20:
                self.draw_prompt_line(label, text)
                key = self.read_key()
            else:
                box_h = 5
                box_w = min(max(36, display_width(label) + 4), max(20, width - 4))
                top = max(0, (height - box_h) // 2)
                left = max(0, (width - box_w) // 2)
                geometry = (top, left, box_h, box_w)
                if previous_geometry is not None and geometry != previous_geometry:
                    self.refresh_modal_background()
                previous_geometry = geometry

                input_w = max(1, box_w - 4)
                visible_text = right_clip_text(text, input_w)
                try:
                    win = curses.newwin(box_h, box_w, top, left)
                except curses.error:
                    self.draw_prompt_line(label, text)
                    key = self.read_key()
                else:
                    try:
                        win.keypad(True)
                    except curses.error:
                        pass
                    safe_window_box(win)
                    safe_window_addstr(win, 0, 2, clip_text(f" {label} ", max(0, box_w - 4)))
                    safe_window_addstr(win, 2, 2, fit_text(visible_text, input_w), curses.A_REVERSE)
                    footer = "Enter accept  Esc cancel"
                    safe_window_addstr(win, box_h - 1, 2, fit_text(footer, max(0, box_w - 4)))
                    safe_window_move(win, 2, 2 + display_width(visible_text))
                    safe_window_refresh(win)
                    key = self.read_key(win, idle_timeout=-1)

            if key == getattr(curses, "KEY_RESIZE", None):
                self.refresh_modal_background()
                continue
            if key in (ESC, CTRL_Q):
                return None
            if key in (10, 13):
                return text.strip() if strip else text
            if key in (curses.KEY_BACKSPACE, 127, 8):
                text = text[:-1]
            elif isinstance(key, str) and is_printable_text(key):
                text += key

    def pick(self, title: str, options: list[str]) -> int | None:
        if not options:
            return None
        selected = 0
        filter_text = ""
        previous_geometry: tuple[int, int, int, int] | None = None
        while True:
            filtered_indexes = filtered_picker_indexes(options, filter_text)
            selected = clamp_picker_selection(selected, len(filtered_indexes))
            height, width = self.screen.getmaxyx()
            option_rows = max(1, len(filtered_indexes))
            box_h = max(4, min(max(4, height - 4), option_rows + 3))
            filter_label = f"Filter: {filter_text}"
            max_text_width = max(display_width(opt) for opt in [*options, filter_label, "No matches"])
            box_w = max(4, min(max(4, width - 4), max(30, max_text_width + 4)))
            top = max(1, (height - box_h) // 2)
            left = max(1, (width - box_w) // 2)
            geometry = (top, left, box_h, box_w)
            if previous_geometry is not None and geometry != previous_geometry:
                self.refresh_modal_background()
            previous_geometry = geometry
            win = curses.newwin(box_h, box_w, top, left)
            win.keypad(True)
            win.box()
            win.addstr(0, 2, f" {title} ")
            win.addstr(1, 1, fit_text(filter_label, box_w - 2))
            visible = max(1, box_h - 3)
            start = max(0, selected - visible + 1)
            if filtered_indexes:
                for idx in range(visible):
                    filtered_idx = start + idx
                    if filtered_idx >= len(filtered_indexes):
                        break
                    opt_idx = filtered_indexes[filtered_idx]
                    attr = curses.A_REVERSE if filtered_idx == selected else 0
                    win.addstr(idx + 2, 1, fit_text(options[opt_idx], box_w - 2), attr)
            else:
                win.addstr(2, 1, fit_text("No matches", box_w - 2))
            win.refresh()
            key = self.read_key(win, idle_timeout=-1)
            if key in (ESC, CTRL_Q):
                return None
            if key in (10, 13):
                if filtered_indexes:
                    return filtered_indexes[selected]
                continue
            if key == curses.KEY_UP:
                selected = clamp_picker_selection(selected - 1, len(filtered_indexes))
            elif key == curses.KEY_DOWN:
                selected = clamp_picker_selection(selected + 1, len(filtered_indexes))
            elif key == curses.KEY_PPAGE:
                selected = clamp_picker_selection(selected - visible, len(filtered_indexes))
            elif key == curses.KEY_NPAGE:
                selected = clamp_picker_selection(selected + visible, len(filtered_indexes))
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                if filter_text:
                    filter_text = filter_text[:-1]
                    selected = 0
            elif isinstance(key, str) and is_printable_text(key):
                filter_text += key
                selected = 0

    def refresh_modal_background(self) -> None:
        touchwin = getattr(self.screen, "touchwin", None)
        if callable(touchwin):
            try:
                touchwin()
            except curses.error:
                pass
        refresh = getattr(self.screen, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except curses.error:
                pass

    def confirm_quit(self) -> bool:
        if self.db_operation_active():
            self.state.status = "Quit unavailable while database operation is running"
            return False
        original_idx = self.state.active_tab_idx
        for idx, tab in enumerate(list(self.state.tabs)):
            if tab.buffer.dirty and not self.confirm_dirty_tab(idx, "Quit"):
                self.state.active_tab_idx = min(original_idx, len(self.state.tabs) - 1)
                return False
        self.state.active_tab_idx = min(original_idx, len(self.state.tabs) - 1)
        return True

    def _clear_table_result_state(self, focus: str = FOCUS_EDITOR) -> None:
        self.discard_insert_draft()
        self.release_result_continuation(self.state.active_result)
        self.state.active_result = None
        self.state.explain_result = None
        self.state.explain_scroll = 0
        self.state.dbms_output = []
        self.state.show_dbms_output = False
        self.state.focus = focus
        self.state.result_row = 0
        self.state.result_col = 0
        self.state.result_row_scroll = 0
        self.state.result_col_scroll = 0
        self.state.active_tab.dbms_output_scroll = None

    def show_help(self, workspace_messages: list[str] | None = None, *, focus_results: bool = True) -> None:
        self._clear_table_result_state(FOCUS_RESULTS if focus_results else FOCUS_EDITOR)
        help_lines = build_help_lines(workspace_messages)
        self.state.active_tab.help_lines = help_lines
        self.state.results = [line.text for line in help_lines]
        self.state.results_style = RESULT_STYLE_HELP
        self.state.active_tab.results_scroll = 0
        self.state.status = "Help"

    def set_results(self, lines: list[str], clear_table: bool = True) -> None:
        if clear_table:
            self._clear_table_result_state()
        wrapped: list[str] = []
        width = max(40, self.screen.getmaxyx()[1] - 1)
        for line in lines:
            if not line:
                wrapped.append("")
                continue
            wrapped.extend(wrap_display_text(line, width) or [""])
        self.state.results = wrapped
        self.state.active_tab.results_scroll = None
