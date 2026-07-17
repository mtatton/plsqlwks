from __future__ import annotations

import curses
from collections.abc import Callable
from typing import Any

from ..db import empty_schema_object_groups
from ..sqlsplit import statement_at_cursor
from .catalog import BrowserController, CatalogService
from .clipboard import copy_to_system_clipboard, paste_from_clipboard
from .completion import (
    CompletionCandidate,
    CompletionContext,
    column_completion_candidates,
    completion_context_for_buffer,
    dedupe_completion_candidates,
    find_search_matches,
    keyword_completion_candidates,
    object_completion_candidates,
    ordered_reference_tables,
    resolve_completion_qualifier,
    search_match_index,
    search_navigation_offset,
    search_status,
    select_search_match,
)
from .constants import (
    FOCUS_BROWSER,
    FOCUS_EDITOR,
    KEY_CTRL_BACKSPACE,
    KEY_CTRL_DELETE,
    KEY_CTRL_END,
    KEY_CTRL_HOME,
    KEY_CTRL_LEFT,
    KEY_CTRL_RIGHT,
    KEY_CTRL_SHIFT_END,
    KEY_CTRL_SHIFT_HOME,
    KEY_CTRL_SHIFT_LEFT,
    KEY_CTRL_SHIFT_RIGHT,
    KEY_SHIFT_DOWN,
    KEY_SHIFT_END,
    KEY_SHIFT_HOME,
    KEY_SHIFT_LEFT,
    KEY_SHIFT_PAGEDOWN,
    KEY_SHIFT_PAGEUP,
    KEY_SHIFT_RIGHT,
    KEY_SHIFT_UP,
)
from .display import is_printable_text
from .errors import short_error
from .ports import DbOperationsPort, DialogPort
from .sql import (
    SQL_GENERATOR_CHOICES,
    generated_sql_for_choice,
    generated_sql_table_from_statement,
    selected_browser_table_name,
    statement_table_references,
)
from .state import FileTab, UIState
from .syntax import transform_sql_code_in_selection


CopyToClipboard = Callable[[str], str | None]
PasteFromClipboard = Callable[[str], tuple[str, str]]
EditorFingerprint = tuple[FileTab, str, int, int, tuple[int, int] | None, str]


class EditorController:
    def __init__(
        self,
        state: UIState,
        dialogs: DialogPort,
        *,
        copy_to_clipboard: CopyToClipboard = copy_to_system_clipboard,
        paste_from_clipboard: PasteFromClipboard = paste_from_clipboard,
    ) -> None:
        self.state = state
        self.dialogs = dialogs
        self.copy_to_clipboard = copy_to_clipboard
        self.paste_from_clipboard = paste_from_clipboard

    def prompt_search(self, direction: int = 1) -> None:
        query = self.dialogs.prompt(
            "Find",
            self.state.active_tab.search_query,
            strip=False,
        )
        if query is None:
            self.state.status = "Search cancelled"
            return
        self.state.active_tab.search_query = query
        if not query:
            self.state.buffer.clear_selection()
            self.state.status = "Search cleared"
            return
        self.move_to_search_match(direction)

    def repeat_search(self, direction: int) -> None:
        if not self.state.active_tab.search_query:
            self.prompt_search(direction)
            return
        self.move_to_search_match(direction)

    def repeat_search_forward(self) -> None:
        self.repeat_search(1)

    def repeat_search_backward(self) -> None:
        self.repeat_search(-1)

    def prompt_go_to_line(self) -> None:
        buffer = self.state.buffer
        answer = self.dialogs.prompt_text_box("Go to line", str(buffer.row + 1))
        if answer is None:
            self.state.status = "Go to line cancelled"
            return
        try:
            line_number = int(answer)
        except ValueError:
            self.state.status = "Invalid line number"
            return
        if not 1 <= line_number <= len(buffer.lines):
            self.state.status = f"Line number must be 1-{len(buffer.lines)}"
            return
        buffer.move_to(line_number - 1, 0)
        self.state.focus = FOCUS_EDITOR
        self.state.status = f"Moved to line {line_number}/{len(buffer.lines)}"

    def move_to_search_match(self, direction: int) -> None:
        tab = self.state.active_tab
        buffer = tab.buffer
        query = tab.search_query
        matches = find_search_matches(buffer.lines, query)
        if not matches:
            buffer.clear_selection()
            self.state.status = f'No matches for "{query}"'
            return
        start_offset = search_navigation_offset(buffer, direction)
        picked = search_match_index(matches, start_offset, direction)
        if picked is None:
            buffer.clear_selection()
            self.state.status = f'No matches for "{query}"'
            return
        match_idx, wrapped = picked
        select_search_match(buffer, matches[match_idx])
        self.state.status = search_status(query, match_idx, len(matches), wrapped)

    def edit_key(self, key: int | str) -> None:
        buffer = self.state.buffer
        if isinstance(key, str):
            if is_printable_text(key):
                buffer.insert_char(key)
            return
        if key in (KEY_SHIFT_LEFT, KEY_SHIFT_RIGHT, KEY_SHIFT_UP, KEY_SHIFT_DOWN):
            self.move_editor_cursor(key, extend=True)
        elif key == KEY_SHIFT_HOME:
            buffer.move_line_start(extend=True)
        elif key == KEY_SHIFT_END:
            buffer.move_line_end(extend=True)
        elif key == KEY_CTRL_SHIFT_HOME:
            buffer.move_file_start(extend=True)
        elif key == KEY_CTRL_SHIFT_END:
            buffer.move_file_end(extend=True)
        elif key == KEY_CTRL_SHIFT_LEFT:
            buffer.move_word_left(extend=True)
        elif key == KEY_CTRL_SHIFT_RIGHT:
            buffer.move_word_right(extend=True)
        elif key == KEY_SHIFT_PAGEUP:
            buffer.page(-10, extend=True)
        elif key == KEY_SHIFT_PAGEDOWN:
            buffer.page(10, extend=True)
        elif key == KEY_CTRL_LEFT:
            buffer.move_word_left()
        elif key == KEY_CTRL_RIGHT:
            buffer.move_word_right()
        elif key == KEY_CTRL_HOME:
            buffer.move_file_start()
        elif key == KEY_CTRL_END:
            buffer.move_file_end()
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN):
            self.move_editor_cursor(key, extend=False)
        elif key == curses.KEY_HOME:
            buffer.clear_selection()
            buffer.col = 0
        elif key == curses.KEY_END:
            buffer.clear_selection()
            buffer.col = len(buffer.lines[buffer.row])
        elif key == curses.KEY_PPAGE:
            buffer.page(-10)
        elif key == curses.KEY_NPAGE:
            buffer.page(10)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            buffer.backspace()
        elif key == KEY_CTRL_BACKSPACE:
            buffer.delete_word_left()
        elif key == KEY_CTRL_DELETE:
            buffer.delete_word_right()
        elif key == curses.KEY_DC:
            buffer.delete()
        elif key == 13:
            buffer.newline()

    def move_editor_cursor(self, key: int, extend: bool = False) -> None:
        buffer = self.state.buffer
        if extend:
            buffer.start_selection()
        else:
            buffer.clear_selection()
        if key in (curses.KEY_LEFT, KEY_SHIFT_LEFT):
            if buffer.col > 0:
                buffer.col -= 1
            elif buffer.row > 0:
                buffer.row -= 1
                buffer.col = len(buffer.lines[buffer.row])
        elif key in (curses.KEY_RIGHT, KEY_SHIFT_RIGHT):
            if buffer.col < len(buffer.lines[buffer.row]):
                buffer.col += 1
            elif buffer.row < len(buffer.lines) - 1:
                buffer.row += 1
                buffer.col = 0
        elif key in (curses.KEY_UP, KEY_SHIFT_UP):
            buffer.row = max(0, buffer.row - 1)
            buffer.col = min(buffer.col, len(buffer.lines[buffer.row]))
        elif key in (curses.KEY_DOWN, KEY_SHIFT_DOWN):
            buffer.row = min(len(buffer.lines) - 1, buffer.row + 1)
            buffer.col = min(buffer.col, len(buffer.lines[buffer.row]))

    def copy_selection(self) -> None:
        selected = self.state.buffer.selected_text()
        if not selected:
            self.state.status = "No selection"
            return
        self.state.internal_clipboard = selected
        provider = self.copy_to_clipboard(selected)
        if provider:
            self.state.status = f"Copied {len(selected)} char(s) to {provider}"
        else:
            self.state.status = f"Copied {len(selected)} char(s) internally"

    def cut_selection(self) -> None:
        selected = self.state.buffer.selected_text()
        if not selected:
            self.state.status = "No selection"
            return
        self.state.internal_clipboard = selected
        provider = self.copy_to_clipboard(selected)
        self.state.buffer.delete_selection()
        if provider:
            self.state.status = f"Cut {len(selected)} char(s) to {provider}"
        else:
            self.state.status = f"Cut {len(selected)} char(s) internally"

    def paste_clipboard(self) -> None:
        text, source = self.paste_from_clipboard(self.state.internal_clipboard)
        if not text:
            self.state.status = "Clipboard empty"
            return
        self.state.buffer.insert_text(text)
        self.state.status = f"Pasted {len(text)} char(s) from {source}"

    def toggle_current_line_comment(self) -> None:
        commented, count = self.state.buffer.toggle_comment()
        if count == 0:
            self.state.status = "No comment target"
            return
        action = "Commented" if commented else "Uncommented"
        suffix = "line" if count == 1 else f"{count} lines"
        self.state.status = f"{action} {suffix}"

    def transform_selection_case(
        self,
        transform: Callable[[str], str],
        status: str,
    ) -> None:
        if not self.state.buffer.transform_selection(transform):
            self.state.status = "No selection"
            return
        self.state.status = status

    def transform_selection_sql_code_case(
        self,
        transform: Callable[[str], str],
        status: str,
    ) -> None:
        selected = self.state.buffer.selection_range()
        if selected is None:
            self.state.status = "No selection"
            return
        transformed = transform_sql_code_in_selection(
            self.state.buffer.lines,
            selected,
            transform,
        )
        self.state.buffer.transform_selection(lambda _selected: transformed)
        self.state.status = status

    def uppercase_selection(self) -> None:
        self.transform_selection_sql_code_case(str.upper, "Uppercased selection")

    def lowercase_selection(self) -> None:
        self.transform_selection_sql_code_case(str.lower, "Lowercased selection")

    def undo_buffer(self) -> None:
        if self.state.buffer.undo():
            self.state.status = "Undo"
        else:
            self.state.status = "Nothing to undo"

    def redo_buffer(self) -> None:
        if self.state.buffer.redo():
            self.state.status = "Redo"
        else:
            self.state.status = "Nothing to redo"


class CompletionController:
    def __init__(
        self,
        state: UIState,
        dialogs: DialogPort,
        db_operations: DbOperationsPort,
        catalog: CatalogService,
        browser: BrowserController,
    ) -> None:
        self.state = state
        self.dialogs = dialogs
        self.db_operations = db_operations
        self.catalog = catalog
        self.browser = browser

    def generate_sql_with_columns(self) -> None:
        default_table = self.default_generate_sql_table()
        table_name = self.dialogs.prompt("Table or view", default_table)
        if table_name is None:
            self.state.status = "SQL generation cancelled"
            return
        table_name = table_name.upper()
        if not table_name:
            self.state.status = "No table or view selected"
            return
        columns = self.catalog.columns(table_name)
        if columns is None:
            fingerprint = self.capture_editor_context()

            def columns_loaded(loaded: list[str]) -> None:
                if not self.editor_context_is_current(fingerprint):
                    self.state.status = f"Loaded columns for {table_name}; retry SQL generation"
                    return
                self.finish_generate_sql_with_columns(table_name, loaded)

            def columns_failed(exc: Exception) -> None:
                self.state.status = f"Column metadata failed: {short_error(exc)}"

            self.catalog.load_columns(
                table_name,
                "column-metadata",
                f"Loading columns for {table_name}",
                on_success=columns_loaded,
                on_error=columns_failed,
            )
            return
        self.finish_generate_sql_with_columns(table_name, columns)

    def finish_generate_sql_with_columns(
        self,
        table_name: str,
        columns: list[str],
    ) -> None:
        if not columns:
            self.state.status = f"No columns found for {table_name}"
            return
        options = list(SQL_GENERATOR_CHOICES)
        choice = self.dialogs.pick("Generate SQL", options)
        if choice is None:
            self.state.status = "SQL generation cancelled"
            return
        sql = generated_sql_for_choice(options[choice], table_name, columns)
        if not sql:
            self.state.status = "SQL generation failed"
            return
        self.state.buffer.insert_text(sql)
        self.state.focus = FOCUS_EDITOR
        self.state.status = f"Inserted {options[choice]} for {table_name}"

    def capture_editor_context(self) -> EditorFingerprint:
        tab = self.state.active_tab
        buffer = tab.buffer
        return (
            tab,
            buffer.text(),
            buffer.row,
            buffer.col,
            buffer.selection_anchor,
            self.state.focus,
        )

    def editor_context_is_current(self, fingerprint: EditorFingerprint) -> bool:
        tab, text, row, col, selection_anchor, focus = fingerprint
        buffer = tab.buffer
        return (
            self.db_operations.completion_target_was_active
            and self.state.active_tab is tab
            and buffer.text() == text
            and buffer.row == row
            and buffer.col == col
            and buffer.selection_anchor == selection_anchor
            and self.state.focus == focus
        )

    def default_generate_sql_table(self) -> str:
        entry = self.browser.active_browser_entry() if self.state.focus == FOCUS_BROWSER else None
        browser_table = selected_browser_table_name(self.state.focus, entry)
        if browser_table:
            return browser_table
        buffer = self.state.buffer
        statement = statement_at_cursor(buffer.text(), buffer.row, buffer.col)
        if statement is None:
            return ""
        return generated_sql_table_from_statement(statement.text)

    def autocomplete_editor(self) -> None:
        buffer = self.state.buffer
        statement = statement_at_cursor(buffer.text(), buffer.row, buffer.col)
        statement_text = statement.text if statement is not None else buffer.text()
        context = completion_context_for_buffer(buffer, statement_text)
        if not context.prefix and context.qualifier is None:
            self.state.status = "No completion prefix"
            return
        if self.load_completion_metadata_if_needed(context):
            return
        candidates, metadata_error = self.completion_candidates(context)
        if not candidates:
            if metadata_error:
                self.state.status = f"Completion metadata failed: {metadata_error}"
                return
            target = (
                f"{context.qualifier}.{context.prefix}"
                if context.qualifier
                else context.prefix
            )
            self.state.status = f'No completions for "{target}"'
            return
        if len(candidates) == 1:
            self.apply_completion(context, candidates[0])
            return
        choice = self.dialogs.pick(
            "Complete",
            [candidate.label for candidate in candidates],
        )
        if choice is None:
            self.state.status = "Completion cancelled"
            return
        self.apply_completion(context, candidates[choice])

    def load_completion_metadata_if_needed(self, context: CompletionContext) -> bool:
        references = statement_table_references(context.statement)
        cached_objects = {
            object_type: list(names)
            for object_type, names in self.catalog.schema_objects.items()
        }
        need_objects = not self.state.browser_loaded
        if (
            context.qualifier is None
            and not references
            and keyword_completion_candidates(context.prefix)
        ):
            return False
        cached_column_names = set(self.state.schema_columns)
        if context.qualifier is not None:
            resolved = resolve_completion_qualifier(
                context.qualifier,
                references,
                cached_objects if self.state.browser_loaded else empty_schema_object_groups(),
            )
            table_names = [resolved] if resolved is not None else []
        else:
            table_names = ordered_reference_tables(references)
        missing_columns = [
            name for name in table_names if self.catalog.columns(name) is None
        ]
        if not need_objects and not missing_columns:
            return False

        fingerprint = self.capture_editor_context()

        def load_metadata(
            db: Any,
            progress: Callable[[str], None],
        ) -> tuple[dict[str, list[str]] | None, dict[str, list[str]]]:
            objects = db.list_schema_objects() if need_objects else None
            available_objects = objects if objects is not None else cached_objects
            names = table_names
            if context.qualifier is not None and not names:
                resolved_name = resolve_completion_qualifier(
                    context.qualifier,
                    references,
                    available_objects,
                )
                names = [resolved_name] if resolved_name is not None else []
            columns = {
                name.upper(): db.list_object_columns(name.upper())
                for name in names
                if name.upper() not in cached_column_names
            }
            return objects, columns

        def metadata_loaded(
            loaded: tuple[dict[str, list[str]] | None, dict[str, list[str]]],
        ) -> None:
            objects, columns = loaded
            if objects is not None:
                self.catalog.replace_schema_objects(objects)
            self.catalog.update_columns(columns)
            if not self.editor_context_is_current(fingerprint):
                self.state.status = "Completion metadata loaded; retry completion"
                return
            self.autocomplete_editor()

        def metadata_failed(exc: Exception) -> None:
            self.state.status = f"Completion metadata failed: {short_error(exc)}"

        self.db_operations.start(
            "completion-metadata",
            "Loading completion metadata",
            load_metadata,
            on_success=metadata_loaded,
            on_error=metadata_failed,
        )
        return True

    def completion_candidates(
        self,
        context: CompletionContext,
    ) -> tuple[list[CompletionCandidate], str]:
        references = statement_table_references(context.statement)
        candidates: list[CompletionCandidate] = []
        if context.qualifier is not None:
            schema_objects = (
                self.catalog.schema_objects
                if self.state.browser_loaded
                else empty_schema_object_groups()
            )
            table_name = resolve_completion_qualifier(
                context.qualifier,
                references,
                schema_objects,
            )
            if table_name is None:
                return [], ""
            columns, _column_error = self.completion_columns(table_name)
            candidates.extend(
                column_completion_candidates(columns, context.prefix, table_name)
            )
            return dedupe_completion_candidates(candidates), ""

        candidates.extend(keyword_completion_candidates(context.prefix))
        schema_objects, _object_error = self.completion_schema_objects()
        candidates.extend(object_completion_candidates(schema_objects, context.prefix))
        for table_name in ordered_reference_tables(references):
            columns, _column_error = self.completion_columns(table_name)
            candidates.extend(
                column_completion_candidates(columns, context.prefix, table_name)
            )
        return dedupe_completion_candidates(candidates), ""

    def completion_schema_objects(self) -> tuple[dict[str, list[str]], str]:
        return self.catalog.schema_objects, ""

    def completion_columns(self, object_name: str) -> tuple[list[str], str]:
        return self.catalog.columns(object_name) or [], ""

    def refresh_autocomplete_cache(self) -> None:
        self.catalog.refresh_autocomplete_cache()

    def apply_completion(
        self,
        context: CompletionContext,
        candidate: CompletionCandidate,
    ) -> None:
        buffer = self.state.buffer
        line = buffer.lines[context.row]
        new_line = (
            line[: context.start_col]
            + candidate.insert_text
            + line[context.end_col :]
        )
        new_col = context.start_col + len(candidate.insert_text)
        if new_line != line or buffer.row != context.row or buffer.col != new_col:
            buffer.record_undo()
            buffer.lines[context.row] = new_line
            buffer.row = context.row
            buffer.col = new_col
            buffer.clear_selection()
            buffer.refresh_dirty()
        source = f" {candidate.source}" if candidate.source else ""
        self.state.status = (
            f"Completed {candidate.kind}{source}: {candidate.insert_text}"
        )
