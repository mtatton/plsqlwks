from __future__ import annotations

import pytest

from plsqlwks.db.sql_analysis import (
    find_top_level_sql_keyword,
    oracle_q_quote_start,
    sql_code_mask,
    strip_sql_comments,
    tail_sql_words,
)


def test_oracle_q_quote_start_rejects_missing_and_whitespace_delimiters():
    assert oracle_q_quote_start("ordinary", 0) is None
    assert oracle_q_quote_start("q'", 0) is None
    assert oracle_q_quote_start("q' value '", 0) is None
    assert oracle_q_quote_start("nq'!value!'", 0) == (4, "!")


def test_sql_code_mask_covers_escaped_quotes_comments_q_quotes_and_incomplete_regions():
    statement = (
        "select 'it''s FOR UPDATE', \"A\"\"FOR UPDATE\", q'[FOR UPDATE]', nq'!FOR UPDATE!', code\n"
        "-- FOR UPDATE\n"
        "from dual /* FOR UPDATE */ where note = 'incomplete"
    )

    masked = sql_code_mask(statement, preserve_quoted_identifiers=True)

    assert len(masked) == len(statement)
    assert '"A""FOR UPDATE"' in masked
    assert "select" in masked and "from dual" in masked and "where note" in masked
    assert "it" not in masked and "q'[" not in masked


@pytest.mark.parametrize(
    ("statement", "keyword", "expected"),
    [
        ("update", "update", 0),
        ("select (select update from dual) from dual", "update", None),
        ("select 1 from dual update", "update", 19),
        ("select update_count from dual", "update", None),
        ("select 1 from dual", "missing", None),
        ("select (1)) update", "update", 12),
    ],
)
def test_find_top_level_keyword_honors_depth_and_identifier_boundaries(statement, keyword, expected):
    assert find_top_level_sql_keyword(statement, keyword) == expected


def test_strip_sql_comments_preserves_all_quoted_regions_and_physical_newlines():
    statement = (
        "select 'a''--b', \"A\"\"/*B*/\", q'[-- q /* q */]', nq'!-- nq!'\n"
        "-- removed\n"
        "from dual /* removed */ where value = 'unfinished"
    )

    stripped = strip_sql_comments(statement)

    assert "-- removed" not in stripped
    assert "/* removed */" not in stripped
    assert "'a''--b'" in stripped
    assert '"A""/*B*/"' in stripped
    assert "q'[-- q /* q */]'" in stripped
    assert stripped.count("\n") == statement.count("\n")


def test_tail_sql_words_ignores_every_protected_region_and_handles_incomplete_input():
    tail = (
        "alpha 'hidden'' word' \"quoted\"\"word\" q'[q hidden]' nq'!nq hidden!' "
        "-- line hidden\n beta /* block hidden */ gamma 'unfinished hidden"
    )

    assert tail_sql_words(tail) == ["ALPHA", "BETA", "GAMMA"]
    assert tail_sql_words("alpha /* unfinished hidden") == ["ALPHA"]
    assert tail_sql_words('alpha "unfinished hidden') == ["ALPHA"]
    assert tail_sql_words("alpha q'[unfinished hidden") == ["ALPHA"]
