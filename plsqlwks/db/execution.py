from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re
from typing import Any, Mapping
from uuid import uuid4

import oracledb

from .. import exporting
from ..sqlsplit import split_script, strip_leading_sql_comments
from .models import (
    DBMS_OUTPUT_BUFFER_SIZE,
    DBMS_OUTPUT_FETCH_LINES,
    DBMS_OUTPUT_LINE_SIZE,
    NULL_DISPLAY_TOKEN,
    OracleCompilationError,
    OracleExecutionError,
    PlsqlCompileDiagnostic,
    PlsqlObject,
    QueryResult,
    QueryResultContinuation,
    QueryResultPage,
    ResultColumnMetadata,
    TruncatedLobValue,
)


ORACLE_IDENTIFIER_RE = r'(?:"(?:""|[^"])*"|[A-Za-z][A-Za-z0-9_$#]*)'
ORACLE_QUALIFIED_IDENTIFIER_RE = rf"{ORACLE_IDENTIFIER_RE}(?:\s*\.\s*{ORACLE_IDENTIFIER_RE})?"
ORACLE_IDENTIFIER_TOKEN_RE = re.compile(ORACLE_IDENTIFIER_RE)
CREATE_PLSQL_OBJECT_RE = re.compile(
    rf"""
    ^\s*create\s+
    (?:or\s+replace\s+)?
    (?:(?:editionable|noneditionable)\s+)?
    (?P<object_type>package\s+body|type\s+body|package|procedure|function|trigger|type)
    \s+(?P<object_name>{ORACLE_QUALIFIED_IDENTIFIER_RE})(?=\s|\(|$)
    """,
    re.IGNORECASE | re.VERBOSE,
)
LOB_DISPLAY_LIMIT = 64 * 1024


@dataclass
class _QueryResultContinuationState:
    cursor: Any
    lookahead_row: Any
    elapsed_seconds: float


class ExecutionMixin:
    def execute_statement(
        self,
        statement: str,
        title: str = "Statement",
        bind_values: Mapping[str, object] | None = None,
    ) -> QueryResult:
        self.ensure_statement_allowed(statement)
        conn = self.ensure_connected()
        cursor = conn.cursor()
        cursor.arraysize = self.config.arraysize
        cursor.outputtypehandler = decimal_output_type_handler
        started = datetime.now()
        keep_cursor_open = False
        try:
            def read_error_output() -> tuple[list[str], str]:
                try:
                    return self.read_dbms_output(), ""
                except Exception as output_exc:
                    return [], str(output_exc)

            try:
                execute_user_statement(cursor, statement, bind_values)
            except Exception as exc:
                self.synchronize_pending_transaction(conn)
                output, output_error = read_error_output()
                raise OracleExecutionError(exc, title, output, output_error, statement) from exc
            self.record_statement_transaction_state(statement, cursor.rowcount)
            self.synchronize_pending_transaction(conn)
            try:
                compilation_error = plsql_compilation_error(conn, statement)
            except Exception as exc:
                output, output_error = read_error_output()
                raise OracleExecutionError(exc, title, output, output_error, statement) from exc
            if compilation_error is not None:
                if self.autocommit:
                    try:
                        conn.commit()
                    finally:
                        self.synchronize_pending_transaction(conn)
                output, output_error = read_error_output()
                raise OracleExecutionError(
                    compilation_error,
                    title,
                    output,
                    output_error,
                    statement,
                ) from compilation_error
            elapsed = (datetime.now() - started).total_seconds()
            if cursor.description:
                columns = [description_value(col, "name", 0, "") for col in cursor.description]
                column_metadata = [column_metadata_from_description(col) for col in cursor.description]
                fetched = cursor.fetchmany(self.config.max_rows + 1)
                result_rows = fetched[: self.config.max_rows]
                rows, original_rows = materialize_result_rows(result_rows)
                try:
                    editable_context, edit_message = self.editable_context_for_result(
                        statement,
                        columns,
                        column_metadata,
                    )
                except Exception as exc:
                    editable_context = None
                    edit_message = f"Editability check failed: {exc}"
                more = ""
                continuation = None
                if len(fetched) > self.config.max_rows:
                    continuation = self._register_result_continuation(
                        cursor,
                        fetched[self.config.max_rows],
                        elapsed,
                    )
                    keep_cursor_open = True
                    more = f" (limited to {len(rows)} rows)"
                return QueryResult(
                    title=title,
                    columns=columns,
                    rows=rows,
                    message=f"{len(rows)} row(s){more} in {elapsed:.2f}s",
                    editable_context=editable_context,
                    edit_message=edit_message,
                    continuation=continuation,
                    original_rows=original_rows,
                )
            if self.autocommit:
                try:
                    conn.commit()
                finally:
                    self.synchronize_pending_transaction(conn)
            output, output_error = read_error_output()
            columns = ["DBMS_OUTPUT"] if output else []
            rows = [[line] for line in output]
            output_msg = f"; {len(output)} dbms_output line(s)" if output else ""
            output_warning = f"; warning: DBMS_OUTPUT read failed: {output_error}" if output_error else ""
            tx_msg = "; pending commit" if not self.autocommit and self.has_uncommitted_changes else ""
            return QueryResult(
                title=title,
                columns=columns,
                rows=rows,
                message=(
                    f"{cursor.rowcount if cursor.rowcount >= 0 else 0} row(s) affected "
                    f"in {elapsed:.2f}s{output_msg}{output_warning}{tx_msg}"
                ),
            )
        finally:
            if not keep_cursor_open:
                cursor.close()

    def fetch_more_rows(
        self,
        continuation: QueryResultContinuation,
        loaded_rows: int,
    ) -> QueryResultPage:
        state = self._result_continuations.get(continuation.token)
        if state is None:
            raise RuntimeError("Query result is stale or no longer available")
        started = datetime.now()
        try:
            fetched = [state.lookahead_row, *state.cursor.fetchmany(self.config.max_rows)]
            elapsed = state.elapsed_seconds + (datetime.now() - started).total_seconds()
            result_rows = fetched[: self.config.max_rows]
            rows, original_rows = materialize_result_rows(result_rows)
            total_loaded_rows = loaded_rows + len(rows)
            more = ""
            next_continuation: QueryResultContinuation | None = None
            if len(fetched) > self.config.max_rows:
                state.lookahead_row = fetched[self.config.max_rows]
                state.elapsed_seconds = elapsed
                next_continuation = continuation
                more = f" (limited to {total_loaded_rows} rows)"
            else:
                self.close_result_continuation(continuation)
            return QueryResultPage(
                rows,
                f"{total_loaded_rows} row(s){more} in {elapsed:.2f}s",
                original_rows,
                next_continuation,
            )
        except BaseException:
            try:
                self.close_result_continuation(continuation)
            except Exception:
                pass
            raise

    def close_result_continuation(self, continuation: QueryResultContinuation) -> None:
        state = self._result_continuations.pop(continuation.token, None)
        if state is not None:
            state.cursor.close()

    def close_all_result_continuations(self) -> None:
        states = list(self._result_continuations.values())
        self._result_continuations.clear()
        first_error: Exception | None = None
        for state in states:
            try:
                state.cursor.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _register_result_continuation(
        self,
        cursor: Any,
        lookahead_row: Any,
        elapsed_seconds: float,
    ) -> QueryResultContinuation:
        continuation = QueryResultContinuation(uuid4().hex)
        self._result_continuations[continuation.token] = _QueryResultContinuationState(
            cursor,
            lookahead_row,
            elapsed_seconds,
        )
        return continuation

    def execute_script(self, script: str) -> list[QueryResult]:
        results: list[QueryResult] = []
        statements = split_script(script)
        for idx, statement in enumerate(statements, start=1):
            title = f"Statement {idx} lines {statement.start_line}-{statement.end_line}"
            result = self.execute_statement(statement.text, title=title)
            self._append_script_result(results, result)
        if not statements:
            results.append(QueryResult("Script", [], [], "No statements to execute."))
        return results

    def _append_script_result(
        self,
        results: list[QueryResult],
        result: QueryResult,
    ) -> None:
        if result.columns and not _is_dbms_output_result(result):
            for previous in reversed(results):
                if not previous.columns or _is_dbms_output_result(previous):
                    continue
                if previous.continuation is not None:
                    try:
                        self.close_result_continuation(previous.continuation)
                    except Exception:
                        pass
                    previous.continuation = None
                break
        results.append(result)

    def export_result(self, result: QueryResult, path: Path) -> None:
        exporting.write_csv(path, result.columns, result.rows)

    def enable_dbms_output(self) -> None:
        if self.connection is None:
            return
        cursor = self.connection.cursor()
        try:
            cursor.callproc("dbms_output.enable", [DBMS_OUTPUT_BUFFER_SIZE])
        finally:
            cursor.close()

    def read_dbms_output(self) -> list[str]:
        conn = self.ensure_connected()
        cursor = conn.cursor()
        lines_var = cursor.arrayvar(str, DBMS_OUTPUT_FETCH_LINES, DBMS_OUTPUT_LINE_SIZE)
        count_var = cursor.var(int)
        output: list[str] = []
        try:
            while True:
                count_var.setvalue(0, DBMS_OUTPUT_FETCH_LINES)
                cursor.callproc("dbms_output.get_lines", [lines_var, count_var])
                count = count_var.getvalue()
                if not count:
                    break
                output.extend(lines_var.getvalue()[:count])
                if count < DBMS_OUTPUT_FETCH_LINES:
                    break
        finally:
            cursor.close()
        return output


def format_value(value: Any, *, lob_limit: int = LOB_DISPLAY_LIMIT) -> str:
    display, _ = materialize_result_value(value, lob_limit=lob_limit)
    return display


def _is_dbms_output_result(result: QueryResult) -> bool:
    return len(result.columns) == 1 and result.columns[0].upper() == "DBMS_OUTPUT"


def materialize_result_value(
    value: Any,
    *,
    lob_limit: int = LOB_DISPLAY_LIMIT,
) -> tuple[str, Any]:
    if value is None:
        return NULL_DISPLAY_TOKEN, None
    if isinstance(value, oracledb.Cursor):
        value.close()
        return "<REF CURSOR>", "<REF CURSOR>"
    if isinstance(value, oracledb.LOB):
        if lob_limit <= 0:
            raise ValueError("LOB display limit must be positive")
        size = int(value.size())
        type_name = lob_type_name(value)
        if size > lob_limit:
            content = value.read(1, lob_limit)
            unit = "bytes" if isinstance(content, bytes) else "characters"
            display = _format_non_lob_value(content)
            display += f"… <{type_name} truncated: showing first {lob_limit} of {size} {unit}>"
            return display, TruncatedLobValue(type_name, size)
        content = value.read()
        return _format_non_lob_value(content), content
    materialized = _materialize_plain_result_value(value, lob_limit=lob_limit)
    return _format_non_lob_value(materialized), materialized


def _materialize_plain_result_value(value: Any, *, lob_limit: int) -> Any:
    """Return result data that contains no live python-oracledb handles."""
    if isinstance(value, oracledb.Cursor):
        value.close()
        return "<REF CURSOR>"
    if isinstance(value, oracledb.LOB):
        return materialize_result_value(value, lob_limit=lob_limit)[1]
    if isinstance(value, list):
        return [_materialize_plain_result_value(item, lob_limit=lob_limit) for item in value]
    if isinstance(value, tuple):
        return tuple(_materialize_plain_result_value(item, lob_limit=lob_limit) for item in value)
    if isinstance(value, dict):
        return {
            _materialize_plain_result_value(key, lob_limit=lob_limit): _materialize_plain_result_value(
                item, lob_limit=lob_limit
            )
            for key, item in value.items()
        }
    if isinstance(value, set):
        return {_materialize_plain_result_value(item, lob_limit=lob_limit) for item in value}
    if isinstance(value, frozenset):
        return frozenset(
            _materialize_plain_result_value(item, lob_limit=lob_limit) for item in value
        )
    if type(value).__module__.startswith("oracledb"):
        return str(value)
    return value


def _format_non_lob_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="auto")
    return str(value)


def read_lob_value(value: Any) -> Any:
    if isinstance(value, oracledb.LOB):
        return value.read()
    return value


def lob_type_name(value: oracledb.LOB) -> str:
    type_code = getattr(value, "type", None)
    if type_code is oracledb.DB_TYPE_BLOB:
        return "BLOB"
    if type_code is oracledb.DB_TYPE_BFILE:
        return "BFILE"
    if type_code is oracledb.DB_TYPE_NCLOB:
        return "NCLOB"
    if type_code is oracledb.DB_TYPE_CLOB:
        return "CLOB"
    return "LOB"


def decimal_output_type_handler(cursor: Any, metadata: Any) -> Any:
    if getattr(metadata, "type_code", None) is oracledb.DB_TYPE_NUMBER:
        return cursor.var(Decimal, arraysize=cursor.arraysize)
    return None


def description_value(
    description: Any,
    attribute: str,
    index: int,
    default: Any = None,
) -> Any:
    value = getattr(description, attribute, None)
    if value is not None:
        return value
    try:
        return description[index]
    except (IndexError, KeyError, TypeError):
        return default


def column_metadata_from_description(description: Any) -> ResultColumnMetadata:
    return ResultColumnMetadata(
        type_code=description_value(description, "type_code", 1),
        precision=description_value(description, "precision", 4),
        scale=description_value(description, "scale", 5),
        null_ok=description_value(description, "null_ok", 6),
    )


def execute_user_statement(
    cursor: Any,
    statement: str,
    bind_values: Mapping[str, object] | None = None,
) -> None:
    if bind_values:
        cursor.execute(statement, dict(bind_values))
        return
    cursor.execute(statement)


def plsql_compilation_error(conn: Any, statement: str) -> OracleCompilationError | None:
    plsql_object = plsql_object_from_create_statement(statement)
    if plsql_object is None:
        return None
    cursor = conn.cursor()
    try:
        diagnostics = fetch_plsql_compile_diagnostics(cursor, plsql_object)
    finally:
        cursor.close()
    if not diagnostics:
        return None
    return OracleCompilationError(plsql_object, diagnostics)


def fetch_plsql_compile_diagnostics(cursor: Any, plsql_object: PlsqlObject) -> list[PlsqlCompileDiagnostic]:
    if plsql_object.owner:
        cursor.execute(
            """
            select line, position, text
            from all_errors
            where owner = :owner
              and name = :object_name
              and type = :object_type
              and attribute = 'ERROR'
            order by sequence
            """,
            {
                "owner": plsql_object.owner,
                "object_name": plsql_object.name,
                "object_type": plsql_object.object_type,
            },
        )
    else:
        cursor.execute(
            """
            select line, position, text
            from user_errors
            where name = :object_name
              and type = :object_type
              and attribute = 'ERROR'
            order by sequence
            """,
            {
                "object_name": plsql_object.name,
                "object_type": plsql_object.object_type,
            },
        )
    return [
        PlsqlCompileDiagnostic(
            line=int(row[0] or 1),
            position=int(row[1] or 1),
            text=str(row[2]).strip(),
        )
        for row in cursor.fetchall()
    ]


def plsql_object_from_create_statement(statement: str) -> PlsqlObject | None:
    sql = strip_leading_sql_comments(statement)
    match = CREATE_PLSQL_OBJECT_RE.match(sql)
    if not match:
        return None
    object_type = " ".join(match.group("object_type").upper().split())
    identifiers = oracle_identifier_parts(match.group("object_name"))
    if not identifiers:
        return None
    owner = normalize_oracle_identifier(identifiers[-2]) if len(identifiers) > 1 else None
    name = normalize_oracle_identifier(identifiers[-1])
    return PlsqlObject(object_type=object_type, owner=owner, name=name)


def oracle_identifier_parts(qualified_name: str) -> list[str]:
    return [match.group(0) for match in ORACLE_IDENTIFIER_TOKEN_RE.finditer(qualified_name)]


def normalize_oracle_identifier(identifier: str) -> str:
    text = identifier.strip()
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1].replace('""', '"')
    return text.upper()


def format_result_rows(rows: list[Any]) -> list[list[str]]:
    formatted, _ = materialize_result_rows(rows)
    return formatted


def materialize_result_rows(rows: list[Any]) -> tuple[list[list[str]], list[list[Any]]]:
    formatted_rows: list[list[str]] = []
    original_rows: list[list[Any]] = []
    for row in rows:
        formatted_row: list[str] = []
        original_row: list[Any] = []
        for value in row:
            formatted, original = materialize_result_value(value)
            formatted_row.append(formatted)
            original_row.append(original)
        formatted_rows.append(formatted_row)
        original_rows.append(original_row)
    return formatted_rows, original_rows


def csv_cell(value: str) -> str:
    return exporting.csv_cell(value)
