from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re

import pytest


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


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    markexpr = config.option.markexpr or ""
    oracle_requested = os.environ.get("PLSQLWKS_TEST_ORACLE") == "1"
    oracle_explicit = oracle_requested or "oracle" in markexpr
    oracle_reason = "set PLSQLWKS_TEST_ORACLE=1 to run Oracle integration tests"
    run_oracle = False
    if oracle_requested:
        missing = [
            name
            for name in ("ORACLE_USER", "ORACLE_DSN", "ORACLE_PASSWORD_FILE")
            if not os.environ.get(name)
        ]
        password_file = Path(os.path.expanduser(os.environ.get("ORACLE_PASSWORD_FILE", "")))
        if missing:
            oracle_reason = f"Oracle integration credentials are missing: {', '.join(missing)}"
        elif not password_file.is_file():
            oracle_reason = f"Oracle password file is unavailable: {password_file}"
        else:
            run_oracle = True
    run_pty = os.environ.get("PLSQLWKS_TEST_PTY") == "1" or "pty" in markexpr
    run_slow = os.environ.get("PLSQLWKS_TEST_SLOW") == "1" or "slow" in markexpr
    run_plugins = os.environ.get("PLSQLWKS_TEST_PLUGINS") == "1" or _marker_is_positively_selected(
        markexpr, "plugin"
    )

    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if (
            ("oracle" in item.keywords and not oracle_explicit)
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
