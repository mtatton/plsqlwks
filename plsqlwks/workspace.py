from __future__ import annotations

from pathlib import Path

from .config import AppConfig, ensure_config_file


STARTER_SQL = """select user as connected_user,
       sys_context('USERENV', 'SERVICE_NAME') as service_name,
       systimestamp as server_time
from dual;
"""


STARTER_PLSQL = """create or replace procedure hello_plsql_workspace as
begin
  dbms_output.put_line('Hello from plsqlwks');
end;
/

begin
  hello_plsql_workspace;
end;
/
"""


def ensure_workspace(config: AppConfig) -> None:
    config.sql_dir.mkdir(parents=True, exist_ok=True)
    config.plsql_dir.mkdir(parents=True, exist_ok=True)
    config.results_dir.mkdir(parents=True, exist_ok=True)
    ensure_config_file(config)
    write_once(config.sql_dir / "session_info.sql", STARTER_SQL)
    write_once(config.plsql_dir / "hello_workspace.sql", STARTER_PLSQL)


def write_once(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def list_workspace_files(config: AppConfig) -> list[Path]:
    files: list[Path] = []
    for folder in (config.sql_dir, config.plsql_dir):
        files.extend(sorted(folder.glob("*.sql")))
    return files
