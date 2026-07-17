from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class EditableResultContext:
    table_name: str
    rowid_column: int
    editable_columns: dict[int, str]
    column_metadata: dict[int, ResultColumnMetadata] = field(default_factory=dict)


@dataclass(frozen=True)
class ResultColumnMetadata:
    type_code: Any = None
    precision: int | None = None
    scale: int | None = None
    null_ok: bool | None = None


@dataclass(frozen=True)
class TruncatedLobValue:
    type_name: str
    size: int


@dataclass(frozen=True)
class CellUpdateResult:
    value: Any
    display: str


@dataclass(frozen=True)
class RowInsertResult:
    values: list[Any]
    display_values: list[str]


@dataclass(frozen=True)
class QueryResultContinuation:
    token: str = field(repr=False)


@dataclass(frozen=True)
class QueryResultPage:
    rows: list[list[str]]
    message: str
    original_rows: list[list[Any]] = field(default_factory=list, repr=False, compare=False)
    continuation: QueryResultContinuation | None = field(default=None, repr=False, compare=False)
    dbms_output: list[str] = field(default_factory=list)
    dbms_output_error: str = ""
    warnings: list[str] = field(default_factory=list)
    has_more_rows: bool = False

    def __post_init__(self) -> None:
        if self.continuation is not None and not self.has_more_rows:
            object.__setattr__(self, "has_more_rows", True)


@dataclass
class QueryResult:
    title: str
    columns: list[str]
    rows: list[list[str]]
    message: str
    editable_context: EditableResultContext | None = None
    edit_message: str = ""
    continuation: QueryResultContinuation | None = field(default=None, repr=False, compare=False)
    original_rows: list[list[Any]] = field(default_factory=list, repr=False, compare=False)
    dbms_output: list[str] = field(default_factory=list)
    dbms_output_error: str = ""
    warnings: list[str] = field(default_factory=list)
    diagnostics: list[PlsqlCompileDiagnostic] = field(default_factory=list)
    statement_start_line: int | None = field(default=None, repr=False, compare=False)
    statement_start_col: int | None = field(default=None, repr=False, compare=False)
    has_more_rows: bool = False

    def __post_init__(self) -> None:
        if self.continuation is not None:
            self.has_more_rows = True


@dataclass(frozen=True)
class ExplainPlanStep:
    id: int
    parent_id: int | None
    depth: int
    operation: str
    options: str
    object_owner: str
    object_name: str
    object_type: str
    cardinality: str
    bytes: str
    cost: str
    time: str


@dataclass(frozen=True)
class ExplainPlanResult:
    title: str
    steps: list[ExplainPlanStep]
    message: str
    raw_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TransactionReport:
    timestamp: datetime
    rows_changed: int
    has_unknown_changes: bool = False


class OracleExecutionError(RuntimeError):
    def __init__(
        self,
        original: Exception,
        title: str,
        dbms_output: list[str] | None = None,
        dbms_output_error: str = "",
        statement: str = "",
        warnings: list[str] | None = None,
    ):
        super().__init__(str(original))
        self.original = original
        self.title = title
        self.dbms_output = list(dbms_output or [])
        self.dbms_output_error = dbms_output_error
        self.statement = statement
        self.warnings = list(warnings or [])


class EditOperationRollbackError(RuntimeError):
    def __init__(
        self,
        original: Exception,
        savepoint_rollback_error: Exception,
        *,
        full_rollback_attempted: bool,
        full_rollback_succeeded: bool = False,
        full_rollback_error: Exception | None = None,
    ):
        message = f"{original}; failed to roll back edit savepoint: {savepoint_rollback_error}"
        if full_rollback_succeeded:
            message += "; full rollback succeeded"
        elif full_rollback_error is not None:
            message += f"; full rollback also failed: {full_rollback_error}"
        elif not full_rollback_attempted:
            message += "; full rollback was not attempted to preserve prior work"
        super().__init__(message)
        self.original = original
        self.savepoint_rollback_error = savepoint_rollback_error
        self.full_rollback_attempted = full_rollback_attempted
        self.full_rollback_succeeded = full_rollback_succeeded
        self.full_rollback_error = full_rollback_error
        self.transaction_may_have_changes = not full_rollback_succeeded


class ExplainPlanCleanupError(RuntimeError):
    def __init__(
        self,
        original: Exception | None,
        savepoint_rollback_error: Exception | None,
        *,
        full_rollback_attempted: bool,
        full_rollback_succeeded: bool = False,
        full_rollback_error: Exception | None = None,
        autocommit_restore_error: Exception | None = None,
        cursor_close_error: Exception | None = None,
    ):
        self.original = original
        self.savepoint_rollback_error = savepoint_rollback_error
        self.full_rollback_attempted = full_rollback_attempted
        self.full_rollback_succeeded = full_rollback_succeeded
        self.full_rollback_error = full_rollback_error
        self.autocommit_restore_error = autocommit_restore_error
        self.cursor_close_error = cursor_close_error
        self.transaction_may_have_changes = (
            not full_rollback_succeeded
            or autocommit_restore_error is not None
            or cursor_close_error is not None
        )
        super().__init__(self._message())

    def _message(self) -> str:
        message = f"{self.original}; " if self.original is not None else ""
        message += "explain plan cleanup failed"
        if self.savepoint_rollback_error is not None:
            message += f": failed to roll back savepoint: {self.savepoint_rollback_error}"
            if self.full_rollback_succeeded:
                message += "; full rollback succeeded"
            elif self.full_rollback_error is not None:
                message += f"; full rollback also failed: {self.full_rollback_error}"
            elif not self.full_rollback_attempted:
                message += "; full rollback was not attempted to preserve prior work"
        elif self.full_rollback_error is not None:
            message += f": failed to end clean transaction with full rollback: {self.full_rollback_error}"
        if self.autocommit_restore_error is not None:
            message += f"; failed to restore autocommit: {self.autocommit_restore_error}"
        if self.cursor_close_error is not None:
            message += f"; failed to close explain cursor: {self.cursor_close_error}"
        return message

    def add_finalizer_failures(
        self,
        *,
        autocommit_restore_error: Exception | None = None,
        cursor_close_error: Exception | None = None,
    ) -> ExplainPlanCleanupError:
        if autocommit_restore_error is not None:
            self.autocommit_restore_error = autocommit_restore_error
        if cursor_close_error is not None:
            self.cursor_close_error = cursor_close_error
        self.transaction_may_have_changes = True
        self.args = (self._message(),)
        return self

    @classmethod
    def from_finalizer_failure(
        cls,
        primary: Exception | None,
        *,
        autocommit_restore_error: Exception | None = None,
        cursor_close_error: Exception | None = None,
    ) -> ExplainPlanCleanupError:
        if isinstance(primary, cls):
            return primary.add_finalizer_failures(
                autocommit_restore_error=autocommit_restore_error,
                cursor_close_error=cursor_close_error,
            )
        return cls(
            primary,
            None,
            full_rollback_attempted=False,
            autocommit_restore_error=autocommit_restore_error,
            cursor_close_error=cursor_close_error,
        )

    @property
    def primary_cause(self) -> Exception | None:
        for error in (
            self.original,
            self.savepoint_rollback_error,
            self.full_rollback_error,
            self.autocommit_restore_error,
            self.cursor_close_error,
        ):
            if error is not None:
                return error
        return None


@dataclass(frozen=True)
class PlsqlObject:
    object_type: str
    name: str
    owner: str | None = None


@dataclass(frozen=True)
class PlsqlCompileDiagnostic:
    line: int
    position: int
    text: str
    severity: str = "ERROR"


class OracleCompilationError(RuntimeError):
    def __init__(self, plsql_object: PlsqlObject, diagnostics: list[PlsqlCompileDiagnostic]):
        super().__init__(format_compilation_error(plsql_object, diagnostics))
        self.plsql_object = plsql_object
        self.diagnostics = diagnostics


class ReadOnlyModeError(RuntimeError):
    pass


class ConcurrentEditError(RuntimeError):
    pass


@dataclass(frozen=True)
class SelectItem:
    kind: str
    name: str = ""


@dataclass(frozen=True)
class SimpleSelect:
    table_name: str
    alias: str | None
    items: list[SelectItem]

DBMS_OUTPUT_BUFFER_SIZE = 1_000_000
DBMS_OUTPUT_FETCH_LINES = 100
DBMS_OUTPUT_LINE_SIZE = 32767
NULL_DISPLAY_TOKEN = "<NULL>"


def format_compilation_error(plsql_object: PlsqlObject, diagnostics: list[PlsqlCompileDiagnostic]) -> str:
    qualified_name = plsql_object.name if plsql_object.owner is None else f"{plsql_object.owner}.{plsql_object.name}"
    lines = [f"PL/SQL compilation failed for {plsql_object.object_type} {qualified_name}"]
    lines.extend(format_compile_diagnostic(diagnostic) for diagnostic in diagnostics)
    return "\n".join(lines)


def format_compile_diagnostic(diagnostic: PlsqlCompileDiagnostic) -> str:
    return (
        f"line {diagnostic.line}, column {diagnostic.position} "
        f"[{diagnostic.severity}]: {diagnostic.text}"
    )
