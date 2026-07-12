from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class SqlBind:
    name: str
    start: int
    end: int
    quoted: bool = False


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


def find_bind_names(statement: str) -> list[str]:
    return [bind.name for bind in find_unique_binds(statement)]


def find_unique_binds(statement: str) -> list[SqlBind]:
    binds: list[SqlBind] = []
    seen: set[str] = set()
    for bind in find_sql_binds(statement):
        if is_trigger_pseudorecord_bind(statement, bind):
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
    idx = 0
    length = len(statement)
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
            continue
        if ch in "nN" and nxt == "'":
            idx = scan_single_quoted_string(statement, idx + 1)
            continue
        if ch == "'":
            idx = scan_single_quoted_string(statement, idx)
            continue
        if ch == '"':
            idx = scan_quoted_identifier(statement, idx)
            continue
        if ch == ":" and idx > 0 and statement[idx - 1] == ":":
            idx += 1
            continue
        if ch == ":":
            name_start = idx + 1
            while name_start < length and statement[name_start].isspace():
                name_start += 1
            if name_start < length and statement[name_start] == '"':
                end = scan_quoted_identifier(statement, name_start)
                binds.append(SqlBind(statement[name_start:end], idx, end, quoted=True))
                idx = end
                continue
            if name_start >= length or not is_bind_char(statement[name_start]):
                idx += 1
                continue
            end = name_start + 1
            while end < length and is_bind_char(statement[end]):
                end += 1
            binds.append(SqlBind(statement[name_start:end], idx, end))
            idx = end
            continue
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


def is_bind_char(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch in "_$#")


def is_trigger_pseudorecord_bind(statement: str, bind: SqlBind) -> bool:
    if bind.name.lower() not in {"new", "old"}:
        return False
    if TRIGGER_RE.search(statement) is None:
        return False
    idx = bind.end
    while idx < len(statement) and statement[idx].isspace():
        idx += 1
    return idx < len(statement) and statement[idx] == "."
