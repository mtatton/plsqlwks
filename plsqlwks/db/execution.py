from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re
from time import monotonic
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
    format_compile_diagnostic,
)
from .transactions import transaction_statement_kind


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


class _DbmsOutputDrainError(RuntimeError):
    def __init__(
        self,
        output: list[str],
        read_error: Exception | None = None,
        cleanup_error: Exception | None = None,
    ) -> None:
        self.output = list(output)
        self.read_error = read_error
        self.cleanup_error = cleanup_error
        parts: list[str] = []
        if read_error is not None:
            parts.append(str(read_error))
        if cleanup_error is not None:
            parts.append(f"cursor cleanup failed: {cleanup_error}")
        super().__init__("; ".join(parts) or "DBMS_OUTPUT drain failed")


class _PlsqlDiagnosticReadError(RuntimeError):
    def __init__(self, original: Exception, warnings: list[str]) -> None:
        super().__init__(str(original))
        self.original = original
        self.warnings = list(warnings)


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
        started = monotonic()
        keep_cursor_open = False
        completed_result: QueryResult | None = None
        primary_error: BaseException | None = None
        try:
            def read_output() -> tuple[list[str], str, list[str]]:
                return self._read_dbms_output_safely()

            try:
                execute_user_statement(cursor, statement, bind_values)
            except Exception as exc:
                self.synchronize_pending_transaction(conn)
                output, output_error, output_warnings = read_output()
                raise OracleExecutionError(
                    exc,
                    title,
                    output,
                    output_error,
                    statement,
                    output_warnings,
                ) from exc
            self.record_statement_transaction_state(statement, cursor.rowcount)
            self.synchronize_pending_transaction(conn)
            try:
                (
                    compilation_error,
                    compilation_diagnostics,
                    compilation_cleanup_warnings,
                ) = _plsql_compilation_result(conn, statement)
            except _PlsqlDiagnosticReadError as exc:
                output, output_error, output_warnings = read_output()
                raise OracleExecutionError(
                    exc.original,
                    title,
                    output,
                    output_error,
                    statement,
                    [*exc.warnings, *output_warnings],
                ) from exc.original
            except Exception as exc:
                output, output_error, output_warnings = read_output()
                raise OracleExecutionError(
                    exc,
                    title,
                    output,
                    output_error,
                    statement,
                    output_warnings,
                ) from exc
            if compilation_error is not None:
                commit_error: Exception | None = None
                if self.autocommit:
                    try:
                        self._commit_autocommit(conn, statement)
                    except Exception as exc:
                        commit_error = exc
                output, output_error, output_warnings = read_output()
                commit_warnings = (
                    [_autocommit_failure_warning(commit_error)]
                    if commit_error is not None
                    else []
                )
                raise OracleExecutionError(
                    compilation_error,
                    title,
                    output,
                    output_error,
                    statement,
                    [
                        *compilation_cleanup_warnings,
                        *commit_warnings,
                        *output_warnings,
                    ],
                ) from compilation_error
            compilation_warnings = [
                f"PL/SQL {format_compile_diagnostic(diagnostic)}"
                for diagnostic in compilation_diagnostics
                if diagnostic.severity == "WARNING"
            ]
            compilation_warnings.extend(compilation_cleanup_warnings)
            if cursor.description:
                columns = [description_value(col, "name", 0, "") for col in cursor.description]
                column_metadata = [column_metadata_from_description(col) for col in cursor.description]
                try:
                    fetched = cursor.fetchmany(self.config.max_rows + 1)
                except Exception as exc:
                    output, output_error, output_warnings = read_output()
                    raise OracleExecutionError(
                        exc,
                        title,
                        output,
                        output_error,
                        statement,
                        output_warnings,
                    ) from exc
                output, output_error, output_warnings = read_output()
                result_rows = fetched[: self.config.max_rows]
                warnings = [*compilation_warnings, *output_warnings]
                try:
                    rows, original_rows = materialize_result_rows(result_rows)
                except Exception as exc:
                    raise OracleExecutionError(
                        exc,
                        title,
                        output,
                        output_error,
                        statement,
                        warnings,
                    ) from exc
                try:
                    editable_context, edit_message = self.editable_context_for_result(
                        statement,
                        columns,
                        column_metadata,
                    )
                except Exception as exc:
                    editable_context = None
                    edit_message = f"Editability check failed: {exc}"
                elapsed = monotonic() - started
                more = ""
                continuation = None
                if len(fetched) > self.config.max_rows:
                    continuation = self._register_result_continuation(
                        cursor,
                        fetched[self.config.max_rows],
                        elapsed,
                    )
                    keep_cursor_open = True
                    more = (
                        f" (limited to {len(rows)} rows; more rows available)"
                    )
                completed_result = QueryResult(
                    title=title,
                    columns=columns,
                    rows=rows,
                    message=(
                        f"{len(rows)} row(s){more} in {elapsed:.2f}s"
                        f"{_dbms_output_message_suffix(output, warnings)}"
                    ),
                    editable_context=editable_context,
                    edit_message=edit_message,
                    continuation=continuation,
                    original_rows=original_rows,
                    dbms_output=output,
                    dbms_output_error=output_error,
                    warnings=warnings,
                    diagnostics=compilation_diagnostics,
                    has_more_rows=continuation is not None,
                )
                return completed_result
            if self.autocommit:
                try:
                    self._commit_autocommit(conn, statement)
                except Exception as exc:
                    output, output_error, output_warnings = read_output()
                    raise OracleExecutionError(
                        exc,
                        title,
                        output,
                        output_error,
                        statement,
                        [
                            _autocommit_failure_warning(exc),
                            *output_warnings,
                        ],
                    ) from exc
            output, output_error, output_warnings = read_output()
            elapsed = monotonic() - started
            warnings = [*compilation_warnings, *output_warnings]
            tx_msg = "; pending commit" if not self.autocommit and self.has_uncommitted_changes else ""
            completed_result = QueryResult(
                title=title,
                columns=[],
                rows=[],
                message=(
                    f"{cursor.rowcount if cursor.rowcount >= 0 else 0} row(s) affected "
                    f"in {elapsed:.2f}s"
                    f"{_dbms_output_message_suffix(output, warnings)}{tx_msg}"
                ),
                dbms_output=output,
                dbms_output_error=output_error,
                warnings=warnings,
                diagnostics=compilation_diagnostics,
            )
            return completed_result
        except BaseException as exc:
            primary_error = exc
            self._mark_transaction_unknown_if_unhealthy(statement, conn)
            raise
        finally:
            if not keep_cursor_open:
                try:
                    cursor.close()
                except BaseException as exc:
                    if not isinstance(exc, Exception):
                        self._mark_transaction_unknown_if_unhealthy(statement, conn)
                        raise
                    if completed_result is not None:
                        _append_result_warning(
                            completed_result,
                            f"Statement cursor cleanup failed: {exc}",
                        )
                    elif isinstance(primary_error, OracleExecutionError):
                        primary_error.warnings.append(
                            f"Statement cursor cleanup failed: {exc}"
                        )
                    elif primary_error is not None:
                        _add_exception_note(
                            primary_error,
                            f"Statement cursor cleanup also failed: {exc}",
                        )
                    else:
                        raise

    def fetch_more_rows(
        self,
        continuation: QueryResultContinuation,
        loaded_rows: int,
    ) -> QueryResultPage:
        state = self._result_continuations.get(continuation.token)
        if state is None:
            raise RuntimeError("Query result is stale or no longer available")
        started = monotonic()
        try:
            try:
                fetched = [
                    state.lookahead_row,
                    *state.cursor.fetchmany(self.config.max_rows),
                ]
            except Exception as exc:
                output, output_error, output_warnings = (
                    self._read_dbms_output_safely()
                )
                raise OracleExecutionError(
                    exc,
                    "Fetch rows",
                    output,
                    output_error,
                    warnings=output_warnings,
                ) from exc
            output, output_error, warnings = self._read_dbms_output_safely()
            result_rows = fetched[: self.config.max_rows]
            try:
                rows, original_rows = materialize_result_rows(result_rows)
            except Exception as exc:
                raise OracleExecutionError(
                    exc,
                    "Fetch rows",
                    output,
                    output_error,
                    warnings=warnings,
                ) from exc
            total_loaded_rows = loaded_rows + len(rows)
            more = ""
            next_continuation: QueryResultContinuation | None = None
            if len(fetched) > self.config.max_rows:
                state.lookahead_row = fetched[self.config.max_rows]
                next_continuation = continuation
                more = (
                    f" (limited to {total_loaded_rows} rows; more rows available)"
                )
            else:
                try:
                    self.close_result_continuation(continuation)
                except Exception as exc:
                    warnings.append(f"Statement cursor cleanup failed: {exc}")
            elapsed = state.elapsed_seconds + (monotonic() - started)
            if next_continuation is not None:
                state.elapsed_seconds = elapsed
            page = QueryResultPage(
                rows,
                (
                    f"{total_loaded_rows} row(s){more} in {elapsed:.2f}s"
                    f"{_dbms_output_message_suffix(output, warnings)}"
                ),
                original_rows,
                next_continuation,
                output,
                output_error,
                warnings,
                next_continuation is not None,
            )
            return page
        except BaseException as exc:
            try:
                self.close_result_continuation(continuation)
            except Exception as close_exc:
                if isinstance(exc, OracleExecutionError):
                    exc.warnings.append(
                        f"Statement cursor cleanup failed: {close_exc}"
                    )
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
            result.statement_start_line = statement.start_line
            result.statement_start_col = statement.start_col
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

    def _read_dbms_output_safely(self) -> tuple[list[str], str, list[str]]:
        try:
            return self.read_dbms_output(), "", []
        except _DbmsOutputDrainError as exc:
            warnings: list[str] = []
            output_error = ""
            if exc.read_error is not None:
                output_error = str(exc.read_error)
                warnings.append(f"DBMS_OUTPUT read failed: {output_error}")
            if exc.cleanup_error is not None:
                warnings.append(
                    f"DBMS_OUTPUT cursor cleanup failed: {exc.cleanup_error}"
                )
            return exc.output, output_error, warnings
        except Exception as exc:
            error = str(exc)
            return [], error, [f"DBMS_OUTPUT read failed: {error}"]

    def _mark_transaction_unknown_if_unhealthy(
        self,
        statement: str,
        connection: Any,
    ) -> None:
        if not transaction_statement_kind(statement):
            return
        is_healthy = getattr(connection, "is_healthy", None)
        if not callable(is_healthy):
            return
        try:
            healthy = bool(is_healthy())
        except BaseException:
            healthy = False
        if not healthy:
            self.mark_pending_transaction_unknown()

    def _commit_autocommit(self, connection: Any, statement: str) -> None:
        try:
            connection.commit()
        except BaseException as exc:
            try:
                self.synchronize_pending_transaction(connection)
            except BaseException as sync_exc:
                _add_exception_note(
                    exc,
                    f"Transaction-state synchronization also failed: {sync_exc}",
                )
            if transaction_statement_kind(statement) or self.has_uncommitted_changes:
                self.mark_pending_transaction_unknown()
            raise
        self.synchronize_pending_transaction(connection)

    def read_dbms_output(self) -> list[str]:
        conn = self.ensure_connected()
        cursor = conn.cursor()
        output: list[str] = []
        read_error: BaseException | None = None
        try:
            lines_var = cursor.arrayvar(str, DBMS_OUTPUT_FETCH_LINES, DBMS_OUTPUT_LINE_SIZE)
            count_var = cursor.var(int)
            while True:
                count_var.setvalue(0, DBMS_OUTPUT_FETCH_LINES)
                cursor.callproc("dbms_output.get_lines", [lines_var, count_var])
                count = count_var.getvalue()
                if not count:
                    break
                output.extend(lines_var.getvalue()[:count])
                if count < DBMS_OUTPUT_FETCH_LINES:
                    break
        except BaseException as exc:
            read_error = exc
        cleanup_error: Exception | None = None
        try:
            cursor.close()
        except Exception as exc:
            cleanup_error = exc
        if read_error is not None:
            if isinstance(read_error, Exception):
                raise _DbmsOutputDrainError(
                    output,
                    read_error,
                    cleanup_error,
                ) from read_error
            raise read_error
        if cleanup_error is not None:
            raise _DbmsOutputDrainError(
                output,
                cleanup_error=cleanup_error,
            ) from cleanup_error
        return output


def format_value(value: Any, *, lob_limit: int = LOB_DISPLAY_LIMIT) -> str:
    display, _ = materialize_result_value(value, lob_limit=lob_limit)
    return display


def _is_dbms_output_result(result: QueryResult) -> bool:
    return not result.columns and bool(result.dbms_output or result.dbms_output_error)


def _dbms_output_message_suffix(output: list[str], warnings: list[str]) -> str:
    suffix = f"; {len(output)} dbms_output line(s)" if output else ""
    return suffix + "".join(f"; warning: {warning}" for warning in warnings)


def _autocommit_failure_warning(exc: Exception) -> str:
    return f"Autocommit commit failed; transaction outcome is unknown: {exc}"


def _add_exception_note(exc: BaseException, note: str) -> None:
    add_note = getattr(exc, "add_note", None)
    if callable(add_note):
        add_note(note)


def _append_result_warning(result: QueryResult, warning: str) -> None:
    result.warnings.append(warning)
    result.message += f"; warning: {warning}"


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
    compilation_error, _, _ = _plsql_compilation_result(conn, statement)
    return compilation_error


def _plsql_compilation_result(
    conn: Any,
    statement: str,
) -> tuple[
    OracleCompilationError | None,
    list[PlsqlCompileDiagnostic],
    list[str],
]:
    plsql_object = plsql_object_from_create_statement(statement)
    if plsql_object is None:
        return None, [], []
    cursor = conn.cursor()
    diagnostics: list[PlsqlCompileDiagnostic] = []
    read_error: BaseException | None = None
    try:
        diagnostics = fetch_plsql_compile_diagnostics(cursor, plsql_object)
    except BaseException as exc:
        read_error = exc
    cleanup_error: Exception | None = None
    try:
        cursor.close()
    except Exception as exc:
        cleanup_error = exc
    cleanup_warnings = (
        [f"PL/SQL diagnostic cursor cleanup failed: {cleanup_error}"]
        if cleanup_error is not None
        else []
    )
    if read_error is not None:
        if isinstance(read_error, Exception) and cleanup_warnings:
            raise _PlsqlDiagnosticReadError(
                read_error,
                cleanup_warnings,
            ) from read_error
        raise read_error
    diagnostics = _offset_plsql_compile_diagnostics(statement, diagnostics)
    if not diagnostics:
        return None, [], cleanup_warnings
    if any(diagnostic.severity == "ERROR" for diagnostic in diagnostics):
        return (
            OracleCompilationError(plsql_object, diagnostics),
            diagnostics,
            cleanup_warnings,
        )
    return None, diagnostics, cleanup_warnings


def fetch_plsql_compile_diagnostics(cursor: Any, plsql_object: PlsqlObject) -> list[PlsqlCompileDiagnostic]:
    if plsql_object.owner:
        cursor.execute(
            """
            select line, position, text, attribute
            from all_errors
            where owner = :owner
              and name = :object_name
              and type = :object_type
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
            select line, position, text, attribute
            from user_errors
            where name = :object_name
              and type = :object_type
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
            severity=(
                str(row[3] or "ERROR").strip().upper()
                if len(row) > 3
                else "ERROR"
            ),
        )
        for row in cursor.fetchall()
    ]


def _offset_plsql_compile_diagnostics(
    statement: str,
    diagnostics: list[PlsqlCompileDiagnostic],
) -> list[PlsqlCompileDiagnostic]:
    sql = strip_leading_sql_comments(statement)
    if not sql:
        return diagnostics
    prefix = statement[: len(statement) - len(sql)]
    line_offset = prefix.count("\n")
    first_line_column_offset = len(prefix.rsplit("\n", 1)[-1])
    return [
        PlsqlCompileDiagnostic(
            line=diagnostic.line + line_offset,
            position=(
                diagnostic.position + first_line_column_offset
                if diagnostic.line == 1
                else diagnostic.position
            ),
            text=diagnostic.text,
            severity=diagnostic.severity,
        )
        for diagnostic in diagnostics
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
