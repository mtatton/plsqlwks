from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Statement:
    text: str
    start_line: int
    end_line: int
    start_col: int = 0
    end_col: int = 0


@dataclass(frozen=True)
class ScriptPreflightIssue:
    kind: str
    message: str
    line: int
    column: int


PLSQL_CREATE_OBJECTS = {"procedure", "function", "package", "trigger", "type"}


Q_QUOTE_DELIMITERS = {
    "[": "]",
    "{": "}",
    "(": ")",
    "<": ">",
}


PLSQL_LABEL_RE = re.compile(r'(?:[A-Za-z][A-Za-z0-9_$#]*|"(?:[^"]|"")+")\Z')
SCRIPT_DIRECTIVES = {
    "accept",
    "alias",
    "apex",
    "aq",
    "append",
    "archive",
    "arbori",
    "argument",
    "attribute",
    "awr",
    "background",
    "blockchain_table",
    "break",
    "bridge",
    "btitle",
    "cd",
    "certificate",
    "change",
    "clear",
    "cloudstorage",
    "codescan",
    "column",
    "compute",
    "connect",
    "connmgr",
    "copy",
    "ctas",
    "ddl",
    "datapump",
    "define",
    "del",
    "describe",
    "disconnect",
    "diff",
    "edit",
    "execute",
    "exit",
    "find",
    "format",
    "get",
    "help",
    "history",
    "host",
    "info",
    "information",
    "input",
    "liquibase",
    "list",
    "load",
    "migrateadvisor",
    "modeler",
    "net",
    "oci",
    "ocidbmetrics",
    "objectstorage",
    "oerr",
    "orapki",
    "password",
    "pause",
    "popd",
    "print",
    "project",
    "prompt",
    "pushd",
    "quit",
    "recover",
    "remark",
    "repeat",
    "repfooter",
    "repheader",
    "reserved_words",
    "rest",
    "run",
    "save",
    "script",
    "secret",
    "show",
    "shutdown",
    "soda",
    "spool",
    "sshtunnel",
    "start",
    "startup",
    "store",
    "timing",
    "tnsping",
    "tosub",
    "ttitle",
    "undefine",
    "unload",
    "vault",
    "variable",
    "wait4",
    "whenever",
    "which",
    "xquery",
}
SCRIPT_DIRECTIVE_MINIMUMS = {
    "accept": "acc",
    "append": "a",
    "attribute": "attr",
    "break": "bre",
    "btitle": "bti",
    "change": "c",
    "clear": "cl",
    "column": "col",
    "compute": "comp",
    "connect": "conn",
    "define": "def",
    "describe": "desc",
    "disconnect": "disc",
    "edit": "ed",
    "execute": "exec",
    "host": "ho",
    "input": "i",
    "list": "l",
    "password": "passw",
    "pause": "pau",
    "print": "pri",
    "prompt": "pro",
    "remark": "rem",
    "repfooter": "repf",
    "repheader": "reph",
    "run": "r",
    "save": "sav",
    "show": "sho",
    "spool": "spo",
    "start": "sta",
    "timing": "timi",
    "ttitle": "tti",
    "undefine": "undef",
    "variable": "var",
}
SCRIPT_DIRECTIVE_ALIASES = {
    command[:length]
    for command, minimum in SCRIPT_DIRECTIVE_MINIMUMS.items()
    for length in range(len(minimum), len(command))
}
SCRIPT_COMMAND_ALIASES = {"cm", "di", "dp", "lb", "proj"}
ORACLE_SET_STATEMENT_KINDS = {"constraint", "constraints", "role", "transaction"}
SCRIPT_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]*")
SQL_STRUCTURE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]*|[();,]")
SUBSTITUTION_RE = re.compile(r"&&?(?=[A-Za-z0-9_$#\"])")


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
        if not keywords and sql.startswith("<<", idx):
            label_end = sql.find(">>", idx + 2)
            if label_end < 0 or PLSQL_LABEL_RE.fullmatch(sql[idx + 2 : label_end].strip()) is None:
                break
            idx = label_end + 2
            continue
        if idx >= len(sql) or not sql[idx].isalpha():
            break
        end = idx + 1
        while end < len(sql) and (sql[end].isalnum() or sql[end] in "_$#"):
            end += 1
        keywords.append(sql[idx:end].lower())
        idx = end
    return keywords


def _sql_structure_tokens(sql: str) -> list[str]:
    masked = _mask_sql_noncode(sql, mask_quoted_identifiers=True)
    return [match.group(0).lower() for match in SQL_STRUCTURE_TOKEN_RE.finditer(masked)]


def _skip_plsql_block(tokens: list[str], start: int) -> int | None:
    """Return the token after a BEGIN block's closing semicolon."""
    block_depth = 0
    case_depth = 0
    idx = start
    while idx < len(tokens):
        token = tokens[idx]
        if token == "begin":
            block_depth += 1
            idx += 1
            continue
        if token == "case":
            case_depth += 1
            idx += 1
            continue
        if token != "end":
            idx += 1
            continue

        qualifier = tokens[idx + 1] if idx + 1 < len(tokens) else None
        if qualifier == "case":
            if case_depth:
                case_depth -= 1
            idx += 2
            continue
        if qualifier in {"if", "loop"}:
            idx += 2
            continue
        if case_depth:
            case_depth -= 1
            idx += 1
            continue

        block_depth -= 1
        if block_depth:
            idx += 1
            continue

        semicolon_idx = idx + 1
        if semicolon_idx < len(tokens) and tokens[semicolon_idx] != ";":
            semicolon_idx += 1  # Optional block or subprogram name after END.
        if semicolon_idx < len(tokens) and tokens[semicolon_idx] == ";":
            return semicolon_idx + 1
        return None
    return None


def _skip_with_plsql_subprogram(tokens: list[str], start: int) -> int | None:
    """Skip a WITH-clause FUNCTION or PROCEDURE declaration."""
    if start >= len(tokens) or tokens[start] not in {"function", "procedure"}:
        return None

    idx = start + 1
    paren_depth = 0
    while idx < len(tokens):
        token = tokens[idx]
        if token == "(":
            paren_depth += 1
        elif token == ")":
            paren_depth = max(0, paren_depth - 1)
        elif paren_depth == 0 and token in {"as", "is"}:
            idx += 1
            break
        elif paren_depth == 0 and token == ";":
            return idx + 1  # A nested forward declaration.
        idx += 1
    else:
        return None

    while idx < len(tokens):
        token = tokens[idx]
        if token in {"function", "procedure"}:
            nested_end = _skip_with_plsql_subprogram(tokens, idx)
            if nested_end is None:
                return None
            idx = nested_end
            continue
        if token == "begin":
            return _skip_plsql_block(tokens, idx)
        idx += 1
    return None


def _with_plsql_main_sql_terminator(sql: str) -> int | None:
    """Return the final SQL semicolon after leading WITH PL/SQL declarations."""
    masked = _mask_sql_noncode(sql, mask_quoted_identifiers=True)
    matches = list(SQL_STRUCTURE_TOKEN_RE.finditer(masked))
    tokens = [match.group(0).lower() for match in matches]
    if len(tokens) < 2 or tokens[0] != "with" or tokens[1] not in {"function", "procedure"}:
        return None

    idx = 1
    declaration_count = 0
    while idx < len(tokens) and tokens[idx] in {"function", "procedure"}:
        declaration_end = _skip_with_plsql_subprogram(tokens, idx)
        if declaration_end is None:
            return None
        declaration_count += 1
        idx = declaration_end
        if idx < len(tokens) and tokens[idx] == ",":
            idx += 1
    if declaration_count == 0 or idx >= len(tokens):
        return None
    for match, token in zip(matches[idx:], tokens[idx:]):
        if token == ";":
            return match.start()
    return None


def _statement_semicolon_plan(
    script: str,
    statement_start: int,
) -> tuple[str, int | None]:
    """Classify semicolon termination once for one statement source span."""
    sql = script[statement_start:]
    if is_plsql_like(sql):
        return "none", None
    keywords = leading_sql_keywords(sql, 2)
    if len(keywords) >= 2 and keywords[0] == "with" and keywords[1] in {
        "function",
        "procedure",
    }:
        terminator = _with_plsql_main_sql_terminator(sql)
        return (
            "exact",
            None if terminator is None else statement_start + terminator,
        )
    return "any", None


def preflight_script(script: str) -> list[ScriptPreflightIssue]:
    """Return unsupported client syntax before a script reaches binds or Oracle.

    Locations are one-based document coordinates. SQL*Plus substitution scanning is
    intentionally lexical: ampersands in SQL strings and comments are ignored, but
    substitutions in executable SQL and quoted identifiers are reported.
    """
    if not script:
        return []
    line_starts = [0]
    line_starts.extend(idx + 1 for idx, ch in enumerate(script) if ch == "\n")
    issues = _directive_preflight_issues(script, line_starts)
    substitution_code = _mask_sql_noncode(script, mask_quoted_identifiers=False)
    for match in SUBSTITUTION_RE.finditer(substitution_code):
        line, column = _document_location(line_starts, match.start())
        token = _substitution_token(script, match.start())
        issues.append(
            ScriptPreflightIssue(
                kind="substitution",
                message=f"SQL*Plus substitution variable {token!r} is not supported.",
                line=line,
                column=column,
            )
        )
    return sorted(issues, key=lambda issue: (issue.line, issue.column, issue.kind))


def _directive_preflight_issues(script: str, line_starts: list[int]) -> list[ScriptPreflightIssue]:
    masked = _mask_sql_noncode(script, mask_quoted_identifiers=True)
    issues: list[ScriptPreflightIssue] = []
    statement_start: int | None = None
    plan_start: int | None = None
    plan_mode = "any"
    exact_terminator: int | None = None

    for line_idx, line_start in enumerate(line_starts):
        line_end = line_starts[line_idx + 1] - 1 if line_idx + 1 < len(line_starts) else len(script)
        code_line = masked[line_start:line_end]
        first_code_offset = len(code_line) - len(code_line.lstrip())
        stripped = code_line[first_code_offset:]
        stripped_token = stripped.strip()

        if stripped_token == "/":
            if statement_start is not None and is_plsql_like(script[statement_start:line_start]):
                statement_start = None
                continue
            issue_idx = line_start + first_code_offset
            line, column = _document_location(line_starts, issue_idx)
            issues.append(
                ScriptPreflightIssue(
                    kind="directive",
                    message="Unsupported SQL*Plus/SQLcl directive '/'.",
                    line=line,
                    column=column,
                )
            )
            statement_start = None
            plan_start = None
            continue
        if statement_start is None:
            if not stripped_token:
                continue
            if stripped_token == ";":
                issue_idx = line_start + first_code_offset
                line, column = _document_location(line_starts, issue_idx)
                issues.append(
                    ScriptPreflightIssue(
                        kind="directive",
                        message="Unsupported SQL*Plus/SQLcl directive ';'.",
                        line=line,
                        column=column,
                    )
                )
                continue
            directive = _script_directive(masked[line_start + first_code_offset :])
            if directive is not None:
                issue_idx = line_start + first_code_offset
                line, column = _document_location(line_starts, issue_idx)
                issues.append(
                    ScriptPreflightIssue(
                        kind="directive",
                        message=f"Unsupported SQL*Plus/SQLcl directive {directive!r}.",
                        line=line,
                        column=column,
                    )
                )
                continue
            statement_start = line_start + first_code_offset
            plan_start = None

        search_start = max(statement_start, line_start)
        while statement_start is not None:
            terminator = masked.find(";", search_start, line_end)
            if terminator < 0:
                break
            if plan_start != statement_start:
                plan_mode, exact_terminator = _statement_semicolon_plan(
                    script,
                    statement_start,
                )
                plan_start = statement_start
            terminates = plan_mode == "any" or (
                plan_mode == "exact" and terminator == exact_terminator
            )
            if not terminates:
                search_start = terminator + 1
                continue
            statement_start = None
            plan_start = None
            search_start = terminator + 1
            trailing = masked[search_start:line_end]
            if trailing.strip():
                statement_start = search_start + len(trailing) - len(trailing.lstrip())
                plan_start = None

    return issues


def _script_directive(code: str) -> str | None:
    if code.startswith("@@"):
        return "@@"
    if code.startswith("@"):
        return "@"
    if code.startswith("!"):
        return "!"
    if code.startswith("?"):
        return "?"
    first = SCRIPT_WORD_RE.match(code)
    if first is None:
        return None
    command = first.group(0)
    command_lower = command.lower()
    if command_lower == "set":
        second = SCRIPT_WORD_RE.search(code, first.end())
        if second is not None and second.group(0).lower() in ORACLE_SET_STATEMENT_KINDS:
            return None
        return command
    if (
        command_lower in SCRIPT_DIRECTIVES
        or command_lower in SCRIPT_DIRECTIVE_ALIASES
        or command_lower in SCRIPT_COMMAND_ALIASES
    ):
        return command
    return None


def _mask_sql_noncode(script: str, *, mask_quoted_identifiers: bool) -> str:
    masked = list(script)
    idx = 0
    while idx < len(script):
        nxt = script[idx + 1] if idx + 1 < len(script) else ""
        if script[idx] == "-" and nxt == "-":
            end = script.find("\n", idx + 2)
            end = len(script) if end < 0 else end
            _mask_range(masked, script, idx, end)
            idx = end
            continue
        if script[idx] == "/" and nxt == "*":
            comment_end = script.find("*/", idx + 2)
            end = len(script) if comment_end < 0 else comment_end + 2
            _mask_range(masked, script, idx, end)
            idx = end
            continue
        q_quote_end = _q_quote_end(script, idx)
        if q_quote_end is not None:
            _mask_range(masked, script, idx, q_quote_end)
            idx = q_quote_end
            continue
        if script[idx] == "'":
            end = _quoted_text_end(script, idx, "'")
            _mask_range(masked, script, idx, end)
            idx = end
            continue
        if script[idx] == '"':
            end = _quoted_text_end(script, idx, '"')
            if mask_quoted_identifiers:
                _mask_range(masked, script, idx, end)
            idx = end
            continue
        idx += 1
    return "".join(masked)


def _q_quote_end(script: str, start: int) -> int | None:
    if script[start : start + 2].lower() == "q'":
        delimiter_idx = start + 2
    elif script[start : start + 3].lower() == "nq'":
        delimiter_idx = start + 3
    else:
        return None
    if delimiter_idx >= len(script) or script[delimiter_idx].isspace():
        return None
    close = Q_QUOTE_DELIMITERS.get(script[delimiter_idx], script[delimiter_idx]) + "'"
    close_idx = script.find(close, delimiter_idx + 1)
    return len(script) if close_idx < 0 else close_idx + len(close)


def _quoted_text_end(script: str, start: int, quote: str) -> int:
    idx = start + 1
    while idx < len(script):
        if script[idx] == quote and idx + 1 < len(script) and script[idx + 1] == quote:
            idx += 2
            continue
        if script[idx] == quote:
            return idx + 1
        idx += 1
    return len(script)


def _mask_range(masked: list[str], script: str, start: int, end: int) -> None:
    for idx in range(start, end):
        if script[idx] not in "\r\n":
            masked[idx] = " "


def _document_location(line_starts: list[int], idx: int) -> tuple[int, int]:
    line_idx = bisect_right(line_starts, idx) - 1
    return line_idx + 1, idx - line_starts[line_idx] + 1


def _substitution_token(script: str, start: int) -> str:
    end = start + (2 if script.startswith("&&", start) else 1)
    if end < len(script) and script[end] == '"':
        return script[start : _quoted_text_end(script, end, '"')]
    while end < len(script) and (script[end].isalnum() or script[end] in "_$#"):
        end += 1
    return script[start:end]


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
    start_offset = 0
    plan_start: int | None = None
    plan_mode = "any"
    exact_terminator: int | None = None
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
        nonlocal current, start_line, start_col, start_offset, plan_start
        text = "\n".join(current).strip()
        if text:
            if text.endswith(";") and not is_plsql_like(text):
                text = text[:-1].rstrip()
            if text and strip_leading_sql_comments(text):
                statement_end_col = (
                    current_end_col(end_line) if end_col is None else end_col
                )
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
        start_offset = 0
        plan_start = None

    lines = script.split("\n")
    if script.endswith("\n"):
        lines.pop()
    line_offsets = [0]
    line_offsets.extend(idx + 1 for idx, ch in enumerate(script) if ch == "\n")
    for line_no, line in enumerate(lines, start=1):
        # A SQL line comment always ends at the physical newline.
        in_line_comment = False
        if not current and not line.strip():
            start_line = line_no + 1
            start_col = 0
            continue

        slash_line = (
            _mask_sql_noncode(line, mask_quoted_identifiers=True).strip() == "/"
        )
        if slash_line and not (in_single or in_double or in_block_comment or in_q_quote):
            if current and is_plsql_like("\n".join(current)):
                flush(line_no - 1)
                start_line = line_no + 1
                start_col = 0
                continue

        if current and line == "":
            current.append("")
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
                start_offset = line_offsets[line_no - 1] + offset
                plan_start = None

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
                if ch == ";":
                    terminator_offset = line_offsets[line_no - 1] + offset + idx
                    if plan_start != start_offset:
                        plan_mode, exact_terminator = _statement_semicolon_plan(
                            script,
                            start_offset,
                        )
                        plan_start = start_offset
                    terminates = plan_mode == "any" or (
                        plan_mode == "exact"
                        and terminator_offset == exact_terminator
                    )
                    if terminates:
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
