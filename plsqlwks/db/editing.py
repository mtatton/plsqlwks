from __future__ import annotations

import math
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import oracledb

from ..sqlsplit import strip_leading_sql_comments
from .execution import decimal_output_type_handler, materialize_result_value
from .identifiers import quote_identifier, scan_oracle_identifier
from .models import (
    NULL_DISPLAY_TOKEN,
    CellUpdateResult,
    ConcurrentEditError,
    EditableResultContext,
    EditOperationRollbackError,
    ReadOnlyModeError,
    ResultColumnMetadata,
    RowInsertResult,
    SelectItem,
    SimpleSelect,
    TruncatedLobValue,
)
from .sql_analysis import (
    find_top_level_sql_keyword,
    sql_code_mask,
    strip_sql_comments,
    tail_sql_words,
)
from .transactions import edit_operation_savepoint

ISO_FRACTION_RE = re.compile(r"\.(?P<digits>\d+)(?:[Zz]|[+-]\d{2}:\d{2})?$")
TAIL_KEYWORDS = {
    "WHERE",
    "ORDER",
    "OFFSET",
    "FETCH",
    "FOR",
    "JOIN",
    "GROUP",
    "HAVING",
    "CONNECT",
    "START",
    "UNION",
    "MINUS",
    "INTERSECT",
}
SAFE_TAIL_START_WORDS = {"WHERE", "ORDER", "OFFSET", "FETCH"}
TAIL_SINGLE_WORD_REJECTIONS = {"JOIN", "HAVING", "UNION", "MINUS", "INTERSECT", "SELECT"}
TAIL_PHRASE_REJECTIONS = {("GROUP", "BY"), ("CONNECT", "BY"), ("START", "WITH"), ("FOR", "UPDATE")}
ORACLE_TABLE_CLAUSE_REJECTIONS = {
    "MATCH_RECOGNIZE",
    "MODEL",
    "PARTITION",
    "PIVOT",
    "SAMPLE",
    "UNPIVOT",
    "VERSIONS",
}
TEXT_EDIT_TYPES = (
    oracledb.DB_TYPE_CHAR,
    oracledb.DB_TYPE_VARCHAR,
    oracledb.DB_TYPE_NCHAR,
    oracledb.DB_TYPE_NVARCHAR,
    oracledb.DB_TYPE_CLOB,
    oracledb.DB_TYPE_NCLOB,
    oracledb.DB_TYPE_ROWID,
    oracledb.DB_TYPE_UROWID,
)
BINARY_EDIT_TYPES = (oracledb.DB_TYPE_RAW, oracledb.DB_TYPE_BLOB)
LOB_EDIT_TYPES = (oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_NCLOB, oracledb.DB_TYPE_BLOB)
FLOAT_EDIT_TYPES = (oracledb.DB_TYPE_BINARY_FLOAT, oracledb.DB_TYPE_BINARY_DOUBLE)
UNSUPPORTED_TIMESTAMP_TYPES = (oracledb.DB_TYPE_TIMESTAMP_TZ, oracledb.DB_TYPE_TIMESTAMP_LTZ)


def is_date_or_timestamp_column(metadata: ResultColumnMetadata | None) -> bool:
    return metadata is not None and metadata.type_code in {
        oracledb.DB_TYPE_DATE,
        oracledb.DB_TYPE_TIMESTAMP,
    }


class EditingMixin:
    def editable_context_for_result(
        self,
        statement: str,
        columns: list[str],
        column_metadata: list[ResultColumnMetadata] | None = None,
    ) -> tuple[EditableResultContext | None, str]:
        if self.read_only:
            return None, "Result editing is disabled in read-only mode"
        parsed, reason = parse_simple_select(statement)
        if parsed is None:
            return None, reason
        table_columns, reason = self.editable_table_columns(parsed.table_name)
        if not table_columns:
            return None, reason
        return build_editable_result_context(statement, columns, table_columns, column_metadata)

    def editable_table_columns(self, table_name: str) -> tuple[list[str], str]:
        try:
            quote_identifier(table_name)
        except ValueError:
            return [], "Only valid current-schema tables are editable"
        conn = self.ensure_connected()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                select
                  count(case when object_type = 'TABLE' then 1 end),
                  count(*)
                from user_objects
                where object_name = :object_name
                """,
                object_name=table_name,
            )
            row = cursor.fetchone()
            table_count = int(row[0]) if row and row[0] is not None else 0
            object_count = int(row[1]) if row and row[1] is not None else 0
            if object_count == 0:
                return [], f"{quote_identifier(table_name)} was not found in the current schema"
            if table_count == 0:
                return [], "Only base tables are editable"
            cursor.execute(
                """
                select column_name
                from user_tab_columns
                where table_name = :table_name
                order by column_id
                """,
                table_name=table_name,
            )
            columns = [str(column_name) for (column_name,) in cursor]
            if not columns:
                return [], f"{quote_identifier(table_name)} has no editable columns"
            return columns, ""
        finally:
            cursor.close()

    def update_cell_by_rowid(
        self,
        context: EditableResultContext,
        rowid: str,
        column_index: int,
        original_value: Any,
        value_text: str,
    ) -> CellUpdateResult:
        if self.read_only:
            raise ReadOnlyModeError("Cell updates are disabled in read-only mode")
        table_name, column_name, metadata = self.validated_edit_target(context, column_index)
        table_sql = quote_identifier(table_name)
        column_sql = quote_identifier(column_name)
        ensure_editable_original(original_value, metadata)
        use_sysdate = is_sysdate_edit_expression(value_text, metadata)
        value = None if use_sysdate else convert_edit_value(value_text, metadata)
        if not rowid or rowid == NULL_DISPLAY_TOKEN:
            raise ValueError("Selected row has no ROWID")
        conn = self.ensure_connected()
        cursor = conn.cursor()
        cursor.outputtypehandler = decimal_output_type_handler
        try:
            try:
                with edit_operation_savepoint(
                    conn,
                    cursor,
                    autocommit=self.autocommit,
                    had_pending_work=self.has_uncommitted_changes,
                ):
                    predicate, original_params = optimistic_edit_predicate(
                        column_sql,
                        original_value,
                        metadata,
                    )
                    params = {"target_rowid": rowid, **original_params}
                    value_sql = "sysdate" if use_sysdate else ":new_value"
                    if not use_sysdate:
                        params["new_value"] = value
                    set_edit_input_sizes(
                        cursor,
                        metadata,
                        include_new=not use_sysdate,
                        include_original="original_value" in original_params,
                    )
                    cursor.execute(
                        f"update {table_sql} set {column_sql} = {value_sql} "
                        f"where rowid = chartorowid(:target_rowid) and {predicate}",
                        **params,
                    )
                    if cursor.rowcount == 0:
                        raise ConcurrentEditError(
                            "Cell changed or the row was deleted since the result was loaded; refresh and retry"
                        )
                    if cursor.rowcount != 1:
                        raise ValueError(f"Expected to update 1 row, updated {cursor.rowcount}")
                    cursor.execute(
                        f"select {column_sql} from {table_sql} where rowid = chartorowid(:target_rowid)",
                        target_rowid=rowid,
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise ValueError("Updated row could not be refreshed")
                    refreshed_display, refreshed_value = materialize_result_value(row[0])
                    if self.autocommit:
                        conn.commit()
                    else:
                        self.record_pending_rows(1)
                self.synchronize_pending_transaction(conn)
                return CellUpdateResult(refreshed_value, refreshed_display)
            except EditOperationRollbackError as exc:
                if exc.transaction_may_have_changes:
                    self.reconcile_uncertain_edit_failure(conn)
                else:
                    self.synchronize_pending_transaction(conn)
                raise
            except Exception:
                self.synchronize_pending_transaction(conn)
                raise
        finally:
            cursor.close()

    def insert_row_for_result(
        self,
        context: EditableResultContext,
        values_by_column_index: dict[int, str],
        result_column_count: int,
    ) -> RowInsertResult:
        if self.read_only:
            raise ReadOnlyModeError("Row inserts are disabled in read-only mode")
        table_name, insert_columns = self.validated_insert_targets(context, result_column_count)
        table_sql = quote_identifier(table_name)
        bind_names = [f"value_{idx}" for idx in range(len(insert_columns))]
        columns_sql = ", ".join(quote_identifier(column_name) for _, column_name, _ in insert_columns)
        value_expressions: list[str] = []
        insert_bindings: list[tuple[str, ResultColumnMetadata | None]] = []
        params: dict[str, object] = {}
        for bind_name, (column_index, _, metadata) in zip(bind_names, insert_columns, strict=True):
            value_text = values_by_column_index.get(column_index, NULL_DISPLAY_TOKEN)
            if is_sysdate_edit_expression(value_text, metadata):
                value_expressions.append("sysdate")
                continue
            value_expressions.append(f":{bind_name}")
            params[bind_name] = convert_edit_value(value_text, metadata)
            insert_bindings.append((bind_name, metadata))
        values_sql = ", ".join(value_expressions)
        conn = self.ensure_connected()
        cursor = conn.cursor()
        cursor.outputtypehandler = decimal_output_type_handler
        try:
            try:
                with edit_operation_savepoint(
                    conn,
                    cursor,
                    autocommit=self.autocommit,
                    had_pending_work=self.has_uncommitted_changes,
                ):
                    new_rowid_var = cursor.var(str)
                    params["new_rowid"] = new_rowid_var
                    set_insert_input_sizes(cursor, insert_bindings)
                    cursor.execute(
                        f"insert into {table_sql} ({columns_sql}) values ({values_sql}) returning rowid into :new_rowid",
                        **params,
                    )
                    if cursor.rowcount != 1:
                        raise ValueError(f"Expected to insert 1 row, inserted {cursor.rowcount}")
                    new_rowid = scalar_var_value(new_rowid_var)
                    if not new_rowid:
                        raise ValueError("Inserted row did not return ROWID")
                    select_list = self.insert_refresh_select_list(context, result_column_count)
                    cursor.execute(
                        f"select {select_list} from {table_sql} where rowid = chartorowid(:new_rowid)",
                        new_rowid=str(new_rowid),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise ValueError("Inserted row could not be refreshed")
                    materialized = [materialize_result_value(value) for value in row]
                    refreshed_row = [display for display, _ in materialized]
                    original_row = [value for _, value in materialized]
                    if self.autocommit:
                        conn.commit()
                    else:
                        self.record_pending_rows(1)
                self.synchronize_pending_transaction(conn)
                return RowInsertResult(original_row, refreshed_row)
            except EditOperationRollbackError as exc:
                if exc.transaction_may_have_changes:
                    self.reconcile_uncertain_edit_failure(conn)
                else:
                    self.synchronize_pending_transaction(conn)
                raise
            except Exception:
                self.synchronize_pending_transaction(conn)
                raise
        finally:
            cursor.close()

    def validated_insert_targets(
        self,
        context: EditableResultContext,
        result_column_count: int,
    ) -> tuple[str, list[tuple[int, str, ResultColumnMetadata | None]]]:
        table_name = context.table_name
        try:
            quote_identifier(table_name)
        except ValueError as exc:
            raise ValueError("Only valid current-schema table columns are editable") from exc
        if result_column_count <= 0 or context.rowid_column < 0 or context.rowid_column >= result_column_count:
            raise ValueError("Result columns do not match the editable insert context")
        table_columns, reason = self.editable_table_columns(table_name)
        if not table_columns:
            raise ValueError(reason)
        targets: list[tuple[int, str, ResultColumnMetadata | None]] = []
        for column_index, raw_column_name in sorted(context.editable_columns.items()):
            if column_index < 0 or column_index >= result_column_count or column_index == context.rowid_column:
                raise ValueError("Result columns do not match the editable insert context")
            column_name = raw_column_name
            if column_name not in table_columns:
                raise ValueError(f"{column_name} is not an editable column on {table_name}")
            metadata = context.column_metadata.get(column_index)
            rejection = edit_metadata_rejection_reason(metadata)
            if rejection:
                raise ValueError(f"{column_name}: {rejection}")
            targets.append((column_index, column_name, metadata))
        if not targets:
            raise ValueError("Result has no editable table columns")
        return table_name, targets

    def insert_refresh_select_list(self, context: EditableResultContext, result_column_count: int) -> str:
        select_items: list[str] = []
        for column_index in range(result_column_count):
            if column_index == context.rowid_column:
                select_items.append("rowid")
                continue
            column_name = context.editable_columns.get(column_index, "")
            if not column_name:
                raise ValueError("Result columns do not match the editable insert context")
            select_items.append(quote_identifier(column_name))
        return ", ".join(select_items)

    def validated_edit_target(
        self,
        context: EditableResultContext,
        column_index: int,
    ) -> tuple[str, str, ResultColumnMetadata | None]:
        table_name = context.table_name
        column_name = context.editable_columns.get(column_index, "")
        try:
            quote_identifier(table_name)
            quote_identifier(column_name)
        except ValueError as exc:
            raise ValueError("Only valid current-schema table columns are editable") from exc
        table_columns, reason = self.editable_table_columns(table_name)
        if not table_columns:
            raise ValueError(reason)
        if column_name not in table_columns:
            raise ValueError(f"{column_name} is not an editable column on {table_name}")
        metadata = context.column_metadata.get(column_index)
        rejection = edit_metadata_rejection_reason(metadata)
        if rejection:
            raise ValueError(f"{column_name}: {rejection}")
        return table_name, column_name, metadata


def normalize_edit_value(text: str) -> str | None:
    stripped = text.strip()
    if stripped == NULL_DISPLAY_TOKEN:
        return None
    return text


def convert_edit_value(text: str, metadata: ResultColumnMetadata | None) -> Any:
    value = normalize_edit_value(text)
    if value is None or metadata is None or metadata.type_code is None:
        return value
    rejection = edit_metadata_rejection_reason(metadata)
    if rejection:
        raise ValueError(rejection)
    type_code = metadata.type_code
    stripped = text.strip()
    if type_code in TEXT_EDIT_TYPES:
        return text
    if type_code in BINARY_EDIT_TYPES:
        try:
            return bytes.fromhex(stripped)
        except ValueError as exc:
            raise ValueError(f"{edit_type_name(metadata)} values must be hexadecimal bytes") from exc
    if type_code is oracledb.DB_TYPE_NUMBER:
        try:
            number = Decimal(stripped)
        except InvalidOperation as exc:
            raise ValueError("NUMBER values must use decimal notation") from exc
        if not number.is_finite():
            raise ValueError("NUMBER values must be finite")
        return number
    if type_code in FLOAT_EDIT_TYPES:
        try:
            number = float(stripped)
        except ValueError as exc:
            raise ValueError(f"{edit_type_name(metadata)} values must be numeric") from exc
        if not math.isfinite(number):
            raise ValueError(f"{edit_type_name(metadata)} values must be finite")
        return number
    if type_code is oracledb.DB_TYPE_DATE:
        fraction_match = ISO_FRACTION_RE.search(stripped)
        parse_text = stripped
        if fraction_match is not None:
            digits = fraction_match.group("digits")
            parse_text = (
                stripped[: fraction_match.start("digits")]
                + digits[:6].ljust(6, "0")
                + stripped[fraction_match.end("digits") :]
            )
        value_datetime = parse_iso_datetime(parse_text, "DATE")
        if value_datetime.tzinfo is not None:
            raise ValueError("DATE values must not include a time zone")
        if fraction_match is not None:
            raise ValueError("DATE values cannot include fractional seconds")
        return value_datetime
    if type_code is oracledb.DB_TYPE_TIMESTAMP:
        value_datetime = parse_iso_datetime(stripped, "TIMESTAMP")
        if value_datetime.tzinfo is not None:
            raise ValueError("TIMESTAMP values must not include a time zone")
        return value_datetime
    if type_code is oracledb.DB_TYPE_BOOLEAN:
        normalized = stripped.lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
        raise ValueError("BOOLEAN values must be true or false")
    raise ValueError(edit_metadata_rejection_reason(metadata) or "Column type is not editable")


def is_sysdate_edit_expression(
    text: str,
    metadata: ResultColumnMetadata | None,
) -> bool:
    if metadata is None:
        return False
    type_code = metadata.type_code
    return (
        type_code is oracledb.DB_TYPE_DATE or type_code is oracledb.DB_TYPE_TIMESTAMP
    ) and text.strip().casefold() == "sysdate"


def parse_iso_datetime(text: str, type_name: str) -> datetime:
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{type_name} values must use ISO format YYYY-MM-DD[ HH:MM:SS[.ffffff]]") from exc


def edit_metadata_rejection_reason(metadata: ResultColumnMetadata | None) -> str:
    if metadata is None or metadata.type_code is None:
        return ""
    type_code = metadata.type_code
    if type_code in UNSUPPORTED_TIMESTAMP_TYPES:
        return f"{edit_type_name(metadata)} columns cannot yet be edited without losing time-zone information"
    if type_code is oracledb.DB_TYPE_TIMESTAMP and metadata.scale is not None and metadata.scale > 6:
        return "TIMESTAMP precision above 6 cannot be represented exactly by Python"
    if (
        type_code in TEXT_EDIT_TYPES
        or type_code in BINARY_EDIT_TYPES
        or type_code in FLOAT_EDIT_TYPES
        or type_code
        in (
            oracledb.DB_TYPE_NUMBER,
            oracledb.DB_TYPE_DATE,
            oracledb.DB_TYPE_TIMESTAMP,
            oracledb.DB_TYPE_BOOLEAN,
        )
    ):
        return ""
    return f"{edit_type_name(metadata)} columns are not supported by the result-grid editor"


def edit_type_name(metadata: ResultColumnMetadata) -> str:
    name = getattr(metadata.type_code, "name", "")
    if name:
        return str(name).removeprefix("DB_TYPE_")
    return str(metadata.type_code)


def ensure_editable_original(original_value: Any, metadata: ResultColumnMetadata | None) -> None:
    if isinstance(original_value, TruncatedLobValue):
        raise ValueError(f"{original_value.type_name} value is truncated at display time and cannot be safely edited")
    rejection = edit_metadata_rejection_reason(metadata)
    if rejection:
        raise ValueError(rejection)


def optimistic_edit_predicate(
    column_name: str,
    original_value: Any,
    metadata: ResultColumnMetadata | None,
) -> tuple[str, dict[str, Any]]:
    if original_value is None:
        return f"{column_name} is null", {}
    type_code = metadata.type_code if metadata is not None else None
    if type_code in LOB_EDIT_TYPES:
        return f"dbms_lob.compare({column_name}, :original_value) = 0", {"original_value": original_value}
    return f"{column_name} = :original_value", {"original_value": original_value}


def set_edit_input_sizes(
    cursor: Any,
    metadata: ResultColumnMetadata | None,
    *,
    include_new: bool,
    include_original: bool,
) -> None:
    setter = getattr(cursor, "setinputsizes", None)
    if not callable(setter):
        return
    sizes: dict[str, Any] = {"target_rowid": oracledb.DB_TYPE_VARCHAR}
    if metadata is not None and metadata.type_code is not None:
        if include_new:
            sizes["new_value"] = metadata.type_code
        if include_original:
            sizes["original_value"] = metadata.type_code
    setter(**sizes)


def set_insert_input_sizes(
    cursor: Any,
    insert_bindings: list[tuple[str, ResultColumnMetadata | None]],
) -> None:
    setter = getattr(cursor, "setinputsizes", None)
    if not callable(setter):
        return
    sizes = {
        bind_name: metadata.type_code
        for bind_name, metadata in insert_bindings
        if metadata is not None and metadata.type_code is not None
    }
    if sizes:
        setter(**sizes)


def scalar_var_value(value: Any) -> Any:
    getter = getattr(value, "getvalue", None)
    if callable(getter):
        value = getter()
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def parse_simple_select(statement: str) -> tuple[SimpleSelect | None, str]:
    sql = strip_leading_sql_comments(statement).strip().rstrip(";").strip()
    select_match = re.match(r"select\b", sql, re.IGNORECASE)
    if not select_match:
        return None, "Result is not editable because this is not a SELECT"
    from_idx = find_top_level_sql_keyword(sql, "from", select_match.end())
    if from_idx is None:
        return None, "Result is not a simple single-table SELECT"

    select_clause = sql[select_match.end() : from_idx].strip()
    from_clause = strip_sql_comments(sql[from_idx + len("from") :]).lstrip()
    if from_clause.startswith("("):
        return None, "Subquery results are not editable"
    table_token = scan_oracle_identifier(from_clause)
    if table_token is None or not table_token.name:
        return None, "Result is not a simple single-table SELECT"
    table_name = table_token.name
    remainder = from_clause[table_token.end :]
    if remainder.lstrip().startswith("."):
        return None, "Result is not a simple single-table SELECT"

    alias = None
    tail = ""
    alias_start = table_token.end
    while alias_start < len(from_clause) and from_clause[alias_start].isspace():
        alias_start += 1
    if alias_start < len(from_clause):
        if alias_start == table_token.end:
            return None, "Result is not a simple single-table SELECT"
        alias_token = scan_oracle_identifier(from_clause, alias_start)
        if alias_token is None:
            tail = from_clause[table_token.end :]
        elif not alias_token.quoted and alias_token.name in TAIL_KEYWORDS:
            tail = from_clause[alias_start:]
        elif not alias_token.quoted and alias_token.name == "AS":
            return None, "Oracle table aliases must not use AS"
        elif not alias_token.quoted and alias_token.name in ORACLE_TABLE_CLAUSE_REJECTIONS:
            return None, "Oracle table clauses and flashback queries are not editable"
        else:
            alias = alias_token.name
            tail = from_clause[alias_token.end :]
    if tail_has_rejected_construct(tail):
        return None, (
            "Joins, grouped queries, locking queries, Oracle table clauses, "
            "flashback queries, and subqueries are not editable"
        )

    if re.match(r"(?is)^distinct\b", strip_sql_comments(select_clause).strip()):
        return None, "DISTINCT results are not editable"
    items, reason = parse_select_items(select_clause, table_name, alias)
    if not items:
        return None, reason
    return SimpleSelect(table_name=table_name, alias=alias, items=items), ""


def tail_has_rejected_construct(tail: str) -> bool:
    stripped = strip_sql_comments(tail).strip()
    if not stripped:
        return False
    if stripped.startswith((",", "@")):
        return True
    words = tail_sql_words(tail)
    if not words or words[0] not in SAFE_TAIL_START_WORDS:
        return True
    if any(word in TAIL_SINGLE_WORD_REJECTIONS or word in ORACLE_TABLE_CLAUSE_REJECTIONS for word in words):
        return True
    return any(pair in TAIL_PHRASE_REJECTIONS for pair in zip(words, words[1:], strict=False))


def parse_select_items(
    select_clause: str,
    table_name: str,
    alias: str | None,
) -> tuple[list[SelectItem], str]:
    items: list[SelectItem] = []
    for raw_item in split_top_level_select_items(select_clause):
        item, reason = parse_select_item(raw_item.strip(), table_name, alias)
        if item is None:
            return [], reason
        items.append(item)
    return items, ""


def split_top_level_select_items(select_clause: str) -> list[str]:
    mask = sql_code_mask(select_clause)
    parts: list[str] = []
    start = 0
    depth = 0
    for idx, ch in enumerate(mask):
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            continue
        if ch == "," and depth == 0:
            parts.append(select_clause[start:idx])
            start = idx + 1
    parts.append(select_clause[start:])
    return parts


def parse_select_item(
    text: str,
    table_name: str,
    alias: str | None,
) -> tuple[SelectItem | None, str]:
    text = strip_sql_comments(text).strip()
    if not text:
        return None, "Empty SELECT items are not editable"
    if text == "*":
        return SelectItem("wildcard"), ""

    pos = 0
    first = scan_oracle_identifier(text, pos)
    if first is None or not first.name:
        return None, "Expressions are not editable"
    pos = first.end
    while pos < len(text) and text[pos].isspace():
        pos += 1

    qualifier = ""
    name_token = first
    wildcard = False
    if pos < len(text) and text[pos] == ".":
        qualifier = first.name
        pos += 1
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos < len(text) and text[pos] == "*":
            wildcard = True
            pos += 1
        else:
            name_token = scan_oracle_identifier(text, pos)
            if name_token is None or not name_token.name:
                return None, "Only simple table columns are editable"
            pos = name_token.end
        if qualifier not in {table_name, alias}:
            return None, "Qualified columns must reference the selected table"

    while pos < len(text) and text[pos].isspace():
        pos += 1
    if pos < len(text):
        as_token = scan_oracle_identifier(text, pos)
        if as_token is None or as_token.quoted or as_token.name != "AS":
            return None, "Expressions are not editable"
        pos = as_token.end
        while pos < len(text) and text[pos].isspace():
            pos += 1
        result_alias = scan_oracle_identifier(text, pos)
        if result_alias is None or not result_alias.name:
            return None, "Expressions are not editable"
        pos = result_alias.end
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos != len(text):
            return None, "Expressions are not editable"

    if wildcard:
        return SelectItem("wildcard"), ""
    if not name_token.quoted and name_token.name == "ROWID":
        return SelectItem("rowid"), ""
    return SelectItem("column", name_token.name), ""


def build_editable_result_context(
    statement: str,
    result_columns: list[str],
    table_columns: list[str],
    column_metadata: list[ResultColumnMetadata] | None = None,
) -> tuple[EditableResultContext | None, str]:
    parsed, reason = parse_simple_select(statement)
    if parsed is None:
        return None, reason
    if len(set(result_columns)) != len(result_columns):
        return None, "Duplicate result columns are not editable"
    table_column_set = set(table_columns)
    rowid_column: int | None = None
    editable_columns: dict[int, str] = {}
    result_index = 0

    for item in parsed.items:
        if item.kind == "wildcard":
            for table_column in table_columns:
                if result_index >= len(result_columns):
                    return None, "Result columns do not match the SELECT list"
                if result_columns[result_index] != table_column:
                    return None, "Wildcard result columns do not match table metadata"
                editable_columns[result_index] = table_column
                result_index += 1
            continue
        if result_index >= len(result_columns):
            return None, "Result columns do not match the SELECT list"
        if item.kind == "rowid":
            if result_columns[result_index] != "ROWID":
                return None, "ROWID must be selected as ROWID"
            if rowid_column is not None:
                return None, "Duplicate ROWID columns are not editable"
            rowid_column = result_index
            result_index += 1
            continue
        if item.name not in table_column_set:
            return None, f"{item.name} is not a column on {parsed.table_name}"
        editable_columns[result_index] = item.name
        result_index += 1

    if result_index != len(result_columns):
        return None, "Result columns do not match the SELECT list"
    if rowid_column is None:
        return None, "Result is not editable because ROWID is not selected"
    if not editable_columns:
        return None, "Result has no editable table columns"
    if len(set(editable_columns.values())) != len(editable_columns):
        return None, "Duplicate table columns are not editable"
    if column_metadata is not None and len(column_metadata) != len(result_columns):
        return None, "Result column metadata does not match the SELECT list"
    editable_metadata = (
        {column_index: column_metadata[column_index] for column_index in editable_columns}
        if column_metadata is not None
        else {}
    )
    return (
        EditableResultContext(
            table_name=parsed.table_name,
            rowid_column=rowid_column,
            editable_columns=editable_columns,
            column_metadata=editable_metadata,
        ),
        "",
    )
