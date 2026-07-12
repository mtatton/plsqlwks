from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .constants import UNDO_HISTORY_LIMIT

@dataclass(frozen=True)
class UndoSnapshot:
    lines: tuple[str, ...]
    row: int
    col: int
    scroll: int
    path: Path | None
    title: str | None
    dirty: bool
    selection_anchor: tuple[int, int] | None


@dataclass
class Buffer:
    lines: list[str] = field(default_factory=lambda: [""])
    row: int = 0
    col: int = 0
    scroll: int = 0
    path: Path | None = None
    title: str | None = None
    dirty: bool = False
    selection_anchor: tuple[int, int] | None = None
    undo_stack: list[UndoSnapshot] = field(default_factory=list)
    redo_stack: list[UndoSnapshot] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.lines)

    def load(self, path: Path, record_undo: bool = True) -> None:
        text = path.read_text(encoding="utf-8")
        if record_undo:
            self.record_undo()
        self.lines = text.splitlines() or [""]
        self.row = 0
        self.col = 0
        self.scroll = 0
        self.path = path
        self.title = path.name
        self.dirty = False
        self.selection_anchor = None

    def save(self, path: Path | None = None) -> Path:
        if path is not None:
            self.path = path
        if self.path is None:
            raise ValueError("No path selected")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.text() + "\n", encoding="utf-8")
        self.title = self.path.name
        self.dirty = False
        return self.path

    def set_text(
        self,
        text: str,
        path: Path | None = None,
        title: str | None = None,
        dirty: bool = True,
        record_undo: bool = True,
    ) -> None:
        if record_undo:
            self.record_undo()
        self.lines = text.splitlines() or [""]
        self.row = 0
        self.col = 0
        self.scroll = 0
        self.path = path
        self.title = title or (path.name if path else None)
        self.dirty = dirty
        self.selection_anchor = None

    def insert_char(self, ch: str) -> None:
        self.insert_text(ch)

    def newline(self) -> None:
        self.record_undo()
        self.delete_selection(record_undo=False)
        self._insert_text_at_cursor("\n")

    def _insert_text_at_cursor(self, text: str) -> None:
        if not text:
            return
        line = self.lines[self.row]
        parts = text.split("\n")
        if len(parts) == 1:
            self.lines[self.row] = line[: self.col] + text + line[self.col :]
            self.col += len(text)
            self.dirty = True
            return
        prefix = line[: self.col]
        suffix = line[self.col :]
        self.lines[self.row] = prefix + parts[0]
        insert_at = self.row + 1
        new_lines = parts[1:-1] + [parts[-1] + suffix]
        self.lines[insert_at:insert_at] = new_lines
        self.row += len(parts) - 1
        self.col = len(parts[-1])
        self.dirty = True

    def insert_text(self, text: str) -> None:
        if not text:
            return
        self.record_undo()
        self.delete_selection(record_undo=False)
        self._insert_text_at_cursor(text)

    def toggle_line_comment(self) -> tuple[bool, int]:
        return self.toggle_comment()

    def toggle_comment(self) -> tuple[bool, int]:
        target_rows = self.comment_target_rows()
        actionable = [row for row in target_rows if self.lines[row].strip()]
        if not actionable:
            self.clear_selection()
            return True, 0
        comment = not all(line_is_sql_commented(self.lines[row]) for row in actionable)
        self.record_undo()
        for row in actionable:
            self.lines[row] = comment_sql_line(self.lines[row]) if comment else uncomment_sql_line(self.lines[row])
        self.clear_selection()
        self.dirty = True
        self.row = min(max(self.row, 0), len(self.lines) - 1)
        self.col = min(self.col, len(self.lines[self.row]))
        return comment, len(actionable)

    def comment_target_rows(self) -> list[int]:
        selected = self.selection_range()
        if selected is None:
            return [self.row]
        (start_row, _), (end_row, end_col) = selected
        if end_col == 0 and end_row > start_row:
            end_row -= 1
        if end_row < start_row:
            return [self.row]
        return list(range(start_row, end_row + 1))

    def backspace(self) -> None:
        if self.delete_selection():
            return
        if self.col > 0:
            self.record_undo()
            line = self.lines[self.row]
            self.lines[self.row] = line[: self.col - 1] + line[self.col :]
            self.col -= 1
            self.dirty = True
        elif self.row > 0:
            self.record_undo()
            previous_len = len(self.lines[self.row - 1])
            self.lines[self.row - 1] += self.lines.pop(self.row)
            self.row -= 1
            self.col = previous_len
            self.dirty = True

    def delete(self) -> None:
        if self.delete_selection():
            return
        line = self.lines[self.row]
        if self.col < len(line):
            self.record_undo()
            self.lines[self.row] = line[: self.col] + line[self.col + 1 :]
            self.dirty = True
        elif self.row < len(self.lines) - 1:
            self.record_undo()
            self.lines[self.row] += self.lines.pop(self.row + 1)
            self.dirty = True

    def delete_word_right(self) -> None:
        if self.delete_selection():
            return
        start_row, start_col = self.row, self.col
        end_row, end_col = next_token_position(self.lines, self.row, self.col)
        if (end_row, end_col) == (start_row, start_col):
            return
        self.record_undo()
        if start_row == end_row:
            line = self.lines[start_row]
            self.lines[start_row] = line[:start_col] + line[end_col:]
        else:
            prefix = self.lines[start_row][:start_col]
            suffix = self.lines[end_row][end_col:]
            self.lines[start_row : end_row + 1] = [prefix + suffix]
        self.row, self.col = self.clamp_position(start_row, start_col)
        self.clear_selection()
        self.dirty = True

    def delete_word_left(self) -> None:
        if self.delete_selection():
            return
        end_row, end_col = self.row, self.col
        start_row, start_col = previous_token_position(self.lines, self.row, self.col)
        if (start_row, start_col) == (end_row, end_col):
            return
        self.record_undo()
        if start_row == end_row:
            line = self.lines[start_row]
            self.lines[start_row] = line[:start_col] + line[end_col:]
        else:
            prefix = self.lines[start_row][:start_col]
            suffix = self.lines[end_row][end_col:]
            self.lines[start_row : end_row + 1] = [prefix + suffix]
        self.row, self.col = self.clamp_position(start_row, start_col)
        self.clear_selection()
        self.dirty = True

    def move(self, delta_row: int, delta_col: int = 0) -> None:
        self.clear_selection()
        self.row = min(max(self.row + delta_row, 0), len(self.lines) - 1)
        self.col = min(max(self.col + delta_col, 0), len(self.lines[self.row]))

    def page(self, amount: int, extend: bool = False) -> None:
        if extend:
            self.start_selection()
        else:
            self.clear_selection()
        self.row = min(max(self.row + amount, 0), len(self.lines) - 1)
        self.col = min(self.col, len(self.lines[self.row]))

    def move_to(self, row: int, col: int, extend: bool = False) -> None:
        if extend:
            self.start_selection()
        else:
            self.clear_selection()
        self.row, self.col = self.clamp_position(row, col)
        if self.row == 0:
            self.scroll = 0

    def move_line_start(self, extend: bool = False) -> None:
        self.move_to(self.row, 0, extend=extend)

    def move_line_end(self, extend: bool = False) -> None:
        self.move_to(self.row, len(self.lines[self.row]), extend=extend)

    def move_file_start(self, extend: bool = False) -> None:
        self.move_to(0, 0, extend=extend)

    def move_file_end(self, extend: bool = False) -> None:
        last_row = len(self.lines) - 1
        self.move_to(last_row, len(self.lines[last_row]), extend=extend)

    def move_word_left(self, extend: bool = False) -> None:
        if extend:
            self.start_selection()
        else:
            self.clear_selection()
        self.row, self.col = previous_token_position(self.lines, self.row, self.col)

    def move_word_right(self, extend: bool = False) -> None:
        if extend:
            self.start_selection()
        else:
            self.clear_selection()
        self.row, self.col = next_token_position(self.lines, self.row, self.col)

    def clear_selection(self) -> None:
        self.selection_anchor = None

    def start_selection(self) -> None:
        if self.selection_anchor is None:
            self.selection_anchor = (self.row, self.col)

    def selection_range(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if self.selection_anchor is None:
            return None
        anchor = self.clamp_position(*self.selection_anchor)
        cursor = self.clamp_position(self.row, self.col)
        if anchor == cursor:
            return None
        return (anchor, cursor) if anchor < cursor else (cursor, anchor)

    def clamp_position(self, row: int, col: int) -> tuple[int, int]:
        row = min(max(row, 0), len(self.lines) - 1)
        col = min(max(col, 0), len(self.lines[row]))
        return row, col

    def selected_text(self) -> str:
        selected = self.selection_range()
        if selected is None:
            return ""
        (start_row, start_col), (end_row, end_col) = selected
        if start_row == end_row:
            return self.lines[start_row][start_col:end_col]
        parts = [self.lines[start_row][start_col:]]
        parts.extend(self.lines[start_row + 1 : end_row])
        parts.append(self.lines[end_row][:end_col])
        return "\n".join(parts)

    def transform_selection(self, transform: Callable[[str], str]) -> bool:
        selected = self.selection_range()
        if selected is None:
            return False
        (start_row, start_col), (end_row, end_col) = selected
        original = self.selected_text()
        transformed = transform(original)
        if transformed == original:
            return True
        self.record_undo()
        prefix = self.lines[start_row][:start_col]
        suffix = self.lines[end_row][end_col:]
        parts = transformed.split("\n")
        if len(parts) == 1:
            self.lines[start_row : end_row + 1] = [prefix + parts[0] + suffix]
            new_row = start_row
            new_col = start_col + len(parts[0])
        else:
            replacement = [prefix + parts[0], *parts[1:-1], parts[-1] + suffix]
            self.lines[start_row : end_row + 1] = replacement
            new_row = start_row + len(parts) - 1
            new_col = len(parts[-1])
        self.selection_anchor = (start_row, start_col)
        self.row, self.col = self.clamp_position(new_row, new_col)
        self.dirty = True
        return True

    def delete_selection(self, record_undo: bool = True) -> bool:
        selected = self.selection_range()
        if selected is None:
            self.clear_selection()
            return False
        if record_undo:
            self.record_undo()
        (start_row, start_col), (end_row, end_col) = selected
        if start_row == end_row:
            line = self.lines[start_row]
            self.lines[start_row] = line[:start_col] + line[end_col:]
        else:
            prefix = self.lines[start_row][:start_col]
            suffix = self.lines[end_row][end_col:]
            self.lines[start_row : end_row + 1] = [prefix + suffix]
        self.row = start_row
        self.col = start_col
        self.clear_selection()
        self.dirty = True
        if not self.lines:
            self.lines = [""]
        return True

    def snapshot(self) -> UndoSnapshot:
        return UndoSnapshot(
            lines=tuple(self.lines),
            row=self.row,
            col=self.col,
            scroll=self.scroll,
            path=self.path,
            title=self.title,
            dirty=self.dirty,
            selection_anchor=self.selection_anchor,
        )

    def record_undo(self) -> None:
        snapshot = self.snapshot()
        if not self.undo_stack or self.undo_stack[-1] != snapshot:
            self._append_undo_snapshot(snapshot)
        self.redo_stack.clear()

    def _append_undo_snapshot(self, snapshot: UndoSnapshot) -> None:
        self.undo_stack.append(snapshot)
        if len(self.undo_stack) > UNDO_HISTORY_LIMIT:
            del self.undo_stack[: len(self.undo_stack) - UNDO_HISTORY_LIMIT]

    def restore_snapshot(self, snapshot: UndoSnapshot) -> None:
        self.lines = list(snapshot.lines) or [""]
        self.row, self.col = self.clamp_position(snapshot.row, snapshot.col)
        self.scroll = snapshot.scroll
        self.path = snapshot.path
        self.title = snapshot.title
        self.dirty = snapshot.dirty
        self.selection_anchor = snapshot.selection_anchor

    def undo(self) -> bool:
        if not self.undo_stack:
            return False
        self.redo_stack.append(self.snapshot())
        self.restore_snapshot(self.undo_stack.pop())
        return True

    def redo(self) -> bool:
        if not self.redo_stack:
            return False
        self._append_undo_snapshot(self.snapshot())
        self.restore_snapshot(self.redo_stack.pop())
        return True

def is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$#"


def token_kind(ch: str) -> str:
    if ch.isspace():
        return "space"
    if is_word_char(ch):
        return "word"
    return "punct"


def position_to_text_index(lines: list[str], row: int, col: int) -> int:
    if not lines:
        return 0
    row = min(max(row, 0), len(lines) - 1)
    col = min(max(col, 0), len(lines[row]))
    return sum(len(line) + 1 for line in lines[:row]) + col


def text_index_to_position(lines: list[str], index: int) -> tuple[int, int]:
    if not lines:
        return 0, 0
    index = min(max(index, 0), len("\n".join(lines)))
    remaining = index
    for row, line in enumerate(lines):
        if remaining <= len(line):
            return row, remaining
        remaining -= len(line) + 1
    last_row = len(lines) - 1
    return last_row, len(lines[last_row])


def previous_token_position(lines: list[str], row: int, col: int) -> tuple[int, int]:
    text = "\n".join(lines)
    idx = position_to_text_index(lines, row, col)
    if idx <= 0:
        return 0, 0
    idx -= 1
    while idx >= 0 and token_kind(text[idx]) == "space":
        idx -= 1
    if idx < 0:
        return 0, 0
    kind = token_kind(text[idx])
    while idx > 0 and token_kind(text[idx - 1]) == kind:
        idx -= 1
    return text_index_to_position(lines, idx)


def next_token_position(lines: list[str], row: int, col: int) -> tuple[int, int]:
    text = "\n".join(lines)
    idx = position_to_text_index(lines, row, col)
    if idx >= len(text):
        return text_index_to_position(lines, len(text))
    kind = token_kind(text[idx])
    while idx < len(text) and token_kind(text[idx]) == kind:
        idx += 1
    while idx < len(text) and token_kind(text[idx]) == "space":
        idx += 1
    return text_index_to_position(lines, idx)

def line_indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def line_is_sql_commented(line: str) -> bool:
    return line[line_indent_width(line) :].startswith("--")


def comment_sql_line(line: str) -> str:
    indent = line_indent_width(line)
    return f"{line[:indent]}-- {line[indent:]}"


def uncomment_sql_line(line: str) -> str:
    indent = line_indent_width(line)
    code = line[indent:]
    if code.startswith("-- "):
        return line[:indent] + code[3:]
    if code.startswith("--"):
        return line[:indent] + code[2:]
    return line
