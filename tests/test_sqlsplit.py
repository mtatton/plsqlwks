import pytest

from plsqlwks.sqlsplit import is_plsql_like, split_script, statement_at_cursor


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
    script = (
        "select 1 -- comment\n"
        "from dual;\n"
        "select 2 from dual;\n"
    )

    assert [statement.text for statement in split_script(script)] == [
        "select 1 -- comment\nfrom dual",
        "select 2 from dual",
    ]


def test_ignores_semicolons_in_multiline_block_comments_and_quoted_identifiers():
    script = '''select "semi;colon" from dual;
select 1
/* ; still comment
   ; still comment */
from dual;
'''
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
