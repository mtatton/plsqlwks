from datetime import datetime
from decimal import Decimal
from pathlib import Path

import oracledb
import pytest

from plsqlwks.config import AppConfig
from plsqlwks.db import (
    NULL_DISPLAY_TOKEN,
    CellUpdateResult,
    ConcurrentEditError,
    EditableResultContext,
    EditOperationRollbackError,
    OracleWorkspace,
    QueryResult,
    ReadOnlyModeError,
    ResultColumnMetadata,
    RowInsertResult,
    SelectItem,
    SimpleSelect,
    TruncatedLobValue,
    build_editable_result_context,
    convert_edit_value,
    normalize_edit_value,
    parse_simple_select,
)
from plsqlwks.ui.results import INSERT_ROWID_MARKER, insert_draft_row, selected_editable_cell


def test_builds_editable_context_for_rowid_wildcard_select():
    context, reason = build_editable_result_context(
        "select rowid, t.* from decisions t",
        ["ROWID", "ID", "NAME"],
        ["ID", "NAME"],
    )

    assert reason == ""
    assert context == EditableResultContext("DECISIONS", 0, {1: "ID", 2: "NAME"})


def test_builds_editable_context_keeps_column_type_metadata():
    metadata = [
        ResultColumnMetadata(oracledb.DB_TYPE_ROWID),
        ResultColumnMetadata(oracledb.DB_TYPE_NUMBER, precision=10, scale=0),
        ResultColumnMetadata(oracledb.DB_TYPE_VARCHAR),
    ]

    context, reason = build_editable_result_context(
        "select rowid, t.* from decisions t",
        ["ROWID", "ID", "NAME"],
        ["ID", "NAME"],
        metadata,
    )

    assert reason == ""
    assert context == EditableResultContext(
        "DECISIONS",
        0,
        {1: "ID", 2: "NAME"},
        {1: metadata[1], 2: metadata[2]},
    )


def test_builds_editable_context_for_multiline_select_keyword_rowid_wildcard():
    context, reason = build_editable_result_context(
        "select\n  rowid,\n  t.*\nfrom decisions t\nwhere id = 1\norder by id desc",
        ["ROWID", "ID", "NAME"],
        ["ID", "NAME"],
    )

    assert reason == ""
    assert context == EditableResultContext("DECISIONS", 0, {1: "ID", 2: "NAME"})


@pytest.mark.parametrize(
    "statement",
    [
        "-- editable result\nselect rowid, t.* from decisions t",
        "/* editable result */\nselect rowid, t.* from decisions t",
    ],
)
def test_builds_editable_context_for_leading_comments_before_rowid_wildcard_select(statement):
    context, reason = build_editable_result_context(
        statement,
        ["ROWID", "ID", "NAME"],
        ["ID", "NAME"],
    )

    assert reason == ""
    assert context == EditableResultContext("DECISIONS", 0, {1: "ID", 2: "NAME"})


def test_builds_editable_context_for_aliased_rowid_and_column_select():
    context, reason = build_editable_result_context(
        "select t.rowid as rowid, t.name from decisions t",
        ["ROWID", "NAME"],
        ["ID", "NAME"],
    )

    assert reason == ""
    assert context == EditableResultContext("DECISIONS", 0, {1: "NAME"})


def test_builds_editable_context_for_jobs_query_with_qualified_columns():
    context, reason = build_editable_result_context(
        """select rowid,
active_flag, job_key,
job_name, contact, salary, jobs.comm, jobs.reason
from jobs
where active_flag = 'Y'""",
        ["ROWID", "ACTIVE_FLAG", "JOB_KEY", "JOB_NAME", "CONTACT", "SALARY", "COMM", "REASON"],
        ["ACTIVE_FLAG", "JOB_KEY", "JOB_NAME", "CONTACT", "SALARY", "COMM", "REASON"],
    )

    assert reason == ""
    assert context == EditableResultContext(
        "JOBS",
        0,
        {
            1: "ACTIVE_FLAG",
            2: "JOB_KEY",
            3: "JOB_NAME",
            4: "CONTACT",
            5: "SALARY",
            6: "COMM",
            7: "REASON",
        },
    )


def test_parse_simple_select_keeps_tail_keywords_out_of_table_alias():
    parsed, reason = parse_simple_select("select rowid, name from decisions where id = 1")

    assert reason == ""
    assert parsed == SimpleSelect(
        table_name="DECISIONS",
        alias=None,
        items=[SelectItem("rowid"), SelectItem("column", "NAME")],
    )


def test_parse_simple_select_allows_safe_offset_tail():
    parsed, reason = parse_simple_select("select rowid, name from decisions offset 10 rows fetch next 5 rows only")

    assert reason == ""
    assert parsed == SimpleSelect(
        table_name="DECISIONS",
        alias=None,
        items=[SelectItem("rowid"), SelectItem("column", "NAME")],
    )


def test_builds_editable_context_for_qualified_alias_with_where_tail():
    context, reason = build_editable_result_context(
        "select d.rowid as rowid, d.name from decisions d where d.id = 1",
        ["ROWID", "NAME"],
        ["ID", "NAME"],
    )

    assert reason == ""
    assert context == EditableResultContext("DECISIONS", 0, {1: "NAME"})


@pytest.mark.parametrize(
    "statement",
    [
        "select rowid, jobs.reason -- , from comment\nfrom jobs where active_flag = 'Y'",
        "select rowid, jobs.reason /* , from comment */ from jobs where active_flag = 'Y'",
    ],
)
def test_builds_editable_context_when_select_item_comments_contain_from_or_commas(statement):
    context, reason = build_editable_result_context(statement, ["ROWID", "REASON"], ["REASON"])

    assert reason == ""
    assert context == EditableResultContext("JOBS", 0, {1: "REASON"})


@pytest.mark.parametrize(
    "statement",
    [
        "select rowid, name from decisions where note = 'join select union group by'",
        "select rowid, name from decisions where note = q'[join select union group by]'",
        "select rowid, name from decisions where id = 1 -- join select\norder by name",
        "select rowid, name from decisions where note = 'start with connect by having minus intersect'",
        "select rowid, name from decisions where note = q'{pivot unpivot model partition sample versions}'",
        "select rowid, name from decisions where note = 'safe' /* pivot model partition sample versions */",
    ],
)
def test_parse_simple_select_allows_rejected_words_inside_literals_and_comments(statement):
    context, reason = build_editable_result_context(statement, ["ROWID", "NAME"], ["ID", "NAME"])

    assert reason == ""
    assert context == EditableResultContext("DECISIONS", 0, {1: "NAME"})


@pytest.mark.parametrize(
    ("statement", "columns", "reason_part"),
    [
        ("select name from decisions", ["NAME"], "ROWID"),
        (
            "select d.rowid as rowid, d.name from decisions d join projects p on p.id = d.project_id",
            ["ROWID", "NAME"],
            "Joins",
        ),
        ("select rowid, name from decisions group by rowid, name", ["ROWID", "NAME"], "Joins"),
        ("select rowid, upper(name) as name from decisions", ["ROWID", "NAME"], "Expressions"),
        ("select rowid, name from decisions for update", ["ROWID", "NAME"], "locking queries"),
        ("select rowid, name, name from decisions", ["ROWID", "NAME", "NAME"], "Duplicate"),
        ("select rowid, missing_column from decisions", ["ROWID", "MISSING_COLUMN"], "MISSING_COLUMN"),
    ],
)
def test_rejects_non_editable_select_shapes(statement, columns, reason_part):
    context, reason = build_editable_result_context(statement, columns, ["ID", "NAME"])

    assert context is None
    assert reason_part in reason


@pytest.mark.parametrize(
    ("statement", "reason_part"),
    [
        ("select rowid, name from (select * from decisions)", "Subquery"),
        ("select rowid, name from decisions as d", "Oracle table aliases"),
        ("select distinct rowid, name from decisions", "DISTINCT"),
        ("select rowid, name from decisions union select rowid, name from archive", "Joins"),
        ("select rowid, name from decisions where exists (select 1 from dual)", "Joins"),
    ],
)
def test_parse_simple_select_rejects_non_editable_shapes(statement, reason_part):
    parsed, reason = parse_simple_select(statement)

    assert parsed is None
    assert reason_part in reason


def test_builds_editable_context_for_quoted_mixed_case_identifiers():
    context, reason = build_editable_result_context(
        'select "t".rowid as rowid, "t"."Display ""Name" as "Shown" from "Mixed Table" "t"',
        ["ROWID", "Shown"],
        ['Display "Name'],
    )

    assert reason == ""
    assert context == EditableResultContext(
        "Mixed Table",
        0,
        {1: 'Display "Name'},
    )


def test_editable_context_keeps_case_distinct_quoted_columns_separate():
    context, reason = build_editable_result_context(
        'select rowid, "FOO", "Foo" from "CaseTable"',
        ["ROWID", "FOO", "Foo"],
        ["FOO", "Foo"],
    )

    assert reason == ""
    assert context == EditableResultContext(
        "CaseTable",
        0,
        {1: "FOO", 2: "Foo"},
    )


@pytest.mark.parametrize(
    "statement",
    [
        "select rowid, name from decisions partition (p2026)",
        "select rowid, name from decisions sample (10)",
        "select rowid, name from decisions pivot (count(*) for status in ('OPEN'))",
        "select rowid, name from decisions unpivot (value for col in (name))",
        "select rowid, name from decisions model dimension by (id) measures (name) rules (name[1] = 'x')",
        "select rowid, name from decisions versions between timestamp systimestamp - 1 and systimestamp",
        "select rowid, name from decisions match_recognize (measures first(rowid) as rowid pattern (x) define x as 1 = 1)",
        "select rowid, name from decisions /* remote */ @remote",
    ],
)
def test_parse_simple_select_rejects_oracle_table_and_flashback_clauses(statement):
    parsed, reason = parse_simple_select(statement)

    assert parsed is None
    assert "not editable" in reason


@pytest.mark.parametrize(
    ("statement", "columns", "table_columns", "reason_part"),
    [
        ("select rowid, * from decisions", ["ROWID", "NAME"], ["ID", "NAME"], "Wildcard result columns"),
        ("select rowid, projects.name from decisions d", ["ROWID", "NAME"], ["ID", "NAME"], "Qualified columns"),
        ("select rowid, name from decisions", ["ROWID", "NAME", "EXTRA"], ["ID", "NAME"], "Result columns"),
        ("select rowid, name from decisions", ["ROWID", "NAME"], [], "NAME is not a column"),
    ],
)
def test_rejects_metadata_mismatches_for_editable_context(statement, columns, table_columns, reason_part):
    context, reason = build_editable_result_context(statement, columns, table_columns)

    assert context is None
    assert reason_part in reason


def test_normalize_edit_value_uses_explicit_database_null_token():
    assert normalize_edit_value(NULL_DISPLAY_TOKEN) is None
    assert normalize_edit_value(f"  {NULL_DISPLAY_TOKEN}  ") is None
    assert normalize_edit_value("NULL") == "NULL"
    assert normalize_edit_value("null") == "null"
    assert normalize_edit_value("Příliš žluťoučký kůň") == "Příliš žluťoučký kůň"


@pytest.mark.parametrize(
    ("type_code", "text", "expected"),
    [
        (oracledb.DB_TYPE_NUMBER, "10.50", Decimal("10.50")),
        (oracledb.DB_TYPE_DATE, "2026-07-11 14:30:05", datetime(2026, 7, 11, 14, 30, 5)),
        (
            oracledb.DB_TYPE_TIMESTAMP,
            "2026-07-11T14:30:05.123456",
            datetime(2026, 7, 11, 14, 30, 5, 123456),
        ),
        (oracledb.DB_TYPE_RAW, "00ff10", b"\x00\xff\x10"),
        (oracledb.DB_TYPE_BLOB, "00ff10", b"\x00\xff\x10"),
        (oracledb.DB_TYPE_BOOLEAN, "true", True),
        (oracledb.DB_TYPE_CLOB, "  text  ", "  text  "),
    ],
)
def test_convert_edit_value_uses_nls_independent_python_types(type_code, text, expected):
    assert convert_edit_value(text, ResultColumnMetadata(type_code, scale=6)) == expected


@pytest.mark.parametrize(
    ("metadata", "text", "message"),
    [
        (ResultColumnMetadata(oracledb.DB_TYPE_NUMBER), "ten", "decimal notation"),
        (ResultColumnMetadata(oracledb.DB_TYPE_DATE), "11-JUL-26", "ISO format"),
        (ResultColumnMetadata(oracledb.DB_TYPE_DATE), "2026-07-11 10:00:00.1", "fractional"),
        (ResultColumnMetadata(oracledb.DB_TYPE_BLOB), "not-hex", "hexadecimal"),
        (ResultColumnMetadata(oracledb.DB_TYPE_TIMESTAMP_TZ), "2026-07-11T10:00:00+02:00", "time-zone"),
        (ResultColumnMetadata(oracledb.DB_TYPE_TIMESTAMP, scale=9), "2026-07-11", "precision above 6"),
    ],
)
def test_convert_edit_value_rejects_lossy_or_invalid_input(metadata, text, message):
    with pytest.raises(ValueError, match=message):
        convert_edit_value(text, metadata)


def test_insert_draft_uses_explicit_database_null_token():
    result = QueryResult(
        "data",
        ["ROWID", "NAME"],
        [],
        "0 rows",
        editable_context=EditableResultContext("DECISIONS", 0, {1: "NAME"}),
    )

    assert insert_draft_row(result) == [INSERT_ROWID_MARKER, NULL_DISPLAY_TOKEN]


def test_selected_editable_cell_distinguishes_null_token_from_literal_null_rowid():
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"})
    null_result = QueryResult("data", ["ROWID", "NAME"], [[NULL_DISPLAY_TOKEN, "old"]], "1 row", context)
    literal_result = QueryResult("data", ["ROWID", "NAME"], [["NULL", "old"]], "1 row", context)

    cell, reason = selected_editable_cell(null_result, 0, 1)
    assert cell is None
    assert reason == "Selected row has no ROWID"

    cell, reason = selected_editable_cell(literal_result, 0, 1)
    assert reason == ""
    assert cell is not None
    assert cell.rowid == "NULL"


def test_selected_editable_cell_rejects_truncated_lob_and_lossy_timestamp_types():
    clob_context = EditableResultContext(
        "DECISIONS",
        0,
        {1: "NAME"},
        {1: ResultColumnMetadata(oracledb.DB_TYPE_CLOB)},
    )
    clob_result = QueryResult(
        "data",
        ["ROWID", "NAME"],
        [["AAABBBCCC", "prefix… <CLOB truncated>"]],
        "1 row",
        editable_context=clob_context,
        original_rows=[["AAABBBCCC", TruncatedLobValue("CLOB", 100_000)]],
    )

    cell, reason = selected_editable_cell(clob_result, 0, 1)
    assert cell is None
    assert reason == "CLOB value is truncated and cannot be safely edited"

    timestamp_context = EditableResultContext(
        "DECISIONS",
        0,
        {1: "NAME"},
        {1: ResultColumnMetadata(oracledb.DB_TYPE_TIMESTAMP_TZ, scale=6)},
    )
    timestamp_result = QueryResult(
        "data",
        ["ROWID", "NAME"],
        [["AAABBBCCC", "2026-07-11 10:00:00 +02:00"]],
        "1 row",
        editable_context=timestamp_context,
    )

    cell, reason = selected_editable_cell(timestamp_result, 0, 1)
    assert cell is None
    assert "cannot yet be edited without losing time-zone information" in reason


def test_update_cell_by_rowid_validates_binds_commits_and_refreshes_value():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(refreshed_value="Žluťoučký")
    workspace.connection = connection
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"})

    refreshed = workspace.update_cell_by_rowid(context, "AAABBBCCC", 1, "old", "Příliš")

    assert refreshed == CellUpdateResult("Žluťoučký", "Žluťoučký")
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.autocommit is True
    savepoint_sql, savepoint_params = connection.statements[2]
    assert savepoint_sql.startswith("savepoint PLSQLWKS_EDIT_")
    assert savepoint_params == {}
    update_sql, update_params = connection.statements[3]
    assert update_sql == (
        'update "DECISIONS" set "NAME" = :new_value '
        'where rowid = chartorowid(:target_rowid) and "NAME" = :original_value'
    )
    assert update_params == {
        "new_value": "Příliš",
        "target_rowid": "AAABBBCCC",
        "original_value": "old",
    }


def test_update_cell_by_rowid_quotes_exact_mixed_case_identifiers():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(
        refreshed_value="new",
        table_columns=['Display "Name'],
    )
    workspace.connection = connection
    context = EditableResultContext(
        "Mixed Table",
        0,
        {1: 'Display "Name'},
    )

    workspace.update_cell_by_rowid(context, "AAABBBCCC", 1, "old", "new")

    update_sql, _params = next(
        (sql, params) for sql, params in connection.statements if sql.startswith('update "Mixed Table"')
    )
    assert update_sql == (
        'update "Mixed Table" set "Display ""Name" = :new_value '
        "where rowid = chartorowid(:target_rowid) "
        'and "Display ""Name" = :original_value'
    )


def test_update_cell_by_rowid_binds_typed_number_and_original_value():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(refreshed_value=Decimal("10.50"))
    workspace.connection = connection
    metadata = ResultColumnMetadata(oracledb.DB_TYPE_NUMBER, precision=10, scale=2)
    context = EditableResultContext("DECISIONS", 0, {1: "ID"}, {1: metadata})

    refreshed = workspace.update_cell_by_rowid(
        context,
        "AAABBBCCC",
        1,
        Decimal("7"),
        "10.50",
    )

    update_sql, update_params = next(
        (sql, params) for sql, params in connection.statements if sql.startswith('update "DECISIONS"')
    )
    assert update_sql.endswith('and "ID" = :original_value')
    assert update_params == {
        "new_value": Decimal("10.50"),
        "target_rowid": "AAABBBCCC",
        "original_value": Decimal("7"),
    }
    assert connection.input_sizes == [
        {
            "target_rowid": oracledb.DB_TYPE_VARCHAR,
            "new_value": oracledb.DB_TYPE_NUMBER,
            "original_value": oracledb.DB_TYPE_NUMBER,
        }
    ]
    assert refreshed == CellUpdateResult(Decimal("10.50"), "10.50")


@pytest.mark.parametrize(
    "type_code",
    [oracledb.DB_TYPE_DATE, oracledb.DB_TYPE_TIMESTAMP],
)
def test_update_cell_by_rowid_uses_fixed_sysdate_expression_for_datetime_types(type_code):
    workspace = OracleWorkspace(make_config())
    original_value = datetime(2026, 7, 11, 14, 30, 5)
    connection = FakeConnection(refreshed_value=original_value)
    workspace.connection = connection
    metadata = ResultColumnMetadata(type_code, scale=6)
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"}, {1: metadata})

    workspace.update_cell_by_rowid(
        context,
        "AAABBBCCC",
        1,
        original_value,
        "  SySdAtE\t",
    )

    update_sql, update_params = next(
        (sql, params) for sql, params in connection.statements if sql.startswith('update "DECISIONS"')
    )
    assert update_sql == (
        'update "DECISIONS" set "NAME" = sysdate where rowid = chartorowid(:target_rowid) and "NAME" = :original_value'
    )
    assert update_params == {
        "target_rowid": "AAABBBCCC",
        "original_value": original_value,
    }
    assert connection.input_sizes == [
        {
            "target_rowid": oracledb.DB_TYPE_VARCHAR,
            "original_value": type_code,
        }
    ]


def test_update_cell_by_rowid_keeps_sysdate_literal_for_text_columns():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(refreshed_value="  SySdAtE  ")
    workspace.connection = connection
    metadata = ResultColumnMetadata(oracledb.DB_TYPE_VARCHAR)
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"}, {1: metadata})

    workspace.update_cell_by_rowid(context, "AAABBBCCC", 1, "old", "  SySdAtE  ")

    update_sql, update_params = next(
        (sql, params) for sql, params in connection.statements if sql.startswith('update "DECISIONS"')
    )
    assert 'set "NAME" = :new_value' in update_sql
    assert update_params["new_value"] == "  SySdAtE  "
    assert connection.input_sizes == [
        {
            "target_rowid": oracledb.DB_TYPE_VARCHAR,
            "new_value": oracledb.DB_TYPE_VARCHAR,
            "original_value": oracledb.DB_TYPE_VARCHAR,
        }
    ]


def test_update_cell_by_rowid_never_interpolates_non_exact_sysdate_input():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection()
    workspace.connection = connection
    metadata = ResultColumnMetadata(oracledb.DB_TYPE_DATE)
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"}, {1: metadata})

    with pytest.raises(ValueError, match="ISO format"):
        workspace.update_cell_by_rowid(
            context,
            "AAABBBCCC",
            1,
            datetime(2026, 7, 11),
            "sysdate + 1",
        )

    assert not any(sql.startswith("update ") for sql, _ in connection.statements)


def test_update_cell_by_rowid_uses_null_and_lob_optimistic_predicates():
    workspace = OracleWorkspace(make_config())
    null_connection = FakeConnection(refreshed_value="new")
    workspace.connection = null_connection
    text_metadata = ResultColumnMetadata(oracledb.DB_TYPE_VARCHAR)
    text_context = EditableResultContext("DECISIONS", 0, {1: "NAME"}, {1: text_metadata})

    workspace.update_cell_by_rowid(text_context, "AAABBBCCC", 1, None, "new")

    null_sql, null_params = next(
        (sql, params) for sql, params in null_connection.statements if sql.startswith('update "DECISIONS"')
    )
    assert null_sql.endswith('and "NAME" is null')
    assert "original_value" not in null_params

    lob_connection = FakeConnection(refreshed_value="new clob")
    workspace.connection = lob_connection
    lob_metadata = ResultColumnMetadata(oracledb.DB_TYPE_CLOB)
    lob_context = EditableResultContext("DECISIONS", 0, {1: "NAME"}, {1: lob_metadata})

    workspace.update_cell_by_rowid(lob_context, "AAABBBCCC", 1, "old clob", "new clob")

    lob_sql, lob_params = next(
        (sql, params) for sql, params in lob_connection.statements if sql.startswith('update "DECISIONS"')
    )
    assert lob_sql.endswith('and dbms_lob.compare("NAME", :original_value) = 0')
    assert lob_params["original_value"] == "old clob"
    assert lob_connection.input_sizes == [
        {
            "target_rowid": oracledb.DB_TYPE_VARCHAR,
            "new_value": oracledb.DB_TYPE_CLOB,
            "original_value": oracledb.DB_TYPE_CLOB,
        }
    ]


def test_update_cell_by_rowid_rejects_truncated_lob_before_dml():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection()
    workspace.connection = connection
    metadata = ResultColumnMetadata(oracledb.DB_TYPE_CLOB)
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"}, {1: metadata})

    with pytest.raises(ValueError, match="truncated.*cannot be safely edited"):
        workspace.update_cell_by_rowid(
            context,
            "AAABBBCCC",
            1,
            TruncatedLobValue("CLOB", 100_000),
            "replacement",
        )

    assert not any(sql.startswith("update ") for sql, _ in connection.statements)


def test_update_cell_by_rowid_preserves_literal_null_and_formats_database_null():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(refreshed_value=None)
    workspace.connection = connection
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"})

    refreshed = workspace.update_cell_by_rowid(context, "AAABBBCCC", 1, "old", "NULL")

    update_sql, update_params = next(
        (sql, params) for sql, params in connection.statements if sql.startswith('update "DECISIONS"')
    )
    assert update_sql.startswith('update "DECISIONS"')
    assert update_params["new_value"] == "NULL"
    assert refreshed == CellUpdateResult(None, NULL_DISPLAY_TOKEN)


def test_update_cell_by_rowid_manual_mode_leaves_update_uncommitted():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(refreshed_value="Žluťoučký")
    workspace.connection = connection
    workspace.set_autocommit(False)
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"})

    refreshed = workspace.update_cell_by_rowid(context, "AAABBBCCC", 1, "old", "Příliš")

    assert refreshed == CellUpdateResult("Žluťoučký", "Žluťoučký")
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert workspace.pending_rows_changed == 1
    assert workspace.pending_unknown_changes is False
    assert workspace.has_uncommitted_changes is True


def test_update_cell_by_rowid_rolls_back_only_its_savepoint_on_concurrent_change():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(update_rowcount=0)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(3)
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"})

    with pytest.raises(ConcurrentEditError, match="changed or the row was deleted"):
        workspace.update_cell_by_rowid(context, "AAABBBCCC", 1, "old", "Příliš")

    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert any(sql.startswith("rollback to savepoint PLSQLWKS_EDIT_") for sql, _ in connection.statements)
    assert workspace.pending_rows_changed == 3
    assert workspace.pending_unknown_changes is False


def test_update_cell_by_rowid_surfaces_savepoint_rollback_failure_and_marks_prior_work_unknown():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(update_rowcount=0, savepoint_rollback_raises=True)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(3)
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"})

    with pytest.raises(EditOperationRollbackError, match="changed or the row was deleted") as excinfo:
        workspace.update_cell_by_rowid(context, "AAABBBCCC", 1, "old", "Příliš")

    assert isinstance(excinfo.value.original, ConcurrentEditError)
    assert str(excinfo.value.savepoint_rollback_error) == "savepoint rollback failed"
    assert "full rollback was not attempted to preserve prior work" in str(excinfo.value)
    assert excinfo.value.full_rollback_attempted is False
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert connection.savepoint_rollbacks == 1
    assert workspace.pending_rows_changed == 0
    assert workspace.pending_unknown_changes is True
    assert workspace.has_uncommitted_changes is True


def test_autocommit_edit_uses_safe_full_rollback_after_savepoint_rollback_failure():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(
        refresh_row_exists=False,
        savepoint_rollback_raises=True,
        transaction_state=False,
    )
    workspace.connection = connection
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"})

    with pytest.raises(EditOperationRollbackError, match="full rollback succeeded") as excinfo:
        workspace.update_cell_by_rowid(context, "AAABBBCCC", 1, "old", "Příliš")

    assert excinfo.value.full_rollback_attempted is True
    assert excinfo.value.full_rollback_succeeded is True
    assert excinfo.value.transaction_may_have_changes is False
    assert connection.rollbacks == 1
    assert connection.autocommit is True
    assert workspace.has_uncommitted_changes is False


def test_autocommit_edit_tracks_unknown_change_when_both_rollbacks_fail():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(
        refresh_row_exists=False,
        savepoint_rollback_raises=True,
        full_rollback_raises=True,
        transaction_state=False,
    )
    workspace.connection = connection
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"})

    with pytest.raises(EditOperationRollbackError, match="full rollback also failed") as excinfo:
        workspace.update_cell_by_rowid(context, "AAABBBCCC", 1, "old", "Příliš")

    assert excinfo.value.full_rollback_attempted is True
    assert excinfo.value.full_rollback_succeeded is False
    assert isinstance(excinfo.value.full_rollback_error, RuntimeError)
    assert connection.rollbacks == 1
    assert connection.autocommit is True
    assert workspace.pending_rows_changed == 0
    assert workspace.pending_unknown_changes is True


def test_update_cell_by_rowid_reports_missing_refresh_row():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(refresh_row_exists=False)
    workspace.connection = connection
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"})

    with pytest.raises(ValueError, match="Updated row could not be refreshed"):
        workspace.update_cell_by_rowid(context, "AAABBBCCC", 1, "old", "Příliš")

    assert connection.commits == 0
    assert any(sql.startswith("rollback to savepoint PLSQLWKS_EDIT_") for sql, _ in connection.statements)


def test_update_refresh_failure_preserves_earlier_manual_changes():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(refresh_row_exists=False)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(2)
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"})

    with pytest.raises(ValueError, match="Updated row could not be refreshed"):
        workspace.update_cell_by_rowid(context, "AAABBBCCC", 1, "old", "Příliš")

    assert connection.rollbacks == 0
    assert connection.savepoint_rollbacks == 1
    assert workspace.pending_rows_changed == 2


def test_update_cell_by_rowid_rejects_missing_rowid_before_update():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection()
    workspace.connection = connection
    context = EditableResultContext("DECISIONS", 0, {1: "NAME"})

    with pytest.raises(ValueError, match="Selected row has no ROWID"):
        workspace.update_cell_by_rowid(context, NULL_DISPLAY_TOKEN, 1, "old", "Příliš")

    assert not any(sql.startswith("update ") for sql, _params in connection.statements)
    assert connection.commits == 0


def test_insert_row_for_result_validates_binds_commits_and_refreshes_row():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(inserted_row=("AAANEW", 7, "Příliš"))
    workspace.connection = connection
    context = EditableResultContext("DECISIONS", 0, {1: "ID", 2: "NAME"})

    row = workspace.insert_row_for_result(context, {1: "7", 2: "Příliš"}, 3)

    assert row == RowInsertResult(
        ["AAANEW", 7, "Příliš"],
        ["AAANEW", "7", "Příliš"],
    )
    assert connection.statements[2][0].startswith("savepoint PLSQLWKS_EDIT_")
    insert_sql, insert_params = connection.statements[3]
    assert insert_sql == (
        'insert into "DECISIONS" ("ID", "NAME") values (:value_0, :value_1) returning rowid into :new_rowid'
    )
    assert insert_params["value_0"] == "7"
    assert insert_params["value_1"] == "Příliš"
    assert "new_rowid" in insert_params
    refresh_sql, refresh_params = connection.statements[4]
    assert refresh_sql == ('select rowid, "ID", "NAME" from "DECISIONS" where rowid = chartorowid(:new_rowid)')
    assert refresh_params == {"new_rowid": "AAANEW"}
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_insert_row_for_result_binds_typed_number_and_date_values():
    workspace = OracleWorkspace(make_config())
    inserted_at = datetime(2026, 7, 11, 14, 30, 5)
    connection = FakeConnection(inserted_row=("AAANEW", Decimal("7"), inserted_at))
    workspace.connection = connection
    context = EditableResultContext(
        "DECISIONS",
        0,
        {1: "ID", 2: "NAME"},
        {
            1: ResultColumnMetadata(oracledb.DB_TYPE_NUMBER),
            2: ResultColumnMetadata(oracledb.DB_TYPE_DATE),
        },
    )

    row = workspace.insert_row_for_result(
        context,
        {1: "7", 2: "2026-07-11 14:30:05"},
        3,
    )

    _insert_sql, insert_params = next(
        (sql, params) for sql, params in connection.statements if sql.startswith('insert into "DECISIONS"')
    )
    assert insert_params["value_0"] == Decimal("7")
    assert insert_params["value_1"] == inserted_at
    assert connection.input_sizes == [
        {
            "value_0": oracledb.DB_TYPE_NUMBER,
            "value_1": oracledb.DB_TYPE_DATE,
        }
    ]
    assert row == RowInsertResult(
        ["AAANEW", Decimal("7"), inserted_at],
        ["AAANEW", "7", "2026-07-11 14:30:05"],
    )


@pytest.mark.parametrize(
    "type_code",
    [oracledb.DB_TYPE_DATE, oracledb.DB_TYPE_TIMESTAMP],
)
def test_insert_row_for_result_uses_fixed_sysdate_expression_for_datetime_types(type_code):
    workspace = OracleWorkspace(make_config())
    inserted_at = datetime(2026, 7, 11, 14, 30, 5)
    connection = FakeConnection(inserted_row=("AAANEW", Decimal("7"), inserted_at))
    workspace.connection = connection
    context = EditableResultContext(
        "DECISIONS",
        0,
        {1: "ID", 2: "NAME"},
        {
            1: ResultColumnMetadata(oracledb.DB_TYPE_NUMBER),
            2: ResultColumnMetadata(type_code, scale=6),
        },
    )

    workspace.insert_row_for_result(context, {1: "7", 2: "  SySdAtE  "}, 3)

    insert_sql, insert_params = next(
        (sql, params) for sql, params in connection.statements if sql.startswith('insert into "DECISIONS"')
    )
    assert insert_sql == (
        'insert into "DECISIONS" ("ID", "NAME") values (:value_0, sysdate) returning rowid into :new_rowid'
    )
    assert insert_params["value_0"] == Decimal("7")
    assert "value_1" not in insert_params
    assert "new_rowid" in insert_params
    assert connection.input_sizes == [{"value_0": oracledb.DB_TYPE_NUMBER}]


def test_insert_row_for_result_manual_mode_leaves_insert_uncommitted():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(inserted_row=("AAANEW", 7, "manual"))
    workspace.connection = connection
    workspace.set_autocommit(False)
    context = EditableResultContext("DECISIONS", 0, {1: "ID", 2: "NAME"})

    row = workspace.insert_row_for_result(context, {1: "7", 2: "manual"}, 3)

    assert row == RowInsertResult(
        ["AAANEW", 7, "manual"],
        ["AAANEW", "7", "manual"],
    )
    assert connection.commits == 0
    assert workspace.pending_rows_changed == 1


def test_insert_row_for_result_preserves_literal_null_and_defaults_missing_values_to_database_null():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(inserted_row=("AAANEW", "NULL", None))
    workspace.connection = connection
    context = EditableResultContext("DECISIONS", 0, {1: "ID", 2: "NAME"})

    row = workspace.insert_row_for_result(context, {1: "NULL"}, 3)

    _insert_sql, insert_params = next(
        (sql, params) for sql, params in connection.statements if sql.startswith('insert into "DECISIONS"')
    )
    assert insert_params["value_0"] == "NULL"
    assert insert_params["value_1"] is None
    assert row == RowInsertResult(
        ["AAANEW", "NULL", None],
        ["AAANEW", "NULL", NULL_DISPLAY_TOKEN],
    )


def test_insert_row_for_result_rolls_back_only_its_savepoint_when_no_single_row_is_inserted():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(insert_rowcount=0)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(3)
    context = EditableResultContext("DECISIONS", 0, {1: "ID", 2: "NAME"})

    with pytest.raises(ValueError, match="Expected to insert 1 row"):
        workspace.insert_row_for_result(context, {1: "7", 2: "new"}, 3)

    assert connection.rollbacks == 0
    assert any(sql.startswith("rollback to savepoint PLSQLWKS_EDIT_") for sql, _ in connection.statements)
    assert workspace.pending_rows_changed == 3


def test_insert_row_for_result_reports_missing_refresh_row():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(insert_refresh_row_exists=False)
    workspace.connection = connection
    context = EditableResultContext("DECISIONS", 0, {1: "ID", 2: "NAME"})

    with pytest.raises(ValueError, match="Inserted row could not be refreshed"):
        workspace.insert_row_for_result(context, {1: "7", 2: "new"}, 3)

    assert connection.commits == 0
    assert any(sql.startswith("rollback to savepoint PLSQLWKS_EDIT_") for sql, _ in connection.statements)


def test_insert_refresh_failure_preserves_earlier_manual_changes():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(insert_refresh_row_exists=False)
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(2)
    context = EditableResultContext("DECISIONS", 0, {1: "ID", 2: "NAME"})

    with pytest.raises(ValueError, match="Inserted row could not be refreshed"):
        workspace.insert_row_for_result(context, {1: "7", 2: "new"}, 3)

    assert connection.rollbacks == 0
    assert connection.savepoint_rollbacks == 1
    assert workspace.pending_rows_changed == 2


def test_insert_refresh_and_savepoint_rollback_failures_mark_prior_work_unknown():
    workspace = OracleWorkspace(make_config())
    connection = FakeConnection(
        insert_refresh_row_exists=False,
        savepoint_rollback_raises=True,
    )
    workspace.connection = connection
    workspace.set_autocommit(False)
    workspace.record_pending_rows(2)
    context = EditableResultContext("DECISIONS", 0, {1: "ID", 2: "NAME"})

    with pytest.raises(EditOperationRollbackError, match="Inserted row could not be refreshed"):
        workspace.insert_row_for_result(context, {1: "7", 2: "new"}, 3)

    assert connection.rollbacks == 0
    assert connection.savepoint_rollbacks == 1
    assert workspace.pending_rows_changed == 0
    assert workspace.pending_unknown_changes is True


def test_insert_row_for_result_rejects_read_only_mode():
    workspace = OracleWorkspace(
        AppConfig(
            user="hr",
            dsn="db",
            password_file=Path("/tmp/orapass"),
            workspace_dir=Path("/tmp/plsqlwks-tests"),
            read_only=True,
        )
    )
    workspace.connection = FakeConnection()
    context = EditableResultContext("DECISIONS", 0, {1: "ID", 2: "NAME"})

    with pytest.raises(ReadOnlyModeError, match="Row inserts are disabled"):
        workspace.insert_row_for_result(context, {1: "7", 2: "new"}, 3)


def make_config() -> AppConfig:
    return AppConfig(
        user="hr",
        dsn="db",
        password_file=Path("/tmp/orapass"),
        workspace_dir=Path("/tmp/plsqlwks-tests"),
        autocommit=True,
    )


class FakeConnection:
    def __init__(
        self,
        update_rowcount: int = 1,
        refreshed_value: str = "updated",
        refresh_row_exists: bool = True,
        savepoint_rollback_raises: bool = False,
        full_rollback_raises: bool = False,
        transaction_state: bool | Exception | None = None,
        insert_rowcount: int = 1,
        inserted_rowid: str = "AAANEW",
        inserted_row: tuple[object, ...] = ("AAANEW", 1, "inserted"),
        insert_refresh_row_exists: bool = True,
        table_columns: list[str] | None = None,
        object_counts: tuple[int, int] = (1, 1),
    ):
        self.update_rowcount = update_rowcount
        self.refreshed_value = refreshed_value
        self.refresh_row_exists = refresh_row_exists
        self.savepoint_rollback_raises = savepoint_rollback_raises
        self.full_rollback_raises = full_rollback_raises
        self.transaction_state = transaction_state
        self.insert_rowcount = insert_rowcount
        self.inserted_rowid = inserted_rowid
        self.inserted_row = inserted_row
        self.insert_refresh_row_exists = insert_refresh_row_exists
        self.table_columns = table_columns or ["ID", "NAME"]
        self.object_counts = object_counts
        self.statements: list[tuple[str, dict[str, object]]] = []
        self.input_sizes: list[dict[str, object]] = []
        self.commits = 0
        self.rollbacks = 0
        self.savepoint_rollbacks = 0
        self.autocommit = True

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        if self.transaction_state is not None and not isinstance(self.transaction_state, Exception):
            self.transaction_state = False

    def rollback(self):
        self.rollbacks += 1
        if self.full_rollback_raises:
            raise RuntimeError("full rollback failed")
        if self.transaction_state is not None and not isinstance(self.transaction_state, Exception):
            self.transaction_state = False

    @property
    def transaction_in_progress(self):
        if isinstance(self.transaction_state, Exception):
            raise self.transaction_state
        return self.transaction_state


class FakeCursor:
    def __init__(self, connection: FakeConnection):
        self.connection = connection
        self.rows: list[tuple[object, ...]] = []
        self.rowcount = -1

    def execute(self, sql: str, **params):
        normalized_sql = " ".join(sql.split())
        self.connection.statements.append((normalized_sql, params))
        lowered = normalized_sql.lower().replace('"', "")
        if lowered.startswith("rollback to savepoint "):
            self.connection.savepoint_rollbacks += 1
            if self.connection.savepoint_rollback_raises:
                raise RuntimeError("savepoint rollback failed")
            self.rows = []
        elif "from user_objects" in lowered:
            self.rows = [self.connection.object_counts]
        elif "from user_tab_columns" in lowered:
            self.rows = [(column,) for column in self.connection.table_columns]
        elif lowered.startswith("insert "):
            if self.connection.transaction_state is not None:
                self.connection.transaction_state = True
            self.rowcount = self.connection.insert_rowcount
            rowid_var = params.get("new_rowid")
            if hasattr(rowid_var, "value"):
                rowid_var.value = self.connection.inserted_rowid
            self.rows = []
        elif lowered.startswith("update "):
            if self.connection.transaction_state is not None:
                self.connection.transaction_state = True
            self.rowcount = self.connection.update_rowcount
            self.rows = []
        elif lowered.startswith("select rowid, id, name from decisions"):
            self.rows = [self.connection.inserted_row] if self.connection.insert_refresh_row_exists else []
        elif lowered.startswith("select ") and ":target_rowid" in lowered:
            self.rows = [(self.connection.refreshed_value,)] if self.connection.refresh_row_exists else []
        else:
            self.rows = []

    def setinputsizes(self, **sizes):
        self.connection.input_sizes.append(sizes)

    def var(self, _type):
        return FakeVar()

    def fetchone(self):
        if not self.rows:
            return None
        return self.rows.pop(0)

    def __iter__(self):
        return iter(self.rows)

    def close(self):
        pass


class FakeVar:
    value: object = None

    def getvalue(self):
        return self.value


def test_editable_table_columns_accepts_table_with_same_named_secondary_object():
    workspace = OracleWorkspace(make_config())
    workspace.connection = FakeConnection(object_counts=(1, 2))

    columns, reason = workspace.editable_table_columns("DECISIONS")

    assert columns == ["ID", "NAME"]
    assert reason == ""


@pytest.mark.parametrize(
    ("object_counts", "reason"),
    (
        ((0, 1), "Only base tables are editable"),
        ((0, 0), '"DECISIONS" was not found in the current schema'),
    ),
)
def test_editable_table_columns_rejects_non_table_or_missing_object(
    object_counts,
    reason,
):
    workspace = OracleWorkspace(make_config())
    workspace.connection = FakeConnection(object_counts=object_counts)

    columns, actual_reason = workspace.editable_table_columns("DECISIONS")

    assert columns == []
    assert actual_reason == reason
