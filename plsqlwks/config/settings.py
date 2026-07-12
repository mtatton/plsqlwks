from __future__ import annotations

import configparser
from pathlib import Path
import os
import tempfile

from .models import AppConfig


EDITOR_COLOR_SECTION = "editor.colors"
EDITOR_COLOR_KINDS = ("keyword", "string", "number", "comment", "bind", "operator")
EXPLAIN_COLOR_SECTION = "explain.colors"
EXPLAIN_COLOR_KINDS = ("connector", "operation", "object", "metrics", "text")
CSV_EXPORT_SECTION = "plugin.csv-export"
EDITOR_COLOR_NAME_VALUES = {
    "black": 0,
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "purple": 5,
    "cyan": 6,
    "white": 7,
    "gray": 8,
    "grey": 8,
    "bright_black": 8,
    "bright_red": 9,
    "bright_green": 10,
    "bright_yellow": 11,
    "bright_blue": 12,
    "bright_magenta": 13,
    "bright_purple": 13,
    "bright_cyan": 14,
    "bright_white": 15,
    "orange": 214,
}


def read_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return parser


def write_ini_atomic(path: Path, parser: configparser.ConfigParser) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            parser.write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        raise


def read_autocommit(parser: configparser.ConfigParser) -> bool:
    try:
        return parser.getboolean("database", "autocommit", fallback=True)
    except ValueError:
        return True


def read_read_only(parser: configparser.ConfigParser) -> bool:
    try:
        return parser.getboolean("database", "read_only", fallback=False)
    except ValueError:
        return False


def read_remember_bind_values(parser: configparser.ConfigParser) -> bool:
    try:
        return parser.getboolean("database", "remember_bind_values", fallback=False)
    except ValueError:
        return False


def read_csv_export_settings(parser: configparser.ConfigParser) -> tuple[str, str, str]:
    """Read CSV plugin settings, tolerating an invalid separator in a user INI.

    Empty null markers and date formats are intentional values. A separator is
    useful only when it is one character, so malformed values fall back to the
    standard comma instead of preventing application startup.
    """

    separator = parser.get(CSV_EXPORT_SECTION, "separator", fallback=",")
    if len(separator) != 1:
        separator = ","
    null_value = parser.get(CSV_EXPORT_SECTION, "null_value", fallback="<NULL>")
    date_format = parser.get(CSV_EXPORT_SECTION, "date_format", fallback="")
    return separator, null_value, date_format


def read_editor_colors(parser: configparser.ConfigParser) -> dict[str, int]:
    return read_color_section(parser, EDITOR_COLOR_SECTION, EDITOR_COLOR_KINDS)


def read_explain_colors(parser: configparser.ConfigParser) -> dict[str, int]:
    return read_color_section(parser, EXPLAIN_COLOR_SECTION, EXPLAIN_COLOR_KINDS)


def read_color_section(
    parser: configparser.ConfigParser,
    section: str,
    kinds: tuple[str, ...],
) -> dict[str, int]:
    if not parser.has_section(section):
        return {}
    colors: dict[str, int] = {}
    for kind in kinds:
        if not parser.has_option(section, kind):
            continue
        color = parse_editor_color(parser.get(section, kind))
        if color is not None:
            colors[kind] = color
    return colors


def parse_editor_color(value: str) -> int | None:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return None
    try:
        color = int(normalized, 0)
    except ValueError:
        return EDITOR_COLOR_NAME_VALUES.get(normalized)
    return color if color >= 0 else None


def ensure_config_file(config: AppConfig) -> None:
    assert config.config_file is not None
    parser = read_ini(config.config_file)
    changed = ensure_database_option(parser, "autocommit", config.autocommit)
    changed = ensure_database_option(parser, "read_only", config.read_only) or changed
    changed = ensure_database_option(parser, "remember_bind_values", config.remember_bind_values) or changed
    if not changed:
        return
    config.config_file.parent.mkdir(parents=True, exist_ok=True)
    with config.config_file.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def ensure_database_option(parser: configparser.ConfigParser, option: str, enabled: bool) -> bool:
    if not parser.has_section("database"):
        parser.add_section("database")
    if parser.has_option("database", option):
        return False
    parser.set("database", option, "yes" if enabled else "no")
    return True


def save_autocommit(config: AppConfig, enabled: bool) -> None:
    assert config.config_file is not None
    parser = read_ini(config.config_file)
    if not parser.has_section("database"):
        parser.add_section("database")
    parser.set("database", "autocommit", "yes" if enabled else "no")
    config.config_file.parent.mkdir(parents=True, exist_ok=True)
    with config.config_file.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def save_read_only(config: AppConfig, enabled: bool) -> None:
    assert config.config_file is not None
    parser = read_ini(config.config_file)
    if not parser.has_section("database"):
        parser.add_section("database")
    parser.set("database", "read_only", "yes" if enabled else "no")
    config.config_file.parent.mkdir(parents=True, exist_ok=True)
    with config.config_file.open("w", encoding="utf-8") as handle:
        parser.write(handle)
