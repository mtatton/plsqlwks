from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Statement:
    text: str
    start_line: int
    end_line: int
    start_col: int = 0
    end_col: int = 0


PLSQL_CREATE_OBJECTS = {"procedure", "function", "package", "trigger", "type"}


Q_QUOTE_DELIMITERS = {
    "[": "]",
    "{": "}",
    "(": ")",
    "<": ">",
}


def is_plsql_like(sql: str) -> bool:
    keywords = leading_sql_keywords(sql, 6)
    if not keywords:
        return False
    if keywords[0] in {"begin", "declare"}:
        return True
    if keywords[0] != "create":
        return False
    idx = 1
    if keywords[idx : idx + 2] == ["or", "replace"]:
        idx += 2
    if idx < len(keywords) and keywords[idx] in {"editionable", "noneditionable"}:
        idx += 1
    return idx < len(keywords) and keywords[idx] in PLSQL_CREATE_OBJECTS


def leading_sql_keywords(sql: str, limit: int) -> list[str]:
    """Read leading keywords while treating SQL comments as whitespace."""
    keywords: list[str] = []
    idx = 0
    while idx < len(sql) and len(keywords) < limit:
        while idx < len(sql):
            if sql[idx].isspace():
                idx += 1
                continue
            if sql.startswith("--", idx):
                newline = sql.find("\n", idx + 2)
                if newline < 0:
                    return keywords
                idx = newline + 1
                continue
            if sql.startswith("/*", idx):
                comment_end = sql.find("*/", idx + 2)
                if comment_end < 0:
                    return keywords
                idx = comment_end + 2
                continue
            break
        if idx >= len(sql) or not sql[idx].isalpha():
            break
        end = idx + 1
        while end < len(sql) and (sql[end].isalnum() or sql[end] in "_$#"):
            end += 1
        keywords.append(sql[idx:end].lower())
        idx = end
    return keywords


def strip_leading_sql_comments(sql: str) -> str:
    remaining = sql.lstrip()
    while remaining:
        if remaining.startswith("--"):
            newline = remaining.find("\n")
            if newline < 0:
                return ""
            remaining = remaining[newline + 1 :].lstrip()
            continue
        if remaining.startswith("/*"):
            end = remaining.find("*/", 2)
            if end < 0:
                return ""
            remaining = remaining[end + 2 :].lstrip()
            continue
        return remaining
    return ""


def split_script(script: str) -> list[Statement]:
    """Split SQL scripts on semicolons, and PL/SQL blocks on slash lines."""
    statements: list[Statement] = []
    current: list[str] = []
    start_line = 1
    start_col = 0
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False
    in_q_quote: str | None = None

    def current_end_col(end_line: int) -> int:
        if not current:
            return 0
        if start_line == end_line and len(current) == 1:
            return start_col + len(current[-1])
        return len(current[-1])

    def flush(end_line: int, end_col: int | None = None) -> None:
        nonlocal current, start_line, start_col
        text = "\n".join(current).strip()
        if text:
            if text.endswith(";") and not is_plsql_like(text):
                text = text[:-1].rstrip()
            statement_end_col = current_end_col(end_line) if end_col is None else end_col
            statements.append(
                Statement(
                    text=text,
                    start_line=start_line,
                    end_line=end_line,
                    start_col=start_col,
                    end_col=max(start_col, statement_end_col),
                )
            )
        current = []
        start_line = end_line + 1
        start_col = 0

    lines = script.splitlines()
    for line_no, line in enumerate(lines, start=1):
        # A SQL line comment always ends at the physical newline.
        in_line_comment = False
        if not current and not line.strip():
            start_line = line_no + 1
            start_col = 0
            continue

        if line.strip() == "/" and not (in_single or in_double or in_block_comment or in_q_quote):
            if current and is_plsql_like("\n".join(current)):
                flush(line_no - 1)
                start_line = line_no + 1
                start_col = 0
                continue
            if not current:
                start_line = line_no + 1
                start_col = 0
                continue

        segment = line
        offset = 0
        while segment:
            if not current:
                leading = len(segment) - len(segment.lstrip())
                segment = segment.lstrip()
                offset += leading
                if not segment:
                    start_line = line_no + 1
                    start_col = 0
                    break
                if segment == "/":
                    start_line = line_no + 1
                    start_col = 0
                    break
                start_line = line_no
                start_col = offset

            current.append(segment)
            idx = 0
            split_remainder: str | None = None
            split_offset = 0
            while idx < len(segment):
                ch = segment[idx]
                nxt = segment[idx + 1] if idx + 1 < len(segment) else ""
                if in_line_comment:
                    break
                if in_block_comment:
                    if ch == "*" and nxt == "/":
                        in_block_comment = False
                        idx += 2
                        continue
                    idx += 1
                    continue
                if in_q_quote is not None:
                    if ch == in_q_quote and nxt == "'":
                        in_q_quote = None
                        idx += 2
                        continue
                    idx += 1
                    continue
                if in_single:
                    if ch == "'" and nxt == "'":
                        idx += 2
                        continue
                    if ch == "'":
                        in_single = False
                    idx += 1
                    continue
                if in_double:
                    if ch == '"':
                        in_double = False
                    idx += 1
                    continue
                if ch == "-" and nxt == "-":
                    in_line_comment = True
                    break
                if ch == "/" and nxt == "*":
                    in_block_comment = True
                    idx += 2
                    continue
                if ch in "qQ" and nxt == "'" and idx + 2 < len(segment):
                    opener = segment[idx + 2]
                    in_q_quote = Q_QUOTE_DELIMITERS.get(opener, opener)
                    idx += 3
                    continue
                if ch == "'":
                    in_single = True
                    idx += 1
                    continue
                if ch == '"':
                    in_double = True
                    idx += 1
                    continue
                if ch == ";" and not is_plsql_like("\n".join(current)):
                    current[-1] = segment[: idx + 1]
                    flush(line_no, offset + idx + 1)
                    remainder = segment[idx + 1 :]
                    if remainder.strip():
                        leading = len(remainder) - len(remainder.lstrip())
                        split_remainder = remainder.lstrip()
                        split_offset = offset + idx + 1 + leading
                    else:
                        start_line = line_no + 1
                        start_col = 0
                    break
                idx += 1

            if split_remainder is None:
                break
            segment = split_remainder
            offset = split_offset

    if current:
        flush(len(lines))
    return statements


def statement_at_cursor(script: str, cursor_line: int, cursor_col: int = 0) -> Statement | None:
    statements = split_script(script)
    if not statements:
        return None
    document_line = cursor_line + 1
    for statement in statements:
        if statement_contains_cursor(statement, document_line, cursor_col):
            return statement
    for statement in statements:
        if statement.start_line > document_line:
            return statement
        if statement.start_line == document_line and statement.start_col >= cursor_col:
            return statement
    return statements[-1]


def statement_contains_cursor(statement: Statement, document_line: int, cursor_col: int) -> bool:
    if not (statement.start_line <= document_line <= statement.end_line):
        return False
    if statement.start_line == statement.end_line:
        return statement.start_col <= cursor_col < statement.end_col
    if document_line == statement.start_line:
        return cursor_col >= statement.start_col
    if document_line == statement.end_line:
        return cursor_col < statement.end_col
    return True
