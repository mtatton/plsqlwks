from __future__ import annotations

import curses
from pathlib import Path
import queue
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
from .results import *
from .state import *

class AppTabsBrowserMixin:
    def new_tab(self, tab: FileTab | None = None, status: str = "New tab") -> None:
        self.state.tabs.append(tab or FileTab())
        self.state.active_tab_idx = len(self.state.tabs) - 1
        self.state.focus = FOCUS_EDITOR
        self.state.status = status

    def switch_tab(self, delta: int) -> None:
        self.state.ensure_tab()
        if len(self.state.tabs) <= 1:
            self.state.status = "Only one tab"
            return
        self.switch_to_tab((self.state.active_tab_idx + delta) % len(self.state.tabs))

    def switch_to_tab(self, index: int, status: str | None = None) -> None:
        self.state.ensure_tab()
        self.state.active_tab_idx = clamp_tab_index(index, self.state.tabs)
        if (
            self.state.focus == FOCUS_RESULTS
            and self.state.active_result is None
            and self.state.explain_result is None
        ):
            self.state.focus = FOCUS_EDITOR
        title = tab_display_title(self.state.active_tab)
        self.state.status = status or f"Tab {self.state.active_tab_idx + 1}/{len(self.state.tabs)}: {title}"

    def switch_to_visible_tab_number(self, number: int) -> None:
        if not 1 <= number <= 9:
            return
        target = self.state.tab_scroll + number - 1
        if target >= len(self.state.tabs):
            self.state.status = "No such visible tab"
            return
        self.switch_to_tab(target)

    def find_tab_by_source_key(self, source_key: str) -> int | None:
        for idx, tab in enumerate(self.state.tabs):
            if tab.source_key == source_key:
                return idx
        return None

    def close_active_tab(self) -> None:
        if self.reject_if_db_operation_active():
            return
        self.state.ensure_tab()
        idx = self.state.active_tab_idx
        tab = self.state.active_tab
        title = tab_display_title(tab)
        if tab.buffer.dirty and not self.confirm_dirty_tab(idx, "Close"):
            return
        self.close_tab_result_continuations(tab)
        self.state.tabs.pop(idx)
        if not self.state.tabs:
            self.state.tabs.append(FileTab())
            self.state.active_tab_idx = 0
            self.state.tab_scroll = 0
            self.state.focus = FOCUS_EDITOR
            self.state.status = "Closed tab; new empty tab"
            return
        self.state.active_tab_idx = min(idx, len(self.state.tabs) - 1)
        self.state.tab_scroll = min(self.state.tab_scroll, self.state.active_tab_idx)
        if (
            self.state.focus == FOCUS_RESULTS
            and self.state.active_result is None
            and self.state.explain_result is None
        ):
            self.state.focus = FOCUS_EDITOR
        self.state.status = f"Closed {title}"

    def confirm_dirty_tab(self, index: int, action: str) -> bool:
        self.switch_to_tab(index, status=None)
        title = tab_display_title(self.state.active_tab)
        answer = self.prompt(f"Save changes to {title}? y/n/c", "")
        if answer is None or not answer or answer.lower().startswith("c"):
            self.state.status = f"{action} cancelled"
            return False
        if answer.lower().startswith("n"):
            return True
        if answer.lower().startswith("y"):
            if self.save_buffer():
                return True
            self.state.status = f"{action} cancelled"
            return False
        self.state.status = f"{action} cancelled"
        return False

    def ensure_active_tab_visible(self, width: int) -> None:
        self.state.ensure_tab()
        active = self.state.active_tab_idx
        if active < self.state.tab_scroll:
            self.state.tab_scroll = active
        while active not in [idx for idx, _, _ in visible_tab_labels(self.state.tabs, self.state.tab_scroll, width)]:
            if self.state.tab_scroll >= active:
                break
            self.state.tab_scroll += 1

    def toggle_browser(self) -> None:
        if not self.state.browser_visible:
            self.state.browser_visible = True
            self.state.focus = FOCUS_BROWSER
            if not self.state.browser_loaded:
                self.refresh_browser()
            else:
                self.state.status = "Schema browser"
            return
        if self.state.focus == FOCUS_BROWSER:
            self.state.browser_visible = False
            self.state.focus = FOCUS_EDITOR
            self.state.status = "Schema browser hidden"
            return
        self.state.focus = FOCUS_BROWSER
        self.state.status = "Schema browser"

    def refresh_browser(self) -> None:
        if self.reject_if_db_operation_active():
            return

        def refreshed(objects: dict[str, list[str]]) -> None:
            self.state.browser_objects = objects
            self.state.browser_loaded = True
            entries = self.browser_entries()
            if self.state.browser_filter:
                self.state.browser_row = next(
                    (idx for idx, entry in enumerate(entries) if entry.kind == "object"),
                    0,
                )
                self.state.browser_scroll = 0
            else:
                self.state.browser_row = clamp_browser_row(self.state.browser_row, entries)
                if not entries:
                    self.state.browser_scroll = 0
            total = sum(len(items) for items in self.state.browser_objects.values())
            self.state.status = f"Loaded {total} schema object(s)"

        def refresh_failed(exc: Exception) -> None:
            self.state.status = "Schema refresh failed"
            self.set_results(["ERROR refreshing schema browser:", *wrap_error(exc)])

        self.start_db_operation(
            "schema-refresh",
            "Loading schema objects",
            lambda db, progress: db.list_schema_objects(),
            on_success=refreshed,
            on_error=refresh_failed,
        )

    def browser_entries(self) -> list[BrowserEntry]:
        return flatten_browser_entries(
            self.state.browser_objects,
            self.state.browser_expanded,
            self.state.browser_filter,
        )

    def set_browser_filter(self, filter_text: str) -> None:
        self.state.browser_filter = filter_text
        entries = self.browser_entries()
        self.state.browser_scroll = 0
        if filter_text:
            self.state.browser_row = next(
                (idx for idx, entry in enumerate(entries) if entry.kind == "object"),
                0,
            )
        else:
            self.state.browser_row = 0

    def handle_browser_key(self, key: int | str) -> None:
        if is_escape_key(key):
            if self.state.browser_filter:
                self.set_browser_filter("")
                self.state.status = "Schema filter cleared"
                return
            self.state.focus = FOCUS_EDITOR
            self.state.status = "Editor focus"
            return
        if key == CTRL_R:
            self.refresh_browser()
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self.state.browser_filter:
                self.set_browser_filter(self.state.browser_filter[:-1])
                self.state.status = (
                    f"Schema filter: {self.state.browser_filter}"
                    if self.state.browser_filter
                    else "Schema filter cleared"
                )
            return
        if key in (" ", ord(" ")):
            if self.state.browser_filter:
                self.set_browser_filter(self.state.browser_filter + " ")
                self.state.status = f"Schema filter: {self.state.browser_filter}"
                return
            self.toggle_browser_group_at_cursor()
            self.ensure_browser_selection_visible(self.state.browser_page_size)
            return
        text = key_to_text(key)
        if len(text) == 1 and is_printable_text(text):
            self.set_browser_filter(self.state.browser_filter + text)
            self.state.status = f"Schema filter: {self.state.browser_filter}"
            return
        entries = self.browser_entries()
        if key == curses.KEY_UP:
            self.state.browser_row = clamp_browser_row(self.state.browser_row - 1, entries)
        elif key == curses.KEY_DOWN:
            self.state.browser_row = clamp_browser_row(self.state.browser_row + 1, entries)
        elif key == curses.KEY_PPAGE:
            self.state.browser_row = clamp_browser_row(self.state.browser_row - self.state.browser_page_size, entries)
        elif key == curses.KEY_NPAGE:
            self.state.browser_row = clamp_browser_row(self.state.browser_row + self.state.browser_page_size, entries)
        elif key in (10, 13):
            self.activate_browser_entry()
        else:
            return
        self.ensure_browser_selection_visible(self.state.browser_page_size)

    def active_browser_entry(self) -> BrowserEntry | None:
        entries = self.browser_entries()
        if not entries:
            return None
        self.state.browser_row = clamp_browser_row(self.state.browser_row, entries)
        return entries[self.state.browser_row]

    def activate_browser_entry(self) -> None:
        entry = self.active_browser_entry()
        if entry is None:
            self.state.status = "Schema browser is empty"
            return
        if entry.kind == "group":
            if self.state.browser_filter:
                return
            self.toggle_browser_group(entry.object_type)
            return
        self.load_schema_object(entry.object_type, entry.object_name)

    def toggle_browser_group_at_cursor(self) -> None:
        if self.state.browser_filter:
            return
        entry = self.active_browser_entry()
        if entry is None:
            return
        if entry.kind == "group":
            self.toggle_browser_group(entry.object_type)

    def toggle_browser_group(self, object_type: str) -> None:
        if self.state.browser_filter:
            return
        if object_type in self.state.browser_expanded:
            self.state.browser_expanded.remove(object_type)
        else:
            self.state.browser_expanded.add(object_type)
        entries = self.browser_entries()
        self.state.browser_row = clamp_browser_row(self.state.browser_row, entries)
        label = BROWSER_GROUP_LABELS.get(object_type, object_type.title())
        state = "expanded" if object_type in self.state.browser_expanded else "collapsed"
        self.state.status = f"{label} {state}"

    def load_schema_object(self, object_type: str, object_name: str) -> None:
        if self.reject_if_db_operation_active():
            return
        title = schema_object_title(self.state.config.user, object_type, object_name)
        existing = self.find_tab_by_source_key(title)
        if existing is not None:
            self.switch_to_tab(existing, f"Switched to {object_type} {object_name}")
            self.state.focus = FOCUS_EDITOR
            return
        def loaded(definition: str) -> None:
            buffer = Buffer()
            buffer.set_text(definition, title=title, dirty=False, record_undo=False)
            self.new_tab(FileTab(buffer=buffer, source_key=title), f"Loaded {object_type} {object_name}")
            self.state.focus = FOCUS_EDITOR

        def load_failed(exc: Exception) -> None:
            self.state.status = "Load definition failed"
            self.set_results([f"ERROR loading {object_type} {object_name}:", *wrap_error(exc)])

        self.start_db_operation(
            "load-definition",
            f"Loading {object_type} {object_name}",
            lambda db, progress: db.get_object_definition(object_type, object_name),
            on_success=loaded,
            on_error=load_failed,
            restore_active_tab=False,
        )

    def ensure_browser_selection_visible(self, visible_rows: int) -> None:
        if visible_rows <= 0:
            return
        if self.state.browser_row < self.state.browser_scroll:
            self.state.browser_scroll = self.state.browser_row
        if self.state.browser_row >= self.state.browser_scroll + visible_rows:
            self.state.browser_scroll = self.state.browser_row - visible_rows + 1
