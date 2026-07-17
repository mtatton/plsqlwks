from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.oracle_matrix import (
    OracleMatrixConfigurationError,
    OracleMatrixSafetyError,
    VerifiedOracleMatrix,
    load_oracle_matrix_config,
    oracle_matrix_requested,
    run_oracle_matrix_preflight,
)


@dataclass(frozen=True)
class LongSpecialSqlCase:
    script: str
    editor_text: str
    expected_statements: list[str]
    expected_ranges: list[tuple[int, int]]
    cursor_checks: list[tuple[int, int, int]]


def _marker_is_positively_selected(markexpr: str, marker: str) -> bool:
    """Return whether a marker occurs outside a negated expression."""
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[()]", markexpr)
    group_negated = [False]
    pending_not = False
    for token in tokens:
        if token == "not":
            pending_not = not pending_not
        elif token == "(":
            group_negated.append(group_negated[-1] ^ pending_not)
            pending_not = False
        elif token == ")":
            if len(group_negated) > 1:
                group_negated.pop()
            pending_not = False
        elif token in {"and", "or"}:
            pending_not = False
        else:
            negated = group_negated[-1] ^ pending_not
            if token == marker and not negated:
                return True
            pending_not = False
    return False


@pytest.fixture
def long_special_sql_case() -> LongSpecialSqlCase:
    script = """with source_rows as (
  select q'[Příliš žluťoučký kůň; join select]' as note
  from dual
  /* block comment with ; join select union group by */
  where q'!semi;colon and 'quote'!' is not null -- line comment ; join select
)
select note
from source_rows
where note like q'<%kůň;%>';

select q'{a'b;c -- not a comment}' as tricky_text
from dual
where 'join select union group by' = 'join select union group by';

begin
  dbms_output.put_line(q'[slash / stays inside; Příliš]');
  dbms_output.put_line('ordinary; semicolon');
end;
/

select 'final; value' as done from dual;
"""
    expected_statements = [
        """with source_rows as (
  select q'[Příliš žluťoučký kůň; join select]' as note
  from dual
  /* block comment with ; join select union group by */
  where q'!semi;colon and 'quote'!' is not null -- line comment ; join select
)
select note
from source_rows
where note like q'<%kůň;%>'""",
        """select q'{a'b;c -- not a comment}' as tricky_text
from dual
where 'join select union group by' = 'join select union group by'""",
        """begin
  dbms_output.put_line(q'[slash / stays inside; Příliš]');
  dbms_output.put_line('ordinary; semicolon');
end;""",
        "select 'final; value' as done from dual",
    ]
    return LongSpecialSqlCase(
        script=script,
        editor_text=script.rstrip("\n"),
        expected_statements=expected_statements,
        expected_ranges=[(1, 9), (11, 13), (15, 18), (21, 21)],
        cursor_checks=[
            (1, 20, 0),
            (11, 8, 1),
            (15, 25, 2),
            (20, 10, 3),
        ],
    )


@pytest.fixture(scope="session", autouse=True)
def oracle_matrix_preflight():
    """Fail closed before any live matrix test can mutate its endpoint."""

    if not oracle_matrix_requested():
        yield None
        return
    try:
        verification = run_oracle_matrix_preflight(load_oracle_matrix_config())
    except (OracleMatrixConfigurationError, OracleMatrixSafetyError) as exc:
        raise pytest.UsageError(str(exc)) from None
    try:
        yield verification
    finally:
        verification.close()


@pytest.fixture(scope="session")
def oracle_matrix_verification(
    oracle_matrix_preflight: VerifiedOracleMatrix | None,
) -> VerifiedOracleMatrix:
    if oracle_matrix_preflight is None:
        pytest.skip("set PLSQLWKS_TEST_ORACLE_MATRIX=1 to run the Oracle matrix")
    return oracle_matrix_preflight


@pytest.fixture(autouse=True)
def oracle_matrix_lock_liveness(
    request: pytest.FixtureRequest,
    oracle_matrix_preflight: VerifiedOracleMatrix | None,
):
    """Prove the cross-platform guard-row lock before and after each live test."""

    if oracle_matrix_preflight is None or "oracle" not in request.node.keywords:
        yield
        return
    oracle_matrix_preflight.assert_lock_alive()
    try:
        yield
    finally:
        oracle_matrix_preflight.assert_lock_alive()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    markexpr = config.option.markexpr or ""
    oracle_requested = os.environ.get("PLSQLWKS_TEST_ORACLE") == "1"
    matrix_requested = oracle_matrix_requested()
    oracle_explicit = oracle_requested or "oracle" in markexpr
    oracle_reason = "set PLSQLWKS_TEST_ORACLE=1 to run Oracle integration tests"
    run_oracle = False
    if matrix_requested:
        try:
            load_oracle_matrix_config()
        except OracleMatrixConfigurationError as exc:
            raise pytest.UsageError(str(exc)) from None
        run_oracle = True
    elif oracle_requested:
        missing = [name for name in ("ORACLE_USER", "ORACLE_DSN", "ORACLE_PASSWORD_FILE") if not os.environ.get(name)]
        password_file = Path(os.path.expanduser(os.environ.get("ORACLE_PASSWORD_FILE", "")))
        if missing:
            raise pytest.UsageError(f"Oracle integration credentials are missing: {', '.join(missing)}")
        try:
            password_file_is_valid = password_file.is_file() and password_file.stat().st_size > 0
        except OSError:
            password_file_is_valid = False
        if not password_file_is_valid:
            raise pytest.UsageError("Oracle password file must be a nonempty regular file")
        run_oracle = True
    run_pty = os.environ.get("PLSQLWKS_TEST_PTY") == "1" or "pty" in markexpr
    run_slow = os.environ.get("PLSQLWKS_TEST_SLOW") == "1" or "slow" in markexpr
    run_plugins = os.environ.get("PLSQLWKS_TEST_PLUGINS") == "1" or _marker_is_positively_selected(markexpr, "plugin")

    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if (
            ("oracle" in item.keywords and not oracle_explicit)
            or ("oracle_matrix" in item.keywords and not matrix_requested)
            or ("plugin" in item.keywords and not run_plugins)
            or ("pty" in item.keywords and not run_pty)
            or ("slow" in item.keywords and not run_slow)
        ):
            deselected.append(item)
        else:
            selected.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected

    if not run_oracle:
        skip_oracle = pytest.mark.skip(reason=oracle_reason)
        for item in items:
            if "oracle" in item.keywords:
                item.add_marker(skip_oracle)
