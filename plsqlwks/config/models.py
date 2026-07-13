from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..plugins.csv_export import _NULL_DISPLAY_VALUE as _CSV_EXPORT_DEFAULT_NULL_VALUE


@dataclass(frozen=True)
class SessionTab:
    path: Path
    row: int = 0
    col: int = 0


@dataclass(frozen=True)
class AppConfig:
    """Immutable application settings assembled from the environment and workspace INI."""

    user: str
    dsn: str
    password_file: Path
    workspace_dir: Path
    max_rows: int = 200
    arraysize: int = 100
    config_file: Path | None = None
    autocommit: bool = True
    read_only: bool = False
    remember_bind_values: bool = False
    session_tabs: tuple[SessionTab, ...] = ()
    active_session_tab: int = 0
    editor_colors: dict[str, int] = field(default_factory=dict)
    explain_colors: dict[str, int] = field(default_factory=dict)
    startup_warnings: tuple[str, ...] = ()
    csv_export_separator: str = ","
    csv_export_null_value: str = _CSV_EXPORT_DEFAULT_NULL_VALUE
    csv_export_date_format: str = ""
    csv_export_enabled: bool = True
    html_export_enabled: bool = True
    xlsx_export_enabled: bool = True
    csv_export_protect_formulas: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.max_rows, bool) or not isinstance(self.max_rows, int) or self.max_rows <= 0:
            raise ValueError("max_rows must be a positive integer")
        if isinstance(self.arraysize, bool) or not isinstance(self.arraysize, int) or self.arraysize <= 0:
            raise ValueError("arraysize must be a positive integer")
        if not isinstance(self.csv_export_separator, str) or len(self.csv_export_separator) != 1:
            raise ValueError("csv_export_separator must be exactly one character")
        if self.config_file is None:
            object.__setattr__(self, "config_file", self.workspace_dir / "config.ini")

    @property
    def sql_dir(self) -> Path:
        return self.workspace_dir / "sql"

    @property
    def plsql_dir(self) -> Path:
        return self.workspace_dir / "plsql"

    @property
    def results_dir(self) -> Path:
        return self.workspace_dir / "results"
