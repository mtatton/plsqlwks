from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from plsqlwks.db import OracleExecutionError, OracleWorkspace
from tests.oracle_matrix import (
    FIXTURE_TABLE,
    OracleMatrixConfig,
    VerifiedOracleMatrix,
    verify_oracle_session,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.oracle,
    pytest.mark.oracle_matrix,
]


@pytest.mark.parametrize("profile_label", ["developer", "dml", "read_only"])
@pytest.mark.parametrize("dsn_style", ["easy_connect", "descriptor"])
def test_oracle_matrix_connection_cell(
    oracle_matrix_verification: VerifiedOracleMatrix,
    profile_label: str,
    dsn_style: str,
) -> None:
    identity = verify_oracle_session(
        oracle_matrix_verification.config,
        profile_label,
        dsn_style,
    )

    assert identity.server_version == oracle_matrix_verification.server_version
    assert oracle_matrix_verification.driver_mode == "thin"


def test_oracle_matrix_records_safe_suite_metadata(
    oracle_matrix_verification: VerifiedOracleMatrix,
    record_testsuite_property,
) -> None:
    record_testsuite_property("oracle_matrix_verified", "true")
    record_testsuite_property("oracle_server_version", oracle_matrix_verification.server_version)
    record_testsuite_property("oracledb_driver_version", oracle_matrix_verification.driver_version)
    record_testsuite_property("oracledb_driver_mode", oracle_matrix_verification.driver_mode)


def test_oracle_matrix_dml_profile_has_only_qualified_dml_access(
    oracle_matrix_verification: VerifiedOracleMatrix,
) -> None:
    matrix = oracle_matrix_verification.config
    verify_oracle_session(matrix, "dml", "easy_connect")
    workspace = _workspace(matrix, "dml")
    fixture = _qualified_fixture(matrix)
    probe_id = uuid.uuid4().hex.upper()
    try:
        workspace.connect()
        inserted = workspace.execute_statement(
            f"insert into {fixture} (probe_id, probe_value) values (:probe_id, :probe_value)",
            bind_values={"probe_id": probe_id, "probe_value": "inserted"},
        )
        assert "1 row(s)" in inserted.message
        loaded = workspace.execute_statement(
            f"select probe_value from {fixture} where probe_id = :probe_id",
            bind_values={"probe_id": probe_id},
        )
        assert loaded.rows == [["inserted"]]

        updated = workspace.execute_statement(
            f"update {fixture} set probe_value = :probe_value where probe_id = :probe_id",
            bind_values={"probe_id": probe_id, "probe_value": "updated"},
        )
        assert "1 row(s)" in updated.message
        loaded = workspace.execute_statement(
            f"select probe_value from {fixture} where probe_id = :probe_id",
            bind_values={"probe_id": probe_id},
        )
        assert loaded.rows == [["updated"]]

        deleted = workspace.execute_statement(
            f"delete from {fixture} where probe_id = :probe_id",
            bind_values={"probe_id": probe_id},
        )
        assert "1 row(s)" in deleted.message
        workspace.rollback()
        assert _probe_count(workspace, fixture, probe_id) == 0

        _assert_create_table_denied(workspace)
    finally:
        try:
            workspace.rollback()
        finally:
            workspace.close()


def test_oracle_matrix_read_only_profile_is_database_enforced(
    oracle_matrix_verification: VerifiedOracleMatrix,
) -> None:
    matrix = oracle_matrix_verification.config
    verify_oracle_session(matrix, "read_only", "easy_connect")
    workspace = _workspace(matrix, "read_only")
    fixture = _qualified_fixture(matrix)
    probe_id = uuid.uuid4().hex.upper()
    try:
        workspace.connect()
        baseline = workspace.execute_statement(
            f"select probe_value from {fixture} where probe_id = :probe_id",
            bind_values={"probe_id": "READ_ONLY_BASELINE"},
        )
        assert baseline.rows == [["compatibility fixture"]]

        with pytest.raises(OracleExecutionError, match="ORA-01031"):
            workspace.execute_statement(
                f"insert into {fixture} (probe_id, probe_value) values (:probe_id, :probe_value)",
                bind_values={"probe_id": probe_id, "probe_value": "must not persist"},
            )
        workspace.rollback()
        assert _probe_count(workspace, fixture, probe_id) == 0

        with pytest.raises(OracleExecutionError, match="ORA-01031"):
            workspace.execute_statement(
                f"update {fixture} set probe_value = :probe_value where probe_id = :probe_id",
                bind_values={"probe_id": "READ_ONLY_BASELINE", "probe_value": "must not persist"},
            )
        workspace.rollback()
        with pytest.raises(OracleExecutionError, match="ORA-01031"):
            workspace.execute_statement(
                f"delete from {fixture} where probe_id = :probe_id",
                bind_values={"probe_id": "READ_ONLY_BASELINE"},
            )
        workspace.rollback()

        _assert_create_table_denied(workspace)
    finally:
        try:
            workspace.rollback()
        finally:
            workspace.close()


@pytest.mark.parametrize("profile_label", ["dml", "read_only"])
def test_oracle_matrix_cross_schema_ui_boundaries_remain_explicit(
    oracle_matrix_verification: VerifiedOracleMatrix,
    profile_label: str,
) -> None:
    matrix = oracle_matrix_verification.config
    workspace = _workspace(matrix, profile_label)
    fixture = _qualified_fixture(matrix)
    try:
        workspace.connect()
        result = workspace.execute_statement(
            f"select rowid, t.* from {fixture} t where probe_id = :probe_id",
            bind_values={"probe_id": "READ_ONLY_BASELINE"},
        )
        assert result.rows
        assert result.editable_context is None
        assert "simple single-table SELECT" in result.edit_message
        assert FIXTURE_TABLE not in workspace.list_schema_objects()["TABLE"]
    finally:
        workspace.close()


def _workspace(matrix: OracleMatrixConfig, profile_label: str) -> OracleWorkspace:
    config = replace(
        matrix.app_config(profile_label, "easy_connect"),
        autocommit=False,
        read_only=False,
    )
    return OracleWorkspace(config)


def _qualified_fixture(matrix: OracleMatrixConfig) -> str:
    return f"{matrix.developer.sql_identifier}.{FIXTURE_TABLE}"


def _probe_count(workspace: OracleWorkspace, fixture: str, probe_id: str) -> int:
    result = workspace.execute_statement(
        f"select count(*) from {fixture} where probe_id = :probe_id",
        bind_values={"probe_id": probe_id},
    )
    return int(result.rows[0][0])


def _assert_create_table_denied(workspace: OracleWorkspace) -> None:
    table_name = f"PWT_MX_{uuid.uuid4().hex[:12].upper()}"
    created = False
    error: OracleExecutionError | None = None
    try:
        try:
            workspace.execute_statement(f"create table {table_name} (id number)")
            created = True
        except OracleExecutionError as exc:
            error = exc
    finally:
        if created and workspace.connection is not None:
            cursor = workspace.connection.cursor()
            try:
                cursor.execute(f"drop table {table_name} purge")
            finally:
                cursor.close()

    if created:
        pytest.fail("restricted Oracle matrix profile unexpectedly created a table")
    assert error is not None
    assert "ORA-01031" in str(error)
