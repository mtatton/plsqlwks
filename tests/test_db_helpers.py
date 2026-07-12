from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

import plsqlwks.db as db_module
import plsqlwks.db.execution as execution_module
import plsqlwks.exporting as exporting_module
from plsqlwks.config import AppConfig
from plsqlwks.db import (
    DBMS_OUTPUT_FETCH_LINES,
    DBMS_OUTPUT_LINE_SIZE,
    NULL_DISPLAY_TOKEN,
    ExplainPlanCleanupError,
    ExplainPlanStep,
    OracleCompilationError,
    OracleWorkspace,
    OracleExecutionError,
    PlsqlObject,
    QueryResult,
    ResultColumnMetadata,
    ReadOnlyModeError,
    TransactionReport,
    TruncatedLobValue,
    assemble_package_definition,
    empty_schema_object_groups,
    ensure_sql_terminator,
    fetch_ddl,
    format_value,
    materialize_result_value,
    metadata_object_type,
    package_body_exists,
    plsql_object_from_create_statement,
    read_only_rejection_reason,
    terminate_plsql_ddl,
    transaction_statement_kind,
    workspace_health,
    csv_cell,
)


def test_format_value_handles_oracle_friendly_types():
    assert format_value(None) == NULL_DISPLAY_TOKEN
    assert format_value(b"\x00\xff") == "00ff"
    assert format_value(Decimal("10.50")) == "10.50"
    assert format_value(datetime(2026, 5, 28, 19, 30, 5)) == "2026-05-28 19:30:05"
    assert format_value(datetime(2026, 5, 28, 19, 30, 5, 123456)) == "2026-05-28 19:30:05.123456"
    assert format_value("Příliš") == "Příliš"
    assert format_value("NULL") == "NULL"


def test_format_value_reads_clob_and_blob_explicitly(monkeypatch):
    monkeypatch.setattr(execution_module.oracledb, "LOB", FakeLob)
    clob = FakeLob(db_module.oracledb.DB_TYPE_CLOB, "Příliš")
    blob = FakeLob(db_module.oracledb.DB_TYPE_BLOB, b"\x00\xff")

    assert format_value(clob) == "Příliš"
    assert format_value(blob) == "00ff"
    assert clob.read_calls == [(1, None)]
    assert blob.read_calls == [(1, None)]


@pytest.mark.parametrize(
    ("type_code", "value", "expected_prefix", "type_name", "unit"),
    [
        (db_module.oracledb.DB_TYPE_CLOB, "abcdef", "abcd", "CLOB", "characters"),
        (db_module.oracledb.DB_TYPE_BLOB, b"\x00\x01\x02\x03\x04", "00010203", "BLOB", "bytes"),
    ],
)
def test_format_value_caps_lob_display_and_marks_original_unavailable(
    monkeypatch,
    type_code,
    value,
    expected_prefix,
    type_name,
    unit,
):
    monkeypatch.setattr(execution_module.oracledb, "LOB", FakeLob)
    lob = FakeLob(type_code, value)

    display, original = materialize_result_value(lob, lob_limit=4)

    assert display == f"{expected_prefix}… <{type_name} truncated: showing first 4 of {len(value)} {unit}>"
    assert original == TruncatedLobValue(type_name, len(value))
    assert lob.read_calls == [(1, 4)]


def test_materialize_result_closes_ref_cursor_and_returns_only_plain_data(monkeypatch):
    class FakeNestedCursor:
        def __init__(self):
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(execution_module.oracledb, "Cursor", FakeNestedCursor)
    cursor = FakeNestedCursor()

    display, original = materialize_result_value(cursor)

    assert display == "<REF CURSOR>"
    assert original == "<REF CURSOR>"
    assert cursor.closed is True


def test_materialize_result_converts_nested_driver_values_to_plain_values():
    class FakeDriverValue:
        __module__ = "oracledb.fake"

        def __str__(self) -> str:
            return "driver value"

    display, original = materialize_result_value({"items": [FakeDriverValue()]})

    assert display == "{'items': ['driver value']}"
    assert original == {"items": ["driver value"]}


def test_fetch_ddl_reads_complete_clob_instead_of_stringifying_locator(monkeypatch):
    monkeypatch.setattr(execution_module.oracledb, "LOB", FakeLob)
    ddl = "create view v_test as select 1 as n from dual"
    lob = FakeLob(db_module.oracledb.DB_TYPE_CLOB, ddl)
    cursor = FakeMetadataCursor(FakeMetadataConnection({("VIEW", "V_TEST"): lob}))

    assert fetch_ddl(cursor, "VIEW", "V_TEST") == ddl
    assert lob.read_calls == [(1, None)]


def test_number_output_handler_requests_exact_decimal_values():
    calls: list[tuple[object, int]] = []

    class Cursor:
        arraysize = 17

        def var(self, value_type, *, arraysize):
            calls.append((value_type, arraysize))
            return "number variable"

    cursor = Cursor()

    assert execution_module.decimal_output_type_handler(
        cursor,
        ResultColumnMetadata(db_module.oracledb.DB_TYPE_NUMBER),
    ) == "number variable"
    assert execution_module.decimal_output_type_handler(
        cursor,
        ResultColumnMetadata(db_module.oracledb.DB_TYPE_VARCHAR),
    ) is None
    assert calls == [(Decimal, 17)]


def test_csv_cell_quotes_commas_quotes_and_newlines():
    assert csv_cell("plain") == "plain"
    assert csv_cell("a,b") == '"a,b"'
    assert csv_cell('a"b') == '"a""b"'
    assert csv_cell("a\nb") == '"a\nb"'


def test_csv_cell_delegates_to_shared_csv_encoder(monkeypatch):
    calls: list[str] = []

    def fake_csv_cell(value: str) -> str:
        calls.append(value)
        return "encoded"

    monkeypatch.setattr(exporting_module, "csv_cell", fake_csv_cell)

    assert csv_cell("value") == "encoded"
    assert calls == ["value"]


def test_export_result_writes_utf8_csv(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    result = QueryResult("data", ["NAME", "NOTE"], [["kůň", 'a,b "quoted"'], ["NULL", "line\nbreak"]], "2 rows")
    path = tmp_path / "results" / "out.csv"

    workspace.export_result(result, path)

    assert path.read_text(encoding="utf-8") == 'NAME,NOTE\nkůň,"a,b ""quoted"""\nNULL,"line\nbreak"\n'


def test_export_result_delegates_to_shared_csv_writer(monkeypatch, tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    result = QueryResult("data", ["NAME"], [["kůň"]], "1 row")
    path = tmp_path / "results" / "out.csv"
    calls: list[tuple[Path, object, object]] = []

    def fake_write_csv(destination, columns, rows):
        calls.append((destination, columns, rows))

    monkeypatch.setattr(exporting_module, "write_csv", fake_write_csv)

    workspace.export_result(result, path)

    assert calls == [(path, result.columns, result.rows)]


def test_execute_script_uses_statement_titles_and_empty_script_message(tmp_path):
    workspace = RecordingWorkspace(make_config(tmp_path))

    results = workspace.execute_script("select 1 from dual;\nselect 2 from dual;\n")
    empty = workspace.execute_script("\n")

    assert workspace.calls == [
        ("select 1 from dual", "Statement 1 lines 1-1"),
        ("select 2 from dual", "Statement 2 lines 2-2"),
    ]
    assert [result.title for result in results] == ["Statement 1 lines 1-1", "Statement 2 lines 2-2"]
    assert empty == [QueryResult("Script", [], [], "No statements to execute.")]


def test_execute_script_uses_long_special_statement_line_titles(tmp_path, long_special_sql_case):
    workspace = RecordingWorkspace(make_config(tmp_path))
    expected_titles = [
        f"Statement {idx} lines {start}-{end}"
        for idx, (start, end) in enumerate(long_special_sql_case.expected_ranges, start=1)
    ]

    results = workspace.execute_script(long_special_sql_case.script)

    assert workspace.calls == list(zip(long_special_sql_case.expected_statements, expected_titles))
    assert [result.title for result in results] == expected_titles


def test_explain_statement_scopes_plan_table_rows_to_a_savepoint(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    rows = [
        (0, None, 0, "SELECT STATEMENT", None, None, None, None, 1, 12, 3, "00:00:01"),
        (1, 0, 1, "TABLE ACCESS", "FULL", "HR", "DECISIONS", "TABLE", 1, 12, 3, None),
    ]
    connection = FakeExplainConnection(rows, autocommit=True, transaction_state=False)
    workspace.connection = connection

    result = workspace.explain_statement("select * from decisions", "Current statement")

    cursor = connection.cursor_instance
    assert len(cursor.calls) == 4
    savepoint_sql, savepoint_params = cursor.calls[0]
    explain_sql, explain_params = cursor.calls[1]
    assert explain_params == {}
    assert savepoint_params == {}
    assert savepoint_sql.startswith("savepoint PLSQLWKS_EXPLAIN_")
    assert explain_sql.startswith("explain plan set statement_id = 'PLSQLWKS_")
    assert explain_sql.endswith(" for select * from decisions")
    plan_query, plan_params = cursor.calls[2]
    cleanup_sql, cleanup_params = cursor.calls[3]
    assert "from plan_table" in plan_query.lower()
    assert cleanup_sql == f"rollback to savepoint {savepoint_sql.split()[-1]}"
    assert cleanup_params == {}
    assert plan_params["statement_id"] in explain_sql
    assert all("delete from plan_table" not in sql.lower() for sql, _ in cursor.calls)
    assert connection.rollbacks == 1
    assert connection.transaction_in_progress is False
    assert connection.autocommit_changes == [False, True]
    assert connection.autocommit is True
    assert result.title == "Current statement"
    assert result.steps == [
        ExplainPlanStep(0, None, 0, "SELECT STATEMENT", "", "", "", "", "1", "12", "3", "00:00:01"),
        ExplainPlanStep(1, 0, 1, "TABLE ACCESS", "FULL", "HR", "DECISIONS", "TABLE", "1", "12", "3", ""),
    ]
    assert result.message.startswith("Explain plan: 2 step(s)")
    assert cursor.closed is True


def test_explain_statement_passes_bind_values_to_plan_statement(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExplainConnection([], transaction_state=False)
    workspace.connection = connection

    workspace.explain_statement("select * from decisions where id = :id", "Current statement", {"id": "42"})

    explain_sql, explain_params = connection.cursor_instance.calls[1]
    assert explain_sql.endswith(" for select * from decisions where id = :id")
    assert explain_params == {"id": "42"}


def test_explain_statement_ends_a_clean_manual_transaction(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExplainConnection([], transaction_state=False)
    workspace.connection = connection
    workspace.set_autocommit(False)
    connection.autocommit_changes.clear()

    workspace.explain_statement("select * from decisions", "Current statement")

    assert connection.savepoint_rollbacks == 1
    assert connection.rollbacks == 1
    assert connection.transaction_in_progress is False
    assert connection.autocommit is False
    assert connection.autocommit_changes == []
    assert workspace.has_uncommitted_changes is False


def test_explain_statement_preserves_pending_manual_work(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExplainConnection([], transaction_state=True)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(4)

    workspace.explain_statement("select * from decisions", "Current statement")

    assert connection.savepoint_rollbacks == 1
    assert connection.rollbacks == 0
    assert connection.transaction_in_progress is True
    assert workspace.pending_rows_changed == 4
    assert workspace.pending_unknown_changes is False


def test_explain_statement_marks_unavailable_prior_transaction_state_unknown(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExplainConnection([], transaction_state=None)
    workspace.connection = connection
    workspace.set_autocommit(False)

    workspace.explain_statement("select * from decisions", "Current statement")

    assert connection.savepoint_rollbacks == 1
    assert connection.rollbacks == 0
    assert connection.transaction_in_progress is True
    assert workspace.pending_rows_changed == 0
    assert workspace.pending_unknown_changes is True
    assert workspace.has_uncommitted_changes is True


def test_explain_statement_failure_is_rolled_back_without_masking_error(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExplainConnection(
        [],
        autocommit=True,
        transaction_state=False,
        explain_raises=True,
    )
    workspace.connection = connection

    with pytest.raises(RuntimeError, match="explain failed"):
        workspace.explain_statement("select * from decisions", "Current statement")

    assert connection.savepoint_rollbacks == 1
    assert connection.rollbacks == 1
    assert connection.transaction_in_progress is False
    assert connection.autocommit is True
    assert workspace.has_uncommitted_changes is False


def test_explain_cleanup_failure_preserves_prior_work_and_original_error(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExplainConnection(
        [],
        transaction_state=True,
        explain_raises=True,
        savepoint_rollback_raises=True,
    )
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(4)

    with pytest.raises(ExplainPlanCleanupError, match="explain failed") as excinfo:
        workspace.explain_statement("select * from decisions", "Current statement")

    assert isinstance(excinfo.value.original, RuntimeError)
    assert str(excinfo.value.original) == "explain failed"
    assert excinfo.value.__cause__ is excinfo.value.original
    assert excinfo.value.full_rollback_attempted is False
    assert "full rollback was not attempted to preserve prior work" in str(excinfo.value)
    assert connection.savepoint_rollbacks == 1
    assert connection.rollbacks == 0
    assert connection.transaction_in_progress is True
    assert workspace.pending_rows_changed == 4
    assert workspace.pending_unknown_changes is True


def test_successful_explain_surfaces_savepoint_failure_without_rolling_back_prior_work(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExplainConnection(
        [],
        transaction_state=True,
        savepoint_rollback_raises=True,
    )
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(4)

    with pytest.raises(ExplainPlanCleanupError, match="failed to roll back savepoint") as excinfo:
        workspace.explain_statement("select * from decisions", "Current statement")

    assert excinfo.value.original is None
    assert isinstance(excinfo.value.savepoint_rollback_error, RuntimeError)
    assert excinfo.value.__cause__ is excinfo.value.savepoint_rollback_error
    assert excinfo.value.full_rollback_attempted is False
    assert connection.savepoint_rollbacks == 1
    assert connection.rollbacks == 0
    assert connection.transaction_in_progress is True
    assert workspace.pending_rows_changed == 4
    assert workspace.pending_unknown_changes is True


def test_clean_explain_surfaces_savepoint_failure_after_safe_full_rollback(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExplainConnection(
        [],
        autocommit=True,
        transaction_state=False,
        savepoint_rollback_raises=True,
    )
    workspace.connection = connection

    with pytest.raises(ExplainPlanCleanupError, match="full rollback succeeded") as excinfo:
        workspace.explain_statement("select * from decisions", "Current statement")

    assert excinfo.value.original is None
    assert excinfo.value.full_rollback_attempted is True
    assert excinfo.value.full_rollback_succeeded is True
    assert excinfo.value.transaction_may_have_changes is False
    assert connection.rollbacks == 1
    assert connection.transaction_in_progress is False
    assert connection.autocommit is True
    assert workspace.has_uncommitted_changes is False


def test_explain_surfaces_failure_to_end_a_clean_transaction(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExplainConnection(
        [],
        autocommit=True,
        transaction_state=False,
        full_rollback_raises=True,
    )
    workspace.connection = connection

    with pytest.raises(ExplainPlanCleanupError, match="failed to end clean transaction") as excinfo:
        workspace.explain_statement("select * from decisions", "Current statement")

    assert excinfo.value.savepoint_rollback_error is None
    assert excinfo.value.full_rollback_attempted is True
    assert isinstance(excinfo.value.full_rollback_error, RuntimeError)
    assert connection.savepoint_rollbacks == 1
    assert connection.rollbacks == 1
    assert connection.transaction_in_progress is True
    assert connection.autocommit is True
    assert workspace.pending_unknown_changes is True


def test_clean_failing_explain_surfaces_savepoint_and_full_rollback_failures(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExplainConnection(
        [],
        autocommit=True,
        transaction_state=False,
        explain_raises=True,
        savepoint_rollback_raises=True,
        full_rollback_raises=True,
    )
    workspace.connection = connection

    with pytest.raises(ExplainPlanCleanupError, match="full rollback also failed") as excinfo:
        workspace.explain_statement("select * from decisions", "Current statement")

    assert isinstance(excinfo.value.original, RuntimeError)
    assert str(excinfo.value.original) == "explain failed"
    assert isinstance(excinfo.value.savepoint_rollback_error, RuntimeError)
    assert isinstance(excinfo.value.full_rollback_error, RuntimeError)
    assert excinfo.value.__cause__ is excinfo.value.original
    assert excinfo.value.full_rollback_attempted is True
    assert excinfo.value.full_rollback_succeeded is False
    assert connection.savepoint_rollbacks == 1
    assert connection.rollbacks == 1
    assert connection.transaction_in_progress is True
    assert connection.autocommit is True
    assert connection.cursor_instance.closed is True
    assert workspace.pending_unknown_changes is True


def test_explain_autocommit_restore_failure_does_not_mask_sql_error(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExplainConnection(
        [],
        autocommit=True,
        transaction_state=False,
        explain_raises=True,
        autocommit_restore_raises=True,
    )
    workspace.connection = connection

    with pytest.raises(ExplainPlanCleanupError, match="failed to restore autocommit") as excinfo:
        workspace.explain_statement("select * from decisions", "Current statement")

    assert isinstance(excinfo.value.original, RuntimeError)
    assert str(excinfo.value.original) == "explain failed"
    assert isinstance(excinfo.value.autocommit_restore_error, RuntimeError)
    assert excinfo.value.cursor_close_error is None
    assert excinfo.value.__cause__ is excinfo.value.original
    assert connection.savepoint_rollbacks == 1
    assert connection.rollbacks == 1
    assert connection.autocommit_changes == [False, True]
    assert connection.autocommit is False
    assert connection.cursor_instance.closed is True
    assert workspace.pending_unknown_changes is True


def test_explain_cursor_close_failure_does_not_mask_sql_or_cleanup_errors(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExplainConnection(
        [],
        transaction_state=True,
        explain_raises=True,
        savepoint_rollback_raises=True,
        cursor_close_raises=True,
    )
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(4)

    with pytest.raises(ExplainPlanCleanupError, match="failed to close explain cursor") as excinfo:
        workspace.explain_statement("select * from decisions", "Current statement")

    assert isinstance(excinfo.value.original, RuntimeError)
    assert str(excinfo.value.original) == "explain failed"
    assert isinstance(excinfo.value.savepoint_rollback_error, RuntimeError)
    assert isinstance(excinfo.value.cursor_close_error, RuntimeError)
    assert excinfo.value.__cause__ is excinfo.value.original
    assert connection.savepoint_rollbacks == 1
    assert connection.rollbacks == 0
    assert connection.cursor_close_attempts == 1
    assert connection.cursor_instance.closed is False
    assert workspace.pending_rows_changed == 4
    assert workspace.pending_unknown_changes is True


def test_read_only_explain_statement_uses_display_cursor_without_plan_table_writes(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    workspace.read_only = True
    connection = FakeReadOnlyExplainConnection(
        [
            ("Plan hash value: 123",),
            ("| Id | Operation        | Name      |",),
            ("|  0 | SELECT STATEMENT |           |",),
        ]
    )
    workspace.connection = connection

    result = workspace.explain_statement("select * from decisions", "Current statement")

    assert result.title == "Current statement"
    assert result.steps == []
    assert result.raw_lines == [
        "Plan hash value: 123",
        "| Id | Operation        | Name      |",
        "|  0 | SELECT STATEMENT |           |",
    ]
    assert result.message.startswith("Explain plan: 3 line(s)")
    assert len(connection.cursors) == 2
    statement_cursor, plan_cursor = connection.cursors
    assert statement_cursor.calls == [("select * from decisions", {})]
    assert len(plan_cursor.calls) == 1
    plan_sql, plan_params = plan_cursor.calls[0]
    assert "dbms_xplan.display_cursor" in plan_sql.lower()
    assert plan_params == {"plan_format": "TYPICAL"}
    all_sql = "\n".join(sql for cursor in connection.cursors for sql, _ in cursor.calls).lower()
    assert "explain plan" not in all_sql
    assert "from plan_table" not in all_sql
    assert "delete from plan_table" not in all_sql
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert statement_cursor.closed is True
    assert plan_cursor.closed is True


def test_read_only_explain_statement_passes_bind_values_to_statement_cursor(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    workspace.read_only = True
    connection = FakeReadOnlyExplainConnection([("Plan hash value: 123",)])
    workspace.connection = connection

    workspace.explain_statement("select * from decisions where id = :id", "Current statement", {"id": "42"})

    statement_cursor, _plan_cursor = connection.cursors
    assert statement_cursor.calls == [("select * from decisions where id = :id", {"id": "42"})]


@pytest.mark.parametrize(
    "statement",
    [
        "insert into decisions(id) values (1)",
        "create table decisions_copy as select * from decisions",
        "begin null; end;",
        "explain plan for select * from decisions",
        "select * from decisions for update",
        "rollback",
    ],
)
def test_read_only_explain_statement_rejects_non_select_plan_inputs(tmp_path, statement):
    workspace = OracleWorkspace(make_config(tmp_path))
    workspace.read_only = True
    connection = FakeReadOnlyExplainConnection([])
    workspace.connection = connection

    with pytest.raises(ReadOnlyModeError):
        workspace.explain_statement(statement, "Current statement")

    assert connection.cursors == []


def test_execute_statement_select_respects_arraysize_max_rows_and_editability_failure(tmp_path):
    config = make_config(tmp_path)
    config = AppConfig(
        user=config.user,
        dsn=config.dsn,
        password_file=config.password_file,
        workspace_dir=config.workspace_dir,
        max_rows=2,
        arraysize=7,
    )
    workspace = OracleWorkspace(config)
    connection = FakeExecuteConnection(description=[("NAME",)], rows=[("kůň",), ("two",), ("three",)])
    workspace.connection = connection
    workspace.editable_context_for_result = lambda statement, columns, metadata: (_ for _ in ()).throw(
        RuntimeError("boom")
    )

    result = workspace.execute_statement("select name from decisions", "Select")

    assert connection.cursor_instance.arraysize == 7
    assert connection.cursor_instance.fetchmany_size == 3
    assert result.columns == ["NAME"]
    assert result.rows == [["kůň"], ["two"]]
    assert result.original_rows == [["kůň"], ["two"]]
    assert result.editable_context is None
    assert result.edit_message == "Editability check failed: boom"
    assert result.continuation is not None
    assert isinstance(result.continuation.token, str)
    assert not hasattr(result.continuation, "cursor")
    assert connection.cursor_instance.closed is False
    assert "limited to 2 rows" in result.message


def test_execute_statement_select_does_not_warn_when_exactly_max_rows(tmp_path):
    config = make_config(tmp_path)
    config = AppConfig(
        user=config.user,
        dsn=config.dsn,
        password_file=config.password_file,
        workspace_dir=config.workspace_dir,
        max_rows=2,
    )
    workspace = OracleWorkspace(config)
    connection = FakeExecuteConnection(description=[("NAME",)], rows=[("one",), ("two",)])
    workspace.connection = connection

    result = workspace.execute_statement("select name from decisions", "Select")

    assert connection.cursor_instance.fetchmany_size == 3
    assert result.rows == [["one"], ["two"]]
    assert result.continuation is None
    assert connection.cursor_instance.closed is True
    assert "limited" not in result.message


def test_execute_statement_passes_bind_values_to_cursor(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection(description=[("NAME",)], rows=[("one",)])
    workspace.connection = connection

    result = workspace.execute_statement(
        "select name from decisions where id = :id",
        "Select",
        {"id": "42"},
    )

    assert connection.cursor_instance.calls == [
        ("select name from decisions where id = :id", {"id": "42"}),
    ]
    assert result.rows == [["one"]]


def test_cancel_current_operation_calls_connection_cancel(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection()
    workspace.connection = connection

    assert workspace.cancel_current_operation() is True

    assert connection.cancel_calls == 1


def test_cancel_current_operation_without_connection_returns_false(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))

    assert workspace.cancel_current_operation() is False


def test_fetch_more_rows_uses_lookahead_and_closes_after_final_page(tmp_path):
    config = make_config(tmp_path)
    config = AppConfig(
        user=config.user,
        dsn=config.dsn,
        password_file=config.password_file,
        workspace_dir=config.workspace_dir,
        max_rows=2,
    )
    workspace = OracleWorkspace(config)
    connection = FakeExecuteConnection(
        description=[("NAME",)],
        rows=[("one",), ("two",), ("three",), ("four",), ("five",)],
    )
    workspace.connection = connection

    result = workspace.execute_statement("select name from decisions", "Select")
    assert result.continuation is not None
    continuation = result.continuation
    first_page = workspace.fetch_more_rows(continuation, len(result.rows))
    result.rows.extend(first_page.rows)
    result.original_rows.extend(first_page.original_rows)
    result.message = first_page.message
    result.continuation = first_page.continuation
    assert result.continuation is continuation
    second_page = workspace.fetch_more_rows(result.continuation, len(result.rows))
    result.rows.extend(second_page.rows)
    result.original_rows.extend(second_page.original_rows)
    result.message = second_page.message
    result.continuation = second_page.continuation

    assert connection.cursor_instance.fetchmany_sizes == [3, 2, 2]
    assert first_page.rows == [["three"], ["four"]]
    assert first_page.original_rows == [["three"], ["four"]]
    assert "limited to 4 rows" in first_page.message
    assert second_page.rows == [["five"]]
    assert second_page.original_rows == [["five"]]
    assert result.original_rows == [["one"], ["two"], ["three"], ["four"], ["five"]]
    assert "limited" not in second_page.message
    assert result.continuation is None
    assert connection.cursor_instance.closed is True


def test_close_result_continuation_is_idempotent_and_makes_token_stale(tmp_path):
    config = make_config(tmp_path)
    config = AppConfig(
        user=config.user,
        dsn=config.dsn,
        password_file=config.password_file,
        workspace_dir=config.workspace_dir,
        max_rows=1,
    )
    workspace = OracleWorkspace(config)
    connection = FakeExecuteConnection(description=[("NAME",)], rows=[("one",), ("two",)])
    workspace.connection = connection

    result = workspace.execute_statement("select name from decisions", "Select")
    assert result.continuation is not None

    workspace.close_result_continuation(result.continuation)
    workspace.close_result_continuation(result.continuation)

    assert connection.cursor_instance.close_calls == 1
    with pytest.raises(RuntimeError, match="stale or no longer available"):
        workspace.fetch_more_rows(result.continuation, len(result.rows))


def test_close_all_result_continuations_closes_every_cursor_and_is_idempotent(tmp_path):
    config = make_config(tmp_path)
    config = AppConfig(
        user=config.user,
        dsn=config.dsn,
        password_file=config.password_file,
        workspace_dir=config.workspace_dir,
        max_rows=1,
    )
    workspace = OracleWorkspace(config)
    connection = FakeExecuteConnection(description=[("NAME",)], rows=[("one",), ("two",)])
    workspace.connection = connection

    first = workspace.execute_statement("select name from first_table", "First")
    second = workspace.execute_statement("select name from second_table", "Second")
    assert first.continuation is not None
    assert second.continuation is not None
    result_cursors = [cursor for cursor in connection.cursors if not cursor.closed]
    assert len(result_cursors) == 2

    workspace.close_all_result_continuations()
    workspace.close_all_result_continuations()

    assert [cursor.close_calls for cursor in result_cursors] == [1, 1]
    with pytest.raises(RuntimeError, match="stale or no longer available"):
        workspace.fetch_more_rows(first.continuation, len(first.rows))
    with pytest.raises(RuntimeError, match="stale or no longer available"):
        workspace.fetch_more_rows(second.continuation, len(second.rows))


def test_execute_script_closes_paged_results_as_later_queries_supersede_them(tmp_path):
    base = make_config(tmp_path)
    config = AppConfig(
        user=base.user,
        dsn=base.dsn,
        password_file=base.password_file,
        workspace_dir=base.workspace_dir,
        max_rows=1,
    )
    workspace = OracleWorkspace(config)
    connection = FakeExecuteConnection(
        description=[("NAME",)],
        rows=[("one",), ("two",)],
    )
    workspace.connection = connection

    results = workspace.execute_script(
        "select name from first_table;"
        "select name from second_table;"
        "select name from third_table;"
    )

    assert [result.continuation is not None for result in results] == [False, False, True]
    result_cursors = [cursor for cursor in connection.cursors if cursor.calls]
    assert [cursor.closed for cursor in result_cursors] == [True, True, False]
    assert len(workspace._result_continuations) == 1


def test_fetch_more_rows_failure_closes_and_invalidates_continuation(tmp_path):
    config = make_config(tmp_path)
    config = AppConfig(
        user=config.user,
        dsn=config.dsn,
        password_file=config.password_file,
        workspace_dir=config.workspace_dir,
        max_rows=1,
    )
    workspace = OracleWorkspace(config)
    connection = FakeExecuteConnection(description=[("NAME",)], rows=[("one",), ("two",), ("three",)])
    workspace.connection = connection
    result = workspace.execute_statement("select name from decisions", "Select")
    assert result.continuation is not None
    connection.cursor_instance.fetch_error = RuntimeError("fetch failed")

    with pytest.raises(RuntimeError, match="fetch failed"):
        workspace.fetch_more_rows(result.continuation, len(result.rows))

    assert connection.cursor_instance.closed is True
    with pytest.raises(RuntimeError, match="stale or no longer available"):
        workspace.fetch_more_rows(result.continuation, len(result.rows))


def test_connect_closes_open_result_continuations_before_replacing_connection(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    config = AppConfig(
        user=config.user,
        dsn=config.dsn,
        password_file=config.password_file,
        workspace_dir=config.workspace_dir,
        max_rows=1,
    )
    workspace = OracleWorkspace(config)
    old_connection = FakeExecuteConnection(description=[("NAME",)], rows=[("one",), ("two",)])
    new_connection = FakeExecuteConnection()
    workspace.connection = old_connection
    result = workspace.execute_statement("select name from decisions", "Select")
    assert result.continuation is not None
    monkeypatch.setattr(db_module, "read_password", lambda path: "secret")
    monkeypatch.setattr(db_module.oracledb, "connect", lambda **params: new_connection)
    workspace.enable_dbms_output = lambda: None

    workspace.connect()

    assert old_connection.cursor_instance.closed is True
    assert old_connection.closed is True
    assert workspace.connection is new_connection
    with pytest.raises(RuntimeError, match="stale or no longer available"):
        workspace.fetch_more_rows(result.continuation, len(result.rows))


def test_execute_statement_dml_commits_and_returns_dbms_output(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection(description=None, rowcount=3)
    workspace.connection = connection
    workspace.read_dbms_output = lambda: ["Příliš", "kůň"]

    result = workspace.execute_statement("begin null; end;", "Block")

    assert workspace.autocommit is True
    assert workspace.has_uncommitted_changes is False
    assert connection.commits == 1
    assert result.columns == ["DBMS_OUTPUT"]
    assert result.rows == [["Příliš"], ["kůň"]]
    assert result.message.startswith("3 row(s) affected")
    assert "2 dbms_output line(s)" in result.message


def test_execute_statement_returns_success_warning_when_dbms_output_read_fails(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection(description=None, rowcount=1)
    workspace.connection = connection

    def fail_read():
        raise RuntimeError("get_lines failed")

    workspace.read_dbms_output = fail_read

    result = workspace.execute_statement("update decisions set name = 'done'", "Update")

    assert connection.commits == 1
    assert result.columns == []
    assert result.rows == []
    assert "1 row(s) affected" in result.message
    assert "warning: DBMS_OUTPUT read failed: get_lines failed" in result.message


def test_execute_statement_failure_captures_dbms_output(tmp_path):
    original = RuntimeError("ORA-20000: boom")
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection(execute_error=original)
    workspace.connection = connection
    output = [
        "Error raised in: <anonymous> at line 3 - ORA-20000: boom",
        "ORA-06512: at line 3",
    ]
    workspace.read_dbms_output = lambda: output

    with pytest.raises(OracleExecutionError) as excinfo:
        workspace.execute_statement("begin raise_application_error(-20000, 'boom'); end;", "Block")

    assert excinfo.value.original is original
    assert excinfo.value.title == "Block"
    assert excinfo.value.dbms_output == output
    assert excinfo.value.dbms_output_error == ""
    assert excinfo.value.statement == "begin raise_application_error(-20000, 'boom'); end;"
    assert excinfo.value.__cause__ is original


def test_execute_statement_failure_preserves_original_when_dbms_output_read_fails(tmp_path):
    original = RuntimeError("ORA-20001: original")
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection(execute_error=original)
    workspace.connection = connection

    def fail_read():
        raise RuntimeError("get_lines failed")

    workspace.read_dbms_output = fail_read

    with pytest.raises(OracleExecutionError) as excinfo:
        workspace.execute_statement("begin raise_application_error(-20001, 'original'); end;", "Block")

    assert excinfo.value.original is original
    assert excinfo.value.dbms_output == []
    assert excinfo.value.dbms_output_error == "get_lines failed"
    assert str(excinfo.value) == "ORA-20001: original"


def test_execute_statement_raises_plsql_compilation_errors(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection(
        compile_errors=[
            (2, 52, 'PLS-00103: Encountered the symbol "NUMBER" when expecting one of the following: ;'),
        ],
    )
    workspace.connection = connection
    workspace.read_dbms_output = lambda: []

    with pytest.raises(OracleExecutionError) as excinfo:
        workspace.execute_statement(
            "create or replace package mathop as\n"
            "  function nthroot(input_number in number, n in number) return result number;\n"
            "end mathop;",
            "Statement 1 lines 1-3",
        )

    assert isinstance(excinfo.value.original, OracleCompilationError)
    assert "PL/SQL compilation failed for PACKAGE MATHOP" in str(excinfo.value)
    assert "line 2, column 52" in str(excinfo.value)
    assert connection.commits == 1
    assert workspace.has_uncommitted_changes is False
    assert len(connection.cursors) == 2
    assert "from user_errors" in " ".join(connection.cursors[1].calls[0][0].lower().split())
    assert connection.cursors[1].calls[0][1] == {
        "object_name": "MATHOP",
        "object_type": "PACKAGE",
    }


def test_plsql_object_from_create_statement_parses_schema_and_quoted_names():
    assert plsql_object_from_create_statement(
        '/* comment */ create or replace package body app."MathOp" as end;'
    ) == PlsqlObject(object_type="PACKAGE BODY", owner="APP", name="MathOp")


def test_execute_statement_manual_mode_leaves_transaction_pending(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection(description=None, rowcount=2)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.read_dbms_output = lambda: []

    result = workspace.execute_statement("update decisions set name = 'new'", "Update")

    assert connection.autocommit is False
    assert connection.commits == 0
    assert result.message.endswith("; pending commit")
    assert workspace.pending_rows_changed == 2
    assert workspace.pending_unknown_changes is False
    assert workspace.has_uncommitted_changes is True

    report = workspace.commit()

    assert isinstance(report, TransactionReport)
    assert report.rows_changed == 2
    assert report.has_unknown_changes is False
    assert connection.commits == 1
    assert workspace.has_uncommitted_changes is False


def test_manual_dml_accumulates_pending_rows_until_commit(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection(description=None, rowcount=2)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.read_dbms_output = lambda: []

    workspace.execute_statement("update decisions set name = 'one'", "Update")
    connection.rowcount = 3
    workspace.execute_statement("merge into decisions d using dual on (1=1) when matched then update set name = 'two'", "Merge")

    assert workspace.pending_rows_changed == 5
    assert workspace.pending_unknown_changes is False
    report = workspace.commit()
    assert report.rows_changed == 5
    assert report.has_unknown_changes is False
    assert workspace.has_uncommitted_changes is False


def test_manual_plsql_marks_unknown_pending_changes(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection(description=None, rowcount=-1)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.read_dbms_output = lambda: []

    result = workspace.execute_statement("begin null; end;", "Block")

    assert result.message.endswith("; pending commit")
    assert workspace.pending_rows_changed == 0
    assert workspace.pending_unknown_changes is True
    report = workspace.rollback()
    assert report.rows_changed == 0
    assert report.has_unknown_changes is True
    assert workspace.has_uncommitted_changes is False


def test_transaction_control_and_ddl_statements_clear_pending_state(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection(description=None, rowcount=-1)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.read_dbms_output = lambda: []

    workspace.record_pending_rows(4)
    workspace.execute_statement("/* setup */ create table t (id number)", "Create")
    assert workspace.has_uncommitted_changes is False

    workspace.record_pending_rows(2)
    workspace.execute_statement("-- done\ncommit", "Commit SQL")
    assert workspace.has_uncommitted_changes is False

    workspace.record_pending_unknown()
    workspace.execute_statement("rollback", "Rollback SQL")
    assert workspace.has_uncommitted_changes is False


@pytest.mark.parametrize(
    "statement",
    [
        "alter table decisions add note varchar2(10)",
        "analyze table decisions compute statistics",
        "associate statistics with columns decisions.name using stats_type",
        "audit select on decisions",
        "comment on table decisions is 'test'",
        "create table decisions_copy (id number)",
        "disassociate statistics from columns decisions.name",
        "drop table decisions_copy",
        "flashback table decisions to before drop",
        "grant select on decisions to app_user",
        "noaudit select on decisions",
        "purge recyclebin",
        "rename decisions to decisions_old",
        "revoke select on decisions from app_user",
        "truncate table decisions",
    ],
)
def test_transaction_statement_kind_recognizes_all_oracle_ddl_families(statement):
    assert transaction_statement_kind(statement) == "ddl"


@pytest.mark.parametrize(
    "statement",
    [
        "alter session set nls_date_format = 'YYYY-MM-DD'",
        "/* admin */ alter system switch logfile",
    ],
)
def test_transaction_statement_kind_does_not_treat_session_or_system_control_as_ddl(statement):
    assert transaction_statement_kind(statement) == ""


@pytest.mark.parametrize(
    "statement",
    [
        "rollback to before_edit",
        "rollback to savepoint before_edit",
        "/* partial */ rollback work to before_edit",
        "rollback work /* keep earlier work */ to savepoint before_edit",
    ],
)
def test_transaction_statement_kind_distinguishes_partial_rollback(statement):
    assert transaction_statement_kind(statement) == "rollback_to"


@pytest.mark.parametrize("statement", ["rollback", "rollback work", "rollback force 'tx-id'"])
def test_transaction_statement_kind_keeps_full_rollback_classification(statement):
    assert transaction_statement_kind(statement) == "rollback"


def test_partial_rollback_fallback_marks_pending_count_unknown(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection(description=None, rowcount=-1)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(4)
    workspace.read_dbms_output = lambda: []

    result = workspace.execute_statement("rollback work to savepoint before_edit", "Partial rollback")

    assert workspace.pending_rows_changed == 0
    assert workspace.pending_unknown_changes is True
    assert workspace.has_uncommitted_changes is True
    assert result.message.endswith("; pending commit")


def test_noncommitting_alter_keeps_fallback_pending_state(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection(description=None, rowcount=-1)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(4)
    workspace.read_dbms_output = lambda: []

    workspace.execute_statement("alter session set nls_date_format = 'YYYY-MM-DD'", "Alter session")

    assert workspace.pending_rows_changed == 4
    assert workspace.pending_unknown_changes is False


def test_driver_transaction_state_clears_tracker_after_success(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = TransactionStateExecuteConnection(False, description=None, rowcount=2)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(3)
    workspace.read_dbms_output = lambda: []

    workspace.execute_statement("update decisions set name = 'new'", "Update")

    assert connection.transaction_state_reads >= 1
    assert workspace.has_uncommitted_changes is False


def test_driver_transaction_state_marks_unclassified_success_as_pending(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = TransactionStateExecuteConnection(True, description=[("VALUE",)], rows=[(1,)])
    workspace.connection = connection
    workspace.set_autocommit(False)

    workspace.execute_statement("select 1 from dual for update", "Select")

    assert connection.transaction_state_reads >= 1
    assert workspace.pending_rows_changed == 0
    assert workspace.pending_unknown_changes is True


def test_driver_transaction_state_reconciles_failed_ddl_precommit(tmp_path):
    original = RuntimeError("ORA-00955: name is already used")
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = TransactionStateExecuteConnection(False, execute_error=original)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(3)
    workspace.read_dbms_output = lambda: []

    with pytest.raises(OracleExecutionError):
        workspace.execute_statement("create table decisions (id number)", "Create")

    assert connection.transaction_state_reads >= 1
    assert workspace.has_uncommitted_changes is False


def test_driver_transaction_state_preserves_pending_work_after_failed_dml(tmp_path):
    original = RuntimeError("ORA-00001: unique constraint")
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = TransactionStateExecuteConnection(True, execute_error=original)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(3)
    workspace.read_dbms_output = lambda: []

    with pytest.raises(OracleExecutionError):
        workspace.execute_statement("insert into decisions(id) values (1)", "Insert")

    assert workspace.pending_rows_changed == 3
    assert workspace.pending_unknown_changes is False


def test_transaction_state_property_failure_uses_statement_fallback(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = TransactionStateExecuteConnection(RuntimeError("driver state unavailable"), rowcount=2)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.read_dbms_output = lambda: []

    workspace.execute_statement("update decisions set name = 'new'", "Update")

    assert connection.transaction_state_reads >= 1
    assert workspace.pending_rows_changed == 2
    assert workspace.pending_unknown_changes is False


@pytest.mark.parametrize(
    "statement",
    [
        'select "FOR UPDATE" from decisions',
        'select "FOR ""UPDATE""" from decisions',
        'with values_ as (select "FOR UPDATE" label from decisions) select label from values_',
        "select nq'[it's FOR UPDATE]' from decisions",
        "with values_ as (select NQ'{apostrophe ' and FOR UPDATE}' label from decisions) select label from values_",
    ],
)
def test_read_only_for_update_detection_ignores_quoted_identifiers_and_nq_literals(statement):
    assert read_only_rejection_reason(statement) == ""


def test_sql_lexers_preserve_and_mask_national_q_literals():
    statement = "select nq'[it's -- literal /* literal */ FROM FOR UPDATE]' as note from decisions"

    assert db_module.strip_sql_comments(statement) == statement
    assert db_module.find_top_level_sql_keyword(statement, "from") == statement.rindex("from")
    assert "FOR UPDATE" not in db_module.sql_code_mask(statement).upper()
    assert ("FOR", "UPDATE") not in zip(
        db_module.tail_sql_words(statement),
        db_module.tail_sql_words(statement)[1:],
    )


def test_read_only_for_update_detection_still_rejects_clause_after_quoted_identifier():
    assert (
        read_only_rejection_reason('select "FOR UPDATE" from decisions for update')
        == "SELECT FOR UPDATE is disabled in read-only mode"
    )


def test_oracle_workspace_uses_configured_autocommit_default(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path, autocommit=False))

    assert workspace.autocommit is False


def test_connect_applies_configured_autocommit_to_connection(monkeypatch, tmp_path):
    connection = FakeExecuteConnection()
    workspace = OracleWorkspace(make_config(tmp_path, autocommit=False))
    monkeypatch.setattr(db_module, "read_password", lambda path: "secret")
    monkeypatch.setattr(db_module.oracledb, "connect", lambda **params: connection)
    workspace.enable_dbms_output = lambda: None

    workspace.connect()

    assert workspace.connection is connection
    assert connection.autocommit is False


def test_connect_closes_and_forgets_partially_initialized_connection(monkeypatch, tmp_path):
    connection = FakeExecuteConnection()
    workspace = OracleWorkspace(make_config(tmp_path))
    monkeypatch.setattr(db_module, "read_password", lambda path: "secret")
    monkeypatch.setattr(db_module.oracledb, "connect", lambda **params: connection)
    workspace.enable_dbms_output = lambda: (_ for _ in ()).throw(
        RuntimeError("DBMS_OUTPUT initialization failed")
    )

    with pytest.raises(RuntimeError, match="DBMS_OUTPUT initialization failed"):
        workspace.connect()

    assert connection.closed is True
    assert workspace.connection is None


def test_commit_and_rollback_use_current_connection(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeExecuteConnection()
    workspace.connection = connection

    workspace.commit()
    workspace.rollback()

    assert connection.commits == 1
    assert connection.rollbacks == 1


def test_read_dbms_output_reads_available_batches(tmp_path):
    long_line = "x" * 4500 + "suffix"
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeOutputConnection([["one", "kůň", long_line], []])
    workspace.connection = connection

    assert workspace.read_dbms_output() == ["one", "kůň", long_line]
    assert connection.cursor_instance.arrayvar_calls == [(str, DBMS_OUTPUT_FETCH_LINES, DBMS_OUTPUT_LINE_SIZE)]
    assert connection.cursor_instance.count_var is not None
    assert connection.cursor_instance.count_var.set_values == [(0, DBMS_OUTPUT_FETCH_LINES)]


def test_read_dbms_output_drains_multiple_full_batches(tmp_path):
    first_batch = [f"line {idx}" for idx in range(DBMS_OUTPUT_FETCH_LINES)]
    second_batch = ["line 100", "line 101"]
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeOutputConnection([first_batch, second_batch])
    workspace.connection = connection

    assert workspace.read_dbms_output() == [*first_batch, *second_batch]
    assert connection.cursor_instance.count_var is not None
    assert connection.cursor_instance.count_var.set_values == [
        (0, DBMS_OUTPUT_FETCH_LINES),
        (0, DBMS_OUTPUT_FETCH_LINES),
    ]


def test_get_object_definition_fetches_package_spec_and_body(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    workspace.connection = FakeMetadataConnection(
        {
            ("PACKAGE", "PKG_TEST"): "create or replace package pkg_test as end;",
            ("PACKAGE_BODY", "PKG_TEST"): "create or replace package body pkg_test as end;",
        },
        package_body_count=1,
    )

    text = workspace.get_object_definition("PACKAGE", "pkg_test")

    assert text == (
        "create or replace package pkg_test as end;\n"
        "/\n\n"
        "create or replace package body pkg_test as end;\n"
        "/"
    )


def test_get_object_definition_terminates_procedure_ddl(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    workspace.connection = FakeMetadataConnection(
        {("PROCEDURE", "P_TEST"): "create or replace procedure p_test as begin null; end"}
    )

    assert workspace.get_object_definition("PROCEDURE", "p_test").endswith(";\n/")


@pytest.mark.parametrize(
    ("object_type", "ddl", "expected"),
    [
        (
            "TRIGGER",
            "create or replace trigger tr_test before insert on t begin null; end",
            "create or replace trigger tr_test before insert on t begin null; end;\n/",
        ),
        ("SEQUENCE", "create sequence seq_test", "create sequence seq_test;"),
        ("INDEX", "create index ix_test on t (id)", "create index ix_test on t (id);"),
        ("SYNONYM", "create synonym syn_test for t", "create synonym syn_test for t;"),
    ],
)
def test_get_object_definition_terminates_extended_schema_object_ddl(
    tmp_path,
    object_type,
    ddl,
    expected,
):
    workspace = OracleWorkspace(make_config(tmp_path))
    workspace.connection = FakeMetadataConnection({(object_type, f"{object_type}_TEST"): ddl})

    assert workspace.get_object_definition(object_type.lower(), f"{object_type.lower()}_test") == expected


def test_list_schema_objects_includes_extended_types_in_display_order(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    connection = FakeColumnConnection(
        [
            ("TABLE", "T_TEST"),
            ("VIEW", "V_TEST"),
            ("PROCEDURE", "P_TEST"),
            ("FUNCTION", "F_TEST"),
            ("PACKAGE", "PKG_TEST"),
            ("TRIGGER", "TR_TEST"),
            ("SEQUENCE", "SEQ_TEST"),
            ("INDEX", "IX_TEST"),
            ("SYNONYM", "SYN_TEST"),
        ]
    )
    workspace.connection = connection

    assert workspace.list_schema_objects() == {
        "TABLE": ["T_TEST"],
        "VIEW": ["V_TEST"],
        "PROCEDURE": ["P_TEST"],
        "FUNCTION": ["F_TEST"],
        "PACKAGE": ["PKG_TEST"],
        "TRIGGER": ["TR_TEST"],
        "SEQUENCE": ["SEQ_TEST"],
        "INDEX": ["IX_TEST"],
        "SYNONYM": ["SYN_TEST"],
    }
    query, params = connection.cursor_instance.calls[0]
    assert params == {}
    object_types = (
        "TABLE",
        "VIEW",
        "PROCEDURE",
        "FUNCTION",
        "PACKAGE",
        "TRIGGER",
        "SEQUENCE",
        "INDEX",
        "SYNONYM",
    )
    for position, object_type in enumerate(
        object_types,
        start=1,
    ):
        assert f"when '{object_type.lower()}' then {position}" in query.lower()
        assert query.lower().count(f"'{object_type.lower()}'") == 2


def test_list_object_columns_returns_current_schema_columns(tmp_path):
    workspace = OracleWorkspace(make_config(tmp_path))
    workspace.connection = FakeColumnConnection([("id",), ("Name",)])

    assert workspace.list_object_columns("decisions") == ["ID", "NAME"]
    assert workspace.connection.cursor_instance.calls == [
        (
            "select column_name from user_tab_columns where table_name = :object_name order by column_id",
            {"object_name": "DECISIONS"},
        )
    ]
    assert workspace.list_object_columns("not valid") == []


def test_metadata_termination_helpers_are_idempotent_and_package_aware():
    assert ensure_sql_terminator("create table t (id number)") == "create table t (id number);"
    assert ensure_sql_terminator(" create table t (id number); \n") == "create table t (id number);"
    assert terminate_plsql_ddl("create or replace procedure p as begin null; end") == (
        "create or replace procedure p as begin null; end;\n/"
    )
    assert assemble_package_definition("create package p as end", None) == "create package p as end;\n/"
    assert assemble_package_definition("create package p as end", "create package body p as end") == (
        "create package p as end;\n/\n\ncreate package body p as end;\n/"
    )


def test_metadata_helpers_validate_types_and_missing_ddl(tmp_path):
    for object_type in (
        "TABLE",
        "VIEW",
        "PROCEDURE",
        "FUNCTION",
        "PACKAGE",
        "TRIGGER",
        "SEQUENCE",
        "INDEX",
        "SYNONYM",
    ):
        assert metadata_object_type(object_type) == object_type
    with pytest.raises(ValueError, match="Unsupported schema object type"):
        metadata_object_type("PACKAGE_BODY")

    cursor = FakeMetadataCursor(FakeMetadataConnection({("VIEW", "EMPTY_VIEW"): None}))
    with pytest.raises(ValueError, match="No DDL returned for VIEW EMPTY_VIEW"):
        fetch_ddl(cursor, "VIEW", "EMPTY_VIEW")

    assert package_body_exists(FakeMetadataCursor(FakeMetadataConnection({}, package_body_count=0)), "PKG") is False


def test_empty_schema_groups_and_workspace_health_report_expected_state(tmp_path):
    config = make_config(tmp_path)

    assert empty_schema_object_groups() == {
        "TABLE": [],
        "VIEW": [],
        "PROCEDURE": [],
        "FUNCTION": [],
        "PACKAGE": [],
        "TRIGGER": [],
        "SEQUENCE": [],
        "INDEX": [],
        "SYNONYM": [],
    }
    assert workspace_health(config) == [
        f"Password file is missing: {config.password_file}",
        f"Will create: {config.sql_dir}",
        f"Will create: {config.plsql_dir}",
        f"Will create: {config.results_dir}",
    ]

    config.password_file.write_text("secret", encoding="utf-8")
    config.sql_dir.mkdir(parents=True)
    config.plsql_dir.mkdir(parents=True)
    config.results_dir.mkdir(parents=True)

    assert workspace_health(config) == []


def make_config(root: Path, autocommit: bool = True) -> AppConfig:
    return AppConfig(
        user="hr",
        dsn="db",
        password_file=root / "orapass",
        workspace_dir=root,
        autocommit=autocommit,
    )


class RecordingWorkspace(OracleWorkspace):
    def __init__(self, config: AppConfig):
        super().__init__(config)
        self.calls: list[tuple[str, str]] = []

    def execute_statement(self, statement: str, title: str = "Statement") -> QueryResult:
        self.calls.append((statement, title))
        return QueryResult(title, [], [], "ok")


class FakeOutputConnection:
    def __init__(self, batches: list[list[str]]):
        self.batches = batches
        self.cursor_instance = FakeOutputCursor(self)

    def cursor(self):
        return self.cursor_instance


class FakeOutputCursor:
    def __init__(self, connection: FakeOutputConnection):
        self.connection = connection
        self.arrayvar_calls: list[tuple[object, int, int]] = []
        self.count_var: FakeCountVar | None = None

    def arrayvar(self, value_type, count: int, size: int = 0):
        self.arrayvar_calls.append((value_type, count, size))
        return FakeArrayVar(value_type, count, size)

    def var(self, value_type):
        self.count_var = FakeCountVar()
        return self.count_var

    def callproc(self, name: str, args: list[object]):
        lines_var, count_var = args
        batch = self.connection.batches.pop(0) if self.connection.batches else []
        lines_var.value = batch
        count_var.value = len(batch)

    def close(self):
        pass


class FakeArrayVar:
    def __init__(self, value_type, count: int, size: int = 0):
        self.value_type = value_type
        self.count = count
        self.size = size
        self.value: list[str] = []

    def getvalue(self):
        return self.value


class FakeCountVar:
    def __init__(self):
        self.value = 0
        self.set_values: list[tuple[int, int]] = []

    def setvalue(self, index: int, value: int):
        self.set_values.append((index, value))
        self.value = value

    def getvalue(self):
        return self.value


class FakeLob:
    def __init__(self, type_code, value):
        self.type = type_code
        self.value = value
        self.read_calls: list[tuple[int, int | None]] = []

    def __str__(self):
        return "<LOB locator>"

    def size(self):
        return len(self.value)

    def read(self, offset: int = 1, amount: int | None = None):
        self.read_calls.append((offset, amount))
        start = offset - 1
        return self.value[start:] if amount is None else self.value[start : start + amount]


class FakeMetadataConnection:
    def __init__(self, ddl: dict[tuple[str, str], str], package_body_count: int = 0):
        self.ddl = ddl
        self.package_body_count = package_body_count

    def cursor(self):
        return FakeMetadataCursor(self)


class FakeMetadataCursor:
    def __init__(self, connection: FakeMetadataConnection):
        self.connection = connection
        self.rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, **params):
        normalized = " ".join(sql.lower().split())
        if "dbms_metadata.get_ddl" in normalized:
            key = (str(params["object_type"]).upper(), str(params["object_name"]).upper())
            self.rows = [(self.connection.ddl.get(key),)]
        elif "object_type = 'package body'" in normalized:
            self.rows = [(self.connection.package_body_count,)]
        else:
            self.rows = []

    def fetchone(self):
        if not self.rows:
            return None
        return self.rows.pop(0)

    def close(self):
        pass


class FakeColumnConnection:
    def __init__(self, rows: list[tuple[object, ...]]):
        self.cursor_instance = FakeColumnCursor(rows)

    def cursor(self):
        return self.cursor_instance


class FakeColumnCursor:
    def __init__(self, rows: list[tuple[object, ...]]):
        self.rows = rows
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, sql: str, **params):
        self.calls.append((" ".join(sql.split()), params))

    def __iter__(self):
        return iter(self.rows)

    def close(self):
        pass


class FakeExecuteConnection:
    def __init__(
        self,
        description=None,
        rows=None,
        rowcount: int = -1,
        execute_error: Exception | None = None,
        compile_errors=None,
    ):
        self.description = description
        self.rows = rows or []
        self.rowcount = rowcount
        self.execute_error = execute_error
        self.compile_errors = compile_errors or []
        self.commits = 0
        self.rollbacks = 0
        self.autocommit = True
        self.cancel_calls = 0
        self.closed = False
        self.cursor_instance: FakeExecuteCursor | None = None
        self.cursors: list[FakeExecuteCursor] = []

    def cursor(self):
        cursor = FakeExecuteCursor(self)
        self.cursors.append(cursor)
        if self.cursor_instance is None:
            self.cursor_instance = cursor
        return cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def cancel(self):
        self.cancel_calls += 1

    def close(self):
        self.closed = True


class TransactionStateExecuteConnection(FakeExecuteConnection):
    def __init__(self, transaction_state: bool | Exception, **kwargs):
        super().__init__(**kwargs)
        self.transaction_state = transaction_state
        self.transaction_state_reads = 0

    @property
    def transaction_in_progress(self):
        self.transaction_state_reads += 1
        if isinstance(self.transaction_state, Exception):
            raise self.transaction_state
        return self.transaction_state


class FakeExplainConnection:
    def __init__(
        self,
        rows,
        *,
        autocommit: bool = True,
        transaction_state: bool | None = False,
        explain_raises: bool = False,
        savepoint_rollback_raises: bool = False,
        full_rollback_raises: bool = False,
        autocommit_restore_raises: bool = False,
        cursor_close_raises: bool = False,
    ):
        self.rows = rows
        self._autocommit = autocommit
        self.autocommit_changes: list[bool] = []
        self.transaction_state = transaction_state
        self.explain_raises = explain_raises
        self.savepoint_rollback_raises = savepoint_rollback_raises
        self.full_rollback_raises = full_rollback_raises
        self.autocommit_restore_raises = autocommit_restore_raises
        self.cursor_close_raises = cursor_close_raises
        self.savepoint_rollbacks = 0
        self.rollbacks = 0
        self.cursor_close_attempts = 0
        self.cursor_instance = FakeExplainCursor(self)

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, enabled: bool):
        self.autocommit_changes.append(enabled)
        if enabled and self.autocommit_restore_raises:
            raise RuntimeError("autocommit restore failed")
        self._autocommit = enabled

    @property
    def transaction_in_progress(self):
        return self.transaction_state

    def cursor(self):
        return self.cursor_instance

    def rollback(self):
        self.rollbacks += 1
        if self.full_rollback_raises:
            raise RuntimeError("full rollback failed")
        self.transaction_state = False


class FakeExplainCursor:
    def __init__(self, connection: FakeExplainConnection):
        self.connection = connection
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.rows = []
        self.closed = False

    def execute(self, sql: str, params: dict[str, object] | None = None, **keyword_params):
        self.calls.append((sql, dict(params or keyword_params)))
        normalized_sql = " ".join(sql.split()).lower()
        if normalized_sql.startswith("savepoint "):
            self.connection.transaction_state = True
            self.rows = []
        elif normalized_sql.startswith("rollback to savepoint "):
            self.connection.savepoint_rollbacks += 1
            if self.connection.savepoint_rollback_raises:
                raise RuntimeError("savepoint rollback failed")
            self.rows = []
        elif normalized_sql.startswith("explain plan "):
            if self.connection.explain_raises:
                raise RuntimeError("explain failed")
            self.rows = []
        elif "from plan_table" in normalized_sql:
            self.rows = list(self.connection.rows)
        else:
            self.rows = []

    def __iter__(self):
        return iter(self.rows)

    def close(self):
        self.connection.cursor_close_attempts += 1
        if self.connection.cursor_close_raises:
            raise RuntimeError("cursor close failed")
        self.closed = True


class FakeReadOnlyExplainConnection:
    def __init__(self, plan_rows):
        self.plan_rows = plan_rows
        self.cursors: list[FakeReadOnlyExplainCursor] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        cursor = FakeReadOnlyExplainCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeReadOnlyExplainCursor:
    def __init__(self, connection: FakeReadOnlyExplainConnection):
        self.connection = connection
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.rows = []
        self.closed = False

    def execute(self, sql: str, params: dict[str, object] | None = None, **keyword_params):
        self.calls.append((sql, dict(params or keyword_params)))
        if "dbms_xplan.display_cursor" in sql.lower():
            self.rows = list(self.connection.plan_rows)
        else:
            self.rows = []

    def __iter__(self):
        return iter(self.rows)

    def close(self):
        self.closed = True


class FakeExecuteCursor:
    def __init__(self, connection: FakeExecuteConnection):
        self.connection = connection
        self.arraysize = 0
        self.description = None
        self.rowcount = -1
        self.fetchmany_size: int | None = None
        self.fetchmany_sizes: list[int] = []
        self.fetch_offset = 0
        self.fetch_error: Exception | None = None
        self.closed = False
        self.close_calls = 0
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.rows = []

    def execute(self, sql: str, params: dict[str, object] | None = None):
        self.calls.append((sql, dict(params or {})))
        if "user_errors" in sql.lower() or "all_errors" in sql.lower():
            self.description = [("LINE",), ("POSITION",), ("TEXT",)]
            self.rows = list(self.connection.compile_errors)
            self.rowcount = len(self.rows)
            return
        if self.connection.execute_error is not None:
            raise self.connection.execute_error
        self.description = self.connection.description
        self.rowcount = self.connection.rowcount

    def fetchall(self):
        return list(self.rows)

    def fetchmany(self, size: int):
        self.fetchmany_size = size
        self.fetchmany_sizes.append(size)
        if self.fetch_error is not None:
            raise self.fetch_error
        rows = self.connection.rows[self.fetch_offset : self.fetch_offset + size]
        self.fetch_offset += len(rows)
        return rows

    def close(self):
        self.close_calls += 1
        self.closed = True
