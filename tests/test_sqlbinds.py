import pytest

from plsqlwks.sqlbinds import (
    SqlBind,
    bind_name_key,
    find_bind_names,
    find_sql_binds,
    find_unique_binds,
)


def test_find_bind_names_returns_ordered_unique_names():
    assert find_bind_names("select * from decisions where id = :id and name = :name or id = :id") == [
        "id",
        "name",
    ]


def test_find_bind_names_deduplicates_unquoted_names_case_insensitively():
    assert find_bind_names("select :id, :ID, :Id from dual") == ["id"]


def test_find_bind_names_keeps_quoted_names_case_sensitive_and_separate():
    statement = 'select :"MixedCase", :"mixedcase", :"MixedCase", :mixedcase, :MIXEDCASE from dual'

    assert find_bind_names(statement) == ['"MixedCase"', '"mixedcase"', "mixedcase"]


def test_bind_name_key_matches_driver_name_rules():
    assert bind_name_key("id") == "ID"
    assert bind_name_key("ID") == "ID"
    assert bind_name_key('"MixedCase"') == '"MixedCase"'
    assert bind_name_key('"mixedcase"') == '"mixedcase"'
    assert bind_name_key("Mixed Name", quoted=True) == '"Mixed Name"'


def test_find_bind_names_ignores_strings_comments_and_quoted_identifiers():
    statement = """
    select ':not_bind', q'[also :not_bind]', "COL:NAME", value
    from decisions
    where id = :id
      and note = n':still_not_bind'
      -- and hidden = :commented
      /* and hidden = :block_commented */
    """

    assert find_bind_names(statement) == ["id"]


def test_find_sql_binds_tracks_offsets():
    statement = "select :id from dual where name = :name"

    assert find_sql_binds(statement) == [
        SqlBind("id", 7, 10),
        SqlBind("name", 34, 39),
    ]


def test_find_sql_binds_detects_quoted_names_and_preserves_driver_keys():
    statement = 'select :"Mixed Name", : "Other Name" from dual'
    first_start = statement.index(":")
    first_end = statement.index(",")
    second_start = statement.index(":", first_end)
    second_end = statement.index(" from dual")

    assert find_sql_binds(statement) == [
        SqlBind('"Mixed Name"', first_start, first_end, quoted=True),
        SqlBind('"Other Name"', second_start, second_end, quoted=True),
    ]
    assert dict.fromkeys(find_bind_names(statement), "ok") == {
        '"Mixed Name"': "ok",
        '"Other Name"': "ok",
    }


def test_find_sql_binds_allows_space_between_colon_and_unquoted_name():
    statement = "select : bind_name from dual"

    assert find_sql_binds(statement) == [SqlBind("bind_name", 7, 18)]


def test_find_unique_binds_returns_first_quoted_aware_occurrence():
    statement = 'select :Name, :name, :"Name", :"Name" from dual'

    assert find_unique_binds(statement) == [
        SqlBind("Name", 7, 12),
        SqlBind('"Name"', 21, 28, quoted=True),
    ]


def test_json_object_colon_separators_are_not_bind_variables():
    statement = """select json_object(
      'name' : first_name,
      'email' : email,
      :dynamic_key : :dynamic_value,
      'legacy' value :legacy_value,
      'nested' : json_object('id' : employee_id, 'filter' : :filter_id)
    ) from employees where department_id = :department_id"""

    assert find_bind_names(statement) == [
        "dynamic_key",
        "dynamic_value",
        "legacy_value",
        "filter_id",
        "department_id",
    ]


def test_bind_only_json_object_argument_remains_a_bind():
    assert find_bind_names("select json_object(:payload) from dual") == ["payload"]


def test_find_bind_names_ignores_trigger_new_old_pseudorecords():
    statement = """
    create or replace trigger decisions_biu
    before insert or update on decisions
    for each row
    begin
      :new.updated_by := user;
      if :old.id is not null then
        :new.id := :old.id;
      end if;
    end;
    """

    assert find_bind_names(statement) == []


def test_find_bind_names_ignores_trigger_pseudorecords_with_qualifier_comments():
    statement = """
    create/* comment */or -- comment
    replace/* comment */noneditionable/* comment */trigger decisions_biu
    before update on decisions for each row
    begin
      :new.updated_by := :old.updated_by;
    end;
    """

    assert find_bind_names(statement) == []


def test_find_bind_names_ignores_parent_and_comments_before_trigger_qualifier_dot():
    statement = """
    create trigger decisions_biu before update on decisions for each row
    begin
      :new /* generated */ .updated_by := :old -- keep prior value
        .updated_by;
      :parent /* nested table owner */ .id := :actual_bind;
    end;
    """

    assert find_bind_names(statement) == ["actual_bind"]


def test_find_bind_names_ignores_custom_trigger_referencing_aliases():
    statement = """
    create trigger decisions_biu
    before update on decisions
    referencing new /* gap */ as newest old as oldest parent as parent_row
    for each row
    begin
      :newest.updated_by := :oldest /* qualifier gap */ .updated_by;
      :parent_row.id := :actual_bind;
      :"newest".quoted_value := 1;
    end;
    """

    assert find_bind_names(statement) == ["actual_bind", '"newest"']


def test_find_bind_names_keeps_quoted_new_in_trigger_source():
    statement = """
    create trigger decisions_biu before update on decisions for each row
    begin
      :new.updated_by := :"new";
    end;
    """

    assert find_bind_names(statement) == ['"new"']


def test_find_bind_names_keeps_new_old_outside_trigger_ddl():
    assert find_bind_names("select * from changes where new_value = :new and old_value = :old") == [
        "new",
        "old",
    ]


@pytest.mark.parametrize(
    "decoy",
    [
        "-- create trigger appears in generated documentation",
        "/* create trigger appears in generated documentation */",
        "message := 'create trigger';",
        "message := q'[create trigger]';",
        "message := nq'{create trigger}';",
        'message := "create trigger";',
    ],
)
def test_find_bind_names_keeps_new_old_when_trigger_phrase_is_noncode(decoy):
    statement = f"""begin
      {decoy}
      consume(:new.value, :old.value, :actual_bind);
    end;"""

    assert find_bind_names(statement) == ["new", "old", "actual_bind"]
