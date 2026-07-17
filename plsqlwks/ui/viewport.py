from __future__ import annotations

from typing import Protocol

from .browser import browser_panel_width, clamp_browser_row, flatten_browser_entries
from .constants import (
    FOCUS_BROWSER,
    FOCUS_EDITOR,
    FOCUS_RESULTS,
    RESULT_GRID,
    RESULT_RATIO_EDITOR_FULLSCREEN,
    RESULT_RATIO_FULLSCREEN,
    RESULT_RATIO_GRID_SPLIT,
    RESULT_ROW_DETAIL,
)
from .results import (
    clamp_cell_view_scroll,
    editor_result_pane_heights,
    explain_plan_lines,
    result_pane_is_editor_fullscreen,
    result_pane_is_fullscreen,
    result_pane_status,
    result_pane_tab_height,
    scroll_start,
    wrapped_dbms_output_lines,
)
from .state import UIState


class ScreenSizePort(Protocol):
    def getmaxyx(self) -> tuple[int, int]: ...


class ResultStatusPort(Protocol):
    def update_explain_status(self) -> None: ...

    def update_result_status(self) -> None: ...


class ViewportController:
    def __init__(self, screen: ScreenSizePort, state: UIState, results: ResultStatusPort):
        self.screen = screen
        self.state = state
        self.results = results

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
