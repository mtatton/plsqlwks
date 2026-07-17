from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import re
from typing import Mapping

import oracledb

from plsqlwks.config import AppConfig, read_password
from plsqlwks.db import OracleWorkspace


MATRIX_ENV_FLAG = "PLSQLWKS_TEST_ORACLE_MATRIX"
GUARD_NAME = "PLSQLWKS_ORACLE_MATRIX"
GUARD_TABLE = "PLSQLWKS_COMPAT_GUARD"
FIXTURE_TABLE = "PLSQLWKS_COMPAT_FIXTURE"

DEVELOPER_PRIVILEGES = frozenset(
    {
        "ALTER SESSION",
        "CREATE PROCEDURE",
        "CREATE SEQUENCE",
        "CREATE SESSION",
        "CREATE SYNONYM",
        "CREATE TABLE",
        "CREATE TRIGGER",
        "CREATE VIEW",
    }
)
RESTRICTED_PRIVILEGES = frozenset({"CREATE SESSION"})
DEVELOPER_OBJECT_PRIVILEGES = frozenset(
    {
        ("SYS", "DBMS_LOB", "EXECUTE"),
        ("SYS", "DBMS_METADATA", "EXECUTE"),
        ("SYS", "DBMS_OUTPUT", "EXECUTE"),
        ("SYS", "DBMS_SESSION", "EXECUTE"),
        ("SYS", "DBMS_UTILITY", "EXECUTE"),
        ("SYS", "DBMS_XPLAN", "EXECUTE"),
        ("SYS", "V_$SESSION", "SELECT"),
        ("SYS", "V_$SQL", "SELECT"),
        ("SYS", "V_$SQL_PLAN", "SELECT"),
        ("SYS", "V_$SQL_PLAN_STATISTICS_ALL", "SELECT"),
    }
)

_MATRIX_ENV_NAMES = (
    "PLSQLWKS_TEST_ORACLE_TARGET",
    "ORACLE_USER",
    "ORACLE_DSN",
    "ORACLE_PASSWORD_FILE",
    "PLSQLWKS_TEST_ORACLE_DESCRIPTOR_DSN",
    "PLSQLWKS_TEST_ORACLE_DML_USER",
    "PLSQLWKS_TEST_ORACLE_DML_PASSWORD_FILE",
    "PLSQLWKS_TEST_ORACLE_READ_ONLY_USER",
    "PLSQLWKS_TEST_ORACLE_READ_ONLY_PASSWORD_FILE",
    "PLSQLWKS_TEST_ORACLE_EXPECTED_DB_UNIQUE_NAME",
    "PLSQLWKS_TEST_ORACLE_EXPECTED_CON_NAME",
    "PLSQLWKS_TEST_ORACLE_EXPECTED_SERVICE_NAME",
    "PLSQLWKS_TEST_ORACLE_GUARD_TOKEN_FILE",
)
_SECRET_FILE_ENV_NAMES = (
    "ORACLE_PASSWORD_FILE",
    "PLSQLWKS_TEST_ORACLE_DML_PASSWORD_FILE",
    "PLSQLWKS_TEST_ORACLE_READ_ONLY_PASSWORD_FILE",
    "PLSQLWKS_TEST_ORACLE_GUARD_TOKEN_FILE",
)
_SIMPLE_ORACLE_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")


class OracleMatrixConfigurationError(ValueError):
    """A redacted static matrix configuration failure."""


class OracleMatrixSafetyError(RuntimeError):
    """A redacted live safety-preflight failure."""


@dataclass(frozen=True)
class OracleMatrixProfile:
    label: str
    user: str = field(repr=False)
    password_file: Path = field(repr=False)

    @property
    def sql_identifier(self) -> str:
        return self.user.upper()


@dataclass(frozen=True)
class OracleSessionIdentity:
    session_user: str = field(repr=False)
    current_schema: str = field(repr=False)
    db_unique_name: str = field(repr=False)
    con_name: str = field(repr=False)
    service_name: str = field(repr=False)
    authentication_method: str
    proxy_user: str = field(repr=False)
    is_dba: str
    database_role: str
    common_user: str
    oracle_maintained: str
    inherited_user: str
    implicit_user: str
    all_shard_user: str
    proxy_only_connect: str
    server_version: str


@dataclass(frozen=True)
class OracleMatrixConfig:
    target: str
    easy_connect_dsn: str = field(repr=False)
    descriptor_dsn: str = field(repr=False)
    developer: OracleMatrixProfile = field(repr=False)
    dml: OracleMatrixProfile = field(repr=False)
    read_only: OracleMatrixProfile = field(repr=False)
    expected_db_unique_name: str = field(repr=False)
    expected_con_name: str = field(repr=False)
    expected_service_name: str = field(repr=False)
    guard_token_file: Path = field(repr=False)

    def profile(self, label: str) -> OracleMatrixProfile:
        if label == "developer":
            return self.developer
        if label == "dml":
            return self.dml
        if label == "read_only":
            return self.read_only
        raise ValueError(f"Unknown Oracle matrix profile label: {label!r}")

    def dsn(self, style: str) -> str:
        if style == "easy_connect":
            return self.easy_connect_dsn
        if style == "descriptor":
            return self.descriptor_dsn
        raise ValueError(f"Unknown Oracle matrix DSN style: {style!r}")

    def app_config(
        self,
        profile_label: str,
        dsn_style: str,
        *,
        base: AppConfig | None = None,
    ) -> AppConfig:
        profile = self.profile(profile_label)
        if base is None:
            base = AppConfig(
                user=profile.user,
                dsn=self.dsn(dsn_style),
                password_file=profile.password_file,
                workspace_dir=Path("."),
            )
        return replace(
            base,
            user=profile.user,
            password_file=profile.password_file,
            dsn=self.dsn(dsn_style),
            autocommit=False,
            read_only=False,
        )


@dataclass(frozen=True)
class VerifiedOracleMatrix:
    config: OracleMatrixConfig = field(repr=False)
    server_version: str
    driver_version: str
    driver_mode: str
    lock_workspace: OracleWorkspace = field(repr=False, compare=False)

    def assert_lock_alive(self) -> None:
        _lock_verified_guard(self.lock_workspace, self.config, require_existing=True)

    def close(self) -> None:
        try:
            try:
                self.lock_workspace.rollback()
            except Exception:
                pass
        finally:
            try:
                self.lock_workspace.close()
            except Exception:
                pass


def oracle_matrix_requested(environ: Mapping[str, str] | None = None) -> bool:
    if environ is None:
        environ = os.environ
    return environ.get(MATRIX_ENV_FLAG) == "1"


def load_oracle_matrix_config(
    environ: Mapping[str, str] | None = None,
) -> OracleMatrixConfig:
    if environ is None:
        environ = os.environ
    if environ.get("PLSQLWKS_TEST_ORACLE") != "1":
        raise OracleMatrixConfigurationError(
            "Oracle matrix mode requires PLSQLWKS_TEST_ORACLE=1"
        )

    missing = [name for name in _MATRIX_ENV_NAMES if not environ.get(name, "").strip()]
    if missing:
        raise OracleMatrixConfigurationError(
            "Oracle matrix configuration is missing: " + ", ".join(missing)
        )

    target = environ["PLSQLWKS_TEST_ORACLE_TARGET"].strip().lower()
    if target not in {"19c", "26ai"}:
        raise OracleMatrixConfigurationError(
            "PLSQLWKS_TEST_ORACLE_TARGET must be 19c or 26ai in matrix mode"
        )

    for name in _SECRET_FILE_ENV_NAMES:
        _validate_secret_file(name, environ[name])

    developer = OracleMatrixProfile(
        "developer",
        environ["ORACLE_USER"].strip(),
        _secret_path(environ["ORACLE_PASSWORD_FILE"]),
    )
    dml = OracleMatrixProfile(
        "dml",
        environ["PLSQLWKS_TEST_ORACLE_DML_USER"].strip(),
        _secret_path(environ["PLSQLWKS_TEST_ORACLE_DML_PASSWORD_FILE"]),
    )
    read_only = OracleMatrixProfile(
        "read_only",
        environ["PLSQLWKS_TEST_ORACLE_READ_ONLY_USER"].strip(),
        _secret_path(environ["PLSQLWKS_TEST_ORACLE_READ_ONLY_PASSWORD_FILE"]),
    )
    profiles = (developer, dml, read_only)
    for profile in profiles:
        if not _SIMPLE_ORACLE_IDENTIFIER_RE.fullmatch(profile.user):
            raise OracleMatrixConfigurationError(
                f"Oracle matrix {profile.label} user must be a simple unquoted identifier"
            )
        if profile.user.upper().startswith("SYS") or profile.user.upper().startswith("C##"):
            raise OracleMatrixConfigurationError(
                f"Oracle matrix {profile.label} user must be a local non-system account"
            )
    if len({profile.user.casefold() for profile in profiles}) != len(profiles):
        raise OracleMatrixConfigurationError(
            "Oracle matrix developer, DML, and read-only users must be distinct"
        )

    easy_connect_dsn = environ["ORACLE_DSN"].strip()
    descriptor_dsn = environ["PLSQLWKS_TEST_ORACLE_DESCRIPTOR_DSN"].strip()
    if not is_easy_connect_dsn(easy_connect_dsn):
        raise OracleMatrixConfigurationError(
            "ORACLE_DSN must use Easy Connect syntax in matrix mode"
        )
    if not is_connect_descriptor(descriptor_dsn):
        raise OracleMatrixConfigurationError(
            "PLSQLWKS_TEST_ORACLE_DESCRIPTOR_DSN must be a full connect descriptor"
        )
    if easy_connect_dsn == descriptor_dsn:
        raise OracleMatrixConfigurationError(
            "Oracle matrix Easy Connect and descriptor DSNs must be distinct"
        )

    guard_token_file = _secret_path(environ["PLSQLWKS_TEST_ORACLE_GUARD_TOKEN_FILE"])
    guard_token = read_secret_file(
        guard_token_file,
        "PLSQLWKS_TEST_ORACLE_GUARD_TOKEN_FILE",
    )
    if not re.fullmatch(r"[0-9a-f]{64}", guard_token):
        raise OracleMatrixConfigurationError(
            "PLSQLWKS_TEST_ORACLE_GUARD_TOKEN_FILE must contain one lowercase 256-bit hex token"
        )

    return OracleMatrixConfig(
        target=target,
        easy_connect_dsn=easy_connect_dsn,
        descriptor_dsn=descriptor_dsn,
        developer=developer,
        dml=dml,
        read_only=read_only,
        expected_db_unique_name=environ[
            "PLSQLWKS_TEST_ORACLE_EXPECTED_DB_UNIQUE_NAME"
        ].strip(),
        expected_con_name=environ["PLSQLWKS_TEST_ORACLE_EXPECTED_CON_NAME"].strip(),
        expected_service_name=environ[
            "PLSQLWKS_TEST_ORACLE_EXPECTED_SERVICE_NAME"
        ].strip(),
        guard_token_file=guard_token_file,
    )


def _secret_path(value: str) -> Path:
    return Path(os.path.expanduser(value))


def _validate_secret_file(name: str, value: str) -> None:
    path = _secret_path(value)
    try:
        valid = path.is_file() and not path.is_symlink() and path.stat().st_size > 0
        if valid and os.name == "posix":
            valid = path.stat().st_mode & 0o077 == 0
    except OSError:
        valid = False
    if not valid:
        raise OracleMatrixConfigurationError(
            f"{name} must be a private, nonempty, regular non-symlink file"
        )


def read_secret_file(path: Path, label: str) -> str:
    try:
        value = read_password(path)
    except (OSError, UnicodeError):
        raise OracleMatrixConfigurationError(f"{label} could not be read") from None
    if "\x00" in value:
        raise OracleMatrixConfigurationError(f"{label} contains invalid data")
    return value


def is_easy_connect_dsn(value: str) -> bool:
    if not value or any(char.isspace() for char in value) or "(" in value or ")" in value:
        return False
    if value.casefold().startswith("tcps://"):
        return False
    without_protocol = re.sub(r"^tcp://", "", value, flags=re.IGNORECASE)
    authority, separator, service = without_protocol.partition("/")
    if not separator or not service or "/" in service:
        return False
    if authority.startswith("["):
        return re.fullmatch(r"\[[^\]]+\]:\d+", authority) is not None
    return re.fullmatch(r"[^/:]+:\d+", authority) is not None


def is_connect_descriptor(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).upper()
    return bool(
        compact.startswith("(DESCRIPTION=")
        and "(CONNECT_DATA=" in compact
        and "(PROTOCOL=TCP)" in compact
        and compact.count("(") == compact.count(")")
    )


def version_matches_target(target: str, version: str) -> bool:
    components = tuple(int(part) for part in re.findall(r"\d+", version))
    if not components:
        return False
    if target == "19c":
        return components[0] == 19
    if target == "26ai":
        return len(components) >= 2 and components[0] == 23 and components[1] >= 26
    return False


def run_oracle_matrix_preflight(config: OracleMatrixConfig) -> VerifiedOracleMatrix:
    if not oracledb.is_thin_mode():
        raise OracleMatrixSafetyError("Oracle matrix requires python-oracledb Thin mode")

    verified_version = ""
    for profile_label in ("developer", "dml", "read_only"):
        for dsn_style in ("easy_connect", "descriptor"):
            identity = verify_oracle_session(config, profile_label, dsn_style)
            if not verified_version:
                verified_version = identity.server_version
            elif identity.server_version != verified_version:
                raise OracleMatrixSafetyError(
                    "Oracle matrix DSNs or profiles reported different server versions"
                )

    lock_workspace = _acquire_matrix_lock(config)
    return VerifiedOracleMatrix(
        config=config,
        server_version=verified_version,
        driver_version=oracledb.__version__,
        driver_mode="thin",
        lock_workspace=lock_workspace,
    )


def verify_oracle_session(
    config: OracleMatrixConfig,
    profile_label: str,
    dsn_style: str,
) -> OracleSessionIdentity:
    profile = config.profile(profile_label)
    workspace: OracleWorkspace | None = None
    try:
        workspace = OracleWorkspace(config.app_config(profile_label, dsn_style))
        try:
            connection = workspace.ensure_connected()
        except Exception:
            raise OracleMatrixSafetyError(
                f"Oracle matrix {profile_label}/{dsn_style} connection failed"
            ) from None
        identity = _read_session_identity(connection)
        _verify_identity(config, profile, identity)
        _verify_privileges(connection, profile_label, config)
        if profile_label == "developer":
            _verify_guard(connection, config)
        else:
            _verify_fixture_read(connection, config.developer.sql_identifier)
        return identity
    except OracleMatrixSafetyError:
        raise
    except Exception:
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile_label}/{dsn_style} preflight failed"
        ) from None
    finally:
        if workspace is not None:
            try:
                workspace.close()
            except Exception:
                pass


def _read_session_identity(connection: oracledb.Connection) -> OracleSessionIdentity:
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            select sys_context('USERENV', 'SESSION_USER'),
                   sys_context('USERENV', 'CURRENT_SCHEMA'),
                   sys_context('USERENV', 'DB_UNIQUE_NAME'),
                   sys_context('USERENV', 'CON_NAME'),
                   sys_context('USERENV', 'SERVICE_NAME'),
                   sys_context('USERENV', 'AUTHENTICATION_METHOD'),
                   sys_context('USERENV', 'PROXY_USER'),
                   sys_context('USERENV', 'ISDBA'),
                   sys_context('USERENV', 'DATABASE_ROLE'),
                   common,
                   oracle_maintained,
                   inherited,
                   implicit,
                   all_shard,
                   proxy_only_connect
            from user_users
            """
        )
        row = cursor.fetchone()
        if row is None:
            raise OracleMatrixSafetyError("Oracle matrix identity query returned no row")
        return OracleSessionIdentity(
            session_user=str(row[0] or ""),
            current_schema=str(row[1] or ""),
            db_unique_name=str(row[2] or ""),
            con_name=str(row[3] or ""),
            service_name=str(row[4] or ""),
            authentication_method=str(row[5] or ""),
            proxy_user=str(row[6] or ""),
            is_dba=str(row[7] or ""),
            database_role=str(row[8] or ""),
            common_user=str(row[9] or ""),
            oracle_maintained=str(row[10] or ""),
            inherited_user=str(row[11] or ""),
            implicit_user=str(row[12] or ""),
            all_shard_user=str(row[13] or ""),
            proxy_only_connect=str(row[14] or ""),
            server_version=str(connection.version),
        )
    finally:
        cursor.close()


def _verify_identity(
    config: OracleMatrixConfig,
    profile: OracleMatrixProfile,
    identity: OracleSessionIdentity,
) -> None:
    expected_user = profile.user.casefold()
    if identity.session_user.casefold() != expected_user:
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile.label} session user did not match its configured profile"
        )
    if identity.current_schema.casefold() != expected_user:
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile.label} current schema did not match its session user"
        )
    expected_identity = (
        config.expected_db_unique_name.casefold(),
        config.expected_con_name.casefold(),
        config.expected_service_name.casefold(),
    )
    actual_identity = (
        identity.db_unique_name.casefold(),
        identity.con_name.casefold(),
        identity.service_name.casefold(),
    )
    if actual_identity != expected_identity:
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile.label} endpoint fingerprint did not match"
        )
    if identity.authentication_method.upper() != "PASSWORD":
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile.label} did not use password authentication"
        )
    if identity.proxy_user:
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile.label} unexpectedly used a proxy user"
        )
    if identity.is_dba.upper() != "FALSE":
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile.label} unexpectedly has DBA session status"
        )
    if identity.database_role.upper() != "PRIMARY":
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile.label} endpoint is not the expected primary database"
        )
    if (
        identity.common_user.upper() != "NO"
        or identity.oracle_maintained.upper() != "N"
        or identity.inherited_user.upper() != "NO"
        or identity.implicit_user.upper() != "NO"
        or identity.all_shard_user.upper() != "NO"
        or identity.proxy_only_connect.upper() != "N"
    ):
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile.label} must be a local application account"
        )
    if not version_matches_target(config.target, identity.server_version):
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile.label} server version did not match target {config.target}"
        )


def _verify_privileges(
    connection: oracledb.Connection,
    profile_label: str,
    config: OracleMatrixConfig,
) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute("select privilege from session_privs")
        privileges = frozenset(str(row[0]).upper() for row in cursor)
        cursor.execute("select role from session_roles")
        roles = frozenset(str(row[0]).upper() for row in cursor)
        cursor.execute(
            "select privilege, admin_option, common, inherited from user_sys_privs"
        )
        direct_system_grants = frozenset(
            tuple(str(value or "").upper() for value in row) for row in cursor
        )
        cursor.execute("select granted_role from user_role_privs")
        received_roles = frozenset(str(row[0]).upper() for row in cursor)
        cursor.execute(
            """
            select owner, table_name, grantor, privilege, grantable,
                   hierarchy, common, type, inherited
            from user_tab_privs_recd
            """
        )
        object_grants = frozenset(
            tuple(str(value or "").upper() for value in row) for row in cursor
        )
        cursor.execute("select count(*) from user_col_privs_recd")
        received_column_grant_count = int(cursor.fetchone()[0])
        cursor.execute(
            """
            select grantee, table_name, grantor, privilege, grantable,
                   hierarchy, common, type, inherited
            from user_tab_privs_made
            """
        )
        made_object_grants = frozenset(
            tuple(str(value or "").upper() for value in row) for row in cursor
        )
        cursor.execute("select count(*) from user_col_privs_made")
        made_column_grant_count = int(cursor.fetchone()[0])
        cursor.execute("select count(*) from user_objects")
        owned_object_count = int(cursor.fetchone()[0])
        cursor.execute("select max_bytes from user_ts_quotas")
        tablespace_quotas = tuple(int(row[0]) for row in cursor)
        cursor.execute(
            """
            select resource_name, limit
            from user_resource_limits
            where resource_name in ('CONNECT_TIME', 'IDLE_TIME')
            """
        )
        session_time_limits = {
            str(row[0]).upper(): str(row[1] or "").upper() for row in cursor
        }
        cursor.execute(
            """
            select grantee, owner, table_name, grantor, privilege, grantable,
                   hierarchy, common, type, inherited
            from all_tab_privs_recd
            where owner = :developer_owner
              and table_name in (:guard_table, :fixture_table)
            """,
            developer_owner=config.developer.sql_identifier,
            guard_table=GUARD_TABLE,
            fixture_table=FIXTURE_TABLE,
        )
        effective_test_object_grants = frozenset(
            tuple(str(value or "").upper() for value in row) for row in cursor
        )
        cursor.execute(
            """
            select count(*)
            from all_tab_privs_recd
            where grantee = 'PUBLIC'
              and privilege in ('ALTER', 'DELETE', 'INDEX', 'INSERT',
                                'REFERENCES', 'UPDATE')
            """
        )
        public_table_mutation_grant_count = int(cursor.fetchone()[0])
        cursor.execute(
            """
            select count(*)
            from all_col_privs_recd
            where grantee = 'PUBLIC'
            """
        )
        public_column_grant_count = int(cursor.fetchone()[0])
        session_schema_grants: frozenset[tuple[str, ...]] = frozenset()
        direct_schema_grants: frozenset[tuple[str, ...]] = frozenset()
        if config.target == "26ai":
            cursor.execute("select privilege, schema from session_schema_privs")
            session_schema_grants = frozenset(
                tuple(str(value or "").upper() for value in row) for row in cursor
            )
            cursor.execute(
                """
                select username, privilege, schema, admin_option, common, inherited
                from user_schema_privs
                """
            )
            direct_schema_grants = frozenset(
                tuple(str(value or "").upper() for value in row) for row in cursor
            )
    finally:
        cursor.close()
    validate_privilege_contract(profile_label, privileges, roles)
    expected_direct_system_grants = frozenset(
        (privilege, "NO", "NO", "NO") for privilege in privileges
    )
    if direct_system_grants != expected_direct_system_grants or received_roles:
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile_label} direct system grant contract did not match"
        )
    validate_schema_privilege_contract(
        profile_label,
        session_schema_grants,
        direct_schema_grants,
    )
    validate_object_grant_contract(
        profile_label,
        config.developer.sql_identifier,
        object_grants,
    )
    validate_schema_isolation_contract(
        profile_label,
        received_column_grant_count=received_column_grant_count,
        made_column_grant_count=made_column_grant_count,
        owned_object_count=owned_object_count,
        public_table_mutation_grant_count=public_table_mutation_grant_count,
        public_column_grant_count=public_column_grant_count,
    )
    validate_quota_contract(profile_label, tablespace_quotas)
    validate_session_time_limit_contract(profile_label, session_time_limits)
    validate_made_object_grant_contract(profile_label, config, made_object_grants)
    validate_effective_test_object_grant_contract(
        profile_label,
        config,
        effective_test_object_grants,
    )


def validate_privilege_contract(
    profile_label: str,
    privileges: frozenset[str],
    roles: frozenset[str],
) -> None:
    expected = DEVELOPER_PRIVILEGES if profile_label == "developer" else RESTRICTED_PRIVILEGES
    if roles:
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile_label} must use direct grants and no roles"
        )
    if privileges != expected:
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile_label} system privilege contract did not match"
        )


def validate_object_grant_contract(
    profile_label: str,
    developer_owner: str,
    object_grants: frozenset[tuple[str, ...]],
) -> None:
    if profile_label == "developer":
        expected = frozenset(
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
    elif profile_label == "dml":
        expected = frozenset(
            {
                ("SYS", "DBMS_OUTPUT", "SYS", "EXECUTE", "NO", "NO", "NO", "PACKAGE", "NO"),
                (developer_owner, FIXTURE_TABLE, developer_owner, "SELECT", "NO", "NO", "NO", "TABLE", "NO"),
                (developer_owner, FIXTURE_TABLE, developer_owner, "INSERT", "NO", "NO", "NO", "TABLE", "NO"),
                (developer_owner, FIXTURE_TABLE, developer_owner, "UPDATE", "NO", "NO", "NO", "TABLE", "NO"),
                (developer_owner, FIXTURE_TABLE, developer_owner, "DELETE", "NO", "NO", "NO", "TABLE", "NO"),
            }
        )
    else:
        expected = frozenset(
            {
                ("SYS", "DBMS_OUTPUT", "SYS", "EXECUTE", "NO", "NO", "NO", "PACKAGE", "NO"),
                (developer_owner, FIXTURE_TABLE, developer_owner, "SELECT", "NO", "NO", "NO", "TABLE", "NO"),
            }
        )
    if object_grants != expected:
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile_label} direct object grant contract did not match"
        )


def validate_schema_isolation_contract(
    profile_label: str,
    *,
    received_column_grant_count: int,
    made_column_grant_count: int,
    owned_object_count: int,
    public_table_mutation_grant_count: int,
    public_column_grant_count: int,
) -> None:
    if (
        received_column_grant_count
        or made_column_grant_count
        or public_table_mutation_grant_count
        or public_column_grant_count
        or (profile_label != "developer" and owned_object_count)
    ):
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile_label} schema isolation contract did not match"
        )


def validate_schema_privilege_contract(
    profile_label: str,
    session_schema_grants: frozenset[tuple[str, ...]],
    direct_schema_grants: frozenset[tuple[str, ...]],
) -> None:
    if session_schema_grants or direct_schema_grants:
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile_label} schema privilege contract did not match"
        )


def validate_quota_contract(
    profile_label: str,
    tablespace_quotas: tuple[int, ...],
) -> None:
    if profile_label == "developer":
        valid = len(tablespace_quotas) == 1 and 0 < tablespace_quotas[0] <= 64 * 1024 * 1024
    else:
        valid = not tablespace_quotas
    if not valid:
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile_label} tablespace quota contract did not match"
        )


def validate_session_time_limit_contract(
    profile_label: str,
    session_time_limits: Mapping[str, str],
) -> None:
    if profile_label == "developer" and session_time_limits != {
        "CONNECT_TIME": "UNLIMITED",
        "IDLE_TIME": "UNLIMITED",
    }:
        raise OracleMatrixSafetyError(
            "Oracle matrix developer session time limits could release its guard lock"
        )


def validate_made_object_grant_contract(
    profile_label: str,
    config: OracleMatrixConfig,
    made_object_grants: frozenset[tuple[str, ...]],
) -> None:
    expected: frozenset[tuple[str, ...]] = frozenset()
    if profile_label == "developer":
        owner = config.developer.sql_identifier
        expected = frozenset(
            {
                (config.dml.sql_identifier, FIXTURE_TABLE, owner, "SELECT", "NO", "NO", "NO", "TABLE", "NO"),
                (config.dml.sql_identifier, FIXTURE_TABLE, owner, "INSERT", "NO", "NO", "NO", "TABLE", "NO"),
                (config.dml.sql_identifier, FIXTURE_TABLE, owner, "UPDATE", "NO", "NO", "NO", "TABLE", "NO"),
                (config.dml.sql_identifier, FIXTURE_TABLE, owner, "DELETE", "NO", "NO", "NO", "TABLE", "NO"),
                (config.read_only.sql_identifier, FIXTURE_TABLE, owner, "SELECT", "NO", "NO", "NO", "TABLE", "NO"),
            }
        )
    if made_object_grants != expected:
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile_label} outgoing object grant contract did not match"
        )


def validate_effective_test_object_grant_contract(
    profile_label: str,
    config: OracleMatrixConfig,
    effective_grants: frozenset[tuple[str, ...]],
) -> None:
    owner = config.developer.sql_identifier
    expected: frozenset[tuple[str, ...]] = frozenset()
    if profile_label == "dml":
        expected = frozenset(
            (
                config.dml.sql_identifier,
                owner,
                FIXTURE_TABLE,
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
    elif profile_label == "read_only":
        expected = frozenset(
            {
                (
                    config.read_only.sql_identifier,
                    owner,
                    FIXTURE_TABLE,
                    owner,
                    "SELECT",
                    "NO",
                    "NO",
                    "NO",
                    "TABLE",
                    "NO",
                )
            }
        )
    if effective_grants != expected:
        raise OracleMatrixSafetyError(
            f"Oracle matrix {profile_label} effective test-object grant contract did not match"
        )


def _verify_guard(connection: oracledb.Connection, config: OracleMatrixConfig) -> None:
    token = read_secret_file(
        config.guard_token_file,
        "PLSQLWKS_TEST_ORACLE_GUARD_TOKEN_FILE",
    )
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"""
            select count(*)
            from {GUARD_TABLE}
            where guard_name = :guard_name
              and token_sha256 = lower(rawtohex(standard_hash(:guard_token, 'SHA256')))
            """,
            guard_name=GUARD_NAME,
            guard_token=token,
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    if not row or int(row[0]) != 1:
        raise OracleMatrixSafetyError("Oracle matrix disposable-environment guard did not match")


def _acquire_matrix_lock(config: OracleMatrixConfig) -> OracleWorkspace:
    workspace: OracleWorkspace | None = None
    try:
        workspace = OracleWorkspace(config.app_config("developer", "easy_connect"))
        workspace.ensure_connected()
        _lock_verified_guard(workspace, config, require_existing=False)
        return workspace
    except OracleMatrixSafetyError:
        if workspace is not None:
            try:
                workspace.close()
            except Exception:
                pass
        raise
    except Exception:
        if workspace is not None:
            try:
                workspace.close()
            except Exception:
                pass
        raise OracleMatrixSafetyError(
            "Oracle matrix endpoint is already in use or could not be locked"
        ) from None


def _lock_verified_guard(
    workspace: OracleWorkspace,
    config: OracleMatrixConfig,
    *,
    require_existing: bool,
) -> None:
    try:
        token = read_secret_file(
            config.guard_token_file,
            "PLSQLWKS_TEST_ORACLE_GUARD_TOKEN_FILE",
        )
        connection = workspace.connection
        if connection is None or workspace.autocommit or connection.autocommit:
            raise OracleMatrixSafetyError("Oracle matrix endpoint lock is unavailable")
        if require_existing and not connection.transaction_in_progress:
            raise OracleMatrixSafetyError("Oracle matrix endpoint lock is unavailable")
        connection.call_timeout = 5_000
        connection.ping()
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"""
                select token_sha256
                from {GUARD_TABLE}
                where guard_name = :guard_name
                  and token_sha256 = lower(rawtohex(standard_hash(:guard_token, 'SHA256')))
                for update nowait
                """,
                guard_name=GUARD_NAME,
                guard_token=token,
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
        if not connection.transaction_in_progress:
            raise OracleMatrixSafetyError("Oracle matrix endpoint lock is unavailable")
    except OracleMatrixSafetyError:
        raise
    except Exception:
        raise OracleMatrixSafetyError("Oracle matrix endpoint lock is unavailable") from None
    if row is None:
        raise OracleMatrixSafetyError("Oracle matrix endpoint lock guard did not match")


def _verify_fixture_read(connection: oracledb.Connection, owner: str) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"select probe_value from {owner}.{FIXTURE_TABLE} where probe_id = :probe_id",
            probe_id="READ_ONLY_BASELINE",
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    if not row or str(row[0]) != "compatibility fixture":
        raise OracleMatrixSafetyError("Oracle matrix compatibility fixture is unavailable")
