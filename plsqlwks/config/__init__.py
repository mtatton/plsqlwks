from __future__ import annotations

import configparser
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Sequence

from .loader import DEFAULT_DSN, load_config
from .models import AppConfig, SessionTab
from .paths import (
    APP_NAME,
    LEGACY_PASSWORD_FILE,
    LEGACY_WORKSPACE_MARKERS,
    SOURCE_CHECKOUT_MARKER,
    is_legacy_source_workspace,
    is_legacy_workspace,
    nonblank_environment_value,
    password_file_warnings,
    platform_config_dir,
    platform_workspace_dir,
    read_password,
    resolve_password_file,
    resolve_workspace,
    source_workspace_dir,
    user_config_path,
    user_data_path,
)
from .session import (
    SESSION_TAB_PATH_PATTERN,
    SESSION_TABS_SECTION,
    read_session_coordinate,
    read_session_tabs,
    save_session_tabs,
    session_path_text,
)
from .settings import (
    EDITOR_COLOR_KINDS,
    EDITOR_COLOR_NAME_VALUES,
    EDITOR_COLOR_SECTION,
    EXPLAIN_COLOR_KINDS,
    EXPLAIN_COLOR_SECTION,
    ensure_config_file,
    ensure_database_option,
    parse_editor_color,
    read_autocommit,
    read_color_section,
    read_editor_colors,
    read_explain_colors,
    read_ini,
    read_read_only,
    read_remember_bind_values,
    save_autocommit,
    save_read_only,
    write_ini_atomic,
)


__all__ = (
    "APP_NAME",
    "AppConfig",
    "DEFAULT_DSN",
    "EDITOR_COLOR_KINDS",
    "EDITOR_COLOR_NAME_VALUES",
    "EDITOR_COLOR_SECTION",
    "EXPLAIN_COLOR_KINDS",
    "EXPLAIN_COLOR_SECTION",
    "LEGACY_PASSWORD_FILE",
    "LEGACY_WORKSPACE_MARKERS",
    "Path",
    "SESSION_TABS_SECTION",
    "SESSION_TAB_PATH_PATTERN",
    "SOURCE_CHECKOUT_MARKER",
    "Sequence",
    "SessionTab",
    "annotations",
    "configparser",
    "dataclass",
    "ensure_config_file",
    "ensure_database_option",
    "field",
    "is_legacy_source_workspace",
    "is_legacy_workspace",
    "load_config",
    "nonblank_environment_value",
    "os",
    "parse_editor_color",
    "password_file_warnings",
    "platform_config_dir",
    "platform_workspace_dir",
    "re",
    "read_autocommit",
    "read_color_section",
    "read_editor_colors",
    "read_explain_colors",
    "read_ini",
    "read_password",
    "read_read_only",
    "read_remember_bind_values",
    "read_session_coordinate",
    "read_session_tabs",
    "resolve_password_file",
    "resolve_workspace",
    "save_autocommit",
    "save_read_only",
    "save_session_tabs",
    "session_path_text",
    "source_workspace_dir",
    "stat",
    "tempfile",
    "user_config_path",
    "user_data_path",
    "write_ini_atomic",
)
