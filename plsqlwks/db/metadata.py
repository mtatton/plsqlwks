from __future__ import annotations

import oracledb

from .editing import normalize_identifier
from .execution import read_lob_value


SCHEMA_OBJECT_TYPES = (
    "TABLE",
    "VIEW",
    "PROCEDURE",
    "FUNCTION",
    "PACKAGE",
    "TRIGGER",
    "SEQUENCE",
    "INDEX",
    "SYNONYM",
)


class MetadataMixin:
    def list_schema_objects(self) -> dict[str, list[str]]:
        conn = self.ensure_connected()
        cursor = conn.cursor()
        groups = empty_schema_object_groups()
        try:
            cursor.execute(
                """
                select object_type, object_name
                from user_objects
                where object_type in (
                  'TABLE', 'VIEW', 'PROCEDURE', 'FUNCTION', 'PACKAGE',
                  'TRIGGER', 'SEQUENCE', 'INDEX', 'SYNONYM'
                )
                order by
                  case object_type
                    when 'TABLE' then 1
                    when 'VIEW' then 2
                    when 'PROCEDURE' then 3
                    when 'FUNCTION' then 4
                    when 'PACKAGE' then 5
                    when 'TRIGGER' then 6
                    when 'SEQUENCE' then 7
                    when 'INDEX' then 8
                    when 'SYNONYM' then 9
                    else 99
                  end,
                  object_name
                """
            )
            for object_type, object_name in cursor:
                groups.setdefault(object_type, []).append(object_name)
        finally:
            cursor.close()
        return groups

    def get_object_definition(self, object_type: str, object_name: str) -> str:
        normalized_type = object_type.upper()
        normalized_name = object_name.upper()
        conn = self.ensure_connected()
        cursor = conn.cursor()
        try:
            configure_metadata_transform(cursor)
            if normalized_type == "PACKAGE":
                spec = fetch_ddl(cursor, "PACKAGE", normalized_name)
                if package_body_exists(cursor, normalized_name):
                    body = fetch_ddl(cursor, "PACKAGE_BODY", normalized_name)
                    return assemble_package_definition(spec, body)
                return terminate_plsql_ddl(spec)
            ddl_type = metadata_object_type(normalized_type)
            ddl = fetch_ddl(cursor, ddl_type, normalized_name)
            if normalized_type in {"PROCEDURE", "FUNCTION", "TRIGGER"}:
                return terminate_plsql_ddl(ddl)
            return ensure_sql_terminator(ddl)
        finally:
            cursor.close()

    def list_object_columns(self, object_name: str) -> list[str]:
        normalized_name = normalize_identifier(object_name)
        if normalized_name is None:
            return []
        conn = self.ensure_connected()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                select column_name
                from user_tab_columns
                where table_name = :object_name
                order by column_id
                """,
                object_name=normalized_name,
            )
            return [str(column_name).upper() for (column_name,) in cursor]
        finally:
            cursor.close()


def empty_schema_object_groups() -> dict[str, list[str]]:
    return {object_type: [] for object_type in SCHEMA_OBJECT_TYPES}


def configure_metadata_transform(cursor: oracledb.Cursor) -> None:
    cursor.execute(
        """
        begin
          dbms_metadata.set_transform_param(dbms_metadata.session_transform, 'PRETTY', true);
          dbms_metadata.set_transform_param(dbms_metadata.session_transform, 'SQLTERMINATOR', true);
          dbms_metadata.set_transform_param(dbms_metadata.session_transform, 'STORAGE', false);
          dbms_metadata.set_transform_param(dbms_metadata.session_transform, 'SEGMENT_ATTRIBUTES', false);
          dbms_metadata.set_transform_param(dbms_metadata.session_transform, 'TABLESPACE', false);
        end;
        """
    )


def metadata_object_type(object_type: str) -> str:
    if object_type not in SCHEMA_OBJECT_TYPES:
        raise ValueError(f"Unsupported schema object type: {object_type}")
    return object_type


def fetch_ddl(cursor: oracledb.Cursor, object_type: str, object_name: str) -> str:
    cursor.execute(
        "select dbms_metadata.get_ddl(:object_type, :object_name) from dual",
        object_type=object_type,
        object_name=object_name,
    )
    row = cursor.fetchone()
    if not row or row[0] is None:
        raise ValueError(f"No DDL returned for {object_type} {object_name}")
    ddl = read_lob_value(row[0])
    if not isinstance(ddl, str):
        raise TypeError(f"DDL for {object_type} {object_name} was not character data")
    return ddl


def package_body_exists(cursor: oracledb.Cursor, object_name: str) -> bool:
    cursor.execute(
        "select count(*) from user_objects where object_type = 'PACKAGE BODY' and object_name = :object_name",
        object_name=object_name,
    )
    row = cursor.fetchone()
    return bool(row and row[0])


def clean_ddl(ddl: str) -> str:
    return ddl.strip()


def ensure_sql_terminator(ddl: str) -> str:
    text = clean_ddl(ddl)
    return text if text.endswith(";") else text + ";"


def terminate_plsql_ddl(ddl: str) -> str:
    text = ensure_sql_terminator(ddl)
    return text + "\n/"


def assemble_package_definition(spec: str, body: str | None = None) -> str:
    parts = [terminate_plsql_ddl(spec)]
    if body:
        parts.append(terminate_plsql_ddl(body))
    return "\n\n".join(parts)
