from __future__ import annotations

import re

from ..db import SCHEMA_OBJECT_TYPES
from .constants import *
from .buffer import is_word_char
from .browser import BrowserEntry

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
TABLE_REFERENCE_RE = re.compile(
    r"\b(?:from|join|update|into)\s+([A-Za-z][A-Za-z0-9_$#]*)"
    r"(?:\s+(?:as\s+)?([A-Za-z][A-Za-z0-9_$#]*))?",
    re.IGNORECASE,
)
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
    for match in TABLE_REFERENCE_RE.finditer(statement):
        table_name = match.group(1).upper()
        alias = (match.group(2) or "").upper()
        references.setdefault(table_name, table_name)
        if alias and alias not in TABLE_ALIAS_STOP_WORDS:
            references[alias] = table_name
    return references


def selected_browser_table_name(focus: str, entry: BrowserEntry | None) -> str:
    if focus != FOCUS_BROWSER or entry is None:
        return ""
    if entry.kind != "object" or entry.object_type not in {"TABLE", "VIEW"}:
        return ""
    return entry.object_name.upper()


def generated_sql_table_from_statement(statement: str) -> str:
    references = statement_table_references(statement)
    tables = ordered_reference_tables(references)
    return tables[0] if tables else ""


def sql_column_lines(columns: list[str], assignment: bool = False) -> list[str]:
    lines: list[str] = []
    for idx, column in enumerate(columns):
        column_name = str(column).upper()
        suffix = "," if idx < len(columns) - 1 else ""
        if assignment:
            lines.append(f"  {column_name} = :{sql_bind_name(column_name)}{suffix}")
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
    return "\n".join(["select", *sql_column_lines(columns), f"from {table_name.upper()};", ""])


def generated_insert_sql(table_name: str, columns: list[str]) -> str:
    bind_lines = [
        f"  :{sql_bind_name(str(column).upper())}{',' if idx < len(columns) - 1 else ''}"
        for idx, column in enumerate(columns)
    ]
    return "\n".join(
        [
            f"insert into {table_name.upper()} (",
            *sql_column_lines(columns),
            ") values (",
            *bind_lines,
            ");",
            "",
        ]
    )


def generated_update_sql(table_name: str, columns: list[str]) -> str:
    return "\n".join(
        [
            f"update {table_name.upper()}",
            "set",
            *sql_column_lines(columns, assignment=True),
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
    return any(token in lowered for token in ["begin", "declare", "create or replace procedure", "create or replace function", "create or replace package"])
