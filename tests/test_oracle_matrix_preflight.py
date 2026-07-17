from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import tests.oracle_matrix as matrix
from plsqlwks.config import AppConfig


class ScriptedCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows: list[tuple[object, ...]] = []
        self.closed = False

    def execute(self, sql, **binds):
        normalized = " ".join(sql.split()).casefold()
        self.connection.queries.append((normalized, binds))
        response = self.connection.responses.get(normalized)
        if isinstance(response, BaseException):
            raise response
        if response is None:
            response = self.connection.default_response(normalized, binds)
        self.rows = list(response)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)

    def close(self):
        self.closed = True
        self.connection.closed_cursors += 1


class ScriptedConnection:
    def __init__(self, identity_row=None):
        self.identity_row = identity_row
        self.responses: dict[str, object] = {}
        self.queries: list[tuple[str, dict[str, object]]] = []
        self.closed_cursors = 0
        self.version = "19.26.0.0.0"
        self.autocommit = False
        self.transaction_in_progress = True
        self.call_timeout = 0
        self.pings = 0
        self.ping_error: Exception | None = None
        self.lock_row: tuple[object, ...] | None = ("HASH",)
        self.lock_establishes_transaction = True

    def cursor(self):
        return ScriptedCursor(self)

    def ping(self):
        self.pings += 1
        if self.ping_error is not None:
            raise self.ping_error

    def default_response(self, sql, binds):
        if "from user_users" in sql:
            return [] if self.identity_row is None else [self.identity_row]
        if "select token_sha256" in sql and matrix.GUARD_TABLE.casefold() in sql:
            self.transaction_in_progress = self.lock_establishes_transaction
            return [] if self.lock_row is None else [self.lock_row]
        if "select count(*)" in sql and matrix.GUARD_TABLE.casefold() in sql:
            return [(1,)]
        if matrix.FIXTURE_TABLE.casefold() in sql and "select probe_value" in sql:
            return [("compatibility fixture",)]
        if "select count(*)" in sql:
            return [(0,)]
        return []


class FakeWorkspace:
    def __init__(self, connection=None, *, ensure_error=None):
        self.connection = connection
        self.autocommit = False
        self.ensure_error = ensure_error
        self.closed = 0
        self.rollbacks = 0
        self.close_error: Exception | None = None

    def ensure_connected(self):
        if self.ensure_error is not None:
            raise self.ensure_error
        return self.connection

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def make_config(tmp_path: Path, *, target: str = "19c") -> matrix.OracleMatrixConfig:
    password_files = {}
    for name in ("developer", "dml", "read_only"):
        path = tmp_path / f"{name}.password"
        path.write_text("secret", encoding="utf-8")
        path.chmod(0o600)
        password_files[name] = path
    token = tmp_path / "guard.token"
    token.write_text("a" * 64, encoding="utf-8")
    token.chmod(0o600)
    return matrix.OracleMatrixConfig(
        target=target,
        easy_connect_dsn="db.example:1521/service",
        descriptor_dsn="(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP))(CONNECT_DATA=(SERVICE_NAME=service)))",
        developer=matrix.OracleMatrixProfile("developer", "DEV_USER", password_files["developer"]),
        dml=matrix.OracleMatrixProfile("dml", "DML_USER", password_files["dml"]),
        read_only=matrix.OracleMatrixProfile("read_only", "READ_USER", password_files["read_only"]),
        expected_db_unique_name="TESTDB",
        expected_con_name="TESTPDB",
        expected_service_name="service",
        guard_token_file=token,
    )


def valid_identity(user="DEV_USER", *, version="19.26.0.0.0"):
    return matrix.OracleSessionIdentity(
        session_user=user,
        current_schema=user,
        db_unique_name="TESTDB",
        con_name="TESTPDB",
        service_name="service",
        authentication_method="PASSWORD",
        proxy_user="",
        is_dba="FALSE",
        database_role="PRIMARY",
        common_user="NO",
        oracle_maintained="N",
        inherited_user="NO",
        implicit_user="NO",
        all_shard_user="NO",
        proxy_only_connect="N",
        server_version=version,
    )


def identity_row(identity):
    return (
        identity.session_user,
        identity.current_schema,
        identity.db_unique_name,
        identity.con_name,
        identity.service_name,
        identity.authentication_method,
        identity.proxy_user,
        identity.is_dba,
        identity.database_role,
        identity.common_user,
        identity.oracle_maintained,
        identity.inherited_user,
        identity.implicit_user,
        identity.all_shard_user,
        identity.proxy_only_connect,
    )


def test_matrix_config_profile_dsn_and_app_config_contract(tmp_path):
    config = make_config(tmp_path)

    assert config.profile("developer") is config.developer
    assert config.profile("dml") is config.dml
    assert config.profile("read_only") is config.read_only
    assert config.dsn("easy_connect") == config.easy_connect_dsn
    assert config.dsn("descriptor") == config.descriptor_dsn
    with pytest.raises(ValueError, match="Unknown Oracle matrix profile"):
        config.profile("missing")
    with pytest.raises(ValueError, match="Unknown Oracle matrix DSN"):
        config.dsn("missing")

    generated = config.app_config("developer", "easy_connect")
    base = AppConfig(
        user="other",
        dsn="other",
        password_file=tmp_path / "other.password",
        workspace_dir=tmp_path,
        autocommit=True,
        read_only=True,
    )
    replaced = config.app_config("read_only", "descriptor", base=base)
    assert (generated.user, generated.dsn, generated.autocommit, generated.read_only) == (
        "DEV_USER",
        config.easy_connect_dsn,
        False,
        False,
    )
    assert (replaced.user, replaced.dsn, replaced.autocommit, replaced.read_only) == (
        "READ_USER",
        config.descriptor_dsn,
        False,
        False,
    )


def test_read_session_identity_returns_all_fields_and_always_closes_cursor():
    expected = valid_identity()
    connection = ScriptedConnection(identity_row(expected))

    assert matrix._read_session_identity(connection) == expected
    assert connection.closed_cursors == 1

    missing = ScriptedConnection()
    with pytest.raises(matrix.OracleMatrixSafetyError, match="identity query returned no row"):
        matrix._read_session_identity(missing)
    assert missing.closed_cursors == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("session_user", "OTHER", "session user"),
        ("current_schema", "OTHER", "current schema"),
        ("db_unique_name", "OTHER", "endpoint fingerprint"),
        ("authentication_method", "KERBEROS", "password authentication"),
        ("proxy_user", "PROXY", "proxy user"),
        ("is_dba", "TRUE", "DBA session status"),
        ("database_role", "STANDBY", "primary database"),
        ("common_user", "YES", "local application account"),
        ("oracle_maintained", "Y", "local application account"),
        ("inherited_user", "YES", "local application account"),
        ("implicit_user", "YES", "local application account"),
        ("all_shard_user", "YES", "local application account"),
        ("proxy_only_connect", "Y", "local application account"),
        ("server_version", "18.0", "did not match target"),
    ],
)
def test_verify_identity_rejects_each_fail_closed_boundary(tmp_path, field, value, message):
    config = make_config(tmp_path)
    identity = replace(valid_identity(), **{field: value})

    with pytest.raises(matrix.OracleMatrixSafetyError, match=message):
        matrix._verify_identity(config, config.developer, identity)


def test_verify_identity_accepts_matching_19c_and_26ai_profiles(tmp_path):
    config = make_config(tmp_path)
    matrix._verify_identity(config, config.developer, valid_identity())

    ai_config = make_config(tmp_path, target="26ai")
    matrix._verify_identity(ai_config, ai_config.developer, valid_identity(version="23.26.1"))


@pytest.mark.parametrize(("profile_label", "guard_calls", "fixture_calls"), [("developer", 1, 0), ("dml", 0, 1)])
def test_verify_oracle_session_routes_guard_and_fixture_and_closes_workspace(
    tmp_path,
    monkeypatch,
    profile_label,
    guard_calls,
    fixture_calls,
):
    config = make_config(tmp_path)
    profile = config.profile(profile_label)
    identity = valid_identity(profile.user)
    workspace = FakeWorkspace(ScriptedConnection(identity_row(identity)))
    calls = {"guard": 0, "fixture": 0, "privileges": 0}
    monkeypatch.setattr(matrix, "OracleWorkspace", lambda _config: workspace)
    monkeypatch.setattr(matrix, "_verify_privileges", lambda *_args: calls.__setitem__("privileges", 1))
    monkeypatch.setattr(matrix, "_verify_guard", lambda *_args: calls.__setitem__("guard", calls["guard"] + 1))
    monkeypatch.setattr(
        matrix,
        "_verify_fixture_read",
        lambda *_args: calls.__setitem__("fixture", calls["fixture"] + 1),
    )

    assert matrix.verify_oracle_session(config, profile_label, "easy_connect") == identity
    assert calls == {"guard": guard_calls, "fixture": fixture_calls, "privileges": 1}
    assert workspace.closed == 1


def test_verify_oracle_session_redacts_connection_and_unexpected_failures(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    failed = FakeWorkspace(ensure_error=RuntimeError("secret endpoint detail"))
    monkeypatch.setattr(matrix, "OracleWorkspace", lambda _config: failed)

    with pytest.raises(matrix.OracleMatrixSafetyError, match="developer/easy_connect connection failed") as error:
        matrix.verify_oracle_session(config, "developer", "easy_connect")
    assert "secret endpoint detail" not in str(error.value)
    assert failed.closed == 1

    unexpected = FakeWorkspace(ScriptedConnection(identity_row(valid_identity())))
    unexpected.close_error = RuntimeError("ignored close")
    monkeypatch.setattr(matrix, "OracleWorkspace", lambda _config: unexpected)
    monkeypatch.setattr(matrix, "_read_session_identity", lambda _connection: (_ for _ in ()).throw(TypeError("raw")))
    with pytest.raises(matrix.OracleMatrixSafetyError, match="developer/easy_connect preflight failed"):
        matrix.verify_oracle_session(config, "developer", "easy_connect")
    assert unexpected.closed == 1


def test_run_preflight_checks_all_profiles_and_dsns_then_returns_lock(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    calls = []
    lock_workspace = FakeWorkspace()
    monkeypatch.setattr(matrix.oracledb, "is_thin_mode", lambda: True)

    def verify(_config, profile, dsn):
        calls.append((profile, dsn))
        return valid_identity(_config.profile(profile).user)

    monkeypatch.setattr(matrix, "verify_oracle_session", verify)
    monkeypatch.setattr(matrix, "_acquire_matrix_lock", lambda _config: lock_workspace)

    verified = matrix.run_oracle_matrix_preflight(config)

    assert calls == [
        (profile, dsn)
        for profile in ("developer", "dml", "read_only")
        for dsn in ("easy_connect", "descriptor")
    ]
    assert verified.server_version == "19.26.0.0.0"
    assert verified.driver_mode == "thin"
    assert verified.lock_workspace is lock_workspace


def test_run_preflight_rejects_thick_mode_and_inconsistent_versions(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(matrix.oracledb, "is_thin_mode", lambda: False)
    with pytest.raises(matrix.OracleMatrixSafetyError, match="requires python-oracledb Thin mode"):
        matrix.run_oracle_matrix_preflight(config)

    versions = iter(("19.1", "19.2"))
    monkeypatch.setattr(matrix.oracledb, "is_thin_mode", lambda: True)
    monkeypatch.setattr(
        matrix,
        "verify_oracle_session",
        lambda *_args: valid_identity(version=next(versions)),
    )
    with pytest.raises(matrix.OracleMatrixSafetyError, match="different server versions"):
        matrix.run_oracle_matrix_preflight(config)


@pytest.mark.parametrize("target", ["19c", "26ai"])
def test_verify_privileges_executes_full_query_contract(tmp_path, monkeypatch, target):
    config = make_config(tmp_path, target=target)
    connection = ScriptedConnection()
    validated = []
    for name in (
        "validate_privilege_contract",
        "validate_schema_privilege_contract",
        "validate_object_grant_contract",
        "validate_schema_isolation_contract",
        "validate_quota_contract",
        "validate_session_time_limit_contract",
        "validate_made_object_grant_contract",
        "validate_effective_test_object_grant_contract",
    ):
        monkeypatch.setattr(matrix, name, lambda *args, _name=name, **kwargs: validated.append((_name, args, kwargs)))

    matrix._verify_privileges(connection, "dml", config)

    assert len(validated) == 8
    assert connection.closed_cursors == 1
    assert any("all_tab_privs_recd" in query for query, _binds in connection.queries)
    assert any("session_schema_privs" in query for query, _binds in connection.queries) is (target == "26ai")


def test_verify_privileges_rejects_non_direct_system_grants_before_contract_validation(tmp_path):
    config = make_config(tmp_path)
    connection = ScriptedConnection()
    connection.responses["select privilege from session_privs"] = [("CREATE SESSION",)]

    with pytest.raises(matrix.OracleMatrixSafetyError, match="direct system grant contract"):
        matrix._verify_privileges(connection, "dml", config)
    assert connection.closed_cursors == 1


def test_guard_and_fixture_checks_use_binds_close_cursors_and_fail_closed(tmp_path):
    config = make_config(tmp_path)
    connection = ScriptedConnection()

    matrix._verify_guard(connection, config)
    matrix._verify_fixture_read(connection, config.developer.sql_identifier)
    assert connection.closed_cursors == 2
    assert any(binds.get("guard_name") == matrix.GUARD_NAME for _query, binds in connection.queries)
    assert any(binds.get("probe_id") == "READ_ONLY_BASELINE" for _query, binds in connection.queries)

    bad_guard = ScriptedConnection()
    bad_guard.responses[next(iter({q for q, _ in connection.queries if matrix.GUARD_TABLE.casefold() in q}))] = [(0,)]
    with pytest.raises(matrix.OracleMatrixSafetyError, match="guard did not match"):
        matrix._verify_guard(bad_guard, config)

    bad_fixture = ScriptedConnection()
    fixture_query = next(q for q, _ in connection.queries if "select probe_value" in q)
    bad_fixture.responses[fixture_query] = [("wrong",)]
    with pytest.raises(matrix.OracleMatrixSafetyError, match="fixture is unavailable"):
        matrix._verify_fixture_read(bad_fixture, config.developer.sql_identifier)


def test_lock_verified_guard_success_and_liveness_requirements(tmp_path):
    config = make_config(tmp_path)
    connection = ScriptedConnection()
    workspace = FakeWorkspace(connection)

    matrix._lock_verified_guard(workspace, config, require_existing=False)
    assert connection.call_timeout == 5_000
    assert connection.pings == 1
    matrix._lock_verified_guard(workspace, config, require_existing=True)

    connection.transaction_in_progress = False
    with pytest.raises(matrix.OracleMatrixSafetyError, match="lock is unavailable"):
        matrix._lock_verified_guard(workspace, config, require_existing=True)


@pytest.mark.parametrize("case", ["missing", "workspace_autocommit", "connection_autocommit", "ping", "row", "transaction"])
def test_lock_verified_guard_rejects_each_failure_mode(tmp_path, case):
    config = make_config(tmp_path)
    connection = ScriptedConnection()
    workspace = FakeWorkspace(connection)
    if case == "missing":
        workspace.connection = None
    elif case == "workspace_autocommit":
        workspace.autocommit = True
    elif case == "connection_autocommit":
        connection.autocommit = True
    elif case == "ping":
        connection.ping_error = RuntimeError("driver detail")
    elif case == "row":
        connection.lock_row = None
    else:
        connection.lock_establishes_transaction = False

    with pytest.raises(matrix.OracleMatrixSafetyError):
        matrix._lock_verified_guard(workspace, config, require_existing=False)


def test_acquire_lock_closes_workspace_on_safety_and_driver_failures(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    successful = FakeWorkspace(ScriptedConnection())
    monkeypatch.setattr(matrix, "OracleWorkspace", lambda _config: successful)
    monkeypatch.setattr(matrix, "_lock_verified_guard", lambda *_args, **_kwargs: None)
    assert matrix._acquire_matrix_lock(config) is successful
    assert successful.closed == 0

    safety = FakeWorkspace(ScriptedConnection())
    monkeypatch.setattr(matrix, "OracleWorkspace", lambda _config: safety)
    monkeypatch.setattr(
        matrix,
        "_lock_verified_guard",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(matrix.OracleMatrixSafetyError("unsafe")),
    )
    with pytest.raises(matrix.OracleMatrixSafetyError, match="unsafe"):
        matrix._acquire_matrix_lock(config)
    assert safety.closed == 1

    driver = FakeWorkspace(ensure_error=RuntimeError("raw driver"))
    driver.close_error = RuntimeError("ignored close")
    monkeypatch.setattr(matrix, "OracleWorkspace", lambda _config: driver)
    with pytest.raises(matrix.OracleMatrixSafetyError, match="already in use or could not be locked") as error:
        matrix._acquire_matrix_lock(config)
    assert "raw driver" not in str(error.value)
    assert driver.closed == 1


def test_verified_matrix_close_and_assert_lock_suppress_cleanup_failures(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    workspace = FakeWorkspace(ScriptedConnection())
    workspace.close_error = RuntimeError("ignored")
    verification = matrix.VerifiedOracleMatrix(config, "19.26", "driver", "thin", workspace)
    lock_calls = []
    monkeypatch.setattr(matrix, "_lock_verified_guard", lambda *args, **kwargs: lock_calls.append((args, kwargs)))

    verification.assert_lock_alive()
    verification.close()

    assert lock_calls[0][1] == {"require_existing": True}
    assert workspace.rollbacks == 1
    assert workspace.closed == 1
