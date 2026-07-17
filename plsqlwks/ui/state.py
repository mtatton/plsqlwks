from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..db import ExplainPlanResult, QueryResult, QueryResultPage, empty_schema_object_groups
from .buffer import Buffer
from .constants import FOCUS_EDITOR, RESULT_GRID, RESULT_RATIO_HALF, RESULT_STYLE_TEXT
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
    interrupted: bool = False


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
    progress_current: int | None = None
    progress_total: int | None = None
    on_interrupt: Callable[[], object] | None = None
    interrupt_database: bool = True


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
class ResultsState:
    lines: list[str] = field(default_factory=list)
    style: str = RESULT_STYLE_TEXT
    help_lines: list[HelpLine] = field(default_factory=lambda: list(HELP_LINES))
    scroll: int | None = None
    dbms_output: list[str] = field(default_factory=list)
    dbms_output_grouped: bool = False
    dbms_output_scroll: int | None = None
    show_dbms_output: bool = False
    last_result: QueryResult | None = None
    active_result: QueryResult | None = None
    explain_result: ExplainPlanResult | None = None
    explain_scroll: int = 0
    explain_page_size: int = 10
    mode: str = RESULT_GRID
    row: int = 0
    col: int = 0
    row_scroll: int = 0
    col_scroll: int = 0
    page_size: int = 10
    insert_draft: ResultInsertDraft | None = None


@dataclass(init=False)
class FileTab:
    """A document tab with separately owned document and result state.

    The explicit initializer and properties preserve the public ``FileTab``
    compatibility surface while result storage lives in ``ResultsState``.
    """

    buffer: Buffer
    source_key: str | None
    result_state: ResultsState
    search_query: str
    execution_diagnostics: list[ExecutionDiagnostic]
    execution_diagnostic_index: int
    execution_diagnostic_source: str | None

    def __init__(
        self,
        buffer: Buffer | None = None,
        source_key: str | None = None,
        results: list[str] | None = None,
        results_style: str = RESULT_STYLE_TEXT,
        help_lines: list[HelpLine] | None = None,
        results_scroll: int | None = None,
        dbms_output: list[str] | None = None,
        dbms_output_grouped: bool = False,
        dbms_output_scroll: int | None = None,
        show_dbms_output: bool = False,
        last_result: QueryResult | None = None,
        active_result: QueryResult | None = None,
        explain_result: ExplainPlanResult | None = None,
        explain_scroll: int = 0,
        explain_page_size: int = 10,
        result_mode: str = RESULT_GRID,
        result_row: int = 0,
        result_col: int = 0,
        result_row_scroll: int = 0,
        result_col_scroll: int = 0,
        result_page_size: int = 10,
        result_insert_draft: ResultInsertDraft | None = None,
        search_query: str = "",
        execution_diagnostics: list[ExecutionDiagnostic] | None = None,
        execution_diagnostic_index: int = -1,
        execution_diagnostic_source: str | None = None,
    ) -> None:
        self.buffer = Buffer() if buffer is None else buffer
        self.source_key = source_key
        self.result_state = ResultsState(
            lines=[] if results is None else results,
            style=results_style,
            help_lines=list(HELP_LINES) if help_lines is None else help_lines,
            scroll=results_scroll,
            dbms_output=[] if dbms_output is None else dbms_output,
            dbms_output_grouped=dbms_output_grouped,
            dbms_output_scroll=dbms_output_scroll,
            show_dbms_output=show_dbms_output,
            last_result=last_result,
            active_result=active_result,
            explain_result=explain_result,
            explain_scroll=explain_scroll,
            explain_page_size=explain_page_size,
            mode=result_mode,
            row=result_row,
            col=result_col,
            row_scroll=result_row_scroll,
            col_scroll=result_col_scroll,
            page_size=result_page_size,
            insert_draft=result_insert_draft,
        )
        self.search_query = search_query
        self.execution_diagnostics = [] if execution_diagnostics is None else execution_diagnostics
        self.execution_diagnostic_index = execution_diagnostic_index
        self.execution_diagnostic_source = execution_diagnostic_source

    @property
    def results(self) -> list[str]:
        return self.result_state.lines

    @results.setter
    def results(self, value: list[str]) -> None:
        self.result_state.lines = value

    @property
    def results_style(self) -> str:
        return self.result_state.style

    @results_style.setter
    def results_style(self, value: str) -> None:
        self.result_state.style = value

    @property
    def help_lines(self) -> list[HelpLine]:
        return self.result_state.help_lines

    @help_lines.setter
    def help_lines(self, value: list[HelpLine]) -> None:
        self.result_state.help_lines = value

    @property
    def results_scroll(self) -> int | None:
        return self.result_state.scroll

    @results_scroll.setter
    def results_scroll(self, value: int | None) -> None:
        self.result_state.scroll = value

    @property
    def dbms_output(self) -> list[str]:
        return self.result_state.dbms_output

    @dbms_output.setter
    def dbms_output(self, value: list[str]) -> None:
        self.result_state.dbms_output = value

    @property
    def dbms_output_grouped(self) -> bool:
        return self.result_state.dbms_output_grouped

    @dbms_output_grouped.setter
    def dbms_output_grouped(self, value: bool) -> None:
        self.result_state.dbms_output_grouped = value

    @property
    def dbms_output_scroll(self) -> int | None:
        return self.result_state.dbms_output_scroll

    @dbms_output_scroll.setter
    def dbms_output_scroll(self, value: int | None) -> None:
        self.result_state.dbms_output_scroll = value

    @property
    def show_dbms_output(self) -> bool:
        return self.result_state.show_dbms_output

    @show_dbms_output.setter
    def show_dbms_output(self, value: bool) -> None:
        self.result_state.show_dbms_output = value

    @property
    def last_result(self) -> QueryResult | None:
        return self.result_state.last_result

    @last_result.setter
    def last_result(self, value: QueryResult | None) -> None:
        self.result_state.last_result = value

    @property
    def active_result(self) -> QueryResult | None:
        return self.result_state.active_result

    @active_result.setter
    def active_result(self, value: QueryResult | None) -> None:
        self.result_state.active_result = value

    @property
    def explain_result(self) -> ExplainPlanResult | None:
        return self.result_state.explain_result

    @explain_result.setter
    def explain_result(self, value: ExplainPlanResult | None) -> None:
        self.result_state.explain_result = value

    @property
    def explain_scroll(self) -> int:
        return self.result_state.explain_scroll

    @explain_scroll.setter
    def explain_scroll(self, value: int) -> None:
        self.result_state.explain_scroll = value

    @property
    def explain_page_size(self) -> int:
        return self.result_state.explain_page_size

    @explain_page_size.setter
    def explain_page_size(self, value: int) -> None:
        self.result_state.explain_page_size = value

    @property
    def result_mode(self) -> str:
        return self.result_state.mode

    @result_mode.setter
    def result_mode(self, value: str) -> None:
        self.result_state.mode = value

    @property
    def result_row(self) -> int:
        return self.result_state.row

    @result_row.setter
    def result_row(self, value: int) -> None:
        self.result_state.row = value

    @property
    def result_col(self) -> int:
        return self.result_state.col

    @result_col.setter
    def result_col(self, value: int) -> None:
        self.result_state.col = value

    @property
    def result_row_scroll(self) -> int:
        return self.result_state.row_scroll

    @result_row_scroll.setter
    def result_row_scroll(self, value: int) -> None:
        self.result_state.row_scroll = value

    @property
    def result_col_scroll(self) -> int:
        return self.result_state.col_scroll

    @result_col_scroll.setter
    def result_col_scroll(self, value: int) -> None:
        self.result_state.col_scroll = value

    @property
    def result_page_size(self) -> int:
        return self.result_state.page_size

    @result_page_size.setter
    def result_page_size(self, value: int) -> None:
        self.result_state.page_size = value

    @property
    def result_insert_draft(self) -> ResultInsertDraft | None:
        return self.result_state.insert_draft

    @result_insert_draft.setter
    def result_insert_draft(self, value: ResultInsertDraft | None) -> None:
        self.result_state.insert_draft = value


@dataclass
class ApplicationState:
    config: AppConfig
    status: str = "Ready"
    focus: str = FOCUS_EDITOR
    running: bool = True
    quit_pending: bool = False
    result_ratio: float = RESULT_RATIO_HALF
    result_grid_fullscreen: bool = False
    result_grid_fullscreen_previous_ratio: float = RESULT_RATIO_HALF
    internal_clipboard: str = ""
    remembered_bind_values: dict[str, str] = field(default_factory=dict)


@dataclass
class DocumentState:
    tabs: list[FileTab] = field(default_factory=lambda: [FileTab()])
    active_tab_idx: int = 0
    tab_scroll: int = 0
    files: list[Path] = field(default_factory=list)


@dataclass
class BrowserState:
    visible: bool = False
    filter_text: str = ""
    objects: dict[str, list[str]] = field(default_factory=empty_schema_object_groups)
    expanded: set[str] = field(default_factory=set)
    row: int = 0
    scroll: int = 0
    page_size: int = 10
    loaded: bool = False
    schema_columns: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class DatabaseActivityState:
    session: object
    operation: DbOperation | None = None
    completion_target_was_active: bool = True
    completion_interrupted: bool = False


def replace_browser_filter(
    state: BrowserState,
    filter_text: str,
    selected_row: int,
) -> BrowserState:
    """Return browser state for a new filter without terminal dependencies."""
    return replace(
        state,
        filter_text=filter_text,
        row=max(0, selected_row),
        scroll=0,
    )


def begin_database_operation(
    state: DatabaseActivityState,
    operation: DbOperation,
) -> DatabaseActivityState:
    if state.operation is not None:
        raise RuntimeError("database operation already running")
    return replace(state, operation=operation)


def update_database_operation_progress(
    operation: DbOperation,
    label: str,
    current: int | None,
    total: int | None,
) -> DbOperation:
    return replace(
        operation,
        label=label,
        progress_current=current,
        progress_total=total,
    )


def request_database_operation_cancel(
    operation: DbOperation,
    label: str,
) -> DbOperation:
    return replace(operation, label=label, cancel_requested=True)


def finish_database_operation(state: DatabaseActivityState) -> DatabaseActivityState:
    return replace(state, operation=None)


def normalize_document_state(state: DocumentState) -> DocumentState:
    """Return a valid document selection without touching terminal state."""
    tabs = state.tabs or [FileTab()]
    active_tab_idx = min(max(state.active_tab_idx, 0), len(tabs) - 1)
    tab_scroll = min(max(state.tab_scroll, 0), active_tab_idx)
    if tabs is state.tabs and active_tab_idx == state.active_tab_idx and tab_scroll == state.tab_scroll:
        return state
    return DocumentState(tabs, active_tab_idx, tab_scroll, state.files)


@dataclass(init=False)
class UIState:
    """Compatibility aggregate over responsibility-focused UI state slices."""

    application: ApplicationState
    documents: DocumentState
    browser: BrowserState
    database: DatabaseActivityState

    def __init__(
        self,
        config: AppConfig,
        db: object,
        tabs: list[FileTab] | None = None,
        active_tab_idx: int = 0,
        tab_scroll: int = 0,
        files: list[Path] | None = None,
        status: str = "Ready",
        result_ratio: float = RESULT_RATIO_HALF,
        result_grid_fullscreen: bool = False,
        result_grid_fullscreen_previous_ratio: float = RESULT_RATIO_HALF,
        focus: str = FOCUS_EDITOR,
        internal_clipboard: str = "",
        browser_visible: bool = False,
        browser_filter: str = "",
        browser_objects: dict[str, list[str]] | None = None,
        browser_expanded: set[str] | None = None,
        browser_row: int = 0,
        browser_scroll: int = 0,
        browser_page_size: int = 10,
        browser_loaded: bool = False,
        schema_columns: dict[str, list[str]] | None = None,
        remembered_bind_values: dict[str, str] | None = None,
        db_operation: DbOperation | None = None,
    ) -> None:
        self.application = ApplicationState(
            config=config,
            status=status,
            focus=focus,
            result_ratio=result_ratio,
            result_grid_fullscreen=result_grid_fullscreen,
            result_grid_fullscreen_previous_ratio=result_grid_fullscreen_previous_ratio,
            internal_clipboard=internal_clipboard,
            remembered_bind_values=(
                {} if remembered_bind_values is None else remembered_bind_values
            ),
        )
        self.documents = normalize_document_state(
            DocumentState(
                tabs=[FileTab()] if tabs is None else tabs,
                active_tab_idx=active_tab_idx,
                tab_scroll=tab_scroll,
                files=[] if files is None else files,
            )
        )
        self.browser = BrowserState(
            visible=browser_visible,
            filter_text=browser_filter,
            objects=(empty_schema_object_groups() if browser_objects is None else browser_objects),
            expanded=set() if browser_expanded is None else browser_expanded,
            row=browser_row,
            scroll=browser_scroll,
            page_size=browser_page_size,
            loaded=browser_loaded,
            schema_columns={} if schema_columns is None else schema_columns,
        )
        self.database = DatabaseActivityState(db, db_operation)

    def ensure_tab(self) -> None:
        self.documents = normalize_document_state(self.documents)

    @property
    def config(self) -> AppConfig:
        return self.application.config

    @config.setter
    def config(self, value: AppConfig) -> None:
        self.application.config = value

    @property
    def db(self) -> object:
        return self.database.session

    @db.setter
    def db(self, value: object) -> None:
        self.database.session = value

    @property
    def tabs(self) -> list[FileTab]:
        return self.documents.tabs

    @tabs.setter
    def tabs(self, value: list[FileTab]) -> None:
        self.documents.tabs = value

    @property
    def active_tab_idx(self) -> int:
        return self.documents.active_tab_idx

    @active_tab_idx.setter
    def active_tab_idx(self, value: int) -> None:
        self.documents.active_tab_idx = value

    @property
    def tab_scroll(self) -> int:
        return self.documents.tab_scroll

    @tab_scroll.setter
    def tab_scroll(self, value: int) -> None:
        self.documents.tab_scroll = value

    @property
    def files(self) -> list[Path]:
        return self.documents.files

    @files.setter
    def files(self, value: list[Path]) -> None:
        self.documents.files = value

    @property
    def status(self) -> str:
        return self.application.status

    @status.setter
    def status(self, value: str) -> None:
        self.application.status = value

    @property
    def result_ratio(self) -> float:
        return self.application.result_ratio

    @result_ratio.setter
    def result_ratio(self, value: float) -> None:
        self.application.result_ratio = value

    @property
    def result_grid_fullscreen(self) -> bool:
        return self.application.result_grid_fullscreen

    @result_grid_fullscreen.setter
    def result_grid_fullscreen(self, value: bool) -> None:
        self.application.result_grid_fullscreen = value

    @property
    def result_grid_fullscreen_previous_ratio(self) -> float:
        return self.application.result_grid_fullscreen_previous_ratio

    @result_grid_fullscreen_previous_ratio.setter
    def result_grid_fullscreen_previous_ratio(self, value: float) -> None:
        self.application.result_grid_fullscreen_previous_ratio = value

    @property
    def focus(self) -> str:
        return self.application.focus

    @focus.setter
    def focus(self, value: str) -> None:
        self.application.focus = value

    @property
    def internal_clipboard(self) -> str:
        return self.application.internal_clipboard

    @internal_clipboard.setter
    def internal_clipboard(self, value: str) -> None:
        self.application.internal_clipboard = value

    @property
    def remembered_bind_values(self) -> dict[str, str]:
        return self.application.remembered_bind_values

    @remembered_bind_values.setter
    def remembered_bind_values(self, value: dict[str, str]) -> None:
        self.application.remembered_bind_values = value

    @property
    def browser_visible(self) -> bool:
        return self.browser.visible

    @browser_visible.setter
    def browser_visible(self, value: bool) -> None:
        self.browser.visible = value

    @property
    def browser_filter(self) -> str:
        return self.browser.filter_text

    @browser_filter.setter
    def browser_filter(self, value: str) -> None:
        self.browser.filter_text = value

    @property
    def browser_objects(self) -> dict[str, list[str]]:
        return self.browser.objects

    @browser_objects.setter
    def browser_objects(self, value: dict[str, list[str]]) -> None:
        self.browser.objects = value

    @property
    def browser_expanded(self) -> set[str]:
        return self.browser.expanded

    @browser_expanded.setter
    def browser_expanded(self, value: set[str]) -> None:
        self.browser.expanded = value

    @property
    def browser_row(self) -> int:
        return self.browser.row

    @browser_row.setter
    def browser_row(self, value: int) -> None:
        self.browser.row = value

    @property
    def browser_scroll(self) -> int:
        return self.browser.scroll

    @browser_scroll.setter
    def browser_scroll(self, value: int) -> None:
        self.browser.scroll = value

    @property
    def browser_page_size(self) -> int:
        return self.browser.page_size

    @browser_page_size.setter
    def browser_page_size(self, value: int) -> None:
        self.browser.page_size = value

    @property
    def browser_loaded(self) -> bool:
        return self.browser.loaded

    @browser_loaded.setter
    def browser_loaded(self, value: bool) -> None:
        self.browser.loaded = value

    @property
    def schema_columns(self) -> dict[str, list[str]]:
        return self.browser.schema_columns

    @schema_columns.setter
    def schema_columns(self, value: dict[str, list[str]]) -> None:
        self.browser.schema_columns = value

    @property
    def db_operation(self) -> DbOperation | None:
        return self.database.operation

    @db_operation.setter
    def db_operation(self, value: DbOperation | None) -> None:
        self.database.operation = value

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
