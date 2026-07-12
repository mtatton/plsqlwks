from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import uuid

import pytest

from plsqlwks.config import load_config
from plsqlwks.db import (
    CellUpdateResult,
    ConcurrentEditError,
    LOB_DISPLAY_LIMIT,
    NULL_DISPLAY_TOKEN,
    OracleExecutionError,
    OracleWorkspace,
    TruncatedLobValue,
)
from plsqlwks.sqlsplit import split_script, statement_at_cursor
from plsqlwks.ui import App, UIState
from plsqlwks.ui.db_worker import DatabaseWorker, DbWorkerFinished


pytestmark = [pytest.mark.integration, pytest.mark.oracle]


def worker_result(worker: DatabaseWorker, task):
    handle = worker.submit(task)
    assert handle.done.wait(30)
    while True:
        event = handle.events.get_nowait()
        if isinstance(event, DbWorkerFinished):
            if event.error is not None:
                raise event.error
            return event.result


def test_oracle_select_and_dbms_output_utf8():
    workspace = OracleWorkspace(load_config())
    try:
        workspace.connect()
        result = workspace.execute_statement("select user, systimestamp from dual", "Smoke select")
        assert result.columns == ["USER", "SYSTIMESTAMP"]
        assert len(result.rows) == 1
        assert result.rows[0][0]

        statement = statement_at_cursor("select 1 as value from dual; select 2 as value from dual;", 0, 35)
        assert statement is not None
        same_line = workspace.execute_statement(statement.text, "Same-line current statement")
        assert same_line.rows == [["2"]]

        output = workspace.execute_statement(
            "begin dbms_output.put_line('Příliš žluťoučký kůň'); end;",
            "DBMS_OUTPUT smoke",
        )
        assert output.columns == ["DBMS_OUTPUT"]
        assert output.rows == [["Příliš žluťoučký kůň"]]

        binds = workspace.execute_statement(
            'select :id as lower_id, :ID as upper_id, :"MixedCase" as quoted_value from dual',
            "Bind-name semantics",
            {"id": "shared", '"MixedCase"': "quoted"},
        )
        assert binds.rows == [["shared", "shared", "quoted"]]
    finally:
        workspace.close()


def test_oracle_worker_pages_result_then_reconnects_cleanly():
    config = load_config()
    expected_rows = config.max_rows * 2 + 1
    worker = DatabaseWorker(OracleWorkspace(config))
    try:
        worker_result(worker, lambda db, progress: db.ensure_connected())
        result = worker_result(
            worker,
            lambda db, progress: db.execute_statement(
                f"select level as value from dual connect by level <= {expected_rows}",
                "Paged worker query",
            ),
        )
        loaded_rows = list(result.rows)
        continuation = result.continuation
        while continuation is not None:
            page = worker_result(
                worker,
                lambda db, progress, token=continuation, count=len(loaded_rows): (
                    db.fetch_more_rows(token, count)
                ),
            )
            loaded_rows.extend(page.rows)
            continuation = page.continuation

        assert len(loaded_rows) == expected_rows
        assert loaded_rows[0] == ["1"]
        assert loaded_rows[-1] == [str(expected_rows)]

        def reconnect(db, progress):
            db.close()
            db.ensure_connected()

        worker_result(worker, reconnect)
        assert worker.session_state.connected is True
    finally:
        worker.shutdown()


def test_oracle_ui_reconnect_resolves_pending_transaction_before_replacing_session():
    config = load_config()
    table_name = object_name("RECONNECT_TX")
    worker = DatabaseWorker(OracleWorkspace(config))
    app = object.__new__(App)
    app.db_worker = worker
    app.state = UIState(config=config, db=worker.session_state)
    app.running = True
    answers = iter(["c", "r", "d", "d"])
    app.prompt = lambda label, default="", strip=True: next(answers)

    def execute(statement: str):
        return worker_result(
            worker,
            lambda db, progress: db.execute_statement(statement),
        )

    try:
        worker_result(worker, lambda db, progress: db.ensure_connected())
        execute(f"create table {table_name} (id number primary key)")
        worker_result(worker, lambda db, progress: db.set_autocommit(False))

        execute(f"insert into {table_name} values (1)")
        app.state.db = worker.session_state
        app.reconnect_database()
        app.wait_for_db_operation(timeout=30)
        assert execute(f"select count(*) from {table_name}").rows == [["1"]]

        execute(f"insert into {table_name} values (2)")
        app.state.db = worker.session_state
        app.reconnect_database()
        app.wait_for_db_operation(timeout=30)
        assert execute(f"select count(*) from {table_name}").rows == [["1"]]

        execute(f"insert into {table_name} values (3)")
        app.state.db = worker.session_state
        app.reconnect_database()
        app.wait_for_db_operation(timeout=30)
        assert execute(f"select count(*) from {table_name}").rows == [["1"]]

        def leave_dead_session(db, progress):
            db.ensure_connected().close()
            db.record_pending_unknown()

        worker_result(worker, leave_dead_session)
        app.state.db = worker.session_state
        app.reconnect_database()
        app.wait_for_db_operation(timeout=30)
        assert app.state.db.connected is True
        assert "warning: old session close failed" in app.state.status
        assert execute("select 1 from dual").rows == [["1"]]
    finally:
        try:
            worker_result(
                worker,
                lambda db, progress: execute_ignoring_missing(
                    db,
                    f"drop table {table_name} purge",
                ),
            )
        finally:
            worker.shutdown()


def test_oracle_dbms_output_line_longer_than_default_arrayvar_size():
    workspace = OracleWorkspace(load_config())
    expected = "x" * 4500 + "END"
    try:
        workspace.connect()
        output = workspace.execute_statement(
            "begin dbms_output.put_line(rpad('x', 4500, 'x') || 'END'); end;",
            "Long DBMS_OUTPUT line",
        )

        assert output.columns == ["DBMS_OUTPUT"]
        assert len(output.rows) == 1
        assert len(output.rows[0][0]) == len(expected)
        assert output.rows[0][0].endswith("END")
        assert output.rows[0][0] == expected
    finally:
        workspace.close()


def test_oracle_dbms_output_is_captured_when_plsql_handler_reraises():
    workspace = OracleWorkspace(load_config())
    try:
        workspace.connect()
        with pytest.raises(OracleExecutionError) as excinfo:
            workspace.execute_statement(
                """
begin
  raise_application_error(-20000, 'diagnostic boom');
exception
  when others then
    dbms_output.put_line(
      'Error raised in: ' || nvl($$plsql_unit, '<anonymous>') ||
      ' at line ' || $$plsql_line || ' - ' || sqlerrm
    );
    dbms_output.put_line(dbms_utility.format_error_backtrace);
    raise;
end;
""",
                "DBMS_OUTPUT failure diagnostics",
            )

        assert any("Error raised in:" in line for line in excinfo.value.dbms_output)
        assert any("ORA-20000: diagnostic boom" in line for line in excinfo.value.dbms_output)
        assert any("ORA-06512" in line for line in excinfo.value.dbms_output)
    finally:
        workspace.close()


def test_oracle_rowid_cell_editing_round_trip():
    workspace = OracleWorkspace(load_config())
    table_name = object_name("T")
    try:
        workspace.connect()
        execute_ignoring_missing(workspace, f"drop table {table_name} purge")
        workspace.execute_statement(f"create table {table_name} (id number primary key, name varchar2(100))")
        workspace.execute_statement(f"insert into {table_name} (id, name) values (1, 'old')")

        result = workspace.execute_statement(f"select rowid, t.* from {table_name} t")
        assert result.editable_context is not None
        rowid = result.rows[0][0]

        original = result.original_rows[0][2]
        refreshed = workspace.update_cell_by_rowid(result.editable_context, rowid, 2, original, "Příliš")
        assert refreshed.display == "Příliš"

        refreshed = workspace.update_cell_by_rowid(
            result.editable_context,
            rowid,
            2,
            refreshed.value,
            "NULL",
        )
        assert refreshed.display == "NULL"

        refreshed = workspace.update_cell_by_rowid(
            result.editable_context,
            rowid,
            2,
            refreshed.value,
            NULL_DISPLAY_TOKEN,
        )
        assert refreshed.display == NULL_DISPLAY_TOKEN
    finally:
        try:
            execute_ignoring_missing(workspace, f"drop table {table_name} purge")
        finally:
            workspace.close()


def test_oracle_rowid_edit_detects_concurrent_update():
    first = OracleWorkspace(load_config())
    second = OracleWorkspace(load_config())
    table_name = object_name("LOCK")
    try:
        first.connect()
        second.connect()
        # This test needs the second session's update to be visible, regardless
        # of the user's persisted transaction-mode preference.
        first.set_autocommit(True)
        second.set_autocommit(True)
        execute_ignoring_missing(first, f"drop table {table_name} purge")
        first.execute_statement(f"create table {table_name} (id number primary key, name varchar2(100))")
        first.execute_statement(f"insert into {table_name} values (1, 'loaded')")

        result = first.execute_statement(f"select rowid, name from {table_name}")
        assert result.editable_context is not None
        rowid = result.rows[0][0]
        original = result.original_rows[0][1]

        second.execute_statement(f"update {table_name} set name = 'concurrent' where id = 1")

        with pytest.raises(ConcurrentEditError, match="changed or the row was deleted"):
            first.update_cell_by_rowid(
                result.editable_context,
                rowid,
                1,
                original,
                "overwritten",
            )

        current = first.execute_statement(f"select name from {table_name} where id = 1")
        assert current.rows == [["concurrent"]]
    finally:
        # Release any lock held by the competing session before dropping the
        # shared table, including when an assertion fails mid-test.
        second.close()
        try:
            execute_ignoring_missing(first, f"drop table {table_name} purge")
        finally:
            first.close()


def test_oracle_typed_rowid_edits_and_lob_schema_ddl_round_trip():
    workspace = OracleWorkspace(load_config())
    table_name = object_name("TYPES")
    try:
        workspace.connect()
        execute_ignoring_missing(workspace, f"drop table {table_name} purge")
        workspace.execute_statement(
            f"""
            create table {table_name} (
              id number primary key,
              amount number(10, 2),
              happened_at date,
              recorded_at timestamp(6),
              notes clob,
              payload blob
            )
            """
        )
        workspace.execute_statement(
            f"""
            insert into {table_name}
              (id, amount, happened_at, recorded_at, notes, payload)
            values
              (1, 1.25, date '2026-07-11', timestamp '2026-07-11 10:11:12.123456', :notes, :payload)
            """,
            bind_values={"notes": "old notes", "payload": b"\x00\xff"},
        )
        workspace.execute_statement("alter session set nls_numeric_characters = ',.'")
        workspace.execute_statement("alter session set nls_date_format = 'DD-MON-RR'")

        result = workspace.execute_statement(f"select rowid, t.* from {table_name} t")
        assert result.editable_context is not None
        rowid = result.rows[0][0]
        original = result.original_rows[0]
        assert original[2] == Decimal("1.25")
        assert original[3] == datetime(2026, 7, 11)
        assert original[4] == datetime(2026, 7, 11, 10, 11, 12, 123456)
        assert original[5] == "old notes"
        assert original[6] == b"\x00\xff"

        amount = workspace.update_cell_by_rowid(
            result.editable_context,
            rowid,
            2,
            original[2],
            "10.50",
        )
        happened_at = workspace.update_cell_by_rowid(
            result.editable_context,
            rowid,
            3,
            original[3],
            "2026-07-12 13:14:15",
        )
        recorded_at = workspace.update_cell_by_rowid(
            result.editable_context,
            rowid,
            4,
            original[4],
            "2026-07-12 13:14:15.654321",
        )
        notes = workspace.update_cell_by_rowid(
            result.editable_context,
            rowid,
            5,
            original[5],
            "Příliš žluťoučký kůň",
        )
        payload = workspace.update_cell_by_rowid(
            result.editable_context,
            rowid,
            6,
            original[6],
            "1020ff",
        )

        assert amount == CellUpdateResult(Decimal("10.5"), "10.5")
        assert happened_at == CellUpdateResult(datetime(2026, 7, 12, 13, 14, 15), "2026-07-12 13:14:15")
        assert recorded_at == CellUpdateResult(
            datetime(2026, 7, 12, 13, 14, 15, 654321),
            "2026-07-12 13:14:15.654321",
        )
        assert notes == CellUpdateResult("Příliš žluťoučký kůň", "Příliš žluťoučký kůň")
        assert payload == CellUpdateResult(b"\x10\x20\xff", "1020ff")

        ddl = workspace.get_object_definition("TABLE", table_name)
        ddl_upper = ddl.upper()
        assert "CREATE TABLE" in ddl_upper
        assert f'"{table_name}"' in ddl_upper
        assert '"NOTES"' in ddl_upper and "CLOB" in ddl_upper
        assert '"PAYLOAD"' in ddl_upper and "BLOB" in ddl_upper
        assert "LOB truncated" not in ddl

        lob_result = workspace.execute_statement(f"select notes, payload from {table_name}")
        assert lob_result.rows == [["Příliš žluťoučký kůň", "1020ff"]]
        assert lob_result.original_rows == [["Příliš žluťoučký kůň", b"\x10\x20\xff"]]

        large_text = "x" * (LOB_DISPLAY_LIMIT + 1)
        large_binary = b"\xab" * (LOB_DISPLAY_LIMIT + 1)
        workspace.execute_statement(
            f"update {table_name} set notes = :notes, payload = :payload where id = 1",
            bind_values={"notes": large_text, "payload": large_binary},
        )
        truncated = workspace.execute_statement(f"select notes, payload from {table_name}")
        assert truncated.rows[0][0].startswith("x" * LOB_DISPLAY_LIMIT)
        assert truncated.rows[0][0].endswith(
            f"… <CLOB truncated: showing first {LOB_DISPLAY_LIMIT} of {LOB_DISPLAY_LIMIT + 1} characters>"
        )
        assert truncated.rows[0][1].startswith("ab" * LOB_DISPLAY_LIMIT)
        assert truncated.rows[0][1].endswith(
            f"… <BLOB truncated: showing first {LOB_DISPLAY_LIMIT} of {LOB_DISPLAY_LIMIT + 1} bytes>"
        )
        assert truncated.original_rows == [
            [
                TruncatedLobValue("CLOB", LOB_DISPLAY_LIMIT + 1),
                TruncatedLobValue("BLOB", LOB_DISPLAY_LIMIT + 1),
            ]
        ]
    finally:
        try:
            execute_ignoring_missing(workspace, f"drop table {table_name} purge")
        finally:
            workspace.close()


def test_oracle_editionable_plsql_script_executes_as_one_block():
    workspace = OracleWorkspace(load_config())
    procedure_name = object_name("EDP")
    script = f"""create or replace editionable procedure {procedure_name} as
begin
  null;
end;
/
"""
    try:
        workspace.connect()
        execute_ignoring_missing(workspace, f"drop procedure {procedure_name}")

        statements = split_script(script)
        assert len(statements) == 1
        workspace.execute_script(script)

        objects = workspace.list_schema_objects()
        assert procedure_name in objects["PROCEDURE"]
    finally:
        try:
            execute_ignoring_missing(workspace, f"drop procedure {procedure_name}")
        finally:
            workspace.close()


def test_oracle_transaction_tracker_follows_session_control_and_failed_ddl_commits():
    workspace = OracleWorkspace(load_config())
    table_name = object_name("TX")
    try:
        workspace.connect()
        execute_ignoring_missing(workspace, f"drop table {table_name} purge")
        workspace.execute_statement(f"create table {table_name} (id number primary key)")
        workspace.set_autocommit(False)

        workspace.execute_statement(f"insert into {table_name} values (1)")
        assert workspace.has_uncommitted_changes is True

        workspace.execute_statement("alter session set nls_date_format = 'YYYY-MM-DD'")
        assert workspace.has_uncommitted_changes is True
        workspace.rollback()
        assert workspace.execute_statement(f"select count(*) from {table_name}").rows == [["0"]]

        workspace.execute_statement(f"insert into {table_name} values (2)")
        assert workspace.has_uncommitted_changes is True
        with pytest.raises(OracleExecutionError):
            workspace.execute_statement(f"create table {table_name} (other_id number)")

        assert workspace.has_uncommitted_changes is False
        assert workspace.execute_statement(f"select count(*) from {table_name}").rows == [["1"]]
    finally:
        try:
            execute_ignoring_missing(workspace, f"drop table {table_name} purge")
        finally:
            workspace.close()


def test_oracle_explain_plan_is_transaction_neutral_and_preserves_prior_work():
    workspace = OracleWorkspace(load_config())
    table_name = object_name("EXP_TX")
    try:
        workspace.connect()
        execute_ignoring_missing(workspace, f"drop table {table_name} purge")
        workspace.execute_statement(f"create table {table_name} (id number primary key)")
        workspace.set_autocommit(False)
        connection = workspace.ensure_connected()

        assert connection.transaction_in_progress is False
        clean_plan = workspace.explain_statement(f"select * from {table_name}")
        assert clean_plan.steps
        assert connection.transaction_in_progress is False
        assert workspace.has_uncommitted_changes is False

        workspace.execute_statement(f"insert into {table_name} values (1)")
        assert connection.transaction_in_progress is True
        assert workspace.pending_rows_changed == 1

        pending_plan = workspace.explain_statement(f"select * from {table_name} where id = 1")
        assert pending_plan.steps
        assert connection.transaction_in_progress is True
        assert workspace.pending_rows_changed == 1
        assert workspace.pending_unknown_changes is False

        workspace.rollback()
        assert workspace.execute_statement(f"select count(*) from {table_name}").rows == [["0"]]
    finally:
        try:
            execute_ignoring_missing(workspace, f"drop table {table_name} purge")
        finally:
            workspace.close()


def test_oracle_schema_definition_loading_for_procedure_and_package():
    workspace = OracleWorkspace(load_config())
    procedure_name = object_name("P")
    package_name = object_name("K")
    try:
        workspace.connect()
        execute_ignoring_missing(workspace, f"drop procedure {procedure_name}")
        execute_ignoring_missing(workspace, f"drop package {package_name}")

        workspace.execute_statement(f"create or replace procedure {procedure_name} as begin null; end;")
        workspace.execute_statement(f"create or replace package {package_name} as procedure run; end;")
        workspace.execute_statement(
            f"create or replace package body {package_name} as procedure run as begin null; end; end;"
        )

        objects = workspace.list_schema_objects()
        assert procedure_name in objects["PROCEDURE"]
        assert package_name in objects["PACKAGE"]

        procedure_ddl = workspace.get_object_definition("PROCEDURE", procedure_name).upper()
        package_ddl = workspace.get_object_definition("PACKAGE", package_name).upper()
        assert procedure_name in procedure_ddl
        assert "PROCEDURE" in procedure_ddl
        assert package_name in package_ddl
        assert "PACKAGE BODY" in package_ddl
        assert "\n/\n\n" in package_ddl
    finally:
        try:
            execute_ignoring_missing(workspace, f"drop procedure {procedure_name}")
            execute_ignoring_missing(workspace, f"drop package {package_name}")
        finally:
            workspace.close()


def test_oracle_schema_definition_loading_for_extended_types():
    workspace = OracleWorkspace(load_config())
    table_name = object_name("T")
    trigger_name = object_name("TR")
    sequence_name = object_name("S")
    index_name = object_name("I")
    synonym_name = object_name("Y")
    try:
        workspace.connect()
        execute_ignoring_missing(workspace, f"drop synonym {synonym_name}")
        execute_ignoring_missing(workspace, f"drop trigger {trigger_name}")
        execute_ignoring_missing(workspace, f"drop sequence {sequence_name}")
        execute_ignoring_missing(workspace, f"drop index {index_name}")
        execute_ignoring_missing(workspace, f"drop table {table_name} purge")

        workspace.execute_statement(f"create table {table_name} (id number)")
        workspace.execute_statement(f"create sequence {sequence_name}")
        workspace.execute_statement(f"create index {index_name} on {table_name} (id)")
        workspace.execute_statement(f"create synonym {synonym_name} for {table_name}")
        workspace.execute_statement(
            f"create or replace trigger {trigger_name} before insert on {table_name} begin null; end;"
        )

        objects = workspace.list_schema_objects()
        assert trigger_name in objects["TRIGGER"]
        assert sequence_name in objects["SEQUENCE"]
        assert index_name in objects["INDEX"]
        assert synonym_name in objects["SYNONYM"]

        trigger_ddl = workspace.get_object_definition("TRIGGER", trigger_name).upper()
        sequence_ddl = workspace.get_object_definition("SEQUENCE", sequence_name).upper()
        index_ddl = workspace.get_object_definition("INDEX", index_name).upper()
        synonym_ddl = workspace.get_object_definition("SYNONYM", synonym_name).upper()
        assert trigger_name in trigger_ddl
        assert trigger_ddl.endswith(";\n/")
        assert sequence_name in sequence_ddl
        assert sequence_ddl.endswith(";")
        assert index_name in index_ddl
        assert index_ddl.endswith(";")
        assert synonym_name in synonym_ddl
        assert synonym_ddl.endswith(";")
    finally:
        try:
            execute_ignoring_missing(workspace, f"drop synonym {synonym_name}")
            execute_ignoring_missing(workspace, f"drop trigger {trigger_name}")
            execute_ignoring_missing(workspace, f"drop sequence {sequence_name}")
            execute_ignoring_missing(workspace, f"drop index {index_name}")
            execute_ignoring_missing(workspace, f"drop table {table_name} purge")
        finally:
            workspace.close()


def object_name(suffix: str) -> str:
    return f"PLSQLWKS_TEST_{uuid.uuid4().hex[:16].upper()}_{suffix}"


def execute_ignoring_missing(workspace: OracleWorkspace, sql: str) -> None:
    if workspace.connection is None:
        return
    cursor = workspace.connection.cursor()
    try:
        try:
            cursor.execute(sql)
        except Exception as exc:
            text = str(exc)
            missing_object_errors = (
                "ORA-00942",
                "ORA-01418",
                "ORA-01434",
                "ORA-02289",
                "ORA-04043",
                "ORA-04080",
            )
            if not any(error in text for error in missing_object_errors):
                raise
    finally:
        cursor.close()
