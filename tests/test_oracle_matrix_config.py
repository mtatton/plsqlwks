from __future__ import annotations

from pathlib import Path

import pytest

from tests.oracle_matrix import (
    DEVELOPER_PRIVILEGES,
    DEVELOPER_OBJECT_PRIVILEGES,
    RESTRICTED_PRIVILEGES,
    OracleMatrixConfigurationError,
    OracleMatrixSafetyError,
    VerifiedOracleMatrix,
    is_connect_descriptor,
    is_easy_connect_dsn,
    load_oracle_matrix_config,
    validate_effective_test_object_grant_contract,
    validate_made_object_grant_contract,
    validate_object_grant_contract,
    validate_privilege_contract,
    validate_quota_contract,
    validate_schema_isolation_contract,
    validate_schema_privilege_contract,
    validate_session_time_limit_contract,
    version_matches_target,
)


def matrix_environment(tmp_path: Path) -> dict[str, str]:
    files: dict[str, Path] = {}
    for label in ("developer", "dml", "read_only"):
        path = tmp_path / f"{label}.secret"
        path.write_text(f"{label}-secret", encoding="utf-8")
        path.chmod(0o600)
        files[label] = path
    guard = tmp_path / "guard.secret"
    guard.write_text("a" * 64, encoding="utf-8")
    guard.chmod(0o600)

    return {
        "PLSQLWKS_TEST_ORACLE": "1",
        "PLSQLWKS_TEST_ORACLE_MATRIX": "1",
        "PLSQLWKS_TEST_ORACLE_TARGET": "19c",
        "ORACLE_USER": "PLSQLWKS_DEV",
        "ORACLE_DSN": "db.example.test:1521/plsqlwks",
        "ORACLE_PASSWORD_FILE": str(files["developer"]),
        "PLSQLWKS_TEST_ORACLE_DESCRIPTOR_DSN": (
            "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=db.example.test)(PORT=1521))"
            "(CONNECT_DATA=(SERVICE_NAME=plsqlwks)))"
        ),
        "PLSQLWKS_TEST_ORACLE_DML_USER": "PLSQLWKS_DML",
        "PLSQLWKS_TEST_ORACLE_DML_PASSWORD_FILE": str(files["dml"]),
        "PLSQLWKS_TEST_ORACLE_READ_ONLY_USER": "PLSQLWKS_READER",
        "PLSQLWKS_TEST_ORACLE_READ_ONLY_PASSWORD_FILE": str(files["read_only"]),
        "PLSQLWKS_TEST_ORACLE_EXPECTED_DB_UNIQUE_NAME": "PLSQLWKSTEST",
        "PLSQLWKS_TEST_ORACLE_EXPECTED_CON_NAME": "PLSQLWKSPDB",
        "PLSQLWKS_TEST_ORACLE_EXPECTED_SERVICE_NAME": "plsqlwks",
        "PLSQLWKS_TEST_ORACLE_GUARD_TOKEN_FILE": str(guard),
    }


def test_load_oracle_matrix_config_accepts_complete_redacted_configuration(tmp_path: Path) -> None:
    environ = matrix_environment(tmp_path)

    config = load_oracle_matrix_config(environ)

    assert config.target == "19c"
    rendered = repr(config) + repr(config.developer) + repr(config.dml) + repr(config.read_only)
    for sensitive in (
        "PLSQLWKS_DEV",
        "PLSQLWKS_DML",
        "PLSQLWKS_READER",
        "db.example.test",
        str(tmp_path),
        "a" * 64,
    ):
        assert sensitive not in rendered


def test_matrix_mode_requires_base_oracle_opt_in(tmp_path: Path) -> None:
    environ = matrix_environment(tmp_path)
    environ.pop("PLSQLWKS_TEST_ORACLE")

    with pytest.raises(
        OracleMatrixConfigurationError,
        match="requires PLSQLWKS_TEST_ORACLE=1",
    ):
        load_oracle_matrix_config(environ)


def test_matrix_configuration_aggregates_missing_variable_names_without_values(tmp_path: Path) -> None:
    environ = matrix_environment(tmp_path)
    environ.pop("PLSQLWKS_TEST_ORACLE_DML_USER")
    environ.pop("PLSQLWKS_TEST_ORACLE_EXPECTED_SERVICE_NAME")

    with pytest.raises(OracleMatrixConfigurationError) as excinfo:
        load_oracle_matrix_config(environ)

    text = str(excinfo.value)
    assert "PLSQLWKS_TEST_ORACLE_DML_USER" in text
    assert "PLSQLWKS_TEST_ORACLE_EXPECTED_SERVICE_NAME" in text
    assert "PLSQLWKS_DEV" not in text
    assert "db.example.test" not in text
    assert str(tmp_path) not in text


def test_matrix_configuration_treats_whitespace_only_values_as_missing(tmp_path: Path) -> None:
    environ = matrix_environment(tmp_path)
    environ["PLSQLWKS_TEST_ORACLE_EXPECTED_SERVICE_NAME"] = "   "

    with pytest.raises(OracleMatrixConfigurationError) as excinfo:
        load_oracle_matrix_config(environ)

    text = str(excinfo.value)
    assert "PLSQLWKS_TEST_ORACLE_EXPECTED_SERVICE_NAME" in text
    assert "PLSQLWKS_DEV" not in text
    assert str(tmp_path) not in text


@pytest.mark.parametrize("target", ["", "21c", "23ai", "19", "26"])
def test_matrix_configuration_rejects_unsupported_target(tmp_path: Path, target: str) -> None:
    environ = matrix_environment(tmp_path)
    environ["PLSQLWKS_TEST_ORACLE_TARGET"] = target

    with pytest.raises(
        OracleMatrixConfigurationError,
        match="PLSQLWKS_TEST_ORACLE_TARGET|must be 19c or 26ai",
    ):
        load_oracle_matrix_config(environ)


@pytest.mark.parametrize("bad_user", ["BAD-USER", "SYSTEM", "SYSBACKUP", "C##COMMON"])
def test_matrix_configuration_rejects_unsafe_users(tmp_path: Path, bad_user: str) -> None:
    environ = matrix_environment(tmp_path)
    environ["PLSQLWKS_TEST_ORACLE_DML_USER"] = bad_user

    with pytest.raises(OracleMatrixConfigurationError, match="dml user"):
        load_oracle_matrix_config(environ)


def test_matrix_configuration_requires_distinct_users(tmp_path: Path) -> None:
    environ = matrix_environment(tmp_path)
    environ["PLSQLWKS_TEST_ORACLE_READ_ONLY_USER"] = environ["ORACLE_USER"].lower()

    with pytest.raises(OracleMatrixConfigurationError, match="must be distinct"):
        load_oracle_matrix_config(environ)


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="symlinks are unavailable")
@pytest.mark.parametrize("kind", ["missing", "empty", "directory", "symlink", "permissive"])
def test_matrix_configuration_rejects_unsafe_secret_files(
    tmp_path: Path,
    kind: str,
) -> None:
    environ = matrix_environment(tmp_path)
    path = tmp_path / "unsafe-secret"
    if kind == "empty":
        path.touch(mode=0o600)
    elif kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "secret-target"
        target.write_text("secret", encoding="utf-8")
        target.chmod(0o600)
        path.symlink_to(target)
    elif kind == "permissive":
        path.write_text("secret", encoding="utf-8")
        path.chmod(0o644)
    environ["PLSQLWKS_TEST_ORACLE_DML_PASSWORD_FILE"] = str(path)

    with pytest.raises(OracleMatrixConfigurationError) as excinfo:
        load_oracle_matrix_config(environ)

    text = str(excinfo.value)
    assert "PLSQLWKS_TEST_ORACLE_DML_PASSWORD_FILE" in text
    assert str(path) not in text


def test_matrix_configuration_requires_lowercase_256_bit_guard_token(tmp_path: Path) -> None:
    environ = matrix_environment(tmp_path)
    guard = Path(environ["PLSQLWKS_TEST_ORACLE_GUARD_TOKEN_FILE"])
    guard.write_text("A" * 64, encoding="utf-8")

    with pytest.raises(OracleMatrixConfigurationError, match="lowercase 256-bit hex token"):
        load_oracle_matrix_config(environ)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("db.example.test:1521/service", True),
        ("tcp://db.example.test:1521/service", True),
        ("[2001:db8::1]:1521/service", True),
        ("tcps://db.example.test:1522/service", False),
        ("db.example.test/service", False),
        ("db.example.test", False),
        ("(DESCRIPTION=(CONNECT_DATA=(SERVICE_NAME=x)))", False),
        ("db.example.test:1521/service name", False),
    ],
)
def test_easy_connect_dsn_classification(value: str, expected: bool) -> None:
    assert is_easy_connect_dsn(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=db)(PORT=1521))"
            "(CONNECT_DATA=(SERVICE_NAME=service)))",
            True,
        ),
        ("db:1521/service", False),
        (
            "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCPS)(HOST=db)(PORT=1522))"
            "(CONNECT_DATA=(SERVICE_NAME=service)))",
            False,
        ),
        ("(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=db)))", False),
        ("(DESCRIPTION=(CONNECT_DATA=(SERVICE_NAME=service))", False),
    ],
)
def test_connect_descriptor_classification(value: str, expected: bool) -> None:
    assert is_connect_descriptor(value) is expected


@pytest.mark.parametrize(
    ("target", "version", "expected"),
    [
        ("19c", "19.3.0.0.0", True),
        ("19c", "19.32.0.0.0", True),
        ("19c", "23.26.0.0.0", False),
        ("26ai", "23.26.0.0.0", True),
        ("26ai", "23.27.1.0.0", True),
        ("26ai", "23.25.0.0.0", False),
        ("26ai", "26.1.0.0.0", False),
        ("26ai", "unknown", False),
    ],
)
def test_version_target_predicates(target: str, version: str, expected: bool) -> None:
    assert version_matches_target(target, version) is expected


def test_privilege_contract_accepts_only_exact_direct_grants() -> None:
    validate_privilege_contract("developer", DEVELOPER_PRIVILEGES, frozenset())
    validate_privilege_contract("dml", RESTRICTED_PRIVILEGES, frozenset())
    validate_privilege_contract("read_only", RESTRICTED_PRIVILEGES, frozenset())

    with pytest.raises(OracleMatrixSafetyError, match="no roles"):
        validate_privilege_contract("developer", DEVELOPER_PRIVILEGES, frozenset({"CONNECT"}))
    with pytest.raises(OracleMatrixSafetyError, match="did not match"):
        validate_privilege_contract(
            "developer",
            DEVELOPER_PRIVILEGES | {"SELECT ANY TABLE"},
            frozenset(),
        )
    with pytest.raises(OracleMatrixSafetyError, match="did not match"):
        validate_privilege_contract(
            "read_only",
            RESTRICTED_PRIVILEGES | {"UNLIMITED TABLESPACE"},
            frozenset(),
        )


def test_object_grant_contract_rejects_extra_access_and_grant_options() -> None:
    developer = frozenset(
        (
            owner,
            object_name,
            owner,
            privilege,
            "NO",
            "NO",
            "NO",
            "PACKAGE" if object_name.startswith("DBMS_") else "VIEW",
            "NO",
        )
        for owner, object_name, privilege in DEVELOPER_OBJECT_PRIVILEGES
    )
    dml = frozenset(
        {
            ("SYS", "DBMS_OUTPUT", "SYS", "EXECUTE", "NO", "NO", "NO", "PACKAGE", "NO"),
            ("PLSQLWKS_DEV", "PLSQLWKS_COMPAT_FIXTURE", "PLSQLWKS_DEV", "SELECT", "NO", "NO", "NO", "TABLE", "NO"),
            ("PLSQLWKS_DEV", "PLSQLWKS_COMPAT_FIXTURE", "PLSQLWKS_DEV", "INSERT", "NO", "NO", "NO", "TABLE", "NO"),
            ("PLSQLWKS_DEV", "PLSQLWKS_COMPAT_FIXTURE", "PLSQLWKS_DEV", "UPDATE", "NO", "NO", "NO", "TABLE", "NO"),
            ("PLSQLWKS_DEV", "PLSQLWKS_COMPAT_FIXTURE", "PLSQLWKS_DEV", "DELETE", "NO", "NO", "NO", "TABLE", "NO"),
        }
    )
    read_only = frozenset(
        {
            ("SYS", "DBMS_OUTPUT", "SYS", "EXECUTE", "NO", "NO", "NO", "PACKAGE", "NO"),
            ("PLSQLWKS_DEV", "PLSQLWKS_COMPAT_FIXTURE", "PLSQLWKS_DEV", "SELECT", "NO", "NO", "NO", "TABLE", "NO"),
        }
    )

    validate_object_grant_contract("developer", "PLSQLWKS_DEV", developer)
    validate_object_grant_contract("dml", "PLSQLWKS_DEV", dml)
    validate_object_grant_contract("read_only", "PLSQLWKS_DEV", read_only)

    with pytest.raises(OracleMatrixSafetyError, match="object grant contract"):
        validate_object_grant_contract(
            "read_only",
            "PLSQLWKS_DEV",
            read_only
            | {
                (
                    "PLSQLWKS_DEV",
                    "PLSQLWKS_COMPAT_FIXTURE",
                    "PLSQLWKS_DEV",
                    "UPDATE",
                    "NO",
                    "NO",
                    "NO",
                    "TABLE",
                    "NO",
                )
            },
        )
    with pytest.raises(OracleMatrixSafetyError, match="object grant contract"):
        validate_object_grant_contract(
            "dml",
            "PLSQLWKS_DEV",
            (
                dml
                - {
                    (
                        "PLSQLWKS_DEV",
                        "PLSQLWKS_COMPAT_FIXTURE",
                        "PLSQLWKS_DEV",
                        "DELETE",
                        "NO",
                        "NO",
                        "NO",
                        "TABLE",
                        "NO",
                    )
                }
            )
            | {
                (
                    "PLSQLWKS_DEV",
                    "PLSQLWKS_COMPAT_FIXTURE",
                    "PLSQLWKS_DEV",
                    "DELETE",
                    "YES",
                    "NO",
                    "NO",
                    "TABLE",
                    "NO",
                )
            },
        )


def test_schema_isolation_contract_rejects_hidden_write_paths() -> None:
    validate_schema_isolation_contract(
        "developer",
        received_column_grant_count=0,
        made_column_grant_count=0,
        owned_object_count=2,
        public_table_mutation_grant_count=0,
        public_column_grant_count=0,
    )
    validate_schema_isolation_contract(
        "read_only",
        received_column_grant_count=0,
        made_column_grant_count=0,
        owned_object_count=0,
        public_table_mutation_grant_count=0,
        public_column_grant_count=0,
    )

    for overrides in (
        {"received_column_grant_count": 1},
        {"made_column_grant_count": 1},
        {"owned_object_count": 1},
        {"public_table_mutation_grant_count": 1},
        {"public_column_grant_count": 1},
    ):
        counts = {
            "received_column_grant_count": 0,
            "made_column_grant_count": 0,
            "owned_object_count": 0,
            "public_table_mutation_grant_count": 0,
            "public_column_grant_count": 0,
        }
        counts.update(overrides)
        with pytest.raises(OracleMatrixSafetyError, match="schema isolation"):
            validate_schema_isolation_contract("read_only", **counts)


def test_schema_privilege_contract_rejects_26ai_schema_grants() -> None:
    validate_schema_privilege_contract("read_only", frozenset(), frozenset())

    with pytest.raises(OracleMatrixSafetyError, match="schema privilege"):
        validate_schema_privilege_contract(
            "read_only",
            frozenset({("UPDATE ANY TABLE", "PLSQLWKS_DEV")}),
            frozenset(),
        )


def test_tablespace_quota_contract_is_small_and_profile_specific() -> None:
    validate_quota_contract("developer", (64 * 1024 * 1024,))
    validate_quota_contract("dml", ())
    validate_quota_contract("read_only", ())

    for quotas in ((), (-1,), (64 * 1024 * 1024 + 1,), (1024, 2048)):
        with pytest.raises(OracleMatrixSafetyError, match="tablespace quota"):
            validate_quota_contract("developer", quotas)
    for profile_label in ("dml", "read_only"):
        with pytest.raises(OracleMatrixSafetyError, match="tablespace quota"):
            validate_quota_contract(profile_label, (1024,))


def test_developer_guard_lock_requires_unlimited_session_time() -> None:
    unlimited = {"CONNECT_TIME": "UNLIMITED", "IDLE_TIME": "UNLIMITED"}
    validate_session_time_limit_contract("developer", unlimited)
    validate_session_time_limit_contract("dml", {"CONNECT_TIME": "1"})

    for limits in ({}, {"CONNECT_TIME": "60", "IDLE_TIME": "UNLIMITED"}):
        with pytest.raises(OracleMatrixSafetyError, match="release its guard lock"):
            validate_session_time_limit_contract("developer", limits)
    with pytest.raises(OracleMatrixSafetyError, match="schema privilege"):
        validate_schema_privilege_contract(
            "read_only",
            frozenset(),
            frozenset(
                {
                    (
                        "PLSQLWKS_READER",
                        "UPDATE ANY TABLE",
                        "PLSQLWKS_DEV",
                        "NO",
                        "NO",
                        "NO",
                    )
                }
            ),
        )


def test_outgoing_and_effective_fixture_grants_are_exact(tmp_path: Path) -> None:
    config = load_oracle_matrix_config(matrix_environment(tmp_path))
    owner = config.developer.sql_identifier
    made = frozenset(
        {
            (config.dml.sql_identifier, "PLSQLWKS_COMPAT_FIXTURE", owner, "SELECT", "NO", "NO", "NO", "TABLE", "NO"),
            (config.dml.sql_identifier, "PLSQLWKS_COMPAT_FIXTURE", owner, "INSERT", "NO", "NO", "NO", "TABLE", "NO"),
            (config.dml.sql_identifier, "PLSQLWKS_COMPAT_FIXTURE", owner, "UPDATE", "NO", "NO", "NO", "TABLE", "NO"),
            (config.dml.sql_identifier, "PLSQLWKS_COMPAT_FIXTURE", owner, "DELETE", "NO", "NO", "NO", "TABLE", "NO"),
            (config.read_only.sql_identifier, "PLSQLWKS_COMPAT_FIXTURE", owner, "SELECT", "NO", "NO", "NO", "TABLE", "NO"),
        }
    )
    effective_dml = frozenset(
        (
            config.dml.sql_identifier,
            owner,
            "PLSQLWKS_COMPAT_FIXTURE",
            owner,
            privilege,
            "NO",
            "NO",
            "NO",
            "TABLE",
            "NO",
        )
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
    )

    validate_made_object_grant_contract("developer", config, made)
    validate_effective_test_object_grant_contract("dml", config, effective_dml)
    with pytest.raises(OracleMatrixSafetyError, match="outgoing object grant"):
        validate_made_object_grant_contract("developer", config, made | {("PUBLIC",)})
    with pytest.raises(OracleMatrixSafetyError, match="effective test-object grant"):
        validate_effective_test_object_grant_contract(
            "dml",
            config,
            effective_dml | {("PUBLIC",)},
        )


def test_matrix_lock_liveness_uses_existing_transaction_and_redacts_failures(tmp_path: Path) -> None:
    config = load_oracle_matrix_config(matrix_environment(tmp_path))

    class FakeCursor:
        def __init__(self, connection) -> None:
            self.connection = connection
            self.closed = False

        def execute(self, statement, **binds) -> None:
            assert "for update nowait" in statement.lower()
            assert binds == {"guard_name": "PLSQLWKS_ORACLE_MATRIX", "guard_token": "a" * 64}
            self.connection.transaction_in_progress = True

        def fetchone(self):
            return ("digest",)

        def close(self) -> None:
            self.closed = True

    class FakeConnection:
        autocommit = False
        transaction_in_progress = True
        call_timeout = 0

        def __init__(self) -> None:
            self.ping_count = 0
            self.last_cursor = None

        def ping(self) -> None:
            self.ping_count += 1

        def cursor(self):
            self.last_cursor = FakeCursor(self)
            return self.last_cursor

    class FakeWorkspace:
        autocommit = False

        def __init__(self, connection) -> None:
            self.connection = connection

    connection = FakeConnection()
    verification = VerifiedOracleMatrix(
        config=config,
        server_version="19.0.0.0.0",
        driver_version="test",
        driver_mode="thin",
        lock_workspace=FakeWorkspace(connection),
    )

    verification.assert_lock_alive()

    assert connection.call_timeout == 5_000
    assert connection.ping_count == 1
    assert connection.last_cursor.closed

    connection.transaction_in_progress = False
    with pytest.raises(OracleMatrixSafetyError, match="lock is unavailable"):
        verification.assert_lock_alive()

    connection.transaction_in_progress = True

    def fail_ping() -> None:
        raise RuntimeError("private endpoint")

    connection.ping = fail_ping
    with pytest.raises(OracleMatrixSafetyError) as excinfo:
        verification.assert_lock_alive()
    assert str(excinfo.value) == "Oracle matrix endpoint lock is unavailable"
    assert "private endpoint" not in str(excinfo.value)
