from __future__ import annotations

import configparser
import re
from collections.abc import Sequence
from pathlib import Path

from .models import AppConfig, SessionTab
from .settings import read_ini, write_ini_atomic

SESSION_TABS_SECTION = "session.tabs"
SESSION_TAB_PATH_PATTERN = re.compile(r"^tab_(\d+)_path$")


def read_session_tabs(
    parser: configparser.ConfigParser,
    workspace_dir: Path,
) -> tuple[tuple[SessionTab, ...], int]:
    if not parser.has_section(SESSION_TABS_SECTION):
        return (), 0
    indexes = sorted(
        {
            int(match.group(1))
            for option in parser.options(SESSION_TABS_SECTION)
            if (match := SESSION_TAB_PATH_PATTERN.fullmatch(option)) is not None
        }
    )
    try:
        configured_active = parser.getint(SESSION_TABS_SECTION, "active", fallback=0)
    except ValueError:
        configured_active = 0
    workspace_root = workspace_dir.expanduser().resolve()
    tabs: list[SessionTab] = []
    active_tab = 0
    active_found = False
    for index in indexes:
        path_text = parser.get(
            SESSION_TABS_SECTION,
            f"tab_{index}_path",
            fallback="",
            raw=True,
        ).strip()
        if not path_text:
            continue
        try:
            path = Path(path_text).expanduser()
            if not path.is_absolute():
                path = workspace_root / path
            path = path.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if index == configured_active:
            active_tab = len(tabs)
            active_found = True
        tabs.append(
            SessionTab(
                path=path,
                row=read_session_coordinate(parser, index, "row"),
                col=read_session_coordinate(parser, index, "col"),
            )
        )
    if not active_found:
        active_tab = 0
    return tuple(tabs), active_tab


def read_session_coordinate(parser: configparser.ConfigParser, index: int, name: str) -> int:
    try:
        return parser.getint(SESSION_TABS_SECTION, f"tab_{index}_{name}")
    except (configparser.Error, ValueError):
        return -1


def save_session_tabs(config: AppConfig, tabs: Sequence[SessionTab], active_index: int) -> None:
    assert config.config_file is not None
    parser = read_ini(config.config_file)
    if parser.has_section(SESSION_TABS_SECTION):
        parser.remove_section(SESSION_TABS_SECTION)
    parser.add_section(SESSION_TABS_SECTION)
    active_index = min(max(active_index, 0), len(tabs) - 1) if tabs else 0
    parser.set(SESSION_TABS_SECTION, "active", str(active_index))
    for index, tab in enumerate(tabs):
        parser.set(SESSION_TABS_SECTION, f"tab_{index}_path", session_path_text(config, tab.path))
        parser.set(SESSION_TABS_SECTION, f"tab_{index}_row", str(tab.row))
        parser.set(SESSION_TABS_SECTION, f"tab_{index}_col", str(tab.col))
    write_ini_atomic(config.config_file, parser)


def session_path_text(config: AppConfig, path: Path) -> str:
    resolved_path = path.expanduser().resolve()
    workspace_root = config.workspace_dir.expanduser().resolve()
    try:
        return resolved_path.relative_to(workspace_root).as_posix()
    except ValueError:
        return str(resolved_path)
