from __future__ import annotations

import curses
from pathlib import Path
import queue
import time
import threading
from typing import Any, Callable

from ..config import save_autocommit
from ..db import (
    CellUpdateResult,
    ExplainPlanResult,
    NULL_DISPLAY_TOKEN,
    OracleWorkspace,
    QueryResult,
    QueryResultPage,
    RowInsertResult,
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

class AppResultsMixin:
    def enter_results_focus(self) -> None:
        if self.state.active_result is None and self.state.explain_result is None:
            if self.state.show_dbms_output and self.state.dbms_output:
                self.state.focus = FOCUS_RESULTS
                self.state.status = f"DBMS_OUTPUT: {len(self.state.dbms_output)} line(s)"
                return
            if self.state.results:
                self.state.focus = FOCUS_RESULTS
                self.state.status = "Results transcript"
                return
            self.state.status = "No table result is available"
            return
        self.state.show_dbms_output = False
        self.state.focus = FOCUS_RESULTS
        if self.state.explain_result is not None:
            self.update_explain_status()
            return
        self.clamp_result_selection()
        self.update_result_status()

    def leave_results_focus(self) -> None:
        self.state.focus = FOCUS_EDITOR
        self.state.status = "Editor focus"

    def toggle_result_mode(self) -> None:
        if self.state.active_result is None:
            self.state.status = "No table result is available"
            return
        if self.active_insert_draft() is not None:
            self.state.status = insert_draft_active_status()
            return
        self.state.show_dbms_output = False
        self.state.result_mode = RESULT_ROW_DETAIL if self.state.result_mode == RESULT_GRID else RESULT_GRID
        self.clamp_result_selection()
        self.update_result_status()

    def toggle_dbms_output_view(self) -> None:
        if not self.state.dbms_output:
            self.state.status = "No DBMS_OUTPUT is available"
            return
        self.state.show_dbms_output = not self.state.show_dbms_output
        if self.state.show_dbms_output:
            self.state.focus = FOCUS_EDITOR
            count = len(self.state.dbms_output)
            self.state.status = f"DBMS_OUTPUT: {count} line(s)"
            return
        if self.state.active_result is not None:
            self.clamp_result_selection()
            self.update_result_status()
            return
        self.state.status = "Results transcript"

    def handle_results_key(self, key: int | str) -> None:
        if key == ESC and self.cancel_insert_draft_if_selected():
            return
        if key in (TAB, ESC):
            self.leave_results_focus()
            return
        if self.state.explain_result is not None:
            self.handle_explain_results_key(key)
            return
        if self.state.active_result is None:
            self.leave_results_focus()
            return
        page = max(1, self.state.result_page_size)
        draft = self.active_insert_draft()
        if key == curses.KEY_IC:
            self.start_insert_draft_row()
            return
        if draft is not None and key in (
            curses.KEY_UP,
            curses.KEY_DOWN,
            curses.KEY_PPAGE,
            curses.KEY_NPAGE,
            KEY_CTRL_HOME,
            KEY_CTRL_END,
        ):
            self.state.result_row = draft.row_index
            self.state.status = insert_draft_active_status()
            return
        if key == curses.KEY_UP:
            self.move_result_selection(delta_row=-1)
        elif key == curses.KEY_DOWN:
            self.move_result_selection(delta_row=1)
        elif key == curses.KEY_LEFT:
            self.move_result_selection(delta_col=-1)
        elif key == curses.KEY_RIGHT:
            self.move_result_selection(delta_col=1)
        elif key == curses.KEY_PPAGE:
            self.move_result_selection(delta_row=-page)
        elif key == curses.KEY_NPAGE:
            if self.fetch_next_result_page_if_needed(page):
                return
            self.move_result_selection(delta_row=page)
        elif key == curses.KEY_HOME:
            self.move_result_selection(to_col=0)
        elif key == curses.KEY_END:
            result = self.state.active_result
            self.move_result_selection(to_col=max(0, len(result.columns) - 1))
        elif key == KEY_CTRL_HOME:
            self.move_result_selection(to_row=0)
        elif key == KEY_CTRL_END:
            result = self.state.active_result
            self.move_result_selection(to_row=max(0, len(result.rows) - 1))
        elif key == curses.KEY_F8:
            self.toggle_result_mode()
            return
        elif key == curses.KEY_F10:
            self.view_selected_result_cell()
            return
        elif key in (10, 13):
            if self.selected_insert_draft() is not None:
                self.edit_insert_draft_cell()
            else:
                self.edit_selected_result_cell()
            return
        else:
            return
        self.update_result_status()

    def active_insert_draft(self) -> ResultInsertDraft | None:
        draft = self.state.active_tab.result_insert_draft
        result = self.state.active_result
        if draft is None:
            return None
        if (
            result is not None
            and draft.result is result
            and 0 <= draft.row_index < len(result.rows)
            and result.rows[draft.row_index] is draft.row
        ):
            return draft
        self.state.active_tab.result_insert_draft = None
        return None

    def selected_insert_draft(self) -> ResultInsertDraft | None:
        draft = self.active_insert_draft()
        if draft is None or self.state.result_row != draft.row_index:
            return None
        return draft

    def start_insert_draft_row(self) -> None:
        if self.reject_if_db_operation_active():
            return
        result = self.state.active_result
        if result is None:
            self.state.status = "No table result is available"
            return
        if self.state.result_mode != RESULT_GRID:
            self.state.status = "Insert row is only available in result grid"
            return
        if is_read_only_enabled(self.state.db):
            self.state.status = "Row inserts are disabled in read-only mode"
            return
        if result.editable_context is None:
            self.state.status = result.edit_message or "Result is not ROWID-editable"
            return
        draft = self.active_insert_draft()
        if draft is not None:
            self.state.result_row = draft.row_index
            self.state.result_row_scroll = draft.row_index
            self.state.result_col = first_editable_result_column(result)
            self.state.status = insert_draft_active_status()
            return
        row = insert_draft_row(result)
        result.rows.insert(0, row)
        result.original_rows.insert(0, [None] * len(result.columns))
        draft = ResultInsertDraft(result, 0, row)
        self.state.active_tab.result_insert_draft = draft
        self.state.result_row = draft.row_index
        self.state.result_row_scroll = draft.row_index
        self.state.result_col = first_editable_result_column(result)
        self.clamp_result_selection()
        self.state.status = insert_draft_active_status()

    def edit_insert_draft_cell(self) -> None:
        draft = self.selected_insert_draft()
        result = self.state.active_result
        if draft is None or result is None or result.editable_context is None:
            self.state.status = "No insert draft is active"
            return
        column_name = result.editable_context.editable_columns.get(self.state.result_col)
        if column_name is None:
            self.state.status = "ROWID column is read-only"
            return
        current_value = (
            draft.row[self.state.result_col]
            if self.state.result_col < len(draft.row)
            else NULL_DISPLAY_TOKEN
        )
        new_value = self.prompt(f"Set {column_name}", current_value, strip=False)
        if new_value is None:
            self.state.status = "Edit cancelled"
            return
        draft.row[self.state.result_col] = new_value
        self.state.status = f"Set {column_name} in insert draft"

    def cancel_insert_draft_if_selected(self) -> bool:
        draft = self.selected_insert_draft()
        if draft is None:
            return False
        self.remove_insert_draft(draft)
        self.clamp_result_selection()
        self.state.status = "Insert draft cancelled"
        return True

    def remove_insert_draft(self, draft: ResultInsertDraft) -> None:
        if 0 <= draft.row_index < len(draft.result.rows) and draft.result.rows[draft.row_index] is draft.row:
            draft.result.rows.pop(draft.row_index)
            if draft.row_index < len(draft.result.original_rows):
                draft.result.original_rows.pop(draft.row_index)
        self.state.active_tab.result_insert_draft = None

    def discard_insert_draft(self) -> None:
        draft = self.active_insert_draft()
        if draft is not None:
            self.remove_insert_draft(draft)
        self.state.active_tab.result_insert_draft = None

    def commit_insert_draft_if_active(self) -> bool:
        if self.state.focus != FOCUS_RESULTS:
            return False
        draft = self.active_insert_draft()
        result = self.state.active_result
        if draft is None or result is None or result.editable_context is None:
            return False
        if self.reject_if_db_operation_active():
            return True
        if is_read_only_enabled(self.state.db):
            self.state.status = "Row inserts are disabled in read-only mode"
            return True
        values = {
            column_index: (
                draft.row[column_index] if column_index < len(draft.row) else NULL_DISPLAY_TOKEN
            )
            for column_index in result.editable_context.editable_columns
        }
        context = result.editable_context
        column_count = len(result.columns)

        def inserted(refreshed: RowInsertResult | list[str]) -> None:
            if self.state.active_result is not result or self.active_insert_draft() is not draft:
                self.state.status = "Inserted row result discarded because the grid changed"
                return
            if isinstance(refreshed, RowInsertResult):
                result.rows[draft.row_index] = refreshed.display_values
                result.original_rows[draft.row_index] = refreshed.values
            else:
                result.rows[draft.row_index] = refreshed
                result.original_rows[draft.row_index] = list(refreshed)
            self.state.active_tab.result_insert_draft = None
            suffix = "" if is_autocommit_enabled(self.state.db) else " (pending commit)"
            self.state.status = f"Inserted row{suffix}"

        def insert_failed(exc: Exception) -> None:
            details = wrap_error(exc)
            detail = details[0] if details else str(exc)
            self.state.result_row = draft.row_index
            self.state.status = f"Insert failed: {detail}"

        self.start_db_operation(
            "insert-row",
            "Inserting row",
            lambda db, progress: db.insert_row_for_result(context, values, column_count),
            on_success=inserted,
            on_error=insert_failed,
        )
        return True

    def fetch_next_result_page_if_needed(self, page: int) -> bool:
        result = self.state.active_result
        if (
            result is None
            or self.state.result_mode != RESULT_GRID
            or result.continuation is None
            or self.state.result_row + page < len(result.rows)
        ):
            return False
        if self.reject_if_db_operation_active():
            return True
        target_row = self.state.result_row + page
        continuation = result.continuation
        loaded_rows = len(result.rows)
        assert continuation is not None

        def fetch_worker(db: Any, progress: Callable[[str], None]) -> ResultFetchMore:
            fetched = db.fetch_more_rows(continuation, loaded_rows)
            return ResultFetchMore(result, fetched, target_row)

        self.start_db_operation(
            "fetch-more",
            f"Fetching next {self.state.config.max_rows} result row(s)",
            fetch_worker,
        )
        return True

    def handle_explain_results_key(self, key: int | str) -> None:
        result = self.state.explain_result
        if result is None:
            self.leave_results_focus()
            return
        page = max(1, self.state.explain_page_size)
        total = len(explain_plan_lines(result))
        if key == curses.KEY_UP:
            self.state.explain_scroll -= 1
        elif key == curses.KEY_DOWN:
            self.state.explain_scroll += 1
        elif key == curses.KEY_PPAGE:
            self.state.explain_scroll -= page
        elif key == curses.KEY_NPAGE:
            self.state.explain_scroll += page
        elif key == curses.KEY_HOME:
            self.state.explain_scroll = 0
        elif key == curses.KEY_END:
            self.state.explain_scroll = total
        else:
            return
        self.state.explain_scroll = clamp_cell_view_scroll(self.state.explain_scroll, total, page)
        self.update_explain_status()

    def move_result_selection(
        self,
        delta_row: int = 0,
        delta_col: int = 0,
        to_row: int | None = None,
        to_col: int | None = None,
    ) -> None:
        row = self.state.result_row + delta_row if to_row is None else to_row
        col = self.state.result_col + delta_col if to_col is None else to_col
        pos = clamp_result_position(
            self.state.active_result,
            row,
            col,
            self.state.result_row_scroll,
            self.state.result_col_scroll,
        )
        self.apply_result_position(pos)

    def clamp_result_selection(self) -> None:
        pos = clamp_result_position(
            self.state.active_result,
            self.state.result_row,
            self.state.result_col,
            self.state.result_row_scroll,
            self.state.result_col_scroll,
        )
        self.apply_result_position(pos)

    def apply_result_position(self, position: ResultPosition) -> None:
        self.state.result_row = position.row
        self.state.result_col = position.col
        self.state.result_row_scroll = position.row_scroll
        self.state.result_col_scroll = position.col_scroll

    def ensure_selected_row_visible(self, visible_rows: int) -> None:
        if visible_rows <= 0:
            return
        if self.state.result_row < self.state.result_row_scroll:
            self.state.result_row_scroll = self.state.result_row
        if self.state.result_row >= self.state.result_row_scroll + visible_rows:
            self.state.result_row_scroll = self.state.result_row - visible_rows + 1

    def ensure_selected_column_visible(self, widths: list[int], width: int) -> None:
        if self.state.result_col < self.state.result_col_scroll:
            self.state.result_col_scroll = self.state.result_col
        while self.state.result_col_scroll < self.state.result_col:
            visible = visible_table_columns(widths, self.state.result_col_scroll, width)
            if any(column.index == self.state.result_col for column in visible):
                return
            self.state.result_col_scroll += 1

    def ensure_selected_detail_field_visible(self, visible_lines: int) -> None:
        if visible_lines <= 0:
            return
        if self.state.result_col < self.state.result_col_scroll:
            self.state.result_col_scroll = self.state.result_col
        if self.state.result_col >= self.state.result_col_scroll + visible_lines:
            self.state.result_col_scroll = self.state.result_col - visible_lines + 1

    def update_result_status(self) -> None:
        result = self.state.active_result
        if result is None:
            self.state.status = "No table result is available"
            return
        row_total = len(result.rows)
        col_total = len(result.columns)
        row = min(self.state.result_row + 1, row_total) if row_total else 0
        col = min(self.state.result_col + 1, col_total) if col_total else 0
        mode = "row detail" if self.state.result_mode == RESULT_ROW_DETAIL else "grid"
        if self.active_insert_draft() is not None:
            self.state.status = f"Results {mode}: row {row}/{row_total}, col {col}/{col_total} | {insert_draft_active_status()}"
            return
        edit = ""
        if result.editable_context is not None and not is_read_only_enabled(self.state.db):
            edit = " | Enter edits cell | INS inserts row"
        self.state.status = f"Results {mode}: row {row}/{row_total}, col {col}/{col_total} | F10 views cell{edit}"

    def update_explain_status(self) -> None:
        result = self.state.explain_result
        if result is None:
            self.state.status = "No explain plan is available"
            return
        total = len(explain_plan_lines(result))
        page = max(1, self.state.explain_page_size)
        start = min(self.state.explain_scroll + 1, total) if total else 0
        end = min(self.state.explain_scroll + page, total)
        self.state.status = f"Explain plan: lines {start}-{end}/{total}"

    def edit_selected_result_cell(self) -> None:
        if self.reject_if_db_operation_active():
            return
        result = self.state.active_result
        if result is None:
            self.state.status = "No table result is available"
            return
        if is_read_only_enabled(self.state.db):
            self.state.status = "Cell updates are disabled in read-only mode"
            return
        cell, message = selected_editable_cell(result, self.state.result_row, self.state.result_col)
        if cell is None:
            self.state.status = message
            return
        default_value = "" if cell.current_value == NULL_DISPLAY_TOKEN else cell.current_value
        new_value = self.prompt(f"Set {cell.table_column}", default_value, strip=False)
        if new_value is None:
            self.state.status = "Edit cancelled"
            return
        assert result.editable_context is not None
        context = result.editable_context
        row_index = self.state.result_row
        column_index = self.state.result_col

        def updated(refreshed: CellUpdateResult | str) -> None:
            if self.state.active_result is not result:
                self.state.status = "Cell update result discarded because the grid changed"
                return
            if isinstance(refreshed, CellUpdateResult):
                result.rows[row_index][column_index] = refreshed.display
                if result.original_rows:
                    result.original_rows[row_index][column_index] = refreshed.value
            else:
                result.rows[row_index][column_index] = refreshed
            suffix = "" if is_autocommit_enabled(self.state.db) else " (pending commit)"
            self.state.status = f"Updated {cell.table_column}{suffix}"

        def update_failed(exc: Exception) -> None:
            details = wrap_error(exc)
            detail = details[0] if details else str(exc)
            self.state.status = f"Cell update failed: {detail}"

        self.start_db_operation(
            "update-cell",
            f"Updating {cell.table_column}",
            lambda db, progress: db.update_cell_by_rowid(
                context,
                cell.rowid,
                column_index,
                cell.original_value,
                new_value,
            ),
            on_success=updated,
            on_error=update_failed,
        )

    def view_selected_result_cell(self) -> None:
        result = self.state.active_result
        if result is None:
            self.state.status = "No table result is available"
            return
        cell, message = selected_result_cell(result, self.state.result_row, self.state.result_col)
        if cell is None:
            self.state.status = message
            return
        self.show_cell_viewer(cell)
        self.state.status = f"Viewed {cell.column_name}"

    def show_cell_viewer(self, cell: ResultCell) -> None:
        scroll = 0
        while True:
            height, width = self.screen.getmaxyx()
            box_h = min(max(7, height - 2), max(7, int(height * 0.75)))
            box_w = min(max(30, width - 2), 100)
            box_h = min(box_h, height)
            box_w = min(box_w, width)
            top = max(0, (height - box_h) // 2)
            left = max(0, (width - box_w) // 2)
            body_h = max(1, box_h - 4)
            body_w = max(1, box_w - 4)
            lines = cell_view_lines(cell, body_w)
            scroll = clamp_cell_view_scroll(scroll, len(lines), body_h)

            win = curses.newwin(box_h, box_w, top, left)
            win.keypad(True)
            win.box()
            safe_window_addstr(win, 0, 2, " Cell ")
            footer = "Esc/Enter/F10 close  Up/Down scroll"
            safe_window_addstr(win, box_h - 1, 2, fit_text(footer, max(0, box_w - 4)))
            for idx in range(body_h):
                line_idx = scroll + idx
                text = lines[line_idx] if line_idx < len(lines) else ""
                safe_window_addstr(win, 2 + idx, 2, fit_text(text, body_w))
            if len(lines) > body_h:
                marker = f"{scroll + 1}-{min(scroll + body_h, len(lines))}/{len(lines)}"
                safe_window_addstr(win, 1, max(2, box_w - display_width(marker) - 2), marker)
            win.refresh()

            key = self.read_key(win, idle_timeout=-1)
            if key in (ESC, curses.KEY_F10, 10, 13):
                return
            if key == curses.KEY_UP:
                scroll -= 1
            elif key == curses.KEY_DOWN:
                scroll += 1
            elif key == curses.KEY_PPAGE:
                scroll -= body_h
            elif key == curses.KEY_NPAGE:
                scroll += body_h
            elif key == curses.KEY_HOME:
                scroll = 0
            elif key == curses.KEY_END:
                scroll = len(lines)
