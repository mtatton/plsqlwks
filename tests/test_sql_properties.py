from __future__ import annotations

from itertools import product

import pytest

from plsqlwks.db.transactions import read_only_rejection_reason
from plsqlwks.sqlbinds import SqlBind, find_bind_names, find_sql_binds
from plsqlwks.sqlsplit import split_script, statement_at_cursor

PAYLOADS = (
    "plain text",
    "semi;colon / slash",
    ":hidden_bind FOR UPDATE",
    "-- not a comment /* neither */",
    "Příliš žluťoučký kůň 🐍",
    "apostrophe ' and double \" quote",
)
Q_DELIMITERS = (("[", "]"), ("{", "}"), ("(", ")"), ("<", ">"), ("!", "!"))
REAL_BINDS = (
    (":id", "id", False),
    (": ID", "ID", False),
    (':"Mixed Name"', '"Mixed Name"', True),
    (":1", "1", False),
    (":žluť", "žluť", False),
)


def protected_sql_variants(phrase: str | None = None):
    """Yield representative Oracle strings, identifiers, and comments."""
    for payload in (PAYLOADS if phrase is None else (phrase,)):
        yield f"'{payload.replace(chr(39), chr(39) * 2)}'"
        yield f"n'{payload.replace(chr(39), chr(39) * 2)}'"
        yield f'"{payload.replace(chr(34), chr(34) * 2)}"'
        yield f"-- {payload}\n"
        yield f"/* {payload.replace('*/', '* /')} */"
        for prefix, (opener, closer) in product(("q", "nq"), Q_DELIMITERS):
            safe_payload = payload.replace(closer + "'", closer + " ''")
            yield f"{prefix}'{opener}{safe_payload}{closer}'"


def plsql_unit(kind: str, protected: str) -> str:
    if kind == "begin":
        return f"begin\n  consume({protected});\n  null;\nend;"
    if kind == "declare":
        return f"declare\n  value varchar2(100) := {protected};\nbegin\n  consume(value);\nend;"
    if kind == "procedure":
        return f"create procedure p as\nbegin\n  consume({protected});\nend;"
    if kind == "package":
        return f"create package body p as\nprocedure run is begin consume({protected}); end;\nend;"
    return f"create trigger t before insert on decisions begin consume({protected}); end;"


def test_splitter_generated_strings_comments_binds_and_plsql_blocks():
    for protected, kind in product(
        protected_sql_variants(),
        ("sql", "begin", "declare", "procedure", "package", "trigger"),
    ):
        first = (
            f"select {protected}, :value_1 from dual"
            if kind == "sql"
            else plsql_unit(kind, protected)
        )
        terminator = ";" if kind == "sql" else "\n/ -- block delimiter"
        script = f"{first}{terminator}\nselect 2 from dual;"

        statements = split_script(script)

        assert [statement.text for statement in statements] == [first, "select 2 from dual"]
        assert statement_at_cursor(script, statements[0].start_line - 1, statements[0].start_col) == statements[0]


def test_splitter_generated_with_function_bodies():
    for protected in protected_sql_variants():
        first = (
            "with function f return varchar2 is\n"
            f"begin return {protected}; end;\n"
            "select f() from dual"
        )

        assert [statement.text for statement in split_script(f"{first};\nselect 2 from dual;")] == [
            first,
            "select 2 from dual",
        ]


def test_splitter_generated_incomplete_statements_remain_one_fragment():
    closers = {"'": "'", "n'": "'", '"': '"', "q'[": "]'", "nq'{": "}'", "/*": "*/"}
    for opener, payload in product(closers, PAYLOADS):
        safe_payload = payload.replace(closers[opener], "")
        script = f"select {opener}{safe_payload}; / :still_protected"

        statements = split_script(script)

        assert len(statements) == 1
        assert statements[0].text == script


def test_bind_scanner_generated_inputs_have_exact_offsets_and_deduplicate():
    for protected, (rendered, name, quoted) in product(protected_sql_variants(), REAL_BINDS):
        statement = f"select {protected}, {rendered} from dual"
        start = statement.index(rendered, len("select "))

        assert find_sql_binds(statement) == [SqlBind(name, start, start + len(rendered), quoted=quoted)]

    statement = "select " + ", ".join(rendered for rendered, _, _ in (*REAL_BINDS, *REAL_BINDS)) + " from dual"
    assert find_bind_names(statement) == ["id", '"Mixed Name"', "1", "žluť"]


def test_bind_scanner_generated_trigger_and_incomplete_inputs_ignore_decoys():
    for protected, actual_name in product(protected_sql_variants(), ("actual", "žluť", "1")):
        trigger = (
            "create trigger t before update on decisions for each row\nbegin\n"
            f"  :new.note := {protected};\n  :new.value := :old.value;\n"
            f"  consume(:{actual_name});\nend;"
        )
        assert find_bind_names(trigger) == [actual_name]

    closers = {"'": "'", "n'": "'", '"': '"', "q'[": "]'", "nq'{": "}'", "/*": "*/", "--": "\n"}
    for opener, payload in product(closers, PAYLOADS):
        safe_payload = payload.replace(closers[opener], "")
        assert find_sql_binds(f"select {opener}{safe_payload} :hidden_bind") == []


def test_read_only_classifier_generated_protected_and_real_for_update_clauses():
    for protected in protected_sql_variants("FOR UPDATE"):
        statement = f"select :for,:update, 'Příliš žluťoučký kůň', {protected} from dual"
        assert read_only_rejection_reason(statement) == ""

    prefixes = ("select", "SELECT", "SeLeCt", "with x as (select 1 from dual) select")
    gaps = (" ", "\n", "\t", " /* generated gap */ ")
    for prefix, gap in product(prefixes, gaps):
        statement = f"/* leading */ {prefix} * from decisions for{gap}update"
        assert read_only_rejection_reason(statement) == "SELECT FOR UPDATE is disabled in read-only mode"


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("-- comment only", ""),
        ("rollback", ""),
        ("commit", "COMMIT is disabled in read-only mode"),
        ("insert into t values (1)", "DML statements are disabled in read-only mode"),
        ("create table t (id number)", "DDL statements are disabled in read-only mode"),
        ("begin null; end;", "PL/SQL execution is disabled in read-only mode"),
        ("explain plan for select 1 from dual", "EXPLAIN PLAN is disabled in read-only mode because it writes to PLAN_TABLE"),
        ("show parameter", "Only SELECT, WITH, and ROLLBACK statements are allowed in read-only mode"),
    ],
)
def test_read_only_keyword_family_regressions(statement, reason):
    assert read_only_rejection_reason(f"/* Příliš */\n{statement}") == reason
