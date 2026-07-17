from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

APP_NAME = "plsqlwks"
LEGACY_PASSWORD_FILE = Path("/tmp/orapass")
LEGACY_WORKSPACE_MARKERS = ("config.ini", "sql", "plsql")
SOURCE_CHECKOUT_MARKER = "pyproject.toml"


def resolve_workspace(workspace: str | Path | None = None) -> tuple[Path, Path, tuple[str, ...]]:
    if workspace is not None:
        selected = Path(workspace).expanduser()
        return selected, selected / "config.ini", ()

    environment_workspace = nonblank_environment_value("PLSQLWKS_WORKSPACE")
    if environment_workspace is not None:
        selected = Path(environment_workspace).expanduser()
        return selected, selected / "config.ini", ()

    legacy_workspace = source_workspace_dir()
    if is_legacy_source_workspace(legacy_workspace):
        warning = (
            f"Using legacy source workspace: {legacy_workspace}. "
            "Use --workspace or PLSQLWKS_WORKSPACE to choose a user data location."
        )
        return legacy_workspace, legacy_workspace / "config.ini", (warning,)

    selected = platform_workspace_dir()
    return selected, platform_config_dir() / "config.ini", ()


def source_workspace_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "workspace"


def is_legacy_workspace(path: Path) -> bool:
    return any((path / marker).exists() for marker in LEGACY_WORKSPACE_MARKERS)


def is_legacy_source_workspace(path: Path) -> bool:
    return (path.parent / SOURCE_CHECKOUT_MARKER).is_file() and is_legacy_workspace(path)


def user_config_path(
    appname: str | None = None,
    appauthor: str | bool | None = None,
    version: str | None = None,
    roaming: bool = False,
    ensure_exists: bool = False,
) -> Path:
    return _user_path(
        appname,
        appauthor=appauthor,
        version=version,
        roaming=roaming,
        ensure_exists=ensure_exists,
        kind="config",
    )


def user_data_path(
    appname: str | None = None,
    appauthor: str | bool | None = None,
    version: str | None = None,
    roaming: bool = False,
    ensure_exists: bool = False,
) -> Path:
    return _user_path(
        appname,
        appauthor=appauthor,
        version=version,
        roaming=roaming,
        ensure_exists=ensure_exists,
        kind="data",
    )


def _user_path(
    appname: str | None,
    *,
    appauthor: str | bool | None,
    version: str | None,
    roaming: bool,
    ensure_exists: bool,
    kind: str,
) -> Path:
    path = _user_path_root(kind, roaming=roaming)
    if appname:
        if sys.platform == "win32" and appauthor is not False:
            author = appauthor if isinstance(appauthor, str) and appauthor else appname
            path /= author
        path /= appname
        if version:
            path /= version
    if ensure_exists:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _user_path_root(kind: str, *, roaming: bool) -> Path:
    if sys.platform == "win32":
        known_folder = _windows_known_folder(roaming=roaming)
        if known_folder is not None:
            return known_folder
        environment_name = "APPDATA" if roaming else "LOCALAPPDATA"
        configured = _environment_path(environment_name)
        if configured is not None:
            return configured
        folder = "Roaming" if roaming else "Local"
        return _home_path() / "AppData" / folder
    if sys.platform == "darwin":
        return _home_path() / "Library" / "Application Support"
    environment_name = "XDG_CONFIG_HOME" if kind == "config" else "XDG_DATA_HOME"
    configured = _environment_path(environment_name)
    if configured is not None:
        return configured
    suffix = (".config",) if kind == "config" else (".local", "share")
    return _home_path().joinpath(*suffix)


def _environment_path(name: str) -> Path | None:
    value = nonblank_environment_value(name)
    return Path(value) if value is not None else None


def _windows_known_folder(*, roaming: bool) -> Path | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(260)
        shell32 = getattr(getattr(ctypes, "windll"), "shell32")  # noqa: B009  # reason: ctypes exposes Windows loader attributes dynamically on guarded Windows paths
        csidl = 26 if roaming else 28
        result = shell32.SHGetFolderPathW(None, csidl, None, 0, buffer)
    except (AttributeError, OSError):
        return None
    return Path(buffer.value) if result == 0 and buffer.value else None


def _home_path() -> Path:
    return Path.home()


def platform_workspace_dir() -> Path:
    return user_data_path(APP_NAME, appauthor=False)


def platform_config_dir() -> Path:
    return user_config_path(APP_NAME, appauthor=False)


def resolve_password_file(config_dir: Path) -> tuple[Path, tuple[str, ...]]:
    configured_path = nonblank_environment_value("ORACLE_PASSWORD_FILE")
    preferred_path = config_dir / "orapass"
    if configured_path is not None:
        selected = Path(configured_path).expanduser()
    elif preferred_path.exists():
        selected = preferred_path
    elif LEGACY_PASSWORD_FILE.exists():
        selected = LEGACY_PASSWORD_FILE
    else:
        selected = preferred_path
    return selected, password_file_warnings(selected)


def nonblank_environment_value(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value is not None and value.strip() else None


def password_file_warnings(path: Path) -> tuple[str, ...]:
    warnings: list[str] = []
    if path == LEGACY_PASSWORD_FILE:
        warnings.append(
            f"Using legacy password file: {path}. Move it to {platform_config_dir() / 'orapass'} "
            "or set ORACLE_PASSWORD_FILE."
        )
    if os.name == "posix" and path.exists():
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            pass
        else:
            if mode != 0o600:
                warnings.append(f"Password file permissions are {mode:04o}, expected 0600: {path}")
    return tuple(warnings)


def read_password(path: Path) -> str:
    return path.read_text(encoding="utf-8").rstrip("\r\n")
