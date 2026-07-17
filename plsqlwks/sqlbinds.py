from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SqlBind:
    name: str
    start: int
    end: int
    quoted: bool = False


@dataclass
class _CallFrame:
    json_object: bool
    entry_has_expression: bool = False
    separator_seen: bool = False


Q_QUOTE_DELIMITERS = {
    "[": "]",
    "{": "}",
    "(": ")",
    "<": ">",
}
SQL_GAP = r"(?:\s|--[^\r\n]*(?:\r?\n|$)|/\*.*?\*/)+"
TRIGGER_RE = re.compile(
    rf"\bcreate{SQL_GAP}(?:or{SQL_GAP}replace{SQL_GAP})?"
    rf"(?:(?:non)?editionable{SQL_GAP})?trigger\b",
    re.IGNORECASE | re.DOTALL,
)
_TRIGGER_PSEUDORECORD_NAMES = {"new", "old", "parent"}


def find_bind_names(statement: str) -> list[str]:
    return [bind.name for bind in find_unique_binds(statement)]


def find_unique_binds(statement: str) -> list[SqlBind]:
    binds: list[SqlBind] = []
    seen: set[str] = set()
    trigger_names = _trigger_pseudorecord_context(statement)
    for bind in find_sql_binds(statement):
        if _is_trigger_pseudorecord_bind(statement, bind, trigger_names):
            continue
        key = bind_name_key(bind.name, bind.quoted)
        if key in seen:
            continue
        seen.add(key)
        binds.append(bind)
    return binds


def bind_name_key(name: str, quoted: bool | None = None) -> str:
    """Return the key python-oracledb uses to identify a named bind."""
    has_quotes = len(name) >= 2 and name.startswith('"') and name.endswith('"')
    if quoted is None:
        quoted = has_quotes
    if quoted:
        return name if has_quotes else f'"{name}"'
    return name.upper()


def find_sql_binds(statement: str) -> list[SqlBind]:
    binds: list[SqlBind] = []
    call_frames: list[_CallFrame] = []
    last_word: str | None = None
    idx = 0
    length = len(statement)

    def mark_json_expression() -> None:
        if call_frames and call_frames[-1].json_object:
            call_frames[-1].entry_has_expression = True

    while idx < length:
        ch = statement[idx]
        nxt = statement[idx + 1] if idx + 1 < length else ""
        if ch == "-" and nxt == "-":
            newline = statement.find("\n", idx + 2)
            idx = length if newline == -1 else newline + 1
            continue
        if ch == "/" and nxt == "*":
            end = statement.find("*/", idx + 2)
            idx = length if end == -1 else end + 2
            continue
        q_quote = q_quote_info(statement, idx)
        if q_quote is not None:
            content_start, close = q_quote
            end = statement.find(close, content_start)
            idx = length if end == -1 else end + len(close)
            mark_json_expression()
            last_word = None
            continue
        if ch in "nN" and nxt == "'":
            idx = scan_single_quoted_string(statement, idx + 1)
            mark_json_expression()
            last_word = None
            continue
        if ch == "'":
            idx = scan_single_quoted_string(statement, idx)
            mark_json_expression()
            last_word = None
            continue
        if ch == '"':
            idx = scan_quoted_identifier(statement, idx)
            mark_json_expression()
            last_word = None
            continue
        if ch.isalpha():
            end = idx + 1
            while end < length and (statement[end].isalnum() or statement[end] in "_$#"):
                end += 1
            word = statement[idx:end].lower()
            if call_frames and call_frames[-1].json_object:
                frame = call_frames[-1]
                if word == "value" and frame.entry_has_expression and not frame.separator_seen:
                    frame.separator_seen = True
                else:
                    frame.entry_has_expression = True
            last_word = word
            idx = end
            continue
        if ch == "(":
            mark_json_expression()
            call_frames.append(_CallFrame(last_word == "json_object"))
            last_word = None
            idx += 1
            continue
        if ch == ")":
            if call_frames:
                call_frames.pop()
            mark_json_expression()
            last_word = None
            idx += 1
            continue
        if ch == ",":
            if call_frames and call_frames[-1].json_object:
                frame = call_frames[-1]
                frame.entry_has_expression = False
                frame.separator_seen = False
            last_word = None
            idx += 1
            continue
        if ch == ":" and idx > 0 and statement[idx - 1] == ":":
            idx += 1
            continue
        if ch == ":":
            if call_frames and call_frames[-1].json_object:
                frame = call_frames[-1]
                if frame.entry_has_expression and not frame.separator_seen:
                    frame.separator_seen = True
                    last_word = None
                    idx += 1
                    continue
            name_start = idx + 1
            while name_start < length and statement[name_start].isspace():
                name_start += 1
            if name_start < length and statement[name_start] == '"':
                end = scan_quoted_identifier(statement, name_start)
                binds.append(SqlBind(statement[name_start:end], idx, end, quoted=True))
                mark_json_expression()
                last_word = None
                idx = end
                continue
            if name_start >= length or not is_bind_char(statement[name_start]):
                last_word = None
                idx += 1
                continue
            end = name_start + 1
            while end < length and is_bind_char(statement[end]):
                end += 1
            binds.append(SqlBind(statement[name_start:end], idx, end))
            mark_json_expression()
            last_word = None
            idx = end
            continue
        if not ch.isspace() and ch != ".":
            mark_json_expression()
            last_word = None
        idx += 1
    return binds


def q_quote_info(statement: str, start: int) -> tuple[int, str] | None:
    delimiter_idx = -1
    if statement[start : start + 2].lower() == "q'":
        delimiter_idx = start + 2
    elif statement[start : start + 3].lower() == "nq'":
        delimiter_idx = start + 3
    if delimiter_idx < 0 or delimiter_idx >= len(statement):
        return None
    delimiter = statement[delimiter_idx]
    if delimiter.isspace():
        return None
    close_delimiter = Q_QUOTE_DELIMITERS.get(delimiter, delimiter)
    return delimiter_idx + 1, close_delimiter + "'"


def scan_single_quoted_string(statement: str, start: int) -> int:
    idx = start + 1
    while idx < len(statement):
        if statement[idx] == "'" and idx + 1 < len(statement) and statement[idx + 1] == "'":
            idx += 2
            continue
        if statement[idx] == "'":
            return idx + 1
        idx += 1
    return len(statement)


def scan_quoted_identifier(statement: str, start: int) -> int:
    idx = start + 1
    while idx < len(statement):
        if statement[idx] == '"' and idx + 1 < len(statement) and statement[idx + 1] == '"':
            idx += 2
            continue
        if statement[idx] == '"':
            return idx + 1
        idx += 1
    return len(statement)


def _mask_trigger_detection_noncode(statement: str) -> str:
    """Mask text that cannot contain CREATE TRIGGER while preserving offsets."""
    masked = list(statement)
    idx = 0
    while idx < len(statement):
        nxt = statement[idx + 1] if idx + 1 < len(statement) else ""
        if statement[idx] == "-" and nxt == "-":
            newline = statement.find("\n", idx + 2)
            end = len(statement) if newline < 0 else newline
        elif statement[idx] == "/" and nxt == "*":
            comment_end = statement.find("*/", idx + 2)
            end = len(statement) if comment_end < 0 else comment_end + 2
        else:
            q_quote = q_quote_info(statement, idx)
            if q_quote is not None:
                content_start, close = q_quote
                quote_end = statement.find(close, content_start)
                end = len(statement) if quote_end < 0 else quote_end + len(close)
            elif statement[idx] == "'":
                end = scan_single_quoted_string(statement, idx)
            elif statement[idx] == '"':
                end = scan_quoted_identifier(statement, idx)
            else:
                idx += 1
                continue
        for mask_idx in range(idx, end):
            if statement[mask_idx] not in "\r\n":
                masked[mask_idx] = " "
        idx = end
    return "".join(masked)


def is_bind_char(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch in "_$#")


def is_trigger_pseudorecord_bind(statement: str, bind: SqlBind) -> bool:
    return _is_trigger_pseudorecord_bind(
        statement,
        bind,
        _trigger_pseudorecord_context(statement),
    )


def _trigger_pseudorecord_context(statement: str) -> set[str] | None:
    trigger_match = TRIGGER_RE.search(_mask_trigger_detection_noncode(statement))
    if trigger_match is None:
        return None
    return _trigger_pseudorecord_names(statement, trigger_match.end())


def _is_trigger_pseudorecord_bind(
    statement: str,
    bind: SqlBind,
    trigger_names: set[str] | None,
) -> bool:
    if bind.quoted or trigger_names is None:
        return False
    if bind.name.lower() not in trigger_names:
        return False
    idx = _skip_sql_gap(statement, bind.end)
    return idx < len(statement) and statement[idx] == "."


def _trigger_pseudorecord_names(statement: str, header_start: int) -> set[str]:
    names = set(_TRIGGER_PSEUDORECORD_NAMES)
    tokens = _trigger_header_tokens(statement, header_start)
    try:
        idx = next(idx for idx, (token, _) in enumerate(tokens) if token == "referencing") + 1
    except StopIteration:
        return names
    while idx < len(tokens):
        token, _ = tokens[idx]
        if token in {"begin", "compound", "declare", "for", "when"}:
            break
        if token not in _TRIGGER_PSEUDORECORD_NAMES:
            idx += 1
            continue
        alias_idx = idx + 1
        if alias_idx < len(tokens) and tokens[alias_idx][0] == "as":
            alias_idx += 1
        if alias_idx < len(tokens):
            alias, quoted = tokens[alias_idx]
            if not quoted:
                names.add(alias)
            idx = alias_idx + 1
            continue
        break
    return names


def _trigger_header_tokens(statement: str, start: int) -> list[tuple[str, bool]]:
    tokens: list[tuple[str, bool]] = []
    idx = start
    while idx < len(statement):
        nxt = statement[idx + 1] if idx + 1 < len(statement) else ""
        if statement[idx] == "-" and nxt == "-":
            newline = statement.find("\n", idx + 2)
            idx = len(statement) if newline < 0 else newline + 1
            continue
        if statement[idx] == "/" and nxt == "*":
            comment_end = statement.find("*/", idx + 2)
            idx = len(statement) if comment_end < 0 else comment_end + 2
            continue
        q_quote = q_quote_info(statement, idx)
        if q_quote is not None:
            content_start, close = q_quote
            quote_end = statement.find(close, content_start)
            idx = len(statement) if quote_end < 0 else quote_end + len(close)
            continue
        if statement[idx] == "'":
            idx = scan_single_quoted_string(statement, idx)
            continue
        if statement[idx] == '"':
            end = scan_quoted_identifier(statement, idx)
            tokens.append((statement[idx:end].lower(), True))
            idx = end
            continue
        if statement[idx].isalpha():
            end = idx + 1
            while end < len(statement) and is_bind_char(statement[end]):
                end += 1
            tokens.append((statement[idx:end].lower(), False))
            idx = end
            continue
        idx += 1
    return tokens


def _skip_sql_gap(statement: str, start: int) -> int:
    idx = start
    while idx < len(statement):
        if statement[idx].isspace():
            idx += 1
            continue
        if statement.startswith("--", idx):
            newline = statement.find("\n", idx + 2)
            return len(statement) if newline < 0 else _skip_sql_gap(statement, newline + 1)
        if statement.startswith("/*", idx):
            comment_end = statement.find("*/", idx + 2)
            return len(statement) if comment_end < 0 else _skip_sql_gap(statement, comment_end + 2)
        break
    return idx
