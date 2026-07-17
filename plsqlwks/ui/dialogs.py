from __future__ import annotations

import contextlib
import curses
from collections.abc import Sequence
from typing import cast

from .commands import CommandMenuItem
from .constants import CTRL_Q, ESC
from .display import (
    clamp_picker_selection,
    clip_text,
    display_unit_width,
    display_units,
    display_width,
    filtered_picker_indexes,
    fit_text,
    is_printable_text,
)
from .key_reader import KeyReader
from .menu import (
    TreeMenuItem,
    first_tree_menu_row_index,
    tree_menu_row_label,
    tree_menu_rows,
    tree_menu_section_row_index,
    tree_menu_sections,
)
from .results import ResultCell, cell_view_lines, clamp_cell_view_scroll, safe_window_addstr
from .state import UIState


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
    with contextlib.suppress(curses.error):
        window.box()


def safe_window_move(window: curses.window, y: int, x: int) -> None:
    height, width = window.getmaxyx()
    if height <= 0 or width <= 0:
        return
    with contextlib.suppress(curses.error):
        window.move(min(max(y, 0), height - 1), min(max(x, 0), width - 1))


def safe_window_refresh(window: curses.window) -> None:
    with contextlib.suppress(curses.error):
        window.refresh()


class DialogService:
    """Own terminal-modal prompts and pickers."""

    def __init__(self, screen: curses.window, state: UIState, key_reader: KeyReader):
        self.screen = screen
        self.state = state
        self.key_reader = key_reader

    def prompt(self, label: str, default: str = "", strip: bool = True) -> str | None:
        with contextlib.suppress(curses.error):
            curses.curs_set(1)
        text = default
        cursor = len(text)
        while True:
            self.draw_prompt_line(label, text, cursor)
            key = self.key_reader.read_key()
            if key in (ESC, CTRL_Q):
                return None
            if key in (10, 13):
                return text.strip() if strip else text
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if cursor > 0:
                    text = text[: cursor - 1] + text[cursor:]
                    cursor -= 1
            elif key == curses.KEY_DC:
                if cursor < len(text):
                    text = text[:cursor] + text[cursor + 1 :]
            elif key == curses.KEY_LEFT:
                cursor = max(0, cursor - 1)
            elif key == curses.KEY_RIGHT:
                cursor = min(len(text), cursor + 1)
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = len(text)
            elif isinstance(key, str) and is_printable_text(key):
                text = text[:cursor] + key + text[cursor:]
                cursor += len(key)

    def draw_prompt_line(self, label: str, text: str, cursor: int | None = None) -> None:
        height, width = self.screen.getmaxyx()
        if height <= 0 or width <= 0:
            return
        if cursor is None:
            prompt_text = f"{label}: {text}"
            cursor_x = display_width(prompt_text)
        else:
            cursor = min(max(cursor, 0), len(text))
            prefix = clip_text(f"{label}: ", max(0, width - 1))
            prefix_width = display_width(prefix)
            value_width = max(1, width - prefix_width)
            before = right_clip_text(text[:cursor], max(0, value_width - 1))
            after = clip_text(text[cursor:], max(0, value_width - display_width(before)))
            prompt_text = prefix + before + after
            cursor_x = prefix_width + display_width(before)
        safe_window_addstr(
            self.screen,
            height - 1,
            0,
            fit_text(prompt_text, width),
            curses.color_pair(1),
        )
        safe_window_move(self.screen, height - 1, min(width - 1, cursor_x))
        safe_window_refresh(self.screen)

    def prompt_text_box(
        self,
        label: str,
        default: str = "",
        strip: bool = True,
    ) -> str | None:
        with contextlib.suppress(curses.error):
            curses.curs_set(1)
        text = default
        previous_geometry: tuple[int, int, int, int] | None = None
        while True:
            height, width = self.screen.getmaxyx()
            key: int | str
            if height < 5 or width < 20:
                self.draw_prompt_line(label, text)
                key = self.key_reader.read_key()
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
                    key = self.key_reader.read_key()
                else:
                    with contextlib.suppress(curses.error):
                        win.keypad(True)
                    safe_window_box(win)
                    safe_window_addstr(win, 0, 2, clip_text(f" {label} ", max(0, box_w - 4)))
                    safe_window_addstr(win, 2, 2, fit_text(visible_text, input_w), curses.A_REVERSE)
                    footer = "Enter accept  Esc cancel"
                    safe_window_addstr(win, box_h - 1, 2, fit_text(footer, max(0, box_w - 4)))
                    safe_window_move(win, 2, 2 + display_width(visible_text))
                    safe_window_refresh(win)
                    key = self.key_reader.read_key(win, idle_timeout=-1)

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
            key = self.key_reader.read_key(win, idle_timeout=-1)
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

    def pick_command_menu(
        self,
        commands: tuple[CommandMenuItem, ...],
    ) -> CommandMenuItem | None:
        if not commands:
            return None
        items = cast(Sequence[TreeMenuItem], commands)
        filter_text = ""
        previous_geometry: tuple[int, int, int, int] | None = None
        sections = tree_menu_sections(items)
        expanded_sections = {section.name for section in sections}
        shortcut_width = max(display_width(command.shortcut) for command in commands)
        rows = tree_menu_rows(items, filter_text, expanded_sections)
        selected = first_tree_menu_row_index(rows)
        while True:
            rows = tree_menu_rows(items, filter_text, expanded_sections)
            selected = clamp_picker_selection(selected, len(rows))
            height, width = self.screen.getmaxyx()
            if height < 4 or width < 20:
                self.state.status = "Terminal too small for command menu"
                return None
            visible_rows = max(1, min(len(rows) or 1, height - 4))
            box_h = min(height - 1, visible_rows + 3)
            labels = [tree_menu_row_label(row, items, shortcut_width) for row in rows]
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
            with contextlib.suppress(curses.error):
                win.keypad(True)
            safe_window_box(win)
            safe_window_addstr(
                win,
                0,
                2,
                clip_text(" Commands ", max(0, box_w - 4)),
                curses.A_BOLD,
            )
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
                    text = tree_menu_row_label(row, items, shortcut_width)
                    safe_window_addstr(win, idx + 2, 1, fit_text(text, box_w - 2), attr)
            else:
                safe_window_addstr(win, 2, 1, fit_text("No matches", box_w - 2))
            safe_window_refresh(win)
            key = self.key_reader.read_key(win, idle_timeout=-1)
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
                    selected = first_tree_menu_row_index(tree_menu_rows(items, filter_text, expanded_sections))
            elif isinstance(key, str) and is_printable_text(key):
                filter_text += key
                selected = first_tree_menu_row_index(tree_menu_rows(items, filter_text, expanded_sections))

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
                safe_window_addstr(
                    win,
                    1,
                    max(2, box_w - display_width(marker) - 2),
                    marker,
                )
            win.refresh()

            key = self.key_reader.read_key(win, idle_timeout=-1)
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

    def refresh_modal_background(self) -> None:
        touchwin = getattr(self.screen, "touchwin", None)
        if callable(touchwin):
            with contextlib.suppress(curses.error):
                touchwin()
        refresh = getattr(self.screen, "refresh", None)
        if callable(refresh):
            with contextlib.suppress(curses.error):
                refresh()
