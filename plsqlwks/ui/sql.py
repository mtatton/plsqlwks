from __future__ import annotations

import re

from ..db.identifiers import (
    render_identifier,
    scan_oracle_identifier,
)
from ..db.sql_analysis import sql_code_mask
from .browser import BrowserEntry
from .constants import FOCUS_BROWSER, SQL_KEYWORDS

COMPLETION_KIND_ORDER = {
    "column": 0,
    "table": 1,
    "view": 2,
    "procedure": 3,
    "function": 4,
    "package": 5,
    "keyword": 6,
}
SQL_GENERATOR_CHOICES = (
    "SELECT with columns",
    "INSERT with columns",
    "UPDATE with columns",
)
INSERT_ROWID_MARKER = "<new>"
# Retain the pre-package facade symbol for callers that imported the original
# simple unquoted-reference matcher. Internal parsing uses the masked scanner
# below so comments, strings, and quoted identifiers are handled safely.
TABLE_REFERENCE_RE = re.compile(
    r"\b(?:from|join|update|into)\s+([A-Za-z][A-Za-z0-9_$#]*)"
    r"(?:\s+(?:as\s+)?([A-Za-z][A-Za-z0-9_$#]*))?",
    re.IGNORECASE,
)
TABLE_REFERENCE_KEYWORD_RE = re.compile(r"\b(?:from|join|update|into)\b", re.IGNORECASE)
TABLE_ALIAS_STOP_WORDS = {
    "CONNECT",
    "CROSS",
    "FETCH",
    "FOR",
    "FULL",
    "GROUP",
    "HAVING",
    "INNER",
    "INTERSECT",
    "JOIN",
    "LEFT",
    "MINUS",
    "MODEL",
    "NATURAL",
    "ON",
    "ORDER",
    "OUTER",
    "PARTITION",
    "PIVOT",
    "RIGHT",
    "SAMPLE",
    "START",
    "UNION",
    "UNPIVOT",
    "VERSIONS",
    "WHERE",
    *{keyword.upper() for keyword in SQL_KEYWORDS},
}


def statement_table_references(statement: str) -> dict[str, str]:
    references: dict[str, str] = {}
    keyword_mask = sql_code_mask(statement)
    reference_mask = sql_code_mask(
        statement,
        preserve_quoted_identifiers=True,
    )
    for match in TABLE_REFERENCE_KEYWORD_RE.finditer(keyword_mask):
        table_start = _next_reference_token_start(reference_mask, match.end())
        table_token = scan_oracle_identifier(statement, table_start)
        if table_token is None or not table_token.name:
            continue
        table_name = table_token.name
        references.setdefault(table_name, table_name)
        alias_start = _next_reference_token_start(reference_mask, table_token.end)
        alias_token = scan_oracle_identifier(statement, alias_start)
        if alias_token is None:
            continue
        if not alias_token.quoted and alias_token.name == "AS":
            alias_start = _next_reference_token_start(
                reference_mask,
                alias_token.end,
            )
            alias_token = scan_oracle_identifier(statement, alias_start)
            if alias_token is None:
                continue
        if alias_token.quoted or alias_token.name not in TABLE_ALIAS_STOP_WORDS:
            references[alias_token.name] = table_name
    return references


def _next_reference_token_start(mask: str, start: int) -> int:
    idx = start
    while idx < len(mask) and mask[idx].isspace():
        idx += 1
    return idx


def selected_browser_table_name(focus: str, entry: BrowserEntry | None) -> str:
    if focus != FOCUS_BROWSER or entry is None:
        return ""
    if entry.kind != "object" or entry.object_type not in {"TABLE", "VIEW"}:
        return ""
    return sql_identifier(entry.object_name)


def generated_sql_table_from_statement(statement: str) -> str:
    references = statement_table_references(statement)
    tables = ordered_reference_tables(references)
    return sql_identifier(tables[0]) if tables else ""


def sql_identifier(name: str) -> str:
    return render_identifier(name, {keyword.upper() for keyword in SQL_KEYWORDS})


def sql_bind_names(columns: list[str]) -> list[str]:
    names: list[str] = []
    used: set[str] = set()
    for column in columns:
        base = sql_bind_name(str(column))
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        names.append(candidate)
        used.add(candidate.casefold())
    return names


def sql_column_lines(
    columns: list[str],
    assignment: bool = False,
    bind_names: list[str] | None = None,
) -> list[str]:
    lines: list[str] = []
    names = bind_names if bind_names is not None else sql_bind_names(columns)
    for idx, column in enumerate(columns):
        column_name = sql_identifier(str(column))
        suffix = "," if idx < len(columns) - 1 else ""
        if assignment:
            lines.append(f"  {column_name} = :{names[idx]}{suffix}")
        else:
            lines.append(f"  {column_name}{suffix}")
    return lines


def sql_bind_name(column: str) -> str:
    bind = re.sub(r"[^A-Za-z0-9_]", "_", column.lower()).strip("_")
    if not bind:
        return "value"
    if bind[0].isdigit():
        return f"value_{bind}"
    return bind


def generated_select_sql(table_name: str, columns: list[str]) -> str:
    return "\n".join(["select", *sql_column_lines(columns), f"from {sql_identifier(table_name)};", ""])


def generated_insert_sql(table_name: str, columns: list[str]) -> str:
    bind_names = sql_bind_names(columns)
    bind_lines = [f"  :{bind_names[idx]}{',' if idx < len(columns) - 1 else ''}" for idx, _column in enumerate(columns)]
    return "\n".join(
        [
            f"insert into {sql_identifier(table_name)} (",
            *sql_column_lines(columns, bind_names=bind_names),
            ") values (",
            *bind_lines,
            ");",
            "",
        ]
    )


def generated_update_sql(table_name: str, columns: list[str]) -> str:
    bind_names = sql_bind_names(columns)
    return "\n".join(
        [
            f"update {sql_identifier(table_name)}",
            "set",
            *sql_column_lines(columns, assignment=True, bind_names=bind_names),
            "where <condition>;",
            "",
        ]
    )


def generated_sql_for_choice(choice: str, table_name: str, columns: list[str]) -> str:
    if choice == "SELECT with columns":
        return generated_select_sql(table_name, columns)
    if choice == "INSERT with columns":
        return generated_insert_sql(table_name, columns)
    if choice == "UPDATE with columns":
        return generated_update_sql(table_name, columns)
    return ""


def ordered_reference_tables(references: dict[str, str]) -> list[str]:
    tables: list[str] = []
    for table_name in references.values():
        if table_name not in tables:
            tables.append(table_name)
    return tables


def looks_like_plsql(text: str) -> bool:
    lowered = text.lower()
    return any(
        token in lowered
        for token in [
            "begin",
            "declare",
            "create or replace procedure",
            "create or replace function",
            "create or replace package",
        ]
    )
