import pytest

import plsqlwks.sqlsplit as sqlsplit_module
from plsqlwks.sqlsplit import (
    ScriptPreflightIssue,
    is_plsql_like,
    preflight_script,
    split_script,
    statement_at_cursor,
)


def test_splits_simple_sql_statements():
    statements = split_script("select 1 from dual;\nselect 'a;b' from dual;\n")
    assert [statement.text for statement in statements] == [
        "select 1 from dual",
        "select 'a;b' from dual",
    ]


def test_keeps_plsql_until_slash():
    script = """create or replace procedure p as
begin
  null;
end;
/
select 1 from dual;
"""
    statements = split_script(script)
    assert len(statements) == 2
    assert statements[0].text.startswith("create or replace procedure p")
    assert statements[0].text.endswith("end;")
    assert statements[1].text == "select 1 from dual"


def test_plsql_slash_delimiter_allows_trailing_comments():
    script = """begin
  null;
end;
/ -- execute the block
select 2 from dual;
"""

    assert [statement.text for statement in split_script(script)] == [
        "begin\n  null;\nend;",
        "select 2 from dual",
    ]


def test_comment_only_scripts_and_trailing_comments_are_not_statements():
    assert split_script("-- only a comment\n/* and another */\n") == []
    assert [
        statement.text for statement in split_script("select 1 from dual; -- trailing\n/* trailing block */\n")
    ] == ["select 1 from dual"]


def test_leading_comments_remain_attached_to_a_real_statement():
    statements = split_script("-- context\n\nselect 1 from dual;\n")

    assert [statement.text for statement in statements] == ["-- context\n\nselect 1 from dual"]


def test_splitter_preserves_blank_lines_and_non_newline_control_characters():
    payload = "first\u2028second\u0085third\vfourth\ffifth"
    script = f"select q'[line one\n\n{payload}]' from dual;\n"

    statements = split_script(script)

    assert statements[0].text == script.removesuffix(";\n")


def test_keeps_labeled_anonymous_plsql_until_slash():
    script = """<<outer_block>>
/* between labels */
<<inner_block>>
begin
  null;
end;
/
select 1 from dual;
"""

    statements = split_script(script)

    assert is_plsql_like(statements[0].text) is True
    assert [statement.text for statement in statements] == [
        "<<outer_block>>\n/* between labels */\n<<inner_block>>\nbegin\n  null;\nend;",
        "select 1 from dual",
    ]


def test_preflight_reports_client_directives_and_substitutions_with_document_locations():
    script = """prompt Deploying
select &owner.table_name, &&column_name from dual;
  set serveroutput on
@@next_script.sql
"""

    assert preflight_script(script) == [
        ScriptPreflightIssue(
            "directive",
            "Unsupported SQL*Plus/SQLcl directive 'prompt'.",
            1,
            1,
        ),
        ScriptPreflightIssue(
            "substitution",
            "SQL*Plus substitution variable '&owner' is not supported.",
            2,
            8,
        ),
        ScriptPreflightIssue(
            "substitution",
            "SQL*Plus substitution variable '&&column_name' is not supported.",
            2,
            27,
        ),
        ScriptPreflightIssue(
            "directive",
            "Unsupported SQL*Plus/SQLcl directive 'set'.",
            3,
            3,
        ),
        ScriptPreflightIssue(
            "directive",
            "Unsupported SQL*Plus/SQLcl directive '@@'.",
            4,
            1,
        ),
    ]


def test_preflight_accepts_oracle_set_statements_and_ignores_literals_and_comments():
    script = """set transaction read only;
set role reporting;
set constraint decisions_fk immediate;
set constraints all deferred;
select '&literal', q'[&&q_literal]', "&quoted_identifier" from dual;
-- prompt &commented
/* spool &&also_commented */
"""

    assert preflight_script(script) == [
        ScriptPreflightIssue(
            "substitution",
            "SQL*Plus substitution variable '&quoted_identifier' is not supported.",
            5,
            39,
        )
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "set\n  transaction read only;",
        "set\n  role reporting;",
        "set\n  constraint decisions_fk immediate;",
        "set\n  constraints all deferred;",
        "set /* native SQL */\n  transaction read write;",
    ],
)
def test_preflight_accepts_multiline_native_set_statements(statement):
    assert preflight_script(statement) == []


@pytest.mark.parametrize(
    "command",
    [
        "prom deploying",
        "promp deploying",
        "spoo deployment.log",
        "colu status format a20",
        "apex export 100",
        "repeat 3 1",
        "soda list",
        "datapump export schema",
        "dp export schema",
        "connmgr list",
        "cm list",
        "diff schema one schema two",
        "di schema one schema two",
        "objectstorage list",
        "lb status",
        "proj init",
        "? history",
    ],
)
def test_preflight_reports_intermediate_abbreviations_and_current_sqlcl_commands(command):
    issue = preflight_script(command)[0]

    assert issue.kind == "directive"
    assert (issue.line, issue.column) == (1, 1)


def test_preflight_does_not_treat_command_words_inside_sql_as_directives():
    script = """select
  apex,
  repeat,
  soda
from command_log;
with project as (select 1 as id from dual)
select project.id from project;
"""

    assert preflight_script(script) == []


def test_preflight_reports_slash_reexecution_but_accepts_plsql_delimiter():
    script = """/
select 1 from dual;
  / -- rerun the SQL buffer
begin
  null;
end;
/ -- terminate the PL/SQL block
"""

    assert preflight_script(script) == [
        ScriptPreflightIssue(
            "directive",
            "Unsupported SQL*Plus/SQLcl directive '/'.",
            1,
            1,
        ),
        ScriptPreflightIssue(
            "directive",
            "Unsupported SQL*Plus/SQLcl directive '/'.",
            3,
            3,
        ),
    ]


def test_preflight_reports_standalone_semicolon_buffer_command():
    assert preflight_script("  ; -- list the SQLcl buffer\n") == [
        ScriptPreflightIssue(
            "directive",
            "Unsupported SQL*Plus/SQLcl directive ';'.",
            1,
            3,
        )
    ]


def test_preflight_does_not_treat_plsql_identifiers_as_directives():
    script = """<<run_block>>
declare
  prompt varchar2(20) := 'spool &ignored';
begin
  prompt := q'[set &&ignored]';
  execute_work(prompt);
end;
/
show errors
"""

    assert preflight_script(script) == [
        ScriptPreflightIssue(
            "directive",
            "Unsupported SQL*Plus/SQLcl directive 'show'.",
            9,
            1,
        )
    ]


@pytest.mark.parametrize("editioning", ["editionable", "noneditionable"])
@pytest.mark.parametrize(
    "object_clause",
    ["procedure", "function", "package", "package body", "trigger", "type", "type body"],
)
def test_keeps_editioned_plsql_objects_until_slash(editioning, object_clause):
    script = f"""create or replace {editioning} {object_clause} demo as
begin
  null;
end;
/
select 1 from dual;
"""

    statements = split_script(script)

    assert [statement.text for statement in statements] == [
        f"create or replace {editioning} {object_clause} demo as\nbegin\n  null;\nend;",
        "select 1 from dual",
    ]


def test_keeps_editioned_plsql_without_or_replace_until_slash():
    script = """create editionable function demo return number as
begin
  return 1;
end;
/
select 1 from dual;
"""

    assert [statement.text for statement in split_script(script)] == [
        "create editionable function demo return number as\nbegin\n  return 1;\nend;",
        "select 1 from dual",
    ]


def test_plsql_detection_ignores_comments_between_create_qualifiers():
    script = """-- deployment header
/* generated object */
create/* after create */or -- after or
replace/* after replace */noneditionable/* after editioning */package/* before body */body demo as
  procedure run;
end;
/
select 1 from dual;
"""

    statements = split_script(script)

    assert len(statements) == 2
    assert statements[0].text.startswith("-- deployment header\n/* generated object */\ncreate")
    assert statements[0].text.endswith("end;")
    assert statements[1].text == "select 1 from dual"


def test_plsql_detection_does_not_misclassify_ordinary_create_ddl():
    statement = "create table procedure_log (id number)"

    assert is_plsql_like(statement) is False


def test_keeps_plsql_declare_block_with_leading_line_comment_until_slash():
    script = """-- setup before declaration
declare
  x number;
begin
  null;
end;
/
select 1 from dual;
"""
    statements = split_script(script)

    assert [statement.text for statement in statements] == [
        "-- setup before declaration\ndeclare\n  x number;\nbegin\n  null;\nend;",
        "select 1 from dual",
    ]
    assert [(statement.start_line, statement.end_line) for statement in statements] == [(1, 6), (8, 8)]
    assert statement_at_cursor(script, 1, 1).text == statements[0].text
    assert statement_at_cursor(script, 3, 1).text == statements[0].text


def test_keeps_plsql_declare_block_with_leading_block_comment_until_slash():
    script = """/* setup before declaration */
declare
  x number;
begin
  null;
end;
/
"""
    statements = split_script(script)

    assert len(statements) == 1
    assert statements[0].text == "/* setup before declaration */\ndeclare\n  x number;\nbegin\n  null;\nend;"


def test_statement_at_cursor():
    script = "select 1 from dual;\nselect 2 from dual;\n"
    assert statement_at_cursor(script, 1).text == "select 2 from dual"


def test_statement_at_cursor_uses_column_for_same_line_statements():
    script = "select 1 from dual; select 2 from dual; select 3 from dual;\n"
    statements = split_script(script)

    assert [(statement.text, statement.start_col, statement.end_col) for statement in statements] == [
        ("select 1 from dual", 0, 19),
        ("select 2 from dual", 20, 39),
        ("select 3 from dual", 40, 59),
    ]
    assert statement_at_cursor(script, 0, 3).text == "select 1 from dual"
    assert statement_at_cursor(script, 0, 25).text == "select 2 from dual"
    assert statement_at_cursor(script, 0, 45).text == "select 3 from dual"


def test_statement_at_cursor_line_only_call_keeps_first_same_line_statement():
    script = "select 1 from dual; select 2 from dual;\n"

    assert statement_at_cursor(script, 0).text == "select 1 from dual"


def test_ignores_semicolons_in_comments_and_block_comments():
    script = """select 1 /* ; still comment */ from dual;
select 2 from dual -- ; still comment
;
"""
    statements = split_script(script)

    assert [statement.text for statement in statements] == [
        "select 1 /* ; still comment */ from dual",
        "select 2 from dual -- ; still comment",
    ]


def test_line_comment_ends_at_newline():
    script = "select 1 -- comment\nfrom dual;\nselect 2 from dual;\n"

    assert [statement.text for statement in split_script(script)] == [
        "select 1 -- comment\nfrom dual",
        "select 2 from dual",
    ]


def test_ignores_semicolons_in_multiline_block_comments_and_quoted_identifiers():
    script = """select "semi;colon" from dual;
select 1
/* ; still comment
   ; still comment */
from dual;
"""
    statements = split_script(script)

    assert [statement.text for statement in statements] == [
        'select "semi;colon" from dual',
        "select 1\n/* ; still comment\n   ; still comment */\nfrom dual",
    ]


def test_ignores_semicolons_and_apostrophes_in_oracle_q_quotes():
    script = """select q'[a'b;c]' from dual;
select Q'{x'y;z}' from dual;
select q'<m'n;o>' from dual;
select q'!p'q;r!' from dual;
"""
    statements = split_script(script)

    assert [statement.text for statement in statements] == [
        "select q'[a'b;c]' from dual",
        "select Q'{x'y;z}' from dual",
        "select q'<m'n;o>' from dual",
        "select q'!p'q;r!' from dual",
    ]
    assert statement_at_cursor(script, 0, 20).text == "select q'[a'b;c]' from dual"
    assert statement_at_cursor(script, 1, 5).text == "select Q'{x'y;z}' from dual"


def test_keeps_plsql_q_quote_until_slash():
    script = """begin
  dbms_output.put_line(q'[a'b;c]');
end;
/
select 2 from dual;
"""
    statements = split_script(script)

    assert [statement.text for statement in statements] == [
        "begin\n  dbms_output.put_line(q'[a'b;c]');\nend;",
        "select 2 from dual",
    ]


def test_keeps_with_plsql_declarations_and_main_sql_as_one_sql_statement():
    script = """with
  function normalize_value(p_value number) return number is
    function fallback_value return number is
    begin
      return 1;
    end fallback_value;
  begin
    if p_value is null then
      return case when fallback_value() = 1 then 0 else 2 end;
    end if;
    return p_value;
  end normalize_value;
  procedure audit_value is
  begin
    null;
  end audit_value;
select normalize_value(2) from dual;
select 9 from dual;
"""

    statements = split_script(script)

    assert len(statements) == 2
    assert preflight_script(script) == []
    assert (
        statements[0].text
        == """with
  function normalize_value(p_value number) return number is
    function fallback_value return number is
    begin
      return 1;
    end fallback_value;
  begin
    if p_value is null then
      return case when fallback_value() = 1 then 0 else 2 end;
    end if;
    return p_value;
  end normalize_value;
  procedure audit_value is
  begin
    null;
  end audit_value;
select normalize_value(2) from dual"""
    )
    assert statements[1].text == "select 9 from dual"
    assert statement_at_cursor(script, 4, 6) == statements[0]
    assert statement_at_cursor(script, 16, 8) == statements[0]
    assert statement_at_cursor(script, 17, 8) == statements[1]


@pytest.mark.parametrize("declaration", ["function", "procedure"])
def test_keeps_single_with_plsql_declaration_until_main_sql_terminator(declaration):
    if declaration == "function":
        source = """with
function local_value return number is
begin
  return 1;
end;
select local_value from dual;
"""
    else:
        source = """with
procedure local_action is
begin
  null;
end;
select 1 from dual;
"""

    statements = split_script(source)

    assert len(statements) == 1
    assert statements[0].text.endswith("from dual")
    assert "end;\nselect" in statements[0].text


def test_keeps_same_line_with_function_semicolons_inside_the_sql_statement():
    script = "with function f return number is begin return 1; end; select f() from dual; select 2 from dual;"

    statements = split_script(script)

    assert [statement.text for statement in statements] == [
        "with function f return number is begin return 1; end; select f() from dual",
        "select 2 from dual",
    ]
    assert statement_at_cursor(script, 0, 20) == statements[0]
    assert statement_at_cursor(script, 0, 80) == statements[1]
    assert preflight_script(f"{script}\nprompt done") == [
        ScriptPreflightIssue(
            "directive",
            "Unsupported SQL*Plus/SQLcl directive 'prompt'.",
            2,
            1,
        )
    ]


def test_large_with_function_is_parsed_once_per_operation(monkeypatch):
    assignments = "\n".join("    value := value + 1;" for _ in range(500))
    script = f"""with function f return number is
  value number := 0;
begin
{assignments}
  return value;
end;
select f() from dual;
"""
    original = sqlsplit_module._with_plsql_main_sql_terminator
    calls = 0

    def counted(source):
        nonlocal calls
        calls += 1
        return original(source)

    monkeypatch.setattr(
        sqlsplit_module,
        "_with_plsql_main_sql_terminator",
        counted,
    )

    statements = split_script(script)
    assert len(statements) == 1
    assert calls == 1

    assert preflight_script(script) == []
    assert calls == 2


def test_splits_long_special_sql_case(long_special_sql_case):
    statements = split_script(long_special_sql_case.script)

    assert [statement.text for statement in statements] == long_special_sql_case.expected_statements
    assert [(statement.start_line, statement.end_line) for statement in statements] == (
        long_special_sql_case.expected_ranges
    )


def test_statement_at_cursor_handles_long_special_sql_case(long_special_sql_case):
    for cursor_line, cursor_col, expected_idx in long_special_sql_case.cursor_checks:
        statement = statement_at_cursor(long_special_sql_case.script, cursor_line, cursor_col)

        assert statement is not None
        assert statement.text == long_special_sql_case.expected_statements[expected_idx]


def test_splits_oracle_specific_select_clauses_with_embedded_semicolons():
    script = """select *
from decisions as of timestamp systimestamp
where note = q'[flashback;literal]';

select *
from decisions partition (p2026)
where note = 'partition; literal';

select *
from (
  select status, amount from decisions
)
pivot (
  count(*) for status in ('OPEN' as open_count, 'CLOSED' as closed_count)
);

select *
from decisions
model
  dimension by (id)
  measures (amount)
  rules (amount[1] = q'{semi; inside model}');
"""
    expected = [
        """select *
from decisions as of timestamp systimestamp
where note = q'[flashback;literal]'""",
        """select *
from decisions partition (p2026)
where note = 'partition; literal'""",
        """select *
from (
  select status, amount from decisions
)
pivot (
  count(*) for status in ('OPEN' as open_count, 'CLOSED' as closed_count)
)""",
        """select *
from decisions
model
  dimension by (id)
  measures (amount)
  rules (amount[1] = q'{semi; inside model}')""",
    ]

    statements = split_script(script)

    assert [statement.text for statement in statements] == expected
    assert [(statement.start_line, statement.end_line) for statement in statements] == [
        (1, 3),
        (5, 7),
        (9, 15),
        (17, 22),
    ]
    assert statement_at_cursor(script, 1, 15).text == expected[0]
    assert statement_at_cursor(script, 5, 22).text == expected[1]
    assert statement_at_cursor(script, 13, 4).text == expected[2]
    assert statement_at_cursor(script, 20, 10).text == expected[3]


def test_tracks_blank_line_offsets_for_plsql_slash_termination():
    script = """

begin
  null;
end;
/

select 1 from dual;
"""
    statements = split_script(script)

    assert statements[0].start_line == 3
    assert statements[0].end_line == 5
    assert statements[0].text == "begin\n  null;\nend;"
    assert statements[1].start_line == 8
    assert statements[1].text == "select 1 from dual"


def test_slash_only_terminates_plsql_not_sql():
    script = """select '/' as slash from dual;
/
select 2 from dual;
"""
    statements = split_script(script)

    assert [statement.text for statement in statements] == ["""select '/' as slash from dual""", "select 2 from dual"]


def test_statement_at_cursor_falls_back_to_last_statement():
    script = "select 1 from dual;\n\nselect 2 from dual;\n"

    assert statement_at_cursor(script, 99).text == "select 2 from dual"


def test_statement_at_cursor_before_first_and_between_statements_choose_following():
    script = "\nselect 1 from dual;\n\nselect 2 from dual;\n"

    assert statement_at_cursor(script, 0, 0).text == "select 1 from dual"
    assert statement_at_cursor(script, 2, 0).text == "select 2 from dual"
