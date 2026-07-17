from __future__ import annotations

import curses
from typing import Protocol

from .browser import (
    BrowserEntry,
    browser_entry_text,
    browser_panel_width,
    clamp_browser_row,
    tab_display_title,
    visible_tab_labels,
)
from .constants import (
    COLOR_SYNTAX_OPERATOR,
    FOCUS_BROWSER,
    FOCUS_RESULTS,
    HELP_BORDER,
    HELP_KEY,
    HELP_SECTION,
    HELP_TIP,
    HELP_TITLE,
    PLAN_COLOR_PAIR_BY_KIND,
    PLAN_CONNECTOR,
    PLAN_METRICS,
    PLAN_OBJECT,
    PLAN_OPERATION,
    PLAN_TEXT,
    RESULT_GRID,
    RESULT_ROW_DETAIL,
    RESULT_STYLE_HELP,
    SYNTAX_DEFAULT,
    _SYNTAX_COLOR_ATTRS,
    _SYNTAX_FALLBACK_ATTRS,
    configured_plan_color_pairs,
    syntax_color_palette,
)
from .display import clip_text, display_width, fit_text
from .help import HelpLine
from .results import (
    ExplainPlanLine,
    VisibleColumn,
    access_mode_name,
    active_operation_status,
    clamp_cell_view_scroll,
    editor_result_pane_heights,
    explain_plan_label,
    explain_plan_lines,
    is_database_connected,
    is_read_only_enabled,
    result_label,
    result_pane_is_editor_fullscreen,
    result_pane_tab_height,
    row_detail_lines,
    scroll_start,
    table_column_widths,
    transaction_mode_name,
    transaction_pending_indicator,
    visible_table_columns,
    wrapped_dbms_output_lines,
)
from .state import UIState
from .syntax import (
    SyntaxToken,
    find_matching_bracket_positions,
    syntax_kind_at_position,
    syntax_line_segments,
    tokenize_sql_lines,
)


class DocumentRenderPort(Protocol):
    def ensure_active_tab_visible(self, width: int) -> None: ...


class BrowserRenderPort(Protocol):
    def browser_entries(self) -> list[BrowserEntry]: ...

    def ensure_browser_selection_visible(self, visible_rows: int) -> None: ...


class ResultRenderPort(Protocol):
    def clamp_result_selection(self) -> None: ...

    def ensure_selected_row_visible(self, visible_rows: int) -> None: ...

    def ensure_selected_column_visible(self, widths: list[int], width: int) -> None: ...

    def ensure_selected_detail_field_visible(self, visible_lines: int) -> None: ...

class Renderer:
    def __init__(
        self,
        screen: curses.window,
        state: UIState,
        documents: DocumentRenderPort,
        browser: BrowserRenderPort,
        results: ResultRenderPort,
    ):
        self.screen = screen
        self.state = state
        self.documents = documents
        self.browser = browser
        self.results = results
        self.draw_offset_x = 0
        self.syntax_colors_enabled = False
        self.explain_color_kinds_enabled: set[str] = set()

    def init_colors(self) -> None:
        self.syntax_colors_enabled = False
        self.explain_color_kinds_enabled = set()
        if not curses.has_colors():
            return
        curses.start_color()
        default_bg = -1
        try:
            curses.use_default_colors()
        except curses.error:
            default_bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_CYAN, default_bg)
        curses.init_pair(3, curses.COLOR_YELLOW, default_bg)
        curses.init_pair(4, curses.COLOR_RED, default_bg)
        curses.init_pair(5, curses.COLOR_GREEN, default_bg)
        if getattr(curses, "COLOR_PAIRS", COLOR_SYNTAX_OPERATOR + 1) <= COLOR_SYNTAX_OPERATOR:
            return
        try:
            for pair_number, foreground in syntax_color_palette(self.state.config.editor_colors).items():
                curses.init_pair(pair_number, foreground, default_bg)
            self.syntax_colors_enabled = True
        except curses.error:
            self.syntax_colors_enabled = False
        for kind, (pair_number, foreground) in configured_plan_color_pairs(
            self.state.config.explain_colors
        ).items():
            try:
                curses.init_pair(pair_number, foreground, default_bg)
            except curses.error:
                continue
            self.explain_color_kinds_enabled.add(kind)

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if self.state.result_grid_fullscreen and self.state.active_result is not None:
            self.draw_offset_x = 0
            result_cursor = self.draw_result_grid(0, height, width, show_label=False)
            self.show_cursor()
            self.move_cursor(result_cursor)
            self.screen.refresh()
            return
        if result_pane_is_editor_fullscreen(self.state.result_ratio):
            self.draw_offset_x = 0
            editor_cursor = self.draw_editor(0, height, width)
            self.show_cursor()
            self.move_cursor(editor_cursor)
            self.screen.refresh()
            return
        status_h = 1
        header_h = 1
        tab_h = result_pane_tab_height(self.state.result_ratio)
        usable_h = max(3, height - status_h - header_h)
        content_usable_h = max(3, usable_h - tab_h)
        browser_w = browser_panel_width(width) if self.state.browser_visible else 0
        content_x = browser_w + 1 if self.state.browser_visible else 0
        content_w = max(1, width - content_x)
        editor_h, result_h = editor_result_pane_heights(content_usable_h, self.state.result_ratio)

        self.draw_offset_x = 0
        self.draw_header(width)
        browser_cursor = (1, 0)
        if self.state.browser_visible:
            browser_cursor = self.draw_browser(1, usable_h, browser_w)
            for line_y in range(1, height - 1):
                self.addstr(line_y, browser_w, "|", curses.color_pair(2))
        self.draw_offset_x = content_x
        if tab_h:
            self.draw_tab_bar(1, content_w)
        editor_y = 1 + tab_h
        editor_cursor = (editor_y, self.draw_offset_x)
        if editor_h > 0:
            editor_cursor = self.draw_editor(editor_y, editor_h, content_w)
        result_cursor = self.draw_results(editor_y + editor_h, result_h, content_w)
        self.draw_offset_x = 0
        self.draw_status(height - 1, width)
        self.show_cursor()
        if self.state.focus == FOCUS_BROWSER:
            cursor_position = browser_cursor
        elif self.state.focus == FOCUS_RESULTS:
            cursor_position = result_cursor
        elif editor_h <= 0:
            cursor_position = result_cursor
        else:
            cursor_position = editor_cursor
        self.move_cursor(cursor_position)
        self.screen.refresh()

    def draw_header(self, width: int) -> None:
        title = tab_display_title(self.state.active_tab)
        dirty = "*" if self.state.buffer.dirty else ""
        focus = self.state.focus.upper()
        tab_count = len(self.state.tabs)
        tx_mode = transaction_mode_name(self.state.db)
        access = access_mode_name(self.state.db)
        connection = (
            "db connected"
            if is_database_connected(self.state.db)
            else "db disconnected"
        )
        left = (
            f"[O] | {focus} | {connection} | tx {tx_mode} | {access} | "
            f"tab {self.state.active_tab_idx + 1}/{tab_count} | {title}{dirty} "
        )
        self.addstr(0, 0, fit_text(left, width), curses.color_pair(1) | curses.A_DIM)

    def draw_tab_bar(self, y: int, width: int) -> None:
        self.documents.ensure_active_tab_visible(width)
        self.addstr(y, 0, fit_text("", width), curses.color_pair(2))
        x = 0
        visible = visible_tab_labels(self.state.tabs, self.state.tab_scroll, width)
        for idx, number, label in visible:
            if x >= width:
                break
            selected = idx == self.state.active_tab_idx
            attr = curses.color_pair(2) | curses.A_REVERSE if selected else curses.color_pair(2) | curses.A_DIM
            text = f"[{number} {label}]"
            clipped = clip_text(text, width - x)
            if not clipped:
                break
            self.addstr(y, x, clipped, attr)
            x += display_width(clipped)
            if x < width:
                self.addstr(y, x, " ", curses.color_pair(2))
                x += 1

    def draw_browser(self, y: int, height: int, width: int) -> tuple[int, int]:
        entries = self.browser.browser_entries()
        self.state.browser_row = clamp_browser_row(self.state.browser_row, entries)
        body_h = max(0, height - 1)
        self.state.browser_page_size = max(1, body_h)
        self.browser.ensure_browser_selection_visible(body_h)
        label = f" Schema | Filter: {self.state.browser_filter}"
        self.addstr(y, 0, fit_text(label, width), curses.color_pair(3) | curses.A_BOLD)
        cursor = (y + 1, 1)
        if self.state.focus == FOCUS_BROWSER:
            cursor = (y, min(max(0, width - 1), display_width(clip_text(label, width))))
        if self.state.browser_filter and not entries and body_h:
            self.addstr(y + 1, 0, fit_text(" No matches", width), curses.A_DIM)
        for idx in range(body_h):
            entry_idx = self.state.browser_scroll + idx
            screen_y = y + 1 + idx
            if entry_idx >= len(entries):
                if not (self.state.browser_filter and not entries and idx == 0):
                    self.addstr(screen_y, 0, fit_text("", width))
                continue
            entry = entries[entry_idx]
            selected = entry_idx == self.state.browser_row and self.state.focus == FOCUS_BROWSER
            attr = curses.A_REVERSE if selected else 0
            self.addstr(
                screen_y,
                0,
                fit_text(
                    browser_entry_text(entry, self.state.browser_expanded, self.state.browser_filter),
                    width,
                ),
                attr,
            )
        return cursor

    def draw_editor(self, y: int, height: int, width: int) -> tuple[int, int]:
        buf = self.state.buffer
        if buf.row < buf.scroll:
            buf.scroll = buf.row
        if buf.row >= buf.scroll + height:
            buf.scroll = buf.row - height + 1
        line_no_width = len(str(len(buf.lines))) + 2
        token_lines = tokenize_sql_lines(buf.lines)
        matching_brackets = find_matching_bracket_positions(buf.lines, token_lines, buf.row, buf.col)
        for idx in range(height):
            line_idx = buf.scroll + idx
            screen_y = y + idx
            if line_idx >= len(buf.lines):
                self.addstr(screen_y, 0, "~".ljust(width), curses.color_pair(2))
                continue
            line_no = str(line_idx + 1).rjust(line_no_width - 1) + " "
            self.addstr(screen_y, 0, fit_text(line_no, min(line_no_width, width)), curses.color_pair(2))
            line = buf.lines[line_idx]
            self.draw_editor_line(
                screen_y,
                line_no_width,
                line,
                line_idx,
                max(0, width - line_no_width),
                token_lines[line_idx],
            )
            self.draw_editor_bracket_overlays(
                screen_y,
                line_no_width,
                line,
                line_idx,
                max(0, width - line_no_width),
                token_lines,
                matching_brackets,
            )
        cursor_y = y + buf.row - buf.scroll
        cursor_x = min(width - 1, line_no_width + display_width(buf.lines[buf.row][: buf.col]))
        if y <= cursor_y < y + height:
            return cursor_y, self.draw_offset_x + cursor_x
        return y, self.draw_offset_x + line_no_width

    def draw_editor_line(
        self,
        y: int,
        x: int,
        line: str,
        line_idx: int,
        width: int,
        tokens: list[SyntaxToken] | None = None,
    ) -> None:
        selected = self.state.buffer.selection_range()
        segments = syntax_line_segments(line, line_idx, selected, tokens)
        current_x = x
        remaining = width
        for segment in segments:
            text = segment.text
            if remaining <= 0:
                return
            if text == "" and segment.selected:
                self.addstr(y, current_x, " ", curses.A_REVERSE)
                current_x += 1
                remaining -= 1
                continue
            clipped = clip_text(text, remaining)
            if not clipped:
                continue
            attr = curses.A_REVERSE if segment.selected else self.syntax_attr(segment.kind)
            self.addstr(y, current_x, clipped, attr)
            used = display_width(clipped)
            current_x += used
            remaining -= used

    def draw_editor_bracket_overlays(
        self,
        y: int,
        x: int,
        line: str,
        line_idx: int,
        width: int,
        token_lines: list[list[SyntaxToken]],
        matching_brackets: set[tuple[int, int]],
    ) -> None:
        for row, col in sorted(matching_brackets):
            if row != line_idx or col < 0 or col >= len(line):
                continue
            text_x = display_width(line[:col])
            if text_x >= width:
                continue
            kind = syntax_kind_at_position(token_lines, line_idx, col) or SYNTAX_DEFAULT
            attr = self.syntax_attr(kind) | curses.A_REVERSE | curses.A_BOLD
            self.addstr(y, x + text_x, line[col], attr)

    def syntax_attr(self, kind: str) -> int:
        if self.syntax_colors_enabled:
            color_attr = _SYNTAX_COLOR_ATTRS.get(kind)
            if color_attr is None:
                return 0
            pair_number, attr = color_attr
            return curses.color_pair(pair_number) | attr
        return _SYNTAX_FALLBACK_ATTRS.get(kind, 0)

    def draw_results(self, y: int, height: int, width: int) -> tuple[int, int]:
        if self.state.show_dbms_output and self.state.dbms_output:
            return self.draw_dbms_output(y, height, width)
        if self.state.explain_result is not None:
            return self.draw_explain_plan(y, height, width)
        if self.state.active_result is not None:
            if self.state.result_mode == RESULT_ROW_DETAIL:
                return self.draw_result_detail(y, height, width)
            return self.draw_result_grid(y, height, width)
        if self.state.results_style == RESULT_STYLE_HELP:
            return self.draw_help_results(y, height, width)
        return self.draw_text_results(y, height, width)

    def draw_dbms_output(self, y: int, height: int, width: int) -> tuple[int, int]:
        target = "results" if self.state.active_result is not None else "transcript"
        label = f" DBMS_OUTPUT | F6 {target} "
        self.addstr(y, 0, fit_text(label, width), curses.color_pair(3) | curses.A_BOLD)
        body_h = max(0, height - 1)
        wrapped = wrapped_dbms_output_lines(self.state.dbms_output, width)
        scroll = scroll_start(self.state.active_tab.dbms_output_scroll, len(wrapped), body_h)
        if self.state.active_tab.dbms_output_scroll is not None:
            self.state.active_tab.dbms_output_scroll = scroll
        lines = wrapped[scroll : scroll + body_h] if body_h else []
        for idx in range(body_h):
            text = lines[idx] if idx < len(lines) else ""
            self.addstr(y + 1 + idx, 0, fit_text(text, width))
        return y + 1, self.draw_offset_x

    def draw_text_results(self, y: int, height: int, width: int) -> tuple[int, int]:
        label = " Results "
        self.addstr(y, 0, fit_text(label, width), curses.color_pair(3) | curses.A_BOLD)
        body_h = max(0, height - 1)
        scroll = scroll_start(self.state.active_tab.results_scroll, len(self.state.results), body_h)
        if self.state.active_tab.results_scroll is not None:
            self.state.active_tab.results_scroll = scroll
        lines = self.state.results[scroll : scroll + body_h] if body_h else []
        for idx in range(body_h):
            text = lines[idx] if idx < len(lines) else ""
            attr = curses.color_pair(4) if text.startswith("ERROR") else 0
            self.addstr(y + 1 + idx, 0, fit_text(text, width), attr)
        return y + 1, self.draw_offset_x

    def draw_help_results(self, y: int, height: int, width: int) -> tuple[int, int]:
        label = " Help | Ctrl-Up/Down scroll focused pane "
        self.addstr(y, 0, fit_text(label, width), curses.color_pair(3) | curses.A_BOLD)
        body_h = max(0, height - 1)
        lines = self.state.active_tab.help_lines
        scroll = scroll_start(self.state.active_tab.results_scroll, len(lines), body_h)
        if self.state.active_tab.results_scroll is not None:
            self.state.active_tab.results_scroll = scroll
        for idx in range(body_h):
            line_idx = scroll + idx
            screen_y = y + 1 + idx
            if line_idx >= len(lines):
                self.addstr(screen_y, 0, fit_text("", width))
                continue
            self.draw_help_line(screen_y, lines[line_idx], width)
        return y + 1, self.draw_offset_x

    def draw_help_line(self, y: int, line: HelpLine, width: int) -> None:
        x = 0
        remaining = width
        for segment in line.segments:
            if remaining <= 0:
                return
            clipped = clip_text(segment.text, remaining)
            if not clipped:
                continue
            self.addstr(y, x, clipped, self.help_attr(segment.kind))
            used = display_width(clipped)
            x += used
            remaining -= used

    def help_attr(self, kind: str) -> int:
        if kind == HELP_BORDER:
            return curses.color_pair(2) | curses.A_DIM
        if kind == HELP_TITLE:
            return curses.color_pair(3) | curses.A_BOLD
        if kind == HELP_SECTION:
            return curses.color_pair(3) | curses.A_BOLD
        if kind == HELP_KEY:
            return curses.color_pair(2) | curses.A_BOLD
        if kind == HELP_TIP:
            return curses.color_pair(5)
        return 0

    def draw_explain_plan(self, y: int, height: int, width: int) -> tuple[int, int]:
        result = self.state.explain_result
        if result is None:
            return self.draw_text_results(y, height, width)
        body_h = max(0, height - 1)
        self.state.explain_page_size = max(1, body_h)
        lines = explain_plan_lines(result)
        self.state.explain_scroll = clamp_cell_view_scroll(self.state.explain_scroll, len(lines), body_h)
        label = explain_plan_label(result, self.state.focus == FOCUS_RESULTS, bool(lines))
        self.addstr(y, 0, fit_text(label, width), curses.color_pair(2) | curses.A_DIM)
        cursor = (y + 1, self.draw_offset_x)
        for idx in range(body_h):
            line_idx = self.state.explain_scroll + idx
            screen_y = y + 1 + idx
            if line_idx >= len(lines):
                self.addstr(screen_y, 0, fit_text("", width))
                continue
            self.draw_explain_plan_line(screen_y, lines[line_idx], width)
        return cursor

    def draw_explain_plan_line(self, y: int, line: ExplainPlanLine, width: int) -> None:
        x = 0
        remaining = width
        for segment in line.segments:
            if remaining <= 0:
                return
            clipped = clip_text(segment.text, remaining)
            if not clipped:
                continue
            self.addstr(y, x, clipped, self.explain_plan_attr(segment.kind))
            used = display_width(clipped)
            x += used
            remaining -= used

    def explain_plan_attr(self, kind: str) -> int:
        configured_attr = self.configured_explain_plan_attr(kind)
        if kind == PLAN_CONNECTOR:
            attr = configured_attr if configured_attr is not None else curses.color_pair(2)
            return attr | curses.A_DIM
        if kind == PLAN_OPERATION:
            attr = configured_attr if configured_attr is not None else curses.color_pair(3)
            return attr | curses.A_BOLD
        if kind == PLAN_OBJECT:
            return configured_attr if configured_attr is not None else curses.color_pair(5)
        if kind == PLAN_METRICS:
            attr = configured_attr if configured_attr is not None else 0
            return attr | curses.A_DIM
        if kind == PLAN_TEXT:
            return configured_attr if configured_attr is not None else 0
        return 0

    def configured_explain_plan_attr(self, kind: str) -> int | None:
        enabled: set[str] = getattr(self, "explain_color_kinds_enabled", set())
        if kind not in enabled:
            return None
        pair_number = PLAN_COLOR_PAIR_BY_KIND.get(kind)
        if pair_number is None:
            return None
        return curses.color_pair(pair_number)

    def draw_result_grid(self, y: int, height: int, width: int, show_label: bool = True) -> tuple[int, int]:
        result = self.state.active_result
        if result is None:
            return self.draw_text_results(y, height, width)
        label_h = 1 if show_label else 0
        body_h = max(0, height - label_h)
        visible_rows = max(0, body_h - 2)
        self.state.result_page_size = max(1, visible_rows)
        self.results.clamp_result_selection()
        if visible_rows:
            self.results.ensure_selected_row_visible(visible_rows)
        widths = table_column_widths(result)
        self.results.ensure_selected_column_visible(widths, width)
        visible = visible_table_columns(widths, self.state.result_col_scroll, width)
        if show_label:
            label = result_label(
                result,
                RESULT_GRID,
                self.state.focus == FOCUS_RESULTS,
                bool(self.state.dbms_output),
                is_read_only_enabled(self.state.db),
                is_database_connected(self.state.db),
            )
            self.addstr(y, 0, fit_text(label, width), curses.color_pair(1) | curses.A_DIM)
        if body_h <= 0:
            return y, self.draw_offset_x
        table_y = y + label_h
        if not result.columns:
            self.addstr(table_y, 0, fit_text("No table result.", width))
            return table_y, self.draw_offset_x
        if not visible:
            self.addstr(table_y, 0, fit_text("No visible columns.", width))
            return table_y, self.draw_offset_x

        self.draw_table_line(table_y, visible, result.columns, width, curses.A_BOLD | curses.color_pair(2))
        if body_h >= 2:
            self.draw_table_separator(table_y + 1, visible, width)
        cursor = (table_y, self.draw_offset_x + visible[0].x)
        if not result.rows and body_h >= 3:
            self.addstr(table_y + 2, 0, fit_text("(no rows)", width))
        for idx in range(visible_rows):
            row_idx = self.state.result_row_scroll + idx
            if row_idx >= len(result.rows):
                break
            screen_y = table_y + 2 + idx
            row = result.rows[row_idx]
            selected = row_idx == self.state.result_row
            line_cursor = self.draw_table_line(
                screen_y,
                visible,
                row,
                width,
                0,
                selected_col=self.state.result_col if selected else None,
            )
            if selected and line_cursor is not None:
                cursor = line_cursor
        return cursor

    def draw_result_detail(self, y: int, height: int, width: int) -> tuple[int, int]:
        result = self.state.active_result
        if result is None:
            return self.draw_text_results(y, height, width)
        body_h = max(0, height - 1)
        self.state.result_page_size = max(1, body_h)
        self.results.clamp_result_selection()
        self.results.ensure_selected_detail_field_visible(body_h)
        label = result_label(
            result,
            RESULT_ROW_DETAIL,
            self.state.focus == FOCUS_RESULTS,
            bool(self.state.dbms_output),
            is_read_only_enabled(self.state.db),
            is_database_connected(self.state.db),
        )
        self.addstr(y, 0, fit_text(label, width), curses.color_pair(3) | curses.A_BOLD)
        lines = row_detail_lines(result, self.state.result_row, width, self.state.result_col_scroll)
        while (
            body_h > 0
            and self.state.result_col_scroll < self.state.result_col
            and not any(field_idx == self.state.result_col for field_idx, _ in lines[:body_h])
        ):
            self.state.result_col_scroll += 1
            lines = row_detail_lines(result, self.state.result_row, width, self.state.result_col_scroll)
        cursor = (y + 1, self.draw_offset_x)
        for idx in range(body_h):
            if idx >= len(lines):
                self.addstr(y + 1 + idx, 0, fit_text("", width))
                continue
            field_idx, text = lines[idx]
            selected = field_idx == self.state.result_col and self.state.focus == FOCUS_RESULTS
            attr = curses.A_REVERSE if selected else 0
            self.addstr(y + 1 + idx, 0, fit_text(text, width), attr)
            if selected:
                cursor = (y + 1 + idx, self.draw_offset_x)
        return cursor

    def draw_table_line(
        self,
        y: int,
        visible: list[VisibleColumn],
        values: list[str],
        width: int,
        base_attr: int,
        selected_col: int | None = None,
    ) -> tuple[int, int] | None:
        cursor: tuple[int, int] | None = None
        for pos, column in enumerate(visible):
            if pos > 0:
                self.addstr(y, column.x - 3, " | ")
            value = values[column.index] if column.index < len(values) else ""
            selected = selected_col == column.index and self.state.focus == FOCUS_RESULTS
            attr = curses.A_REVERSE if selected else base_attr
            self.addstr(y, column.x, fit_text(value, column.width), attr)
            if selected:
                cursor = (y, self.draw_offset_x + column.x)
        return cursor

    def draw_table_separator(self, y: int, visible: list[VisibleColumn], width: int) -> None:
        for pos, column in enumerate(visible):
            if pos > 0:
                self.addstr(y, column.x - 3, "-+-")
            self.addstr(y, column.x, "-" * min(column.width, max(0, width - column.x)))

    def draw_status(self, y: int, width: int) -> None:
        text = active_operation_status(self.state) if self.state.db_operation is not None else self.state.status
        status = f"{transaction_pending_indicator(self.state.db)} {text} "
        self.addstr(y, 0, fit_text(status, width), curses.color_pair(1) | curses.A_DIM)

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        screen_x = self.draw_offset_x + x
        if y < 0 or y >= height or screen_x >= width:
            return
        try:
            self.screen.addstr(y, screen_x, clip_text(text, max(0, width - screen_x)), attr)
        except curses.error:
            pass

    def move_cursor(self, position: tuple[int, int]) -> None:
        y, x = position
        height, width = self.screen.getmaxyx()
        try:
            self.screen.move(min(max(y, 0), height - 1), min(max(x, 0), width - 1))
        except curses.error:
            pass

    def show_cursor(self) -> None:
        try:
            curses.curs_set(2)
        except curses.error:
            try:
                curses.curs_set(1)
            except curses.error:
                pass
