from __future__ import annotations

import curses
from collections.abc import Callable
from typing import Protocol

from .browser import (
    BrowserEntry,
    clamp_browser_row,
    flatten_browser_entries,
    schema_object_title,
)
from .buffer import Buffer
from .constants import BROWSER_GROUP_LABELS, CTRL_R, FOCUS_BROWSER, FOCUS_EDITOR
from .documents import DocumentController
from .display import is_printable_text
from .errors import wrap_error
from .keys import is_escape_key, key_to_text
from .ports import DbOperationsPort
from .state import FileTab, UIState


class CatalogResultPort(Protocol):
    def set_results(self, lines: list[str], clear_table: bool = True) -> None: ...


class CatalogService:
    def __init__(
        self,
        state: UIState,
        db_operations: DbOperationsPort,
        presenter: CatalogResultPort,
    ) -> None:
        self.state = state
        self.db_operations = db_operations
        self.presenter = presenter

    @property
    def schema_objects(self) -> dict[str, list[str]]:
        return self.state.browser_objects

    def columns(self, object_name: str) -> list[str] | None:
        return self.state.schema_columns.get(object_name.upper())

    def replace_schema_objects(
        self,
        objects: dict[str, list[str]],
        *,
        clear_columns: bool = False,
    ) -> None:
        self.state.browser_objects = objects
        self.state.browser_loaded = True
        if clear_columns:
            self.state.schema_columns.clear()

    def store_columns(self, object_name: str, columns: list[str]) -> None:
        self.state.schema_columns[object_name.upper()] = columns

    def update_columns(self, columns: dict[str, list[str]]) -> None:
        self.state.schema_columns.update(
            {object_name.upper(): names for object_name, names in columns.items()}
        )

    def load_schema_objects(
        self,
        kind: str,
        label: str,
        *,
        clear_columns: bool = False,
        on_success: Callable[[dict[str, list[str]]], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> bool:
        if self.db_operations.reject_if_active():
            return False

        def loaded(objects: dict[str, list[str]]) -> None:
            self.replace_schema_objects(objects, clear_columns=clear_columns)
            if on_success is not None:
                on_success(objects)

        return self.db_operations.start(
            kind,
            label,
            lambda db, progress: db.list_schema_objects(),
            on_success=loaded,
            on_error=on_error,
        )

    def load_columns(
        self,
        object_name: str,
        kind: str,
        label: str,
        *,
        on_success: Callable[[list[str]], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> bool:
        if self.db_operations.reject_if_active():
            return False
        normalized_name = object_name.upper()

        def loaded(columns: list[str]) -> None:
            self.store_columns(normalized_name, columns)
            if on_success is not None:
                on_success(columns)

        return self.db_operations.start(
            kind,
            label,
            lambda db, progress: db.list_object_columns(normalized_name),
            on_success=loaded,
            on_error=on_error,
        )

    def refresh_autocomplete_cache(self) -> None:
        def refresh_failed(exc: Exception) -> None:
            self.state.status = "Autocomplete cache refresh failed"
            self.presenter.set_results(
                ["ERROR refreshing autocomplete cache:", *wrap_error(exc)]
            )

        def refreshed(objects: dict[str, list[str]]) -> None:
            entries = flatten_browser_entries(
                self.state.browser_objects,
                self.state.browser_expanded,
                self.state.browser_filter,
            )
            self.state.browser_row = clamp_browser_row(self.state.browser_row, entries)
            total = sum(len(items) for items in objects.values())
            self.state.status = f"Refreshed autocomplete cache: {total} schema object(s)"

        self.load_schema_objects(
            "autocomplete-refresh",
            "Refreshing autocomplete cache",
            clear_columns=True,
            on_success=refreshed,
            on_error=refresh_failed,
        )


class BrowserController:
    def __init__(
        self,
        state: UIState,
        catalog: CatalogService,
        documents: DocumentController,
        db_operations: DbOperationsPort,
        presenter: CatalogResultPort,
    ) -> None:
        self.state = state
        self.catalog = catalog
        self.documents = documents
        self.db_operations = db_operations
        self.presenter = presenter

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
        def refreshed(objects: dict[str, list[str]]) -> None:
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
            total = sum(len(items) for items in objects.values())
            self.state.status = f"Loaded {total} schema object(s)"

        def refresh_failed(exc: Exception) -> None:
            self.state.status = "Schema refresh failed"
            self.presenter.set_results(
                ["ERROR refreshing schema browser:", *wrap_error(exc)]
            )

        self.catalog.load_schema_objects(
            "schema-refresh",
            "Loading schema objects",
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
            self.state.browser_row = clamp_browser_row(
                self.state.browser_row - self.state.browser_page_size,
                entries,
            )
        elif key == curses.KEY_NPAGE:
            self.state.browser_row = clamp_browser_row(
                self.state.browser_row + self.state.browser_page_size,
                entries,
            )
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
        if self.db_operations.reject_if_active():
            return
        title = schema_object_title(self.state.config.user, object_type, object_name)
        existing = self.documents.find_tab_by_source_key(title)
        if existing is not None:
            self.documents.switch_to_tab(
                existing,
                f"Switched to {object_type} {object_name}",
            )
            self.state.focus = FOCUS_EDITOR
            return

        def loaded(definition: str) -> None:
            buffer = Buffer()
            buffer.set_text(definition, title=title, dirty=False, record_undo=False)
            self.documents.new_tab(
                FileTab(buffer=buffer, source_key=title),
                f"Loaded {object_type} {object_name}",
            )
            self.state.focus = FOCUS_EDITOR

        def load_failed(exc: Exception) -> None:
            self.state.status = "Load definition failed"
            self.presenter.set_results(
                [f"ERROR loading {object_type} {object_name}:", *wrap_error(exc)]
            )

        self.db_operations.start(
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
