from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .browser import (
    browser_panel_width,
    clamp_browser_row,
    flatten_browser_entries,
    visible_tab_labels,
)
from .constants import (
    FOCUS_BROWSER,
    FOCUS_EDITOR,
    FOCUS_RESULTS,
    RESULT_GRID,
    RESULT_RATIO_EDITOR_FULLSCREEN,
    RESULT_RATIO_FULLSCREEN,
    RESULT_RATIO_GRID_SPLIT,
    RESULT_ROW_DETAIL,
    RESULT_STYLE_HELP,
)
from .results import (
    clamp_cell_view_scroll,
    clamp_result_position,
    editor_result_pane_heights,
    explain_plan_lines,
    result_pane_is_editor_fullscreen,
    result_pane_is_fullscreen,
    result_pane_status,
    result_pane_tab_height,
    row_detail_lines,
    scroll_start,
    table_column_widths,
    visible_table_columns,
    wrapped_dbms_output_lines,
)
from .state import DocumentState, UIState, normalize_document_state


class ScreenSizePort(Protocol):
    def getmaxyx(self) -> tuple[int, int]: ...


class ResultStatusPort(Protocol):
    def update_explain_status(self) -> None: ...

    def update_result_status(self) -> None: ...


@dataclass(frozen=True)
class LayoutSnapshot:
    height: int
    width: int
    grid_fullscreen: bool
    editor_fullscreen: bool
    usable_height: int
    tab_height: int
    browser_width: int
    content_x: int
    content_width: int
    editor_height: int
    result_height: int
    browser_body_height: int


def build_layout_snapshot(state: UIState, height: int, width: int) -> LayoutSnapshot:
    """Calculate terminal geometry without mutating UI state."""
    height = max(0, height)
    width = max(1, width)
    grid_fullscreen = state.result_grid_fullscreen and state.active_result is not None
    editor_fullscreen = result_pane_is_editor_fullscreen(state.result_ratio)
    if grid_fullscreen:
        return LayoutSnapshot(
            height,
            width,
            True,
            False,
            height,
            0,
            0,
            0,
            width,
            0,
            height,
            0,
        )
    if editor_fullscreen:
        return LayoutSnapshot(
            height,
            width,
            False,
            True,
            height,
            0,
            0,
            0,
            width,
            height,
            0,
            0,
        )
    status_h = 1
    header_h = 1
    tab_h = result_pane_tab_height(state.result_ratio)
    usable_h = max(3, height - status_h - header_h)
    content_usable_h = max(3, usable_h - tab_h)
    browser_w = browser_panel_width(width) if state.browser_visible else 0
    content_x = browser_w + 1 if state.browser_visible else 0
    content_w = max(1, width - content_x)
    editor_h, result_h = editor_result_pane_heights(content_usable_h, state.result_ratio)
    return LayoutSnapshot(
        height,
        width,
        False,
        False,
        usable_h,
        tab_h,
        browser_w,
        content_x,
        content_w,
        max(0, editor_h),
        max(0, result_h),
        max(0, usable_h - 1),
    )


def reveal_selection(selected: int, scroll: int, visible: int, total: int) -> tuple[int, int]:
    """Clamp a selected index and reveal it inside a scroll window."""
    if total <= 0:
        return 0, 0
    selected = min(max(selected, 0), total - 1)
    visible = max(1, visible)
    scroll = clamp_cell_view_scroll(scroll, total, visible)
    if selected < scroll:
        scroll = selected
    elif selected >= scroll + visible:
        scroll = selected - visible + 1
    return selected, scroll


def visible_tab_scroll(documents: DocumentState, width: int) -> int:
    """Return the first tab index that keeps the active tab visible."""
    normalized = normalize_document_state(documents)
    active = normalized.active_tab_idx
    scroll = normalized.tab_scroll
    if active < scroll:
        return active
    while active not in [
        idx for idx, _, _ in visible_tab_labels(normalized.tabs, scroll, width)
    ]:
        if scroll >= active:
            break
        scroll += 1
    return scroll


class ViewportController:
    def __init__(self, screen: ScreenSizePort, state: UIState, results: ResultStatusPort):
        self.screen = screen
        self.state = state
        self.results = results

    def prepare_frame(self) -> LayoutSnapshot:
        """Normalize viewport state before the renderer reads it."""
        try:
            height, width = self.screen.getmaxyx()
        except Exception:
            height, width = 24, 120
        self.state.documents = normalize_document_state(self.state.documents)
        snapshot = build_layout_snapshot(self.state, height, width)
        self.state.tab_scroll = visible_tab_scroll(self.state.documents, snapshot.content_width)
        self._prepare_browser(snapshot)
        self._prepare_editor(snapshot)
        self._prepare_results(snapshot)
        return snapshot

    def _prepare_browser(self, snapshot: LayoutSnapshot) -> None:
        entries = flatten_browser_entries(
            self.state.browser_objects,
            self.state.browser_expanded,
            self.state.browser_filter,
        )
        visible = max(1, snapshot.browser_body_height)
        row = clamp_browser_row(self.state.browser_row, entries)
        row, scroll = reveal_selection(row, self.state.browser_scroll, visible, len(entries))
        self.state.browser_row = row
        self.state.browser_scroll = scroll
        self.state.browser_page_size = visible

    def _prepare_editor(self, snapshot: LayoutSnapshot) -> None:
        if snapshot.editor_height <= 0:
            return
        buffer = self.state.buffer
        if buffer.row < buffer.scroll:
            buffer.scroll = buffer.row
        elif buffer.row >= buffer.scroll + snapshot.editor_height:
            buffer.scroll = buffer.row - snapshot.editor_height + 1

    def _prepare_results(self, snapshot: LayoutSnapshot) -> None:
        result_height = snapshot.result_height
        body_height = max(0, result_height - 1)
        tab = self.state.active_tab
        if self.state.show_dbms_output and self.state.dbms_output:
            wrapped = wrapped_dbms_output_lines(self.state.dbms_output, snapshot.content_width)
            if tab.dbms_output_scroll is not None:
                tab.dbms_output_scroll = scroll_start(
                    tab.dbms_output_scroll,
                    len(wrapped),
                    body_height,
                )
            return
        if self.state.explain_result is not None:
            lines = explain_plan_lines(self.state.explain_result)
            self.state.explain_page_size = max(1, body_height)
            self.state.explain_scroll = clamp_cell_view_scroll(
                self.state.explain_scroll,
                len(lines),
                body_height,
            )
            return
        result = self.state.active_result
        if result is None:
            lines_count = len(tab.help_lines) if self.state.results_style == RESULT_STYLE_HELP else len(self.state.results)
            if tab.results_scroll is not None:
                tab.results_scroll = scroll_start(tab.results_scroll, lines_count, body_height)
            return
        position = clamp_result_position(
            result,
            self.state.result_row,
            self.state.result_col,
            self.state.result_row_scroll,
            self.state.result_col_scroll,
        )
        self.state.result_row = position.row
        self.state.result_col = position.col
        self.state.result_row_scroll = position.row_scroll
        self.state.result_col_scroll = position.col_scroll
        if self.state.result_mode == RESULT_ROW_DETAIL:
            self.state.result_page_size = max(1, body_height)
            _, self.state.result_col_scroll = reveal_selection(
                self.state.result_col,
                self.state.result_col_scroll,
                body_height,
                len(result.columns),
            )
            while (
                body_height > 0
                and self.state.result_col_scroll < self.state.result_col
                and not any(
                    field_idx == self.state.result_col
                    for field_idx, _ in row_detail_lines(
                        result,
                        self.state.result_row,
                        snapshot.content_width,
                        self.state.result_col_scroll,
                    )[:body_height]
                )
            ):
                self.state.result_col_scroll += 1
            return
        label_height = 0 if snapshot.grid_fullscreen else 1
        visible_rows = max(0, result_height - label_height - 2)
        self.state.result_page_size = max(1, visible_rows)
        self.state.result_row, self.state.result_row_scroll = reveal_selection(
            self.state.result_row,
            self.state.result_row_scroll,
            visible_rows,
            len(result.rows),
        )
        widths = table_column_widths(result)
        if self.state.result_col < self.state.result_col_scroll:
            self.state.result_col_scroll = self.state.result_col
        selected_width = min(
            widths[self.state.result_col],
            snapshot.content_width,
        )
        while self.state.result_col_scroll < self.state.result_col:
            visible = visible_table_columns(
                widths,
                self.state.result_col_scroll,
                snapshot.content_width,
            )
            if any(
                column.index == self.state.result_col and column.width == selected_width
                for column in visible
            ):
                break
            self.state.result_col_scroll += 1

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
        snapshot = build_layout_snapshot(self.state, height, width)
        return (
            snapshot.editor_height,
            snapshot.result_height,
            snapshot.content_width,
            snapshot.browser_body_height,
        )

    def scroll_editor_window(self, delta: int) -> None:
        editor_h, _, _, _ = self.current_pane_sizes()
        if editor_h <= 0:
            self.state.focus = FOCUS_RESULTS
            self.state.status = result_pane_status(self.state.result_ratio)
            return
        buffer = self.state.buffer
        buffer.scroll = clamp_cell_view_scroll(buffer.scroll + delta, len(buffer.lines), editor_h)
        if buffer.row < buffer.scroll:
            buffer.row = buffer.scroll
            buffer.col = min(buffer.col, len(buffer.lines[buffer.row]))
            buffer.clear_selection()
        elif buffer.row >= buffer.scroll + editor_h:
            buffer.row = min(len(buffer.lines) - 1, buffer.scroll + editor_h - 1)
            buffer.col = min(buffer.col, len(buffer.lines[buffer.row]))
            buffer.clear_selection()

    def scroll_browser_window(self, delta: int) -> None:
        entries = flatten_browser_entries(
            self.state.browser_objects,
            self.state.browser_expanded,
            self.state.browser_filter,
        )
        if not entries:
            self.state.browser_row = 0
            self.state.browser_scroll = 0
            return
        visible = max(1, self.state.browser_page_size)
        self.state.browser_scroll = clamp_cell_view_scroll(
            self.state.browser_scroll + delta,
            len(entries),
            visible,
        )
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

    def scroll_results_page(self, direction: int) -> None:
        if direction == 0:
            return
        if self.state.show_dbms_output and self.state.dbms_output:
            _, result_h, _, _ = self.current_pane_sizes()
            page = max(1, result_h - 1)
        elif self.state.explain_result is not None:
            page = max(1, self.state.explain_page_size)
        elif self.state.active_result is not None:
            page = max(1, self.state.result_page_size)
        else:
            _, result_h, _, _ = self.current_pane_sizes()
            page = max(1, result_h - 1)
        self.scroll_results_window(direction * page)

    def scroll_explain_window(self, delta: int) -> None:
        result = self.state.explain_result
        if result is None:
            return
        page = max(1, self.state.explain_page_size)
        total = len(explain_plan_lines(result))
        self.state.explain_scroll = clamp_cell_view_scroll(
            self.state.explain_scroll + delta,
            total,
            page,
        )
        self.results.update_explain_status()

    def scroll_result_grid_window(self, delta: int) -> None:
        result = self.state.active_result
        if result is None:
            return
        page = max(1, self.state.result_page_size)
        total = len(result.rows)
        self.state.result_row_scroll = clamp_cell_view_scroll(
            self.state.result_row_scroll + delta,
            total,
            page,
        )
        if total:
            self.state.result_row = min(max(self.state.result_row, 0), total - 1)
            if self.state.result_row < self.state.result_row_scroll:
                self.state.result_row = self.state.result_row_scroll
            elif self.state.result_row >= self.state.result_row_scroll + page:
                self.state.result_row = min(total - 1, self.state.result_row_scroll + page - 1)
        else:
            self.state.result_row = 0
        self.results.update_result_status()

    def scroll_result_detail_window(self, delta: int) -> None:
        result = self.state.active_result
        if result is None:
            return
        page = max(1, self.state.result_page_size)
        total = len(result.columns)
        self.state.result_col_scroll = clamp_cell_view_scroll(
            self.state.result_col_scroll + delta,
            total,
            page,
        )
        if total:
            self.state.result_col = min(max(self.state.result_col, 0), total - 1)
            if self.state.result_col < self.state.result_col_scroll:
                self.state.result_col = self.state.result_col_scroll
            elif self.state.result_col >= self.state.result_col_scroll + page:
                self.state.result_col = min(total - 1, self.state.result_col_scroll + page - 1)
        else:
            self.state.result_col = 0
        self.results.update_result_status()

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
