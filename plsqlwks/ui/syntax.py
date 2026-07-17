from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .buffer import is_word_char
from .constants import (
    BRACKET_CHARS,
    BRACKET_PAIRS,
    CLOSING_BRACKETS,
    PLSQL_ATTRIBUTES,
    SQL_KEYWORDS,
    SYNTAX_BIND,
    SYNTAX_COMMENT,
    SYNTAX_DEFAULT,
    SYNTAX_KEYWORD,
    SYNTAX_NUMBER,
    SYNTAX_OPERATOR,
    SYNTAX_STRING,
)


@dataclass(frozen=True)
class SyntaxToken:
    text: str
    kind: str


@dataclass(frozen=True)
class SyntaxScanState:
    mode: str = SYNTAX_DEFAULT
    q_close: str = ""


@dataclass(frozen=True)
class SyntaxSegment:
    text: str
    kind: str
    selected: bool = False


SQL_CODE_TRANSFORM_PRESERVED_KINDS = {SYNTAX_COMMENT, SYNTAX_STRING}


def tokenize_sql_lines(lines: list[str]) -> list[list[SyntaxToken]]:
    state = SyntaxScanState()
    token_lines: list[list[SyntaxToken]] = []
    for line in lines:
        tokens, state = tokenize_sql_line_with_state(line, state)
        token_lines.append(tokens)
    return token_lines


def tokenize_sql_line(line: str) -> list[SyntaxToken]:
    tokens, _ = tokenize_sql_line_with_state(line, SyntaxScanState())
    return tokens


def transform_sql_code_in_selection(
    lines: list[str],
    selected: tuple[tuple[int, int], tuple[int, int]],
    transform: Callable[[str], str],
) -> str:
    token_lines = tokenize_sql_lines(lines)
    (start_row, _), (end_row, _) = selected
    transformed_lines: list[str] = []
    for row in range(start_row, end_row + 1):
        bounds = selection_bounds_for_line(lines[row], row, selected)
        if bounds is None:
            continue
        start, end = bounds
        transformed_lines.append(transform_sql_code_slice(lines[row], token_lines[row], start, end, transform))
    return "\n".join(transformed_lines)


def transform_sql_code_slice(
    line: str,
    tokens: list[SyntaxToken],
    start: int,
    end: int,
    transform: Callable[[str], str],
) -> str:
    output: list[str] = []
    offset = 0
    for token in tokens:
        token_start = offset
        token_end = offset + len(token.text)
        offset = token_end
        selected_start = max(start, token_start)
        selected_end = min(end, token_end)
        if selected_start >= selected_end:
            continue
        fragment = token.text[selected_start - token_start : selected_end - token_start]
        if token.kind in SQL_CODE_TRANSFORM_PRESERVED_KINDS:
            output.append(fragment)
        else:
            output.append(transform(fragment))
    return "".join(output)


def tokenize_sql_line_with_state(
    line: str,
    state: SyntaxScanState,
) -> tuple[list[SyntaxToken], SyntaxScanState]:
    tokens: list[SyntaxToken] = []
    idx = 0
    mode = state.mode
    q_close = state.q_close
    while idx < len(line):
        if mode == SYNTAX_COMMENT:
            end = line.find("*/", idx)
            if end == -1:
                tokens.append(SyntaxToken(line[idx:], SYNTAX_COMMENT))
                idx = len(line)
                continue
            token_end = end + 2
            tokens.append(SyntaxToken(line[idx:token_end], SYNTAX_COMMENT))
            idx = token_end
            mode = SYNTAX_DEFAULT
            continue
        if mode == SYNTAX_STRING:
            if q_close:
                end = line.find(q_close, idx)
                if end == -1:
                    tokens.append(SyntaxToken(line[idx:], SYNTAX_STRING))
                    idx = len(line)
                    continue
                token_end = end + len(q_close)
                tokens.append(SyntaxToken(line[idx:token_end], SYNTAX_STRING))
                idx = token_end
                mode = SYNTAX_DEFAULT
                q_close = ""
                continue
            token_end, closed = scan_single_quoted_string_end(line, idx, starts_with_quote=False)
            tokens.append(SyntaxToken(line[idx:token_end], SYNTAX_STRING))
            idx = token_end
            if closed:
                mode = SYNTAX_DEFAULT
            continue
        ch = line[idx]
        nxt = line[idx + 1] if idx + 1 < len(line) else ""
        if ch == "-" and nxt == "-":
            tokens.append(SyntaxToken(line[idx:], SYNTAX_COMMENT))
            break
        if ch == "/" and nxt == "*":
            end = line.find("*/", idx + 2)
            token_end = len(line) if end == -1 else end + 2
            tokens.append(SyntaxToken(line[idx:token_end], SYNTAX_COMMENT))
            idx = token_end
            if end == -1:
                mode = SYNTAX_COMMENT
                q_close = ""
            continue
        q_quote = q_quote_info(line, idx)
        if q_quote is not None:
            content_start, close = q_quote
            end = line.find(close, content_start)
            if end == -1:
                tokens.append(SyntaxToken(line[idx:], SYNTAX_STRING))
                idx = len(line)
                mode = SYNTAX_STRING
                q_close = close
                continue
            token_end = end + len(close)
            tokens.append(SyntaxToken(line[idx:token_end], SYNTAX_STRING))
            idx = token_end
            continue
        if ch in "nN" and nxt == "'":
            token_end, closed = scan_single_quoted_string_end(line, idx + 1, starts_with_quote=True)
            tokens.append(SyntaxToken(line[idx:token_end], SYNTAX_STRING))
            idx = token_end
            if not closed:
                mode = SYNTAX_STRING
                q_close = ""
            continue
        if ch == "'":
            token_end, closed = scan_single_quoted_string_end(line, idx, starts_with_quote=True)
            tokens.append(SyntaxToken(line[idx:token_end], SYNTAX_STRING))
            idx = token_end
            if not closed:
                mode = SYNTAX_STRING
                q_close = ""
            continue
        if ch == '"':
            token_end = scan_quoted_identifier(line, idx)
            tokens.append(SyntaxToken(line[idx:token_end], SYNTAX_STRING))
            idx = token_end
            continue
        if ch == "$" and nxt == "$" and idx + 2 < len(line) and is_bind_start(line[idx + 2]):
            token_end = idx + 3
            while token_end < len(line) and is_word_char(line[token_end]):
                token_end += 1
            tokens.append(SyntaxToken(line[idx:token_end], SYNTAX_BIND))
            idx = token_end
            continue
        if ch == ":" and is_bind_start(nxt):
            token_end = idx + 2
            while token_end < len(line) and is_word_char(line[token_end]):
                token_end += 1
            tokens.append(SyntaxToken(line[idx:token_end], SYNTAX_BIND))
            idx = token_end
            continue
        if ch == "%" and is_bind_start(nxt):
            token_end = idx + 2
            while token_end < len(line) and is_word_char(line[token_end]):
                token_end += 1
            text = line[idx:token_end]
            if text.lower() in PLSQL_ATTRIBUTES:
                tokens.append(SyntaxToken(text, SYNTAX_KEYWORD))
                idx = token_end
                continue
        if ch.isdigit():
            token_end = scan_number(line, idx)
            tokens.append(SyntaxToken(line[idx:token_end], SYNTAX_NUMBER))
            idx = token_end
            continue
        if is_word_char(ch):
            token_end = idx + 1
            while token_end < len(line) and is_word_char(line[token_end]):
                token_end += 1
            text = line[idx:token_end]
            kind = SYNTAX_KEYWORD if text.lower() in SQL_KEYWORDS else SYNTAX_DEFAULT
            tokens.append(SyntaxToken(text, kind))
            idx = token_end
            continue
        if ch.isspace():
            token_end = idx + 1
            while token_end < len(line) and line[token_end].isspace():
                token_end += 1
            tokens.append(SyntaxToken(line[idx:token_end], SYNTAX_DEFAULT))
            idx = token_end
            continue
        tokens.append(SyntaxToken(ch, SYNTAX_OPERATOR))
        idx += 1
    if tokens:
        return tokens, SyntaxScanState(mode, q_close)
    if mode == SYNTAX_COMMENT:
        empty_kind = SYNTAX_COMMENT
    elif mode == SYNTAX_STRING:
        empty_kind = SYNTAX_STRING
    else:
        empty_kind = SYNTAX_DEFAULT
    return [SyntaxToken("", empty_kind)], SyntaxScanState(mode, q_close)


def q_quote_info(line: str, start: int) -> tuple[int, str] | None:
    delimiter_idx = -1
    if line[start : start + 2].lower() == "q'":
        delimiter_idx = start + 2
    elif line[start : start + 3].lower() == "nq'":
        delimiter_idx = start + 3
    if delimiter_idx < 0 or delimiter_idx >= len(line):
        return None
    delimiter = line[delimiter_idx]
    if delimiter.isspace():
        return None
    close_delimiter = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(delimiter, delimiter)
    return delimiter_idx + 1, close_delimiter + "'"


def scan_single_quoted_string(line: str, start: int) -> int:
    idx, _ = scan_single_quoted_string_end(line, start, starts_with_quote=True)
    return idx


def scan_single_quoted_string_end(line: str, start: int, starts_with_quote: bool) -> tuple[int, bool]:
    idx = start + 1 if starts_with_quote else start
    while idx < len(line):
        if line[idx] == "'" and idx + 1 < len(line) and line[idx + 1] == "'":
            idx += 2
            continue
        if line[idx] == "'":
            return idx + 1, True
        idx += 1
    return len(line), False


def scan_quoted_identifier(line: str, start: int) -> int:
    idx = start + 1
    while idx < len(line):
        if line[idx] == '"':
            return idx + 1
        idx += 1
    return len(line)


def scan_number(line: str, start: int) -> int:
    idx = start
    seen_dot = False
    while idx < len(line):
        ch = line[idx]
        if ch.isdigit():
            idx += 1
            continue
        if ch == "." and not seen_dot and idx + 1 < len(line) and line[idx + 1].isdigit():
            seen_dot = True
            idx += 1
            continue
        break
    if idx < len(line) and line[idx] in "eE":
        exp_idx = idx + 1
        if exp_idx < len(line) and line[exp_idx] in "+-":
            exp_idx += 1
        digits_start = exp_idx
        while exp_idx < len(line) and line[exp_idx].isdigit():
            exp_idx += 1
        if exp_idx > digits_start:
            idx = exp_idx
    return idx


def is_bind_start(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch in "_$#")


def find_matching_bracket_positions(
    lines: list[str],
    token_lines: list[list[SyntaxToken]],
    cursor_row: int,
    cursor_col: int,
) -> set[tuple[int, int]]:
    active = bracket_position_at_cursor(lines, token_lines, cursor_row, cursor_col)
    if active is None:
        return set()
    active_row, active_col = active
    active_char = lines[active_row][active_col]
    brackets = list(iter_code_brackets(lines, token_lines))
    active_index = next((idx for idx, (row, col, _) in enumerate(brackets) if (row, col) == active), None)
    if active_index is None:
        return set()
    match = find_matching_bracket_from_index(brackets, active_index, active_char)
    if match is None:
        return set()
    return {active, match}


def bracket_position_at_cursor(
    lines: list[str],
    token_lines: list[list[SyntaxToken]],
    cursor_row: int,
    cursor_col: int,
) -> tuple[int, int] | None:
    for col in (cursor_col, cursor_col - 1):
        if code_bracket_at(lines, token_lines, cursor_row, col):
            return cursor_row, col
    return None


def code_bracket_at(
    lines: list[str],
    token_lines: list[list[SyntaxToken]],
    row: int,
    col: int,
) -> bool:
    if row < 0 or row >= len(lines) or col < 0 or col >= len(lines[row]):
        return False
    if lines[row][col] not in BRACKET_CHARS:
        return False
    return syntax_kind_at_position(token_lines, row, col) not in {SYNTAX_COMMENT, SYNTAX_STRING}


def syntax_kind_at_position(token_lines: list[list[SyntaxToken]], row: int, col: int) -> str | None:
    if row < 0 or row >= len(token_lines):
        return None
    offset = 0
    for token in token_lines[row]:
        token_end = offset + len(token.text)
        if offset <= col < token_end:
            return token.kind
        offset = token_end
    return None


def iter_code_brackets(
    lines: list[str],
    token_lines: list[list[SyntaxToken]],
) -> list[tuple[int, int, str]]:
    brackets: list[tuple[int, int, str]] = []
    for row, tokens in enumerate(token_lines[: len(lines)]):
        offset = 0
        for token in tokens:
            if token.kind not in {SYNTAX_COMMENT, SYNTAX_STRING}:
                for idx, ch in enumerate(token.text):
                    if ch in BRACKET_CHARS:
                        brackets.append((row, offset + idx, ch))
            offset += len(token.text)
    return brackets


def find_matching_bracket_from_index(
    brackets: list[tuple[int, int, str]],
    active_index: int,
    active_char: str,
) -> tuple[int, int] | None:
    if active_char in BRACKET_PAIRS:
        close_char = BRACKET_PAIRS[active_char]
        depth = 1
        for row, col, ch in brackets[active_index + 1 :]:
            if ch == active_char:
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    return row, col
        return None
    if active_char in CLOSING_BRACKETS:
        open_char = CLOSING_BRACKETS[active_char]
        depth = 1
        for row, col, ch in reversed(brackets[:active_index]):
            if ch == active_char:
                depth += 1
            elif ch == open_char:
                depth -= 1
                if depth == 0:
                    return row, col
    return None


def syntax_line_segments(
    line: str,
    line_idx: int,
    selected: tuple[tuple[int, int], tuple[int, int]] | None,
    tokens: list[SyntaxToken] | None = None,
) -> list[SyntaxSegment]:
    tokens = tokens if tokens is not None else tokenize_sql_line(line)
    bounds = selection_bounds_for_line(line, line_idx, selected) if selected is not None else None
    if bounds is None:
        return [SyntaxSegment(token.text, token.kind, False) for token in tokens]
    start, end = bounds
    if not line and start == end:
        return [SyntaxSegment("", SYNTAX_DEFAULT, True)]
    output: list[SyntaxSegment] = []
    offset = 0
    for token in tokens:
        token_start = offset
        token_end = offset + len(token.text)
        output.extend(split_syntax_token_for_selection(token, token_start, start, end))
        offset = token_end
    return merge_syntax_segments(output)


def split_syntax_token_for_selection(
    token: SyntaxToken,
    token_start: int,
    selection_start: int,
    selection_end: int,
) -> list[SyntaxSegment]:
    token_end = token_start + len(token.text)
    if selection_start == selection_end:
        if token_start <= selection_start <= token_end and token.text == "":
            return [SyntaxSegment("", token.kind, True)]
        if not token.text:
            return [SyntaxSegment(token.text, token.kind, False)]
        parts: list[SyntaxSegment] = []
        if token_start < selection_start < token_end:
            split_at = selection_start - token_start
            parts.append(SyntaxSegment(token.text[:split_at], token.kind, False))
            parts.append(SyntaxSegment("", token.kind, True))
            parts.append(SyntaxSegment(token.text[split_at:], token.kind, False))
            return parts
        if selection_start == token_start:
            return [SyntaxSegment("", token.kind, True), SyntaxSegment(token.text, token.kind, False)]
        if selection_start == token_end:
            return [SyntaxSegment(token.text, token.kind, False), SyntaxSegment("", token.kind, True)]
        return [SyntaxSegment(token.text, token.kind, False)]
    selected_start = max(selection_start, token_start)
    selected_end = min(selection_end, token_end)
    if selected_start >= selected_end:
        return [SyntaxSegment(token.text, token.kind, False)]
    parts = []
    prefix_len = selected_start - token_start
    selected_len = selected_end - selected_start
    if prefix_len:
        parts.append(SyntaxSegment(token.text[:prefix_len], token.kind, False))
    parts.append(SyntaxSegment(token.text[prefix_len : prefix_len + selected_len], token.kind, True))
    suffix = token.text[prefix_len + selected_len :]
    if suffix:
        parts.append(SyntaxSegment(suffix, token.kind, False))
    return parts


def merge_syntax_segments(segments: list[SyntaxSegment]) -> list[SyntaxSegment]:
    merged: list[SyntaxSegment] = []
    for segment in segments:
        if segment.text == "" and not segment.selected:
            continue
        if (
            merged
            and segment.text
            and merged[-1].text
            and merged[-1].kind == segment.kind
            and merged[-1].selected == segment.selected
        ):
            merged[-1] = SyntaxSegment(merged[-1].text + segment.text, segment.kind, segment.selected)
        else:
            merged.append(segment)
    return merged or [SyntaxSegment("", SYNTAX_DEFAULT, False)]


def editor_line_segments(
    line: str,
    line_idx: int,
    selected: tuple[tuple[int, int], tuple[int, int]] | None,
) -> list[tuple[str, bool]]:
    if selected is None:
        return [(line, False)]
    bounds = selection_bounds_for_line(line, line_idx, selected)
    if bounds is None:
        return [(line, False)]
    start, end = bounds
    if not line and start == end:
        return [("", True)]
    if start == end:
        return [(line[:start], False), ("", True), (line[end:], False)]
    return [(line[:start], False), (line[start:end], True), (line[end:], False)]


def selection_bounds_for_line(
    line: str,
    line_idx: int,
    selected: tuple[tuple[int, int], tuple[int, int]],
) -> tuple[int, int] | None:
    (start_row, start_col), (end_row, end_col) = selected
    if line_idx < start_row or line_idx > end_row:
        return None
    line_len = len(line)
    if start_row == end_row:
        start = start_col
        end = end_col
    elif line_idx == start_row:
        start = start_col
        end = line_len
    elif line_idx == end_row:
        start = 0
        end = end_col
    else:
        start = 0
        end = line_len
    return min(max(start, 0), line_len), min(max(end, 0), line_len)
