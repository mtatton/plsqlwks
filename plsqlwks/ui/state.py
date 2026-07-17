from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import AppConfig
from ..db import ExplainPlanResult, QueryResult, QueryResultPage, empty_schema_object_groups
from .buffer import Buffer
from .constants import *
from .db_worker import DbCommandHandle
from .help import HELP_LINES, HelpLine
from .results import ResultInsertDraft

@dataclass(frozen=True)
class DbOperationProgress:
    label: str


@dataclass(frozen=True)
class DbOperationFinished:
    kind: str
    result: Any = None
    error: Exception | None = None
    statement_start_line: int = 1
    statement_start_col: int = 0
    partial_results: list[QueryResult] | None = None
    source_text: str | None = None
    source_unchanged: bool = True
    statement_count: int = 1
    failed_statement_index: int | None = None


DbOperationEvent = DbOperationProgress | DbOperationFinished


@dataclass(frozen=True)
class ResultFetchMore:
    result: QueryResult
    page: QueryResultPage
    target_row: int


@dataclass
class DbOperation:
    kind: str
    label: str
    started_at: float
    handle: DbCommandHandle
    tab: FileTab
    statement_start_line: int = 1
    statement_start_col: int = 0
    on_success: Callable[[Any], None] | None = None
    on_error: Callable[[Exception], None] | None = None
    restore_active_tab: bool = True
    cancel_requested: bool = False
    source_text: str | None = None
    statement_count: int = 1


@dataclass(frozen=True)
class ExecutionDiagnostic:
    line: int
    column: int
    message: str


class ScriptExecutionFailed(Exception):
    def __init__(
        self,
        original: Exception,
        statement_start_line: int,
        statement_start_col: int,
        partial_results: list[QueryResult],
        statement_index: int = 1,
        statement_count: int = 1,
    ):
        super().__init__(str(original))
        self.original = original
        self.statement_start_line = statement_start_line
        self.statement_start_col = statement_start_col
        self.partial_results = partial_results
        self.statement_index = statement_index
        self.statement_count = statement_count


@dataclass
class FileTab:
    buffer: Buffer = field(default_factory=Buffer)
    source_key: str | None = None
    results: list[str] = field(default_factory=list)
    results_style: str = RESULT_STYLE_TEXT
    help_lines: list[HelpLine] = field(default_factory=lambda: list(HELP_LINES))
    results_scroll: int | None = None
    dbms_output: list[str] = field(default_factory=list)
    dbms_output_grouped: bool = False
    dbms_output_scroll: int | None = None
    show_dbms_output: bool = False
    last_result: QueryResult | None = None
    active_result: QueryResult | None = None
    explain_result: ExplainPlanResult | None = None
    explain_scroll: int = 0
    explain_page_size: int = 10
    result_mode: str = RESULT_GRID
    result_row: int = 0
    result_col: int = 0
    result_row_scroll: int = 0
    result_col_scroll: int = 0
    result_page_size: int = 10
    result_insert_draft: ResultInsertDraft | None = None
    search_query: str = ""
    execution_diagnostics: list[ExecutionDiagnostic] = field(default_factory=list)
    execution_diagnostic_index: int = -1
    execution_diagnostic_source: str | None = None


@dataclass
class UIState:
    config: AppConfig
    db: object
    tabs: list[FileTab] = field(default_factory=lambda: [FileTab()])
    active_tab_idx: int = 0
    tab_scroll: int = 0
    files: list[Path] = field(default_factory=list)
    status: str = "Ready"
    result_ratio: float = RESULT_RATIO_HALF
    result_grid_fullscreen: bool = False
    result_grid_fullscreen_previous_ratio: float = RESULT_RATIO_HALF
    focus: str = FOCUS_EDITOR
    internal_clipboard: str = ""
    browser_visible: bool = False
    browser_filter: str = ""
    browser_objects: dict[str, list[str]] = field(default_factory=empty_schema_object_groups)
    browser_expanded: set[str] = field(default_factory=set)
    browser_row: int = 0
    browser_scroll: int = 0
    browser_page_size: int = 10
    browser_loaded: bool = False
    schema_columns: dict[str, list[str]] = field(default_factory=dict)
    remembered_bind_values: dict[str, str] = field(default_factory=dict)
    db_operation: DbOperation | None = None

    def __post_init__(self) -> None:
        self.ensure_tab()

    def ensure_tab(self) -> None:
        if not self.tabs:
            self.tabs.append(FileTab())
        self.active_tab_idx = min(max(self.active_tab_idx, 0), len(self.tabs) - 1)
        self.tab_scroll = min(max(self.tab_scroll, 0), self.active_tab_idx)

    @property
    def active_tab(self) -> FileTab:
        self.ensure_tab()
        return self.tabs[self.active_tab_idx]

    @property
    def buffer(self) -> Buffer:
        return self.active_tab.buffer

    @buffer.setter
    def buffer(self, value: Buffer) -> None:
        self.active_tab.buffer = value

    @property
    def results(self) -> list[str]:
        return self.active_tab.results

    @results.setter
    def results(self, value: list[str]) -> None:
        self.active_tab.results = value
        self.active_tab.results_style = RESULT_STYLE_TEXT

    @property
    def results_style(self) -> str:
        return self.active_tab.results_style

    @results_style.setter
    def results_style(self, value: str) -> None:
        self.active_tab.results_style = value

    @property
    def dbms_output(self) -> list[str]:
        return self.active_tab.dbms_output

    @dbms_output.setter
    def dbms_output(self, value: list[str]) -> None:
        self.active_tab.dbms_output = value

    @property
    def show_dbms_output(self) -> bool:
        return self.active_tab.show_dbms_output

    @show_dbms_output.setter
    def show_dbms_output(self, value: bool) -> None:
        self.active_tab.show_dbms_output = value

    @property
    def last_result(self) -> QueryResult | None:
        return self.active_tab.last_result

    @last_result.setter
    def last_result(self, value: QueryResult | None) -> None:
        self.active_tab.last_result = value

    @property
    def active_result(self) -> QueryResult | None:
        return self.active_tab.active_result

    @active_result.setter
    def active_result(self, value: QueryResult | None) -> None:
        self.active_tab.active_result = value

    @property
    def explain_result(self) -> ExplainPlanResult | None:
        return self.active_tab.explain_result

    @explain_result.setter
    def explain_result(self, value: ExplainPlanResult | None) -> None:
        self.active_tab.explain_result = value

    @property
    def explain_scroll(self) -> int:
        return self.active_tab.explain_scroll

    @explain_scroll.setter
    def explain_scroll(self, value: int) -> None:
        self.active_tab.explain_scroll = value

    @property
    def explain_page_size(self) -> int:
        return self.active_tab.explain_page_size

    @explain_page_size.setter
    def explain_page_size(self, value: int) -> None:
        self.active_tab.explain_page_size = value

    @property
    def result_mode(self) -> str:
        return self.active_tab.result_mode

    @result_mode.setter
    def result_mode(self, value: str) -> None:
        self.active_tab.result_mode = value

    @property
    def result_row(self) -> int:
        return self.active_tab.result_row

    @result_row.setter
    def result_row(self, value: int) -> None:
        self.active_tab.result_row = value

    @property
    def result_col(self) -> int:
        return self.active_tab.result_col

    @result_col.setter
    def result_col(self, value: int) -> None:
        self.active_tab.result_col = value

    @property
    def result_row_scroll(self) -> int:
        return self.active_tab.result_row_scroll

    @result_row_scroll.setter
    def result_row_scroll(self, value: int) -> None:
        self.active_tab.result_row_scroll = value

    @property
    def result_col_scroll(self) -> int:
        return self.active_tab.result_col_scroll

    @result_col_scroll.setter
    def result_col_scroll(self, value: int) -> None:
        self.active_tab.result_col_scroll = value

    @property
    def result_page_size(self) -> int:
        return self.active_tab.result_page_size

    @result_page_size.setter
    def result_page_size(self, value: int) -> None:
        self.active_tab.result_page_size = value
