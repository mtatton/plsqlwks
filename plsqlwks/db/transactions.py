from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import re
from typing import Any
import uuid

from ..sqlsplit import strip_leading_sql_comments
from .models import (
    EditOperationRollbackError,
    ExplainPlanCleanupError,
    ReadOnlyModeError,
    TransactionReport,
)
from .sql_analysis import tail_sql_words


DIRECT_DML_KEYWORDS = {"insert", "update", "delete", "merge"}
PLSQL_TRANSACTION_KEYWORDS = {"begin", "declare", "call"}
DDL_KEYWORDS = {
    "alter",
    "analyze",
    "associate",
    "audit",
    "comment",
    "create",
    "disassociate",
    "drop",
    "flashback",
    "grant",
    "noaudit",
    "purge",
    "rename",
    "revoke",
    "truncate",
}
NONCOMMITTING_ALTER_TARGETS = {"session", "system"}


def leading_sql_keyword(statement: str) -> str:
    remaining = strip_leading_sql_comments(statement)
    match = re.match(r"[A-Za-z_]+", remaining)
    return match.group(0).lower() if match else ""


def transaction_statement_kind(statement: str) -> str:
    keyword = leading_sql_keyword(statement)
    if keyword == "commit":
        return keyword
    if keyword == "rollback":
        words = tail_sql_words(strip_leading_sql_comments(statement))
        if len(words) >= 2 and words[1] == "TO":
            return "rollback_to"
        if len(words) >= 3 and words[1] == "WORK" and words[2] == "TO":
            return "rollback_to"
        return keyword
    if keyword in DIRECT_DML_KEYWORDS:
        return "dml"
    if keyword in PLSQL_TRANSACTION_KEYWORDS:
        return "plsql"
    if keyword == "alter":
        words = tail_sql_words(strip_leading_sql_comments(statement))
        if len(words) >= 2 and words[1].lower() in NONCOMMITTING_ALTER_TARGETS:
            return ""
        return "ddl"
    if keyword in DDL_KEYWORDS:
        return "ddl"
    return ""


def connection_transaction_in_progress(connection: Any) -> bool | None:
    try:
        state = getattr(connection, "transaction_in_progress")
    except Exception:
        return None
    return state if isinstance(state, bool) else None


@contextmanager
def edit_operation_savepoint(
    connection: Any,
    cursor: Any,
    *,
    autocommit: bool,
    had_pending_work: bool,
):
    previous_autocommit = getattr(connection, "autocommit", None)
    temporarily_disabled_autocommit = autocommit and previous_autocommit is True
    transaction_before = connection_transaction_in_progress(connection)
    safe_to_full_rollback = transaction_before is False and not had_pending_work
    savepoint_name = f"PLSQLWKS_EDIT_{uuid.uuid4().hex[:16].upper()}"
    savepoint_created = False
    if temporarily_disabled_autocommit:
        connection.autocommit = False
    try:
        cursor.execute(f"savepoint {savepoint_name}")
        savepoint_created = True
        yield
    except Exception as original:
        if savepoint_created:
            try:
                cursor.execute(f"rollback to savepoint {savepoint_name}")
            except Exception as savepoint_rollback_error:
                if not safe_to_full_rollback:
                    raise EditOperationRollbackError(
                        original,
                        savepoint_rollback_error,
                        full_rollback_attempted=False,
                    ) from original
                try:
                    connection.rollback()
                except Exception as full_rollback_error:
                    raise EditOperationRollbackError(
                        original,
                        savepoint_rollback_error,
                        full_rollback_attempted=True,
                        full_rollback_error=full_rollback_error,
                    ) from original
                raise EditOperationRollbackError(
                    original,
                    savepoint_rollback_error,
                    full_rollback_attempted=True,
                    full_rollback_succeeded=True,
                ) from original
            else:
                if safe_to_full_rollback:
                    try:
                        connection.rollback()
                    except Exception:
                        pass
        raise
    finally:
        if temporarily_disabled_autocommit:
            connection.autocommit = previous_autocommit


def rollback_explain_plan_savepoint(
    connection: Any,
    cursor: Any,
    savepoint_name: str,
    *,
    safe_to_full_rollback: bool,
    original: Exception | None,
) -> None:
    try:
        cursor.execute(f"rollback to savepoint {savepoint_name}")
    except Exception as savepoint_rollback_error:
        if not safe_to_full_rollback:
            raise ExplainPlanCleanupError(
                original,
                savepoint_rollback_error,
                full_rollback_attempted=False,
            ) from savepoint_rollback_error
        try:
            connection.rollback()
        except Exception as full_rollback_error:
            raise ExplainPlanCleanupError(
                original,
                savepoint_rollback_error,
                full_rollback_attempted=True,
                full_rollback_error=full_rollback_error,
            ) from savepoint_rollback_error
        raise ExplainPlanCleanupError(
            original,
            savepoint_rollback_error,
            full_rollback_attempted=True,
            full_rollback_succeeded=True,
        ) from savepoint_rollback_error

    if safe_to_full_rollback:
        try:
            connection.rollback()
        except Exception as full_rollback_error:
            raise ExplainPlanCleanupError(
                original,
                None,
                full_rollback_attempted=True,
                full_rollback_error=full_rollback_error,
            ) from full_rollback_error


@contextmanager
def explain_plan_savepoint(
    connection: Any,
    cursor: Any,
    *,
    had_pending_work: bool,
    transaction_before: bool | None,
):
    previous_autocommit = getattr(connection, "autocommit", None)
    temporarily_disabled_autocommit = previous_autocommit is True
    safe_to_full_rollback = transaction_before is False and not had_pending_work
    savepoint_name = f"PLSQLWKS_EXPLAIN_{uuid.uuid4().hex[:16].upper()}"
    if temporarily_disabled_autocommit:
        connection.autocommit = False
    primary_error: Exception | None = None
    try:
        try:
            cursor.execute(f"savepoint {savepoint_name}")
            try:
                yield
            except Exception as original:
                try:
                    rollback_explain_plan_savepoint(
                        connection,
                        cursor,
                        savepoint_name,
                        safe_to_full_rollback=safe_to_full_rollback,
                        original=original,
                    )
                except ExplainPlanCleanupError as cleanup_error:
                    primary_error = cleanup_error
                else:
                    primary_error = original
            else:
                try:
                    rollback_explain_plan_savepoint(
                        connection,
                        cursor,
                        savepoint_name,
                        safe_to_full_rollback=safe_to_full_rollback,
                        original=None,
                    )
                except ExplainPlanCleanupError as cleanup_error:
                    primary_error = cleanup_error
        except Exception as setup_error:
            primary_error = setup_error
    finally:
        if temporarily_disabled_autocommit:
            try:
                connection.autocommit = previous_autocommit
            except Exception as autocommit_restore_error:
                primary_error = ExplainPlanCleanupError.from_finalizer_failure(
                    primary_error,
                    autocommit_restore_error=autocommit_restore_error,
                )
    if primary_error is not None:
        if isinstance(primary_error, ExplainPlanCleanupError):
            cause = primary_error.primary_cause
            if cause is not None:
                raise primary_error from cause
        raise primary_error


def read_only_rejection_reason(statement: str) -> str:
    keyword = leading_sql_keyword(statement)
    if not keyword:
        return ""
    if keyword in {"select", "with"}:
        words = tail_sql_words(statement)
        if any(first == "FOR" and second == "UPDATE" for first, second in zip(words, words[1:])):
            return "SELECT FOR UPDATE is disabled in read-only mode"
        return ""
    if keyword == "rollback":
        return ""
    if keyword == "commit":
        return "COMMIT is disabled in read-only mode"
    if keyword in DIRECT_DML_KEYWORDS:
        return "DML statements are disabled in read-only mode"
    if keyword in DDL_KEYWORDS:
        return "DDL statements are disabled in read-only mode"
    if keyword in PLSQL_TRANSACTION_KEYWORDS:
        return "PL/SQL execution is disabled in read-only mode"
    if keyword == "explain":
        return "EXPLAIN PLAN is disabled in read-only mode because it writes to PLAN_TABLE"
    return "Only SELECT, WITH, and ROLLBACK statements are allowed in read-only mode"


class TransactionMixin:
    connection: Any | None
    autocommit: bool
    read_only: bool
    pending_rows_changed: int
    pending_unknown_changes: bool

    def ensure_connected(self) -> Any:
        raise NotImplementedError

    @property
    def has_uncommitted_changes(self) -> bool:
        return self.pending_rows_changed > 0 or self.pending_unknown_changes

    def commit(self) -> TransactionReport:
        if self.read_only:
            raise ReadOnlyModeError("Commit is disabled in read-only mode")
        conn = self.ensure_connected()
        conn.commit()
        report = self.transaction_report()
        self.clear_pending_transaction()
        return report

    def rollback(self) -> TransactionReport:
        conn = self.ensure_connected()
        conn.rollback()
        report = self.transaction_report()
        self.clear_pending_transaction()
        return report

    def transaction_report(self) -> TransactionReport:
        return TransactionReport(
            timestamp=datetime.now(),
            rows_changed=self.pending_rows_changed,
            has_unknown_changes=self.pending_unknown_changes,
        )

    def clear_pending_transaction(self) -> None:
        self.pending_rows_changed = 0
        self.pending_unknown_changes = False

    def record_pending_rows(self, rows_changed: int) -> None:
        if not self.autocommit and rows_changed > 0:
            self.pending_rows_changed += rows_changed

    def record_pending_unknown(self) -> None:
        if not self.autocommit:
            self.pending_unknown_changes = True

    def mark_pending_transaction_unknown(self) -> None:
        self.pending_rows_changed = 0
        self.pending_unknown_changes = True

    def record_statement_transaction_state(self, statement: str, rowcount: int) -> None:
        kind = transaction_statement_kind(statement)
        if kind in {"commit", "rollback", "ddl"}:
            self.clear_pending_transaction()
        elif kind == "rollback_to":
            self.mark_pending_transaction_unknown()
        elif kind == "dml":
            self.record_pending_rows(rowcount)
        elif kind == "plsql":
            self.record_pending_unknown()

    def synchronize_pending_transaction(self, connection: Any | None = None) -> bool:
        conn = self.connection if connection is None else connection
        if conn is None:
            return False
        in_progress = connection_transaction_in_progress(conn)
        if in_progress is None:
            return False
        if not in_progress:
            self.clear_pending_transaction()
        elif not self.has_uncommitted_changes:
            self.pending_unknown_changes = True
        return True

    def reconcile_uncertain_edit_failure(self, connection: Any) -> None:
        in_progress = connection_transaction_in_progress(connection)
        if in_progress is False:
            self.clear_pending_transaction()
        else:
            self.mark_pending_transaction_unknown()

    def ensure_statement_allowed(self, statement: str) -> None:
        if not self.read_only:
            return
        reason = read_only_rejection_reason(statement)
        if reason:
            raise ReadOnlyModeError(reason)
