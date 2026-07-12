from __future__ import annotations

from rchar import python_string_literal, sequence_description


def test_ctrl_enter_lf_and_enter_cr_literals_stay_distinct():
    assert python_string_literal("\n") == '"\\x0a"'
    assert python_string_literal("\r") == '"\\x0d"'


def test_ctrl_enter_lf_and_enter_cr_descriptions_stay_distinct():
    assert sequence_description(b"\n") == "Ctrl-Enter (LF / \\x0a)"
    assert sequence_description(b"\r") == "Enter (CR / \\x0d)"
    assert sequence_description(b"\n") != sequence_description(b"\r")
