from __future__ import annotations

import curses
from typing import Any, Callable, Protocol

from ..db import (
    CellUpdateResult,
    NULL_DISPLAY_TOKEN,
    ResultColumnMetadata,
    RowInsertResult,
    oracledb,
)
from .clipboard import copy_to_system_clipboard
from .constants import (
    CTRL_C,
    ESC,
    FOCUS_EDITOR,
    FOCUS_RESULTS,
    KEY_CTRL_END,
    KEY_CTRL_HOME,
    RESULT_GRID,
    RESULT_ROW_DETAIL,
    TAB,
)
from .errors import wrap_error
from .ports import DbOperationsPort, DialogPort
from .results import (
    ResultCell,
    ResultInsertDraft,
    ResultPosition,
    clamp_cell_view_scroll,
    clamp_result_position,
    explain_plan_lines,
    first_editable_result_column,
    insert_draft_active_status,
    insert_draft_row,
    is_autocommit_enabled,
    is_database_connected,
    is_read_only_enabled,
    more_rows_status,
    selected_editable_cell,
    selected_result_cell,
    visible_table_columns,
)
from .state import ResultFetchMore, UIState


CopyToClipboard = Callable[[str], str | None]
DATE_EDIT_CHOICES = ("Enter ISO date/time", "SYSDATE")


class ResultDialogPort(DialogPort, Protocol):
    def show_cell_viewer(self, cell: ResultCell) -> None: ...


class ResultPresenterPort(Protocol):
    def apply_fetch_more_result(self, fetched: ResultFetchMore) -> None: ...

    def handle_fetch_more_error(self, exc: Exception) -> None: ...


class ResultController:
    """Own result focus, navigation, editing, paging, and insert drafts."""

    def __init__(
        self,
        state: UIState,
        dialogs: ResultDialogPort,
        db_operations: DbOperationsPort,
        presenter: ResultPresenterPort,
        *,
        copy_to_clipboard: CopyToClipboard = copy_to_system_clipboard,
    ) -> None:
        self.state = state
        self.dialogs = dialogs
        self.db_operations = db_operations
        self.presenter = presenter
        self.copy_to_clipboard = copy_to_clipboard

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
        self.state.result_mode = (
            RESULT_ROW_DETAIL if self.state.result_mode == RESULT_GRID else RESULT_GRID
        )
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
        if key == CTRL_C:
            self.copy_selected_result_cell()
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
        if self.db_operations.reject_if_active():
            return
        result = self.state.active_result
        if result is None:
            self.state.status = "No table result is available"
            return
        if not is_database_connected(self.state.db):
            self.state.status = (
                "Row inserts are unavailable while disconnected; reconnect first"
            )
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
        if not is_database_connected(self.state.db):
            self.state.status = (
                "Insert draft editing is unavailable while disconnected; reconnect first"
            )
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
        metadata = result.editable_context.column_metadata.get(self.state.result_col)
        new_value = self.prompt_cell_edit_value(
            f"Set {column_name}",
            current_value,
            metadata,
        )
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
        if (
            0 <= draft.row_index < len(draft.result.rows)
            and draft.result.rows[draft.row_index] is draft.row
        ):
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
        if self.db_operations.reject_if_active():
            return True
        if not is_database_connected(self.state.db):
            self.state.status = (
                "Row inserts are unavailable while disconnected; reconnect first"
            )
            return True
        if is_read_only_enabled(self.state.db):
            self.state.status = "Row inserts are disabled in read-only mode"
            return True
        values = {
            column_index: (
                draft.row[column_index]
                if column_index < len(draft.row)
                else NULL_DISPLAY_TOKEN
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

        self.db_operations.start(
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
            or self.state.result_row + page < len(result.rows)
        ):
            return False
        connected = is_database_connected(self.state.db)
        paging = more_rows_status(result, connected)
        if result.continuation is None:
            if paging:
                self.state.status = paging
                return True
            return False
        if not connected:
            self.state.status = paging
            return True
        if self.db_operations.reject_if_active():
            return True
        target_row = self.state.result_row + page
        continuation = result.continuation
        loaded_rows = len(result.rows)
        assert continuation is not None

        def fetch_worker(db: Any, progress: Callable[[str], None]) -> ResultFetchMore:
            fetched = db.fetch_more_rows(continuation, loaded_rows)
            return ResultFetchMore(result, fetched, target_row)

        self.db_operations.start(
            "fetch-more",
            f"Fetching next {self.state.config.max_rows} result row(s)",
            fetch_worker,
            on_success=self.presenter.apply_fetch_more_result,
            on_error=self.presenter.handle_fetch_more_error,
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
        self.state.explain_scroll = clamp_cell_view_scroll(
            self.state.explain_scroll,
            total,
            page,
        )
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
            self.state.status = (
                f"Results {mode}: row {row}/{row_total}, col {col}/{col_total} | "
                f"{insert_draft_active_status()}"
            )
            return
        edit = ""
        if not is_database_connected(self.state.db):
            edit = " | disconnected; loaded rows are view-only; reconnect to continue"
        elif result.editable_context is not None and not is_read_only_enabled(self.state.db):
            edit = " | Enter edits cell | INS inserts row"
        status = (
            f"Results {mode}: row {row}/{row_total}, col {col}/{col_total} | "
            f"F10 views cell{edit} | Ctrl-C copies cell"
        )
        paging = more_rows_status(result, is_database_connected(self.state.db))
        self.state.status = f"{status} | {paging}" if paging else status

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
        if self.db_operations.reject_if_active():
            return
        result = self.state.active_result
        if result is None:
            self.state.status = "No table result is available"
            return
        if not is_database_connected(self.state.db):
            self.state.status = (
                "Cell updates are unavailable while disconnected; reconnect first"
            )
            return
        if is_read_only_enabled(self.state.db):
            self.state.status = "Cell updates are disabled in read-only mode"
            return
        cell, message = selected_editable_cell(
            result,
            self.state.result_row,
            self.state.result_col,
        )
        if cell is None:
            self.state.status = message
            return
        assert result.editable_context is not None
        context = result.editable_context
        column_index = self.state.result_col
        default_value = "" if cell.current_value == NULL_DISPLAY_TOKEN else cell.current_value
        new_value = self.prompt_cell_edit_value(
            f"Set {cell.table_column}",
            default_value,
            context.column_metadata.get(column_index),
        )
        if new_value is None:
            self.state.status = "Edit cancelled"
            return
        row_index = self.state.result_row

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

        self.db_operations.start(
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

    def prompt_cell_edit_value(
        self,
        label: str,
        default_value: str,
        metadata: ResultColumnMetadata | None,
    ) -> str | None:
        if metadata is not None and metadata.type_code in {
            oracledb.DB_TYPE_DATE,
            oracledb.DB_TYPE_TIMESTAMP,
        }:
            choice = self.dialogs.pick(label, list(DATE_EDIT_CHOICES))
            if choice is None:
                return None
            if DATE_EDIT_CHOICES[choice] == "SYSDATE":
                return "SYSDATE"
        return self.dialogs.prompt(label, default_value, strip=False)

    def copy_selected_result_cell(self) -> None:
        result = self.state.active_result
        if result is None:
            self.state.status = "No table result is available"
            return
        cell, message = selected_result_cell(
            result,
            self.state.result_row,
            self.state.result_col,
        )
        if cell is None:
            self.state.status = message
            return
        self.state.internal_clipboard = cell.value
        provider = self.copy_to_clipboard(cell.value)
        if provider:
            self.state.status = f"Copied {len(cell.value)} char(s) to {provider}"
        else:
            self.state.status = f"Copied {len(cell.value)} char(s) internally"

    def view_selected_result_cell(self) -> None:
        result = self.state.active_result
        if result is None:
            self.state.status = "No table result is available"
            return
        cell, message = selected_result_cell(
            result,
            self.state.result_row,
            self.state.result_col,
        )
        if cell is None:
            self.state.status = message
            return
        self.dialogs.show_cell_viewer(cell)
        self.state.status = f"Viewed {cell.column_name}"
