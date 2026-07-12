from __future__ import annotations

import os
from pathlib import Path

from .models import AppConfig
from . import paths
from .session import read_session_tabs
from .settings import (
    read_autocommit,
    read_editor_colors,
    read_explain_colors,
    read_ini,
    read_read_only,
    read_remember_bind_values,
)


DEFAULT_DSN = """
(DESCRIPTION =
  (ADDRESS = (PROTOCOL = TCP)(HOST = 127.0.0.1)(PORT = 1521))
  (CONNECT_DATA =
    (SERVER = DEDICATED)
    (SERVICE_NAME = free)
  )
)
""".strip()


def load_config(
    *,
    workspace: str | Path | None = None,
    autocommit: bool | None = None,
    read_only: bool | None = None,
) -> AppConfig:
    workspace, config_file, workspace_warnings = paths.resolve_workspace(workspace)
    password_file, password_warnings = paths.resolve_password_file(paths.platform_config_dir())
    parser = read_ini(config_file)
    config_autocommit = read_autocommit(parser) if autocommit is None else autocommit
    config_read_only = read_read_only(parser) if read_only is None else read_only
    config_remember_bind_values = read_remember_bind_values(parser)
    session_tabs, active_session_tab = read_session_tabs(parser, workspace)
    return AppConfig(
        user=os.environ.get("ORACLE_USER", "hr"),
        dsn=os.environ.get("ORACLE_DSN", DEFAULT_DSN),
        password_file=password_file,
        workspace_dir=workspace,
        max_rows=int(os.environ.get("PLSQLWKS_MAX_ROWS", "200")),
        arraysize=int(os.environ.get("PLSQLWKS_ARRAYSIZE", "100")),
        config_file=config_file,
        autocommit=config_autocommit,
        read_only=config_read_only,
        remember_bind_values=config_remember_bind_values,
        session_tabs=session_tabs,
        active_session_tab=active_session_tab,
        editor_colors=read_editor_colors(parser),
        explain_colors=read_explain_colors(parser),
        startup_warnings=workspace_warnings + password_warnings,
    )
