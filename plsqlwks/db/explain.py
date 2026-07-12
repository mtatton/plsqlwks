from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
import uuid

from .execution import execute_user_statement, format_value
from .models import (
    ExplainPlanCleanupError,
    ExplainPlanResult,
    ExplainPlanStep,
    ReadOnlyModeError,
)
from .transactions import (
    connection_transaction_in_progress,
    explain_plan_savepoint,
    leading_sql_keyword,
)


class ExplainMixin:
    def explain_statement(
        self,
        statement: str,
        title: str = "Explain plan",
        bind_values: Mapping[str, object] | None = None,
    ) -> ExplainPlanResult:
        if self.read_only:
            return self.explain_read_only_statement(statement, title, bind_values)
        return self.explain_plan_table_statement(statement, title, bind_values)

    def explain_plan_table_statement(
        self,
        statement: str,
        title: str,
        bind_values: Mapping[str, object] | None = None,
    ) -> ExplainPlanResult:
        conn = self.ensure_connected()
        cursor = conn.cursor()
        statement_id = f"PLSQLWKS_{uuid.uuid4().hex[:16].upper()}"
        started = datetime.now()
        had_pending_work = self.has_uncommitted_changes
        transaction_before = connection_transaction_in_progress(conn)
        if transaction_before is not False and not had_pending_work:
            self.pending_unknown_changes = True
        result: ExplainPlanResult | None = None
        primary_error: Exception | None = None
        try:
            with explain_plan_savepoint(
                conn,
                cursor,
                had_pending_work=had_pending_work,
                transaction_before=transaction_before,
            ):
                execute_user_statement(
                    cursor,
                    f"explain plan set statement_id = '{statement_id}' for {statement}",
                    bind_values,
                )
                cursor.execute(
                    """
                    select
                      id,
                      parent_id,
                      depth,
                      operation,
                      options,
                      object_owner,
                      object_name,
                      object_type,
                      cardinality,
                      bytes,
                      cost,
                      time
                    from plan_table
                    where statement_id = :statement_id
                    order by id
                    """,
                    statement_id=statement_id,
                )
                steps = [explain_plan_step_from_row(row) for row in cursor]
                elapsed = (datetime.now() - started).total_seconds()
                result = ExplainPlanResult(
                    title,
                    steps,
                    f"Explain plan: {len(steps)} step(s) in {elapsed:.2f}s",
                )
        except Exception as exc:
            primary_error = exc
        try:
            cursor.close()
        except Exception as cursor_close_error:
            primary_error = ExplainPlanCleanupError.from_finalizer_failure(
                primary_error,
                cursor_close_error=cursor_close_error,
            )
        if isinstance(primary_error, ExplainPlanCleanupError):
            if primary_error.transaction_may_have_changes:
                self.pending_unknown_changes = True
            cause = primary_error.primary_cause
            if cause is not None:
                raise primary_error from cause
            raise primary_error
        if primary_error is not None:
            raise primary_error
        assert result is not None
        return result

    def explain_read_only_statement(
        self,
        statement: str,
        title: str,
        bind_values: Mapping[str, object] | None = None,
    ) -> ExplainPlanResult:
        self.ensure_statement_allowed(statement)
        if leading_sql_keyword(statement) not in {"select", "with"}:
            raise ReadOnlyModeError("Only SELECT and WITH statements can be explained in read-only mode")
        conn = self.ensure_connected()
        statement_cursor = conn.cursor()
        plan_cursor = conn.cursor()
        started = datetime.now()
        try:
            execute_user_statement(statement_cursor, statement, bind_values)
            plan_cursor.execute(
                """
                select plan_table_output
                from table(dbms_xplan.display_cursor(null, null, :plan_format))
                """,
                plan_format="TYPICAL",
            )
            raw_lines = [format_plan_value(row[0]) if row else "" for row in plan_cursor]
            elapsed = (datetime.now() - started).total_seconds()
            return ExplainPlanResult(title, [], f"Explain plan: {len(raw_lines)} line(s) in {elapsed:.2f}s", raw_lines)
        finally:
            statement_cursor.close()
            plan_cursor.close()


def format_plan_value(value: Any) -> str:
    return "" if value is None else format_value(value)


def nullable_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def explain_plan_step_from_row(row: tuple[Any, ...]) -> ExplainPlanStep:
    values = list(row) + [None] * 12
    return ExplainPlanStep(
        id=int(values[0]),
        parent_id=nullable_int(values[1]),
        depth=int(values[2] or 0),
        operation=format_plan_value(values[3]),
        options=format_plan_value(values[4]),
        object_owner=format_plan_value(values[5]),
        object_name=format_plan_value(values[6]),
        object_type=format_plan_value(values[7]),
        cardinality=format_plan_value(values[8]),
        bytes=format_plan_value(values[9]),
        cost=format_plan_value(values[10]),
        time=format_plan_value(values[11]),
    )
