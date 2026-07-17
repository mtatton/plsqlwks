from __future__ import annotations

from configparser import ConfigParser
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import os
import re
import time
import uuid

import oracledb
import pytest

from plsqlwks.config import load_config
from plsqlwks.db import (
    CellUpdateResult,
    ConcurrentEditError,
    LOB_DISPLAY_LIMIT,
    NULL_DISPLAY_TOKEN,
    OracleCompilationError,
    OracleExecutionError,
    OracleWorkspace,
    TruncatedLobValue,
)
from plsqlwks.sqlsplit import split_script, statement_at_cursor
from plsqlwks.ui import UIState
from plsqlwks.ui.db_operations import DatabaseOperations
from plsqlwks.ui.db_session import DatabaseSessionController
from plsqlwks.ui.db_worker import DatabaseWorker, DbWorkerFinished
from plsqlwks.ui.result_presenter import ResultPresenter
from plsqlwks.workspace import ensure_workspace
from tests.oracle_matrix import version_matches_target


pytestmark = [pytest.mark.integration, pytest.mark.oracle]


def worker_finished(handle, timeout: float = 30) -> DbWorkerFinished:
    assert handle.done.wait(timeout), f"Oracle worker command {handle.command_id} did not finish within {timeout}s"
    while True:
        event = handle.events.get_nowait()
        if isinstance(event, DbWorkerFinished):
            return event


def worker_result(worker: DatabaseWorker, task, timeout: float = 30):
    handle = worker.submit(task)
    event = worker_finished(handle, timeout)
    if event.error is not None:
        raise event.error
    return event.result


def test_oracle_server_matches_requested_target(record_testsuite_property):
    target = os.environ.get("PLSQLWKS_TEST_ORACLE_TARGET", "").strip().lower()
    if target and target not in {"19c", "26ai"}:
        pytest.fail(f"Unsupported PLSQLWKS_TEST_ORACLE_TARGET: {target!r}")

    worker = DatabaseWorker(OracleWorkspace(load_config()))
    try:
        version = worker_result(worker, lambda db, progress: str(db.ensure_connected().version))
        record_testsuite_property("oracle_server_version", version)
        record_testsuite_property("oracle_target", target or "unspecified")
        record_testsuite_property("oracledb_driver_version", oracledb.__version__)
        record_testsuite_property(
            "oracledb_driver_mode",
            "thin" if oracledb.is_thin_mode() else "thick",
        )

        if not target:
            return
        assert version_matches_target(target, version), (
            f"{target} job connected to Oracle {version}; server release did not match target"
        )
    finally:
        worker.shutdown(timeout=10)


def test_oracle_fresh_manual_workspace_transaction_visibility_and_typed_insert(tmp_path):
    config = load_config(workspace=tmp_path)
    assert config.config_file == tmp_path / "config.ini"
    assert not config.config_file.exists()
    assert config.autocommit is False
    ensure_workspace(config)

    generated = ConfigParser()
    with config.config_file.open(encoding="utf-8") as config_stream:
        generated.read_file(config_stream)
    assert generated.get("database", "autocommit") == "no"

    table_name = object_name("FRESH_TX")
    worker = DatabaseWorker(OracleWorkspace(config))
    observer = DatabaseWorker(OracleWorkspace(config))
    try:
        worker_result(worker, lambda db, progress: db.ensure_connected())
        worker_result(observer, lambda db, progress: db.ensure_connected())
        assert worker.session_state.autocommit is False
        assert observer.session_state.autocommit is False
        assert worker_result(
            worker,
            lambda db, progress: (db.autocommit, db.ensure_connected().autocommit),
        ) == (False, False)

        worker_result(
            worker,
            lambda db, progress: db.execute_statement(
                f"""
                create table {table_name} (
                  id number primary key,
                  label varchar2(100),
                  happened_at date,
                  recorded_at timestamp(6),
                  optional_text varchar2(100)
                )
                """
            ),
        )

        pending_insert = worker_result(
            worker,
            lambda db, progress: db.execute_statement(
                f"insert into {table_name} (id, label) values (99, 'rollback probe')"
            ),
        )
        assert pending_insert.message.endswith("; pending commit")
        assert worker.session_state.has_uncommitted_changes is True
        assert worker_result(
            worker,
            lambda db, progress: db.execute_statement(f"select count(*) from {table_name}"),
        ).rows == [["1"]]
        assert worker_result(
            observer,
            lambda db, progress: db.execute_statement(f"select count(*) from {table_name}"),
        ).rows == [["0"]]
        worker_result(worker, lambda db, progress: db.rollback())
        assert worker_result(
            worker,
            lambda db, progress: db.execute_statement(f"select count(*) from {table_name}"),
        ).rows == [["0"]]
        assert worker_result(
            observer,
            lambda db, progress: db.execute_statement(f"select count(*) from {table_name}"),
        ).rows == [["0"]]

        editable = worker_result(
            worker,
            lambda db, progress: db.execute_statement(f"select rowid, t.* from {table_name} t"),
        )
        assert editable.editable_context is not None
        inserted = worker_result(
            worker,
            lambda db, progress: db.insert_row_for_result(
                editable.editable_context,
                {
                    1: "1",
                    2: "Příliš žluťoučký kůň",
                    3: "2026-07-15 10:11:12",
                    4: "2026-07-15 10:11:12.123456",
                    5: NULL_DISPLAY_TOKEN,
                },
                len(editable.columns),
            ),
        )

        assert inserted.values[0]
        assert inserted.values[1:] == [
            Decimal("1"),
            "Příliš žluťoučký kůň",
            datetime(2026, 7, 15, 10, 11, 12),
            datetime(2026, 7, 15, 10, 11, 12, 123456),
            None,
        ]
        assert inserted.display_values[1:] == [
            "1",
            "Příliš žluťoučký kůň",
            "2026-07-15 10:11:12",
            "2026-07-15 10:11:12.123456",
            NULL_DISPLAY_TOKEN,
        ]
        assert worker.session_state.has_uncommitted_changes is True
        assert worker_result(
            observer,
            lambda db, progress: db.execute_statement(f"select count(*) from {table_name}"),
        ).rows == [["0"]]

        worker_result(worker, lambda db, progress: db.commit())
        assert worker_result(
            observer,
            lambda db, progress: db.execute_statement(f"select count(*) from {table_name}"),
        ).rows == [["1"]]
    finally:
        try:
            worker_result(
                worker,
                lambda db, progress: rollback_and_execute_ignoring_missing(
                    db,
                    f"drop table {table_name} purge",
                ),
            )
        finally:
            worker.shutdown(timeout=10)
            observer.shutdown(timeout=10)


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
        assert output.columns == []
        assert output.rows == []
        assert output.dbms_output == ["Příliš žluťoučký kůň"]

        query_output = workspace.execute_statement(
            """
            with function emit_output return varchar2 is
            begin
              dbms_output.put_line('output emitted while fetching');
              return 'query value';
            end;
            select emit_output as value from dual
            """,
            "SELECT DBMS_OUTPUT smoke",
        )
        assert query_output.columns == ["VALUE"]
        assert query_output.rows == [["query value"]]
        assert query_output.dbms_output == ["output emitted while fetching"]

        binds = workspace.execute_statement(
            'select :id as lower_id, :ID as upper_id, :"MixedCase" as quoted_value from dual',
            "Bind-name semantics",
            {"id": "shared", '"MixedCase"': "quoted"},
        )
        assert binds.rows == [["shared", "shared", "quoted"]]
    finally:
        workspace.close()


def test_oracle_worker_reconnect_closes_open_result_continuation():
    config = load_config()
    expected_rows = config.max_rows + 1
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
        continuation = result.continuation
        assert continuation is not None
        assert len(result.rows) == config.max_rows
        assert result.rows[0] == ["1"]

        def reconnect(db, progress):
            db.close()
            db.ensure_connected()

        worker_result(worker, reconnect)
        assert worker.session_state.connected is True
        with pytest.raises(RuntimeError, match="stale or no longer available"):
            worker_result(
                worker,
                lambda db, progress: db.fetch_more_rows(continuation, len(result.rows)),
            )
        assert worker_result(
            worker,
            lambda db, progress: db.execute_statement("select 1 from dual"),
        ).rows == [["1"]]
    finally:
        worker.shutdown()


def test_oracle_worker_reports_plsql_compile_diagnostics_and_recovers():
    procedure_name = object_name("DIAGNOSTICS")
    worker = DatabaseWorker(OracleWorkspace(load_config()))
    try:
        worker_result(worker, lambda db, progress: db.ensure_connected())
        worker_result(
            worker,
            lambda db, progress: db.execute_statement("alter session set plsql_warnings = 'ENABLE:ALL'"),
        )
        worker_result(
            worker,
            lambda db, progress: db.execute_statement("alter session set plsql_optimize_level = 2"),
        )

        warning_result = worker_result(
            worker,
            lambda db, progress: db.execute_statement(
                f"""
                create or replace procedure {procedure_name} as
                begin
                  return;
                  dbms_output.put_line('unreachable');
                end;
                """,
                "Compile warning probe",
            ),
        )
        assert warning_result.diagnostics
        assert all(diagnostic.severity == "WARNING" for diagnostic in warning_result.diagnostics)
        assert all(diagnostic.line > 0 and diagnostic.position > 0 for diagnostic in warning_result.diagnostics)
        assert any(re.match(r"PLW-\d{5}:", diagnostic.text) for diagnostic in warning_result.diagnostics)

        with pytest.raises(OracleExecutionError) as excinfo:
            worker_result(
                worker,
                lambda db, progress: db.execute_statement(
                    f"""
                    create or replace procedure {procedure_name} as
                    begin
                      plsqlwks_missing_subprogram;
                    end;
                    """,
                    "Compile error probe",
                ),
            )

        compilation_error = excinfo.value.original
        assert isinstance(compilation_error, OracleCompilationError)
        assert any(diagnostic.severity == "ERROR" for diagnostic in compilation_error.diagnostics)
        assert all(diagnostic.line > 0 and diagnostic.position > 0 for diagnostic in compilation_error.diagnostics)
        assert any(re.match(r"PLS-\d{5}:", diagnostic.text) for diagnostic in compilation_error.diagnostics)

        valid_result = worker_result(
            worker,
            lambda db, progress: db.execute_statement(
                f"create or replace procedure {procedure_name} as begin null; end;",
                "Compile recovery probe",
            ),
        )
        assert not any(diagnostic.severity == "ERROR" for diagnostic in valid_result.diagnostics)
        worker_result(
            worker,
            lambda db, progress: db.execute_statement(f"begin {procedure_name}; end;"),
        )
        assert worker_result(
            worker,
            lambda db, progress: db.execute_statement("select 1 from dual"),
        ).rows == [["1"]]
    finally:
        try:
            worker_result(
                worker,
                lambda db, progress: rollback_and_execute_ignoring_missing(
                    db,
                    f"drop procedure {procedure_name}",
                ),
            )
        finally:
            worker.shutdown(timeout=10)


def test_oracle_worker_pages_rows_and_dbms_output_and_closes_continuations():
    total_rows = 223
    config = replace(load_config(), max_rows=37, arraysize=11)
    worker = DatabaseWorker(OracleWorkspace(config))
    try:
        result = worker_result(
            worker,
            lambda db, progress: db.execute_statement(
                f"""
                with
                  function emit_value(p_value number) return number is
                  begin
                    dbms_output.put_line('PLSQLWKS_PAGE:' || to_char(p_value, 'FM9999990'));
                    return p_value;
                  end;
                select emit_value(level) as value
                from dual
                connect by level <= {total_rows}
                """,
                "Paged rows and output",
            ),
        )
        assert result.continuation is not None
        exhausted_token = result.continuation
        loaded_rows = list(result.rows)
        output_lines = list(result.dbms_output)
        continuation = result.continuation
        while continuation is not None:
            page = worker_result(
                worker,
                lambda db, progress, token=continuation, count=len(loaded_rows): db.fetch_more_rows(token, count),
            )
            loaded_rows.extend(page.rows)
            output_lines.extend(page.dbms_output)
            continuation = page.continuation

        assert loaded_rows == [[str(value)] for value in range(1, total_rows + 1)]
        assert len(output_lines) == total_rows
        assert [int(line.removeprefix("PLSQLWKS_PAGE:")) for line in output_lines] == list(
            range(1, total_rows + 1)
        )
        with pytest.raises(RuntimeError, match="stale or no longer available"):
            worker_result(
                worker,
                lambda db, progress: db.fetch_more_rows(exhausted_token, len(loaded_rows)),
            )

        abandoned = worker_result(
            worker,
            lambda db, progress: db.execute_statement(
                "select level as value from dual connect by level <= 100",
                "Explicit continuation close",
            ),
        )
        assert abandoned.continuation is not None
        worker_result(
            worker,
            lambda db, progress: db.close_result_continuation(abandoned.continuation),
        )
        worker_result(
            worker,
            lambda db, progress: db.close_result_continuation(abandoned.continuation),
        )
        with pytest.raises(RuntimeError, match="stale or no longer available"):
            worker_result(
                worker,
                lambda db, progress: db.fetch_more_rows(abandoned.continuation, len(abandoned.rows)),
            )
        assert worker_result(
            worker,
            lambda db, progress: db.execute_statement("select 1 from dual"),
        ).rows == [["1"]]
    finally:
        worker.shutdown(timeout=10)


def test_oracle_worker_cancels_running_database_call_and_recovers():
    table_name = object_name("CANCEL")
    config = replace(load_config(), autocommit=False)
    worker = DatabaseWorker(OracleWorkspace(config))
    observer = DatabaseWorker(OracleWorkspace(config))
    handle = None
    try:
        worker_result(
            worker,
            lambda db, progress: db.execute_statement(
                f"create table {table_name} (marker_id number primary key)"
            ),
        )
        worker_result(observer, lambda db, progress: db.ensure_connected())

        handle = worker.submit(
            lambda db, progress: db.execute_statement(
                f"""
                begin
                  insert into {table_name} values (1);
                  commit;
                  dbms_session.sleep(30);
                end;
                """,
                "Cancellation probe",
            )
        )
        marker_deadline = time.monotonic() + 15
        while True:
            marker_count = worker_result(
                observer,
                lambda db, progress: db.execute_statement(
                    f"select count(*) from {table_name} where marker_id = 1"
                ),
            )
            if marker_count.rows == [["1"]]:
                break
            assert time.monotonic() < marker_deadline, "Oracle cancellation marker was not visible within 15s"
            time.sleep(0.1)

        assert worker.cancel_current_operation(handle.command_id) is True
        cancelled = worker_finished(handle, timeout=15)
        assert isinstance(cancelled.error, OracleExecutionError)
        assert "ORA-01013" in str(cancelled.error)
        assert worker_result(
            worker,
            lambda db, progress: db.execute_statement("select 1 from dual"),
        ).rows == [["1"]]
    finally:
        if handle is not None and not handle.done.is_set():
            worker.cancel_current_operation(handle.command_id)
            handle.done.wait(15)
        try:
            worker_result(
                observer,
                lambda db, progress: rollback_and_execute_ignoring_missing(
                    db,
                    f"drop table {table_name} purge",
                ),
            )
        finally:
            observer.shutdown(timeout=10)
            worker.shutdown(timeout=10)


def test_oracle_read_only_bound_display_cursor_explain_is_transaction_neutral():
    config = replace(load_config(), autocommit=False, read_only=True)
    worker = DatabaseWorker(OracleWorkspace(config))

    def explain(db, progress):
        connection = db.ensure_connected()
        transaction_before = connection.transaction_in_progress
        plan = db.explain_statement(
            "select /* PLSQLWKS_E2E_BOUND_EXPLAIN */ :value as value from dual",
            "Read-only bound explain",
            {"value": Decimal("7")},
        )
        return transaction_before, connection.transaction_in_progress, db.has_uncommitted_changes, plan

    try:
        transaction_before, transaction_after, has_pending, plan = worker_result(worker, explain)
        assert transaction_before is False
        assert transaction_after is False
        assert has_pending is False
        assert plan.steps == []
        assert plan.raw_lines
        assert any("SELECT STATEMENT" in line.upper() for line in plan.raw_lines)
        assert worker_result(
            worker,
            lambda db, progress: db.execute_statement("select 1 from dual"),
        ).rows == [["1"]]
    finally:
        worker.shutdown(timeout=10)


def test_oracle_ui_reconnect_resolves_pending_transaction_before_replacing_session():
    config = load_config()
    table_name = object_name("RECONNECT_TX")
    worker = DatabaseWorker(OracleWorkspace(config))
    state = UIState(config=config, db=worker.session_state)
    answers = iter(["c", "r", "d", "d"])

    class Dialogs:
        def prompt(
            self,
            label: str,
            default: str = "",
            strip: bool = True,
        ) -> str:
            return next(answers)

    operations = DatabaseOperations(state, worker)
    presenter = ResultPresenter(state, operations, screen_width=lambda: 120)
    operations.set_result_handler(presenter.apply_db_operation_result)
    database = DatabaseSessionController(state, operations, Dialogs(), presenter)

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
        state.db = worker.session_state
        database.reconnect_database()
        operations.wait(timeout=30)
        assert execute(f"select count(*) from {table_name}").rows == [["1"]]

        execute(f"insert into {table_name} values (2)")
        state.db = worker.session_state
        database.reconnect_database()
        operations.wait(timeout=30)
        assert execute(f"select count(*) from {table_name}").rows == [["1"]]

        execute(f"insert into {table_name} values (3)")
        state.db = worker.session_state
        database.reconnect_database()
        operations.wait(timeout=30)
        assert execute(f"select count(*) from {table_name}").rows == [["1"]]

        def leave_dead_session(db, progress):
            db.ensure_connected().close()
            db.record_pending_unknown()

        worker_result(worker, leave_dead_session)
        state.db = worker.session_state
        database.reconnect_database()
        operations.wait(timeout=30)
        assert state.db.connected is True
        assert "warning: old session close failed" in state.status
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

        assert output.columns == []
        assert output.rows == []
        assert len(output.dbms_output) == 1
        assert len(output.dbms_output[0]) == len(expected)
        assert output.dbms_output[0].endswith("END")
        assert output.dbms_output[0] == expected
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
    config = load_config()
    first = DatabaseWorker(OracleWorkspace(config))
    second = DatabaseWorker(OracleWorkspace(config))
    table_name = object_name("LOCK")
    try:
        worker_result(first, lambda db, progress: db.ensure_connected())
        worker_result(second, lambda db, progress: db.ensure_connected())
        # This test needs the second session's update to be visible, regardless
        # of the user's persisted transaction-mode preference.
        worker_result(first, lambda db, progress: db.set_autocommit(True))
        worker_result(second, lambda db, progress: db.set_autocommit(True))
        worker_result(
            first,
            lambda db, progress: execute_ignoring_missing(db, f"drop table {table_name} purge"),
        )
        worker_result(
            first,
            lambda db, progress: db.execute_statement(
                f"create table {table_name} (id number primary key, name varchar2(100))"
            ),
        )
        worker_result(
            first,
            lambda db, progress: db.execute_statement(
                f"insert into {table_name} values (1, 'loaded')"
            ),
        )
        worker_result(first, lambda db, progress: db.set_autocommit(False))
        worker_result(
            first,
            lambda db, progress: db.execute_statement(
                f"insert into {table_name} values (2, 'earlier pending work')"
            ),
        )

        result = worker_result(
            first,
            lambda db, progress: db.execute_statement(
                f"select rowid, name from {table_name} where id = 1"
            ),
        )
        assert result.editable_context is not None
        rowid = result.rows[0][0]
        original = result.original_rows[0][1]

        worker_result(
            second,
            lambda db, progress: db.execute_statement(
                f"update {table_name} set name = 'concurrent' where id = 1"
            ),
        )

        with pytest.raises(ConcurrentEditError, match="changed or the row was deleted"):
            worker_result(
                first,
                lambda db, progress: db.update_cell_by_rowid(
                    result.editable_context,
                    rowid,
                    1,
                    original,
                    "overwritten",
                ),
            )

        assert first.session_state.has_uncommitted_changes is True
        assert worker_result(first, lambda db, progress: db.pending_rows_changed) == 1
        current = worker_result(
            first,
            lambda db, progress: db.execute_statement(f"select name from {table_name} where id = 1"),
        )
        assert current.rows == [["concurrent"]]
        assert worker_result(
            first,
            lambda db, progress: db.execute_statement(
                f"select count(*) from {table_name} where id = 2"
            ),
        ).rows == [["1"]]
        assert worker_result(
            second,
            lambda db, progress: db.execute_statement(
                f"select count(*) from {table_name} where id = 2"
            ),
        ).rows == [["0"]]

        worker_result(first, lambda db, progress: db.rollback())
        assert first.session_state.has_uncommitted_changes is False
        assert worker_result(
            first,
            lambda db, progress: db.execute_statement(
                f"select count(*) from {table_name} where id = 2"
            ),
        ).rows == [["0"]]
        assert worker_result(
            second,
            lambda db, progress: db.execute_statement(
                f"select count(*) from {table_name} where id = 2"
            ),
        ).rows == [["0"]]
    finally:
        # Release any lock held by the competing session before dropping the
        # shared table, including when an assertion fails mid-test.
        try:
            second.shutdown(timeout=10)
        finally:
            try:
                worker_result(
                    first,
                    lambda db, progress: rollback_and_execute_ignoring_missing(
                        db,
                        f"drop table {table_name} purge",
                    ),
                )
            finally:
                first.shutdown(timeout=10)


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
        sysdate_before = workspace.execute_statement("select sysdate from dual").original_rows[0][0]
        happened_at_sysdate = workspace.update_cell_by_rowid(
            result.editable_context,
            rowid,
            3,
            happened_at.value,
            "SYSDATE",
        )
        sysdate_after = workspace.execute_statement("select sysdate from dual").original_rows[0][0]
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
        assert isinstance(happened_at_sysdate.value, datetime)
        assert sysdate_before <= happened_at_sysdate.value <= sysdate_after
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


def test_oracle_worker_transaction_tracker_follows_session_control_and_failed_ddl_commits():
    config = load_config()
    worker = DatabaseWorker(OracleWorkspace(config))
    observer = DatabaseWorker(OracleWorkspace(config))
    table_name = object_name("TX")
    try:
        worker_result(worker, lambda db, progress: db.ensure_connected())
        worker_result(
            worker,
            lambda db, progress: execute_ignoring_missing(db, f"drop table {table_name} purge"),
        )
        worker_result(
            worker,
            lambda db, progress: db.execute_statement(
                f"create table {table_name} (id number primary key)"
            ),
        )
        worker_result(observer, lambda db, progress: db.ensure_connected())
        worker_result(worker, lambda db, progress: db.set_autocommit(False))

        worker_result(
            worker,
            lambda db, progress: db.execute_statement(f"insert into {table_name} values (1)"),
        )
        assert worker.session_state.has_uncommitted_changes is True

        worker_result(
            worker,
            lambda db, progress: db.execute_statement(
                "alter session set nls_date_format = 'YYYY-MM-DD'"
            ),
        )
        assert worker.session_state.has_uncommitted_changes is True
        worker_result(worker, lambda db, progress: db.rollback())
        assert worker_result(
            worker,
            lambda db, progress: db.execute_statement(f"select count(*) from {table_name}"),
        ).rows == [["0"]]

        worker_result(
            worker,
            lambda db, progress: db.execute_statement(f"insert into {table_name} values (2)"),
        )
        assert worker.session_state.has_uncommitted_changes is True
        with pytest.raises(OracleExecutionError, match="ORA-00955"):
            worker_result(
                worker,
                lambda db, progress: db.execute_statement(
                    f"create table {table_name} (other_id number)"
                ),
            )

        assert worker.session_state.has_uncommitted_changes is False
        assert worker_result(
            worker,
            lambda db, progress: db.execute_statement(f"select count(*) from {table_name}"),
        ).rows == [["1"]]
        assert worker_result(
            observer,
            lambda db, progress: db.execute_statement(f"select count(*) from {table_name}"),
        ).rows == [["1"]]
    finally:
        try:
            observer.shutdown(timeout=10)
        finally:
            try:
                worker_result(
                    worker,
                    lambda db, progress: rollback_and_execute_ignoring_missing(
                        db,
                        f"drop table {table_name} purge",
                    ),
                )
            finally:
                worker.shutdown(timeout=10)


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
    view_name = object_name("V")
    function_name = object_name("F")
    trigger_name = object_name("TR")
    sequence_name = object_name("S")
    index_name = object_name("I")
    synonym_name = object_name("Y")
    try:
        workspace.connect()
        execute_ignoring_missing(workspace, f"drop synonym {synonym_name}")
        execute_ignoring_missing(workspace, f"drop function {function_name}")
        execute_ignoring_missing(workspace, f"drop view {view_name}")
        execute_ignoring_missing(workspace, f"drop trigger {trigger_name}")
        execute_ignoring_missing(workspace, f"drop sequence {sequence_name}")
        execute_ignoring_missing(workspace, f"drop index {index_name}")
        execute_ignoring_missing(workspace, f"drop table {table_name} purge")

        workspace.execute_statement(f"create table {table_name} (id number)")
        workspace.execute_statement(f"create view {view_name} as select id from {table_name}")
        workspace.execute_statement(
            f"create or replace function {function_name} return number as begin return 1; end;"
        )
        workspace.execute_statement(f"create sequence {sequence_name}")
        workspace.execute_statement(f"create index {index_name} on {table_name} (id)")
        workspace.execute_statement(f"create synonym {synonym_name} for {table_name}")
        workspace.execute_statement(
            f"create or replace trigger {trigger_name} before insert on {table_name} begin null; end;"
        )

        objects = workspace.list_schema_objects()
        assert view_name in objects["VIEW"]
        assert function_name in objects["FUNCTION"]
        assert trigger_name in objects["TRIGGER"]
        assert sequence_name in objects["SEQUENCE"]
        assert index_name in objects["INDEX"]
        assert synonym_name in objects["SYNONYM"]

        view_ddl = workspace.get_object_definition("VIEW", view_name).upper()
        function_ddl = workspace.get_object_definition("FUNCTION", function_name).upper()
        trigger_ddl = workspace.get_object_definition("TRIGGER", trigger_name).upper()
        sequence_ddl = workspace.get_object_definition("SEQUENCE", sequence_name).upper()
        index_ddl = workspace.get_object_definition("INDEX", index_name).upper()
        synonym_ddl = workspace.get_object_definition("SYNONYM", synonym_name).upper()
        assert view_name in view_ddl
        assert view_ddl.endswith(";")
        assert function_name in function_ddl
        assert function_ddl.endswith(";\n/")
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
            execute_ignoring_missing(workspace, f"drop function {function_name}")
            execute_ignoring_missing(workspace, f"drop view {view_name}")
            execute_ignoring_missing(workspace, f"drop trigger {trigger_name}")
            execute_ignoring_missing(workspace, f"drop sequence {sequence_name}")
            execute_ignoring_missing(workspace, f"drop index {index_name}")
            execute_ignoring_missing(workspace, f"drop table {table_name} purge")
        finally:
            workspace.close()


def object_name(suffix: str) -> str:
    return f"PWT_{uuid.uuid4().hex[:12].upper()}_{suffix[:12]}"


def rollback_and_execute_ignoring_missing(workspace: OracleWorkspace, sql: str) -> None:
    if workspace.connection is None:
        return
    workspace.rollback()
    execute_ignoring_missing(workspace, sql)


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
