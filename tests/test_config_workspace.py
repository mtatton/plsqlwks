from __future__ import annotations

import configparser
import os
from pathlib import Path

import pytest

from plsqlwks import config as config_module
from plsqlwks.config import paths as config_paths
from plsqlwks.config import (
    AppConfig,
    SessionTab,
    load_config,
    read_password,
    save_autocommit,
    save_session_tabs,
)
from plsqlwks.workspace import STARTER_PLSQL, STARTER_SQL, ensure_workspace, list_workspace_files, write_once


def test_config_facade_export_contract_matches_pre_package_surface():
    expected_exports = set(
        (Path(__file__).parent / "fixtures" / "config_exports.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    )

    assert len(expected_exports) == 56
    assert len(config_module.__all__) == len(expected_exports)
    assert set(config_module.__all__) == expected_exports
    assert all(hasattr(config_module, name) for name in expected_exports)


def test_config_facade_reexports_owning_module_symbols():
    from plsqlwks.config import loader, models, session, settings

    assert config_module.AppConfig is models.AppConfig
    assert config_module.SessionTab is models.SessionTab
    assert config_module.DEFAULT_DSN is loader.DEFAULT_DSN
    assert config_module.load_config is loader.load_config
    assert config_module.APP_NAME is config_paths.APP_NAME
    assert config_module.resolve_workspace is config_paths.resolve_workspace
    assert config_module.resolve_password_file is config_paths.resolve_password_file
    assert config_module.read_password is config_paths.read_password
    assert config_module.EDITOR_COLOR_SECTION is settings.EDITOR_COLOR_SECTION
    assert config_module.CSV_EXPORT_SECTION == "plugin.csv-export"
    assert config_module.read_csv_export_settings is settings.read_csv_export_settings
    assert config_module.read_editor_colors is settings.read_editor_colors
    assert config_module.save_autocommit is settings.save_autocommit
    assert config_module.SESSION_TABS_SECTION is session.SESSION_TABS_SECTION
    assert config_module.read_session_tabs is session.read_session_tabs
    assert config_module.save_session_tabs is session.save_session_tabs


def test_user_paths_use_xdg_locations_on_linux(monkeypatch, tmp_path):
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    monkeypatch.setattr(config_paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    assert config_paths.user_config_path("worksheet", appauthor=False) == config_home / "worksheet"
    assert config_paths.user_data_path("worksheet", appauthor=False) == data_home / "worksheet"
    assert config_paths.platform_config_dir() == config_home / "plsqlwks"
    assert config_paths.platform_workspace_dir() == data_home / "plsqlwks"


@pytest.mark.parametrize("blank_value", ["", " ", "\t  "])
def test_blank_xdg_locations_fall_back_to_linux_home(monkeypatch, tmp_path, blank_value):
    home = tmp_path / "home"
    monkeypatch.setattr(config_paths.sys, "platform", "linux")
    monkeypatch.setattr(config_paths, "_home_path", lambda: home)
    monkeypatch.setenv("XDG_CONFIG_HOME", blank_value)
    monkeypatch.setenv("XDG_DATA_HOME", blank_value)

    assert config_paths.user_config_path("worksheet") == home / ".config" / "worksheet"
    assert config_paths.user_data_path("worksheet") == home / ".local" / "share" / "worksheet"


def test_user_paths_use_application_support_on_macos(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(config_paths.sys, "platform", "darwin")
    monkeypatch.setattr(config_paths, "_home_path", lambda: home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ignored-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "ignored-data"))

    expected = home / "Library" / "Application Support" / "worksheet"
    assert config_paths.user_config_path("worksheet") == expected
    assert config_paths.user_data_path("worksheet") == expected


def test_user_paths_use_windows_local_roaming_author_and_version(monkeypatch, tmp_path):
    local = tmp_path / "Local"
    roaming = tmp_path / "Roaming"
    monkeypatch.setattr(config_paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))

    data_path = config_paths.user_data_path(
        "worksheet",
        appauthor="Acme",
        version="2",
        ensure_exists=True,
    )

    assert data_path == local / "Acme" / "worksheet" / "2"
    assert data_path.is_dir()
    assert config_paths.user_data_path("worksheet") == local / "worksheet" / "worksheet"
    assert config_paths.user_config_path("worksheet", appauthor=False) == local / "worksheet"
    assert config_paths.user_config_path("worksheet", appauthor=False, roaming=True) == roaming / "worksheet"
    assert config_paths.platform_config_dir() == local / "plsqlwks"
    assert config_paths.platform_workspace_dir() == local / "plsqlwks"


def test_blank_windows_locations_fall_back_to_platform_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    roaming = tmp_path / "Roaming"
    monkeypatch.setattr(config_paths.sys, "platform", "win32")
    monkeypatch.setattr(config_paths, "_home_path", lambda: home)
    monkeypatch.setenv("LOCALAPPDATA", "  ")
    monkeypatch.setenv("APPDATA", str(roaming))

    assert config_paths.user_data_path("worksheet", appauthor=False) == home / "AppData" / "Local" / "worksheet"
    assert config_paths.user_data_path("worksheet", appauthor=False, roaming=True) == roaming / "worksheet"

    monkeypatch.setenv("APPDATA", "\t")

    assert config_paths.user_data_path("worksheet", appauthor=False) == home / "AppData" / "Local" / "worksheet"
    assert config_paths.user_config_path("worksheet", appauthor=False, roaming=True) == (
        home / "AppData" / "Roaming" / "worksheet"
    )


def test_windows_locations_use_known_folder_fallback(monkeypatch, tmp_path):
    local = tmp_path / "KnownLocal"
    roaming = tmp_path / "KnownRoaming"
    monkeypatch.setattr(config_paths.sys, "platform", "win32")
    monkeypatch.setattr(
        config_paths,
        "_windows_known_folder",
        lambda *, roaming: (tmp_path / "KnownRoaming") if roaming else (tmp_path / "KnownLocal"),
    )
    monkeypatch.setenv("LOCALAPPDATA", "")
    monkeypatch.setenv("APPDATA", "")

    assert config_paths.user_data_path("worksheet", appauthor=False) == local / "worksheet"
    assert config_paths.user_data_path("worksheet", appauthor=False, roaming=True) == roaming / "worksheet"


def test_user_path_environment_values_remain_literal_and_overrides_expand_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(config_paths.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", "~/xdg-config")
    monkeypatch.setenv("XDG_DATA_HOME", "~/xdg-data")
    monkeypatch.setenv("ORACLE_PASSWORD_FILE", "~/secrets/orapass")

    workspace, config_file, warnings = config_paths.resolve_workspace("~/oracle-work")
    password_file, _ = config_paths.resolve_password_file(home / "unused-config")

    assert config_paths.user_config_path("worksheet") == Path("~/xdg-config/worksheet")
    assert config_paths.user_data_path("worksheet") == Path("~/xdg-data/worksheet")
    assert workspace == home / "oracle-work"
    assert config_file == home / "oracle-work" / "config.ini"
    assert warnings == ()
    assert password_file == home / "secrets" / "orapass"


def test_load_config_uses_environment_overrides(monkeypatch, tmp_path):
    password_file = tmp_path / "secret.txt"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ORACLE_USER", "tester")
    monkeypatch.setenv("ORACLE_DSN", "localhost:1521/FREE")
    monkeypatch.setenv("ORACLE_PASSWORD_FILE", str(password_file))
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))
    monkeypatch.setenv("PLSQLWKS_MAX_ROWS", "17")
    monkeypatch.setenv("PLSQLWKS_ARRAYSIZE", "9")

    config = load_config()

    assert config.user == "tester"
    assert config.dsn == "localhost:1521/FREE"
    assert config.password_file == password_file
    assert config.workspace_dir == workspace
    assert config.config_file == workspace / "config.ini"
    assert config.max_rows == 17
    assert config.arraysize == 9
    assert config.autocommit is True
    assert config.read_only is False
    assert config.remember_bind_values is False


def test_load_config_explicit_workspace_overrides_environment_and_legacy(monkeypatch, tmp_path):
    explicit_workspace = tmp_path / "explicit"
    environment_workspace = tmp_path / "environment"
    legacy_workspace = tmp_path / "legacy"
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'plsqlwks'\n", encoding="utf-8")
    (legacy_workspace / "sql").mkdir(parents=True)
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(environment_workspace))
    monkeypatch.setattr(config_paths, "source_workspace_dir", lambda: legacy_workspace)

    config = load_config(workspace=explicit_workspace)

    assert config.workspace_dir == explicit_workspace
    assert config.config_file == explicit_workspace / "config.ini"
    assert not any("legacy source workspace" in warning for warning in config.startup_warnings)


def test_load_config_environment_workspace_overrides_legacy(monkeypatch, tmp_path):
    environment_workspace = tmp_path / "environment"
    legacy_workspace = tmp_path / "legacy"
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'plsqlwks'\n", encoding="utf-8")
    (legacy_workspace / "plsql").mkdir(parents=True)
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(environment_workspace))
    monkeypatch.setattr(config_paths, "source_workspace_dir", lambda: legacy_workspace)

    config = load_config()

    assert config.workspace_dir == environment_workspace
    assert config.config_file == environment_workspace / "config.ini"
    assert not any("legacy source workspace" in warning for warning in config.startup_warnings)


@pytest.mark.parametrize("blank_value", ["", " ", "\t  "])
def test_blank_workspace_environment_is_treated_as_unset(monkeypatch, tmp_path, blank_value):
    legacy_workspace = tmp_path / "workspace"
    (legacy_workspace / "sql").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'plsqlwks'\n", encoding="utf-8")
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", blank_value)
    monkeypatch.setattr(config_paths, "source_workspace_dir", lambda: legacy_workspace)

    config = load_config()

    assert config.workspace_dir == legacy_workspace
    assert config.config_file == legacy_workspace / "config.ini"


@pytest.mark.parametrize("marker", ["config.ini", "sql", "plsql"])
def test_load_config_detects_initialized_legacy_workspace(monkeypatch, tmp_path, marker):
    legacy_workspace = tmp_path / "legacy"
    legacy_workspace.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'plsqlwks'\n", encoding="utf-8")
    marker_path = legacy_workspace / marker
    if "." in marker:
        marker_path.write_text("", encoding="utf-8")
    else:
        marker_path.mkdir()
    platform_workspace = tmp_path / "data" / "plsqlwks"
    platform_config = tmp_path / "config" / "plsqlwks"
    monkeypatch.delenv("PLSQLWKS_WORKSPACE", raising=False)
    monkeypatch.setattr(config_paths, "source_workspace_dir", lambda: legacy_workspace)
    monkeypatch.setattr(config_paths, "platform_workspace_dir", lambda: platform_workspace)
    monkeypatch.setattr(config_paths, "platform_config_dir", lambda: platform_config)

    config = load_config()

    assert config.workspace_dir == legacy_workspace
    assert config.config_file == legacy_workspace / "config.ini"
    assert any("legacy source workspace" in warning for warning in config.startup_warnings)
    assert any("--workspace" in warning for warning in config.startup_warnings)


def test_load_config_ignores_initialized_workspace_outside_source_checkout(monkeypatch, tmp_path):
    installed_root = tmp_path / "site-packages"
    installed_workspace = installed_root / "workspace"
    (installed_workspace / "sql").mkdir(parents=True)
    platform_workspace = tmp_path / "data" / "plsqlwks"
    platform_config = tmp_path / "config" / "plsqlwks"
    monkeypatch.delenv("PLSQLWKS_WORKSPACE", raising=False)
    monkeypatch.delenv("ORACLE_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(config_paths, "source_workspace_dir", lambda: installed_workspace)
    monkeypatch.setattr(config_paths, "platform_workspace_dir", lambda: platform_workspace)
    monkeypatch.setattr(config_paths, "platform_config_dir", lambda: platform_config)
    monkeypatch.setattr(config_paths, "LEGACY_PASSWORD_FILE", tmp_path / "missing-legacy-orapass")

    config = load_config()

    assert config.workspace_dir == platform_workspace
    assert config.config_file == platform_config / "config.ini"
    assert not any("legacy source workspace" in warning for warning in config.startup_warnings)


def test_load_config_fresh_install_separates_platform_data_and_config(monkeypatch, tmp_path):
    legacy_workspace = tmp_path / "uninitialized-legacy"
    legacy_workspace.mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'plsqlwks'\n", encoding="utf-8")
    platform_workspace = tmp_path / "data" / "plsqlwks"
    platform_config = tmp_path / "config" / "plsqlwks"
    platform_config.mkdir(parents=True)
    (platform_config / "config.ini").write_text("[database]\nautocommit = no\n", encoding="utf-8")
    monkeypatch.delenv("PLSQLWKS_WORKSPACE", raising=False)
    monkeypatch.delenv("ORACLE_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(config_paths, "source_workspace_dir", lambda: legacy_workspace)
    monkeypatch.setattr(config_paths, "platform_workspace_dir", lambda: platform_workspace)
    monkeypatch.setattr(config_paths, "platform_config_dir", lambda: platform_config)
    monkeypatch.setattr(config_paths, "LEGACY_PASSWORD_FILE", tmp_path / "missing-legacy-orapass")

    config = load_config()

    assert config.workspace_dir == platform_workspace
    assert config.config_file == platform_config / "config.ini"
    assert config.password_file == platform_config / "orapass"
    assert config.autocommit is False
    assert config.startup_warnings == ()


@pytest.mark.parametrize(
    ("environment_name", "value", "message"),
    [
        ("PLSQLWKS_MAX_ROWS", "0", "max_rows must be a positive integer"),
        ("PLSQLWKS_MAX_ROWS", "-1", "max_rows must be a positive integer"),
        ("PLSQLWKS_ARRAYSIZE", "0", "arraysize must be a positive integer"),
        ("PLSQLWKS_ARRAYSIZE", "-1", "arraysize must be a positive integer"),
    ],
)
def test_load_config_rejects_nonpositive_paging_values(monkeypatch, tmp_path, environment_name, value, message):
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv(environment_name, value)

    with pytest.raises(ValueError, match=message):
        load_config()


@pytest.mark.parametrize(("max_rows", "arraysize"), [(0, 100), (200, 0), (-1, 100), (200, -1)])
def test_app_config_rejects_nonpositive_paging_values(tmp_path, max_rows, arraysize):
    with pytest.raises(ValueError, match="must be a positive integer"):
        AppConfig(
            user="hr",
            dsn="db",
            password_file=tmp_path / "orapass",
            workspace_dir=tmp_path / "workspace",
            max_rows=max_rows,
            arraysize=arraysize,
        )


@pytest.mark.parametrize("field_name", ["max_rows", "arraysize"])
@pytest.mark.parametrize("value", [1.5, True, "2"])
def test_app_config_rejects_noninteger_paging_values(tmp_path, field_name, value):
    values = {"max_rows": 200, "arraysize": 100, field_name: value}

    with pytest.raises(ValueError, match=rf"{field_name} must be a positive integer"):
        AppConfig(
            user="hr",
            dsn="db",
            password_file=tmp_path / "orapass",
            workspace_dir=tmp_path / "workspace",
            **values,
        )


@pytest.mark.parametrize("separator", ["", "||", 1, None])
def test_app_config_rejects_invalid_csv_export_separator(tmp_path, separator):
    with pytest.raises(ValueError, match="csv_export_separator must be exactly one character"):
        AppConfig(
            user="hr",
            dsn="db",
            password_file=tmp_path / "orapass",
            workspace_dir=tmp_path / "workspace",
            csv_export_separator=separator,
        )


def test_app_config_accepts_empty_csv_null_value_and_date_format(tmp_path):
    config = AppConfig(
        user="hr",
        dsn="db",
        password_file=tmp_path / "orapass",
        workspace_dir=tmp_path / "workspace",
        csv_export_null_value="",
        csv_export_date_format="",
    )

    assert config.csv_export_null_value == ""
    assert config.csv_export_date_format == ""


def test_csv_settings_preserve_existing_app_config_positional_arguments(tmp_path):
    session_tab = SessionTab(tmp_path / "query.sql", row=2, col=3)
    config = AppConfig(
        "hr",
        "db",
        tmp_path / "orapass",
        tmp_path / "workspace",
        50,
        25,
        tmp_path / "config.ini",
        False,
        True,
        True,
        (session_tab,),
        0,
        {"keyword": 6},
        {"text": 7},
        ("warning",),
    )

    assert config.session_tabs == (session_tab,)
    assert config.editor_colors == {"keyword": 6}
    assert config.explain_colors == {"text": 7}
    assert config.startup_warnings == ("warning",)
    assert config.csv_export_separator == ","
    assert config.csv_export_null_value == ""
    assert config.csv_export_date_format == ""
    assert config.csv_export_enabled is True
    assert config.html_export_enabled is True
    assert config.xlsx_export_enabled is True
    assert config.csv_export_protect_formulas is False


def test_load_config_defaults_autocommit_yes_when_config_is_missing(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    config = load_config()

    assert config.config_file == workspace / "config.ini"
    assert config.autocommit is True
    assert config.read_only is False
    assert config.remember_bind_values is False
    assert config.csv_export_separator == ","
    assert config.csv_export_null_value == ""
    assert config.csv_export_date_format == ""
    assert config.csv_export_enabled is True
    assert config.html_export_enabled is True
    assert config.xlsx_export_enabled is True
    assert config.csv_export_protect_formulas is False
    assert config.session_tabs == ()
    assert config.active_session_tab == 0


def test_load_config_defaults_autocommit_yes_when_key_is_missing(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.ini").write_text("[ui]\ntheme = plain\n", encoding="utf-8")
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    config = load_config()

    assert config.autocommit is True
    assert config.read_only is False
    assert config.remember_bind_values is False


def test_load_config_reads_autocommit_from_workspace_config_ini(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_file = workspace / "config.ini"
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    config_file.write_text("[database]\nautocommit = no\n", encoding="utf-8")
    assert load_config().autocommit is False

    config_file.write_text("[database]\nautocommit = yes\n", encoding="utf-8")
    assert load_config().autocommit is True


def test_load_config_reads_read_only_from_workspace_config_ini(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_file = workspace / "config.ini"
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    config_file.write_text("[database]\nread_only = yes\n", encoding="utf-8")
    assert load_config().read_only is True

    config_file.write_text("[database]\nread_only = no\n", encoding="utf-8")
    assert load_config().read_only is False


def test_load_config_reads_remember_bind_values_from_workspace_config_ini(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_file = workspace / "config.ini"
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    config_file.write_text("[database]\nremember_bind_values = yes\n", encoding="utf-8")
    assert load_config().remember_bind_values is True

    config_file.write_text("[database]\nremember_bind_values = no\n", encoding="utf-8")
    assert load_config().remember_bind_values is False


def test_load_config_reads_csv_export_settings_without_interpolation(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.ini").write_text(
        "\n".join(
            [
                "[plugin.csv-export]",
                "enabled = no",
                "separator = |",
                "null_value = ",
                "date_format = %Y-%m-%d %H:%M:%S",
                "protect_formulas = yes",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    config = load_config()

    assert config.csv_export_separator == "|"
    assert config.csv_export_null_value == ""
    assert config.csv_export_date_format == "%Y-%m-%d %H:%M:%S"
    assert config.csv_export_protect_formulas is True
    assert config.csv_export_enabled is False
    assert config.html_export_enabled is True
    assert config.xlsx_export_enabled is True


@pytest.mark.parametrize(
    ("configured", "expected"),
    (("no", False), ("on", True), ("invalid", False)),
)
def test_load_config_reads_csv_formula_protection_safely(
    monkeypatch,
    tmp_path,
    configured,
    expected,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.ini").write_text(
        f"[plugin.csv-export]\nprotect_formulas = {configured}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    assert load_config().csv_export_protect_formulas is expected


def test_load_config_reads_export_enabled_flags_independently(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.ini").write_text(
        "\n".join(
            [
                "[plugin.csv-export]",
                "enabled = no",
                "[plugin.html-export]",
                "enabled = yes",
                "[plugin.xlsx-export]",
                "enabled = off",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    config = load_config()

    assert config.csv_export_enabled is False
    assert config.html_export_enabled is True
    assert config.xlsx_export_enabled is False


@pytest.mark.parametrize(
    ("malformed_section", "expected"),
    [
        ("plugin.csv-export", (True, False, False)),
        ("plugin.html-export", (False, True, False)),
        ("plugin.xlsx-export", (False, False, True)),
    ],
)
def test_load_config_invalid_export_enabled_falls_back_independently(
    monkeypatch,
    tmp_path,
    malformed_section,
    expected,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sections = ("plugin.csv-export", "plugin.html-export", "plugin.xlsx-export")
    (workspace / "config.ini").write_text(
        "\n".join(
            line
            for section in sections
            for line in (f"[{section}]", f"enabled = {'invalid' if section == malformed_section else 'no'}")
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    config = load_config()

    assert (
        config.csv_export_enabled,
        config.html_export_enabled,
        config.xlsx_export_enabled,
    ) == expected


@pytest.mark.parametrize("separator", ["", "comma", "  "])
def test_load_config_falls_back_to_comma_for_invalid_csv_separator(
    monkeypatch,
    tmp_path,
    separator,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.ini").write_text(
        f"[plugin.csv-export]\nseparator = {separator}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    assert load_config().csv_export_separator == ","


def test_load_config_reads_editor_colors_from_workspace_config_ini(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.ini").write_text(
        "\n".join(
            [
                "[editor.colors]",
                "keyword = bright-cyan",
                "string = 114",
                "number = orange",
                "bind = 0x0d",
                "comment = -1",
                "operator = invalid",
                "unknown = red",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    config = load_config()

    assert config.editor_colors == {
        "keyword": 14,
        "string": 114,
        "number": 214,
        "bind": 13,
    }


def test_load_config_reads_explain_colors_from_workspace_config_ini(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.ini").write_text(
        "\n".join(
            [
                "[explain.colors]",
                "connector = bright-cyan",
                "operation = orange",
                "object = 0x0d",
                "metrics = -1",
                "text = white",
                "unknown = red",
                "ignored = invalid",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    config = load_config()

    assert config.explain_colors == {
        "connector": 14,
        "operation": 214,
        "object": 13,
        "text": 7,
    }


def test_save_autocommit_creates_and_updates_workspace_config_ini(tmp_path):
    config = make_config(tmp_path)

    save_autocommit(config, False)

    parser = configparser.ConfigParser()
    assert config.config_file is not None
    parser.read(config.config_file, encoding="utf-8")
    assert parser.get("database", "autocommit") == "no"

    config.config_file.write_text(
        "[database]\nmax_rows = 17\nautocommit = no\n\n[ui]\ntheme = plain\n",
        encoding="utf-8",
    )

    save_autocommit(config, True)

    parser = configparser.ConfigParser()
    parser.read(config.config_file, encoding="utf-8")
    assert parser.get("database", "autocommit") == "yes"
    assert parser.get("database", "max_rows") == "17"
    assert parser.get("ui", "theme") == "plain"


def test_save_session_tabs_round_trips_order_cursors_active_and_paths(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    internal = workspace / "sql" / "first.sql"
    external = tmp_path / "external 50%.sql"
    config = make_config(workspace)
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    save_session_tabs(
        config,
        (
            SessionTab(internal, row=12, col=4),
            SessionTab(external, row=3, col=17),
        ),
        active_index=1,
    )

    parser = configparser.ConfigParser(interpolation=None)
    assert config.config_file is not None
    parser.read(config.config_file, encoding="utf-8")
    assert parser.getint("session.tabs", "active") == 1
    assert Path(parser.get("session.tabs", "tab_0_path")) == Path("sql") / "first.sql"
    assert parser.getint("session.tabs", "tab_0_row") == 12
    assert parser.getint("session.tabs", "tab_0_col") == 4
    assert Path(parser.get("session.tabs", "tab_1_path")) == external.resolve()
    assert parser.getint("session.tabs", "tab_1_row") == 3
    assert parser.getint("session.tabs", "tab_1_col") == 17

    loaded = load_config()

    assert loaded.session_tabs == (
        SessionTab(internal.resolve(), row=12, col=4),
        SessionTab(external.resolve(), row=3, col=17),
    )
    assert loaded.active_session_tab == 1


def test_load_config_reads_sparse_session_tabs_in_numeric_order_and_marks_bad_positions(
    monkeypatch,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external.sql"
    (workspace / "config.ini").write_text(
        "\n".join(
            [
                "[session.tabs]",
                "active = 7",
                "tab_10_path = sql/ten%done.sql",
                "tab_10_row = not-a-number",
                "tab_2_path = sql/two.sql",
                "tab_2_row = 4",
                "tab_7_path = " + str(external),
                "tab_7_row = -2",
                "tab_7_col = 8",
                "tab_invalid_path = sql/ignored.sql",
                "tab_3_row = 99",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    config = load_config()

    assert config.session_tabs == (
        SessionTab((workspace / "sql" / "two.sql").resolve(), row=4, col=-1),
        SessionTab(external.resolve(), row=-2, col=8),
        SessionTab((workspace / "sql" / "ten%done.sql").resolve(), row=-1, col=-1),
    )
    assert config.active_session_tab == 1


def test_load_config_skips_malformed_session_path(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.ini").write_text(
        "[session.tabs]\ntab_0_path = invalid\0path.sql\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    config = load_config()

    assert config.session_tabs == ()
    assert config.active_session_tab == 0


def test_save_session_tabs_preserves_unrelated_config_and_removes_stale_tabs(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_file = workspace / "config.ini"
    config_file.write_text(
        "[database]\nautocommit = no\nmax_rows = 17\n\n[ui]\ntheme = plain\n",
        encoding="utf-8",
    )
    config = make_config(workspace)
    monkeypatch.setenv("PLSQLWKS_WORKSPACE", str(workspace))

    save_session_tabs(
        config,
        (
            SessionTab(workspace / "sql" / "one.sql", row=1, col=2),
            SessionTab(workspace / "sql" / "two.sql", row=3, col=4),
            SessionTab(workspace / "sql" / "three.sql", row=5, col=6),
        ),
        active_index=2,
    )
    save_session_tabs(
        config,
        (SessionTab(workspace / "sql" / "one.sql", row=7, col=8),),
        active_index=0,
    )

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_file, encoding="utf-8")
    assert parser.get("database", "autocommit") == "no"
    assert parser.get("database", "max_rows") == "17"
    assert parser.get("ui", "theme") == "plain"
    assert parser.getint("session.tabs", "active") == 0
    assert parser.getint("session.tabs", "tab_0_row") == 7
    assert parser.getint("session.tabs", "tab_0_col") == 8
    assert not any(option.startswith(("tab_1_", "tab_2_")) for option in parser.options("session.tabs"))

    save_session_tabs(config, (), active_index=99)

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_file, encoding="utf-8")
    assert parser.get("database", "max_rows") == "17"
    assert parser.get("ui", "theme") == "plain"
    if parser.has_section("session.tabs"):
        assert not any(option.startswith("tab_") for option in parser.options("session.tabs"))
    loaded = load_config()
    assert loaded.session_tabs == ()
    assert loaded.active_session_tab == 0


def test_read_password_removes_only_line_endings(tmp_path):
    path = tmp_path / "orapass"
    path.write_text(" \tsecret \t\r\n\n", encoding="utf-8")

    assert read_password(path) == " \tsecret \t"


def test_password_file_environment_override_has_highest_precedence(monkeypatch, tmp_path):
    platform_config = tmp_path / "config"
    platform_config.mkdir()
    (platform_config / "orapass").write_text("platform", encoding="utf-8")
    legacy_password = tmp_path / "legacy-orapass"
    legacy_password.write_text("legacy", encoding="utf-8")
    environment_password = tmp_path / "environment-orapass"
    environment_password.write_text("environment", encoding="utf-8")
    if os.name == "posix":
        environment_password.chmod(0o600)
    monkeypatch.setenv("ORACLE_PASSWORD_FILE", str(environment_password))
    monkeypatch.setattr(config_paths, "LEGACY_PASSWORD_FILE", legacy_password)

    path, warnings = config_module.resolve_password_file(platform_config)

    assert path == environment_password
    assert warnings == ()


@pytest.mark.parametrize("blank_value", ["", " ", "\t  "])
def test_blank_password_environment_is_treated_as_unset(monkeypatch, tmp_path, blank_value):
    platform_config = tmp_path / "config"
    platform_config.mkdir()
    platform_password = platform_config / "orapass"
    platform_password.write_text("platform", encoding="utf-8")
    legacy_password = tmp_path / "legacy-orapass"
    legacy_password.write_text("legacy", encoding="utf-8")
    if os.name == "posix":
        platform_password.chmod(0o600)
    monkeypatch.setenv("ORACLE_PASSWORD_FILE", blank_value)
    monkeypatch.setattr(config_paths, "LEGACY_PASSWORD_FILE", legacy_password)

    path, warnings = config_module.resolve_password_file(platform_config)

    assert path == platform_password
    assert warnings == ()


def test_existing_platform_password_precedes_legacy_password(monkeypatch, tmp_path):
    platform_config = tmp_path / "config"
    platform_config.mkdir()
    platform_password = platform_config / "orapass"
    platform_password.write_text("platform", encoding="utf-8")
    legacy_password = tmp_path / "legacy-orapass"
    legacy_password.write_text("legacy", encoding="utf-8")
    if os.name == "posix":
        platform_password.chmod(0o600)
    monkeypatch.delenv("ORACLE_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(config_paths, "LEGACY_PASSWORD_FILE", legacy_password)

    path, warnings = config_module.resolve_password_file(platform_config)

    assert path == platform_password
    assert warnings == ()


def test_legacy_password_is_compatibility_fallback_with_warning(monkeypatch, tmp_path):
    platform_config = tmp_path / "config"
    legacy_password = tmp_path / "legacy-orapass"
    legacy_password.write_text("legacy", encoding="utf-8")
    if os.name == "posix":
        legacy_password.chmod(0o600)
    monkeypatch.delenv("ORACLE_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(config_paths, "LEGACY_PASSWORD_FILE", legacy_password)
    monkeypatch.setattr(config_paths, "platform_config_dir", lambda: platform_config)

    path, warnings = config_module.resolve_password_file(platform_config)

    assert path == legacy_password
    assert any("legacy password file" in warning.lower() for warning in warnings)
    assert any(str(platform_config / "orapass") in warning for warning in warnings)


def test_missing_password_files_select_platform_path(monkeypatch, tmp_path):
    platform_config = tmp_path / "config"
    monkeypatch.delenv("ORACLE_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(config_paths, "LEGACY_PASSWORD_FILE", tmp_path / "missing-legacy-orapass")

    path, warnings = config_module.resolve_password_file(platform_config)

    assert path == platform_config / "orapass"
    assert warnings == ()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are required")
def test_password_file_warns_when_permissions_are_not_0600(monkeypatch, tmp_path):
    password_file = tmp_path / "orapass"
    password_file.write_text("secret", encoding="utf-8")
    password_file.chmod(0o644)
    monkeypatch.setenv("ORACLE_PASSWORD_FILE", str(password_file))

    _, warnings = config_module.resolve_password_file(tmp_path / "config")

    assert any("0644" in warning and "expected 0600" in warning for warning in warnings)


def test_parse_args_accepts_workspace_path():
    from plsqlwks.ui.app import parse_args

    args = parse_args(["--workspace", "~/oracle-work", "--manual", "--read-only"])

    assert args.workspace == Path("~/oracle-work")
    assert args.autocommit is False
    assert args.read_only is True


def test_parse_args_describes_read_only_as_client_side_guardrail(capsys):
    from plsqlwks.ui.app import parse_args

    with pytest.raises(SystemExit) as error:
        parse_args(["--help"])

    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "client-side guardrail" in help_text
    assert "not a security boundary" in help_text


def test_ensure_workspace_creates_starter_files_once(tmp_path):
    config = make_config(tmp_path)

    ensure_workspace(config)

    assert config.sql_dir.is_dir()
    assert config.plsql_dir.is_dir()
    assert config.results_dir.is_dir()
    parser = configparser.ConfigParser()
    assert config.config_file is not None
    parser.read(config.config_file, encoding="utf-8")
    assert parser.get("database", "autocommit") == "yes"
    assert parser.get("database", "read_only") == "no"
    assert parser.get("database", "remember_bind_values") == "no"
    assert (config.sql_dir / "session_info.sql").read_text(encoding="utf-8") == STARTER_SQL
    assert (config.plsql_dir / "hello_workspace.sql").read_text(encoding="utf-8") == STARTER_PLSQL

    custom_sql = "select 42 from dual;\n"
    (config.sql_dir / "session_info.sql").write_text(custom_sql, encoding="utf-8")
    ensure_workspace(config)

    assert (config.sql_dir / "session_info.sql").read_text(encoding="utf-8") == custom_sql


def test_write_once_does_not_replace_existing_file(tmp_path):
    path = tmp_path / "scratch.sql"
    path.write_text("original", encoding="utf-8")

    write_once(path, "replacement")

    assert path.read_text(encoding="utf-8") == "original"


def test_list_workspace_files_returns_sorted_sql_and_plsql_files(tmp_path):
    config = make_config(tmp_path)
    config.sql_dir.mkdir(parents=True)
    config.plsql_dir.mkdir(parents=True)
    config.results_dir.mkdir(parents=True)
    (config.sql_dir / "b.sql").write_text("", encoding="utf-8")
    (config.sql_dir / "a.sql").write_text("", encoding="utf-8")
    (config.sql_dir / "ignore.txt").write_text("", encoding="utf-8")
    (config.plsql_dir / "d.sql").write_text("", encoding="utf-8")
    (config.plsql_dir / "c.sql").write_text("", encoding="utf-8")

    files = list_workspace_files(config)

    assert [path.name for path in files] == ["a.sql", "b.sql", "c.sql", "d.sql"]


def make_config(root: Path) -> AppConfig:
    return AppConfig(
        user="hr",
        dsn="db",
        password_file=root / "orapass",
        workspace_dir=root,
    )
