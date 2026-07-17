from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from ..db import QueryResult, QueryResultContinuation, QueryResultPage
from ..exporting import ExportCancelled, raise_if_export_cancelled
from ..html_exporting import render_html_result
from ..plugins._result_export import (
    default_result_filename,
    local_now,
    prepare_export_path,
    short_error,
)
from ..plugins.api import PluginContext, ResultSnapshot
from ..plugins.csv_export import CsvExportOptions, write_csv_snapshot
from ..plugins.html_export import HtmlExportOptions, write_html_snapshot
from ..plugins.xlsx_export import (
    XlsxExportOptions,
    preflight_xlsx_snapshot,
    write_xlsx_snapshot,
)
from .plugin_host import snapshot_result
from .ports import DbOperationsPort, DialogPort
from .results import is_database_connected
from .state import FileTab, UIState

_EXPORT_MODE_TITLE = "Export rows"
_EXPORT_MODE_OPTIONS = (
    "Loaded rows only (default)",
    "All available rows (keep the result grid unchanged)",
)


class ResultExportPresenterPort(Protocol):
    def handle_fetch_more_error(
        self,
        exc: Exception,
        *,
        interrupted: bool = False,
    ) -> None: ...


@dataclass
class _ExportJob:
    format_id: str
    context: PluginContext
    options: object
    path: Path
    result: QueryResult
    export_result: QueryResult
    tab: FileTab
    full: bool
    cancelled: Event


class ResultExportController:
    """Coordinate bundled exports without widening the public plugin API."""

    def __init__(
        self,
        state: UIState,
        db_operations: DbOperationsPort,
        dialogs: DialogPort,
        presenter: ResultExportPresenterPort,
    ) -> None:
        self.state = state
        self.db_operations = db_operations
        self.dialogs = dialogs
        self.presenter = presenter
        self._job: _ExportJob | None = None

    def __call__(
        self,
        format_id: str,
        context: PluginContext,
        options: object,
    ) -> None:
        if context.has_active_insert_draft():
            context.set_status("Export unavailable while an insert draft is active; commit or cancel the draft first")
            return
        result = self.state.active_result
        if result is None or not result.columns:
            context.set_status("No table result is available for export")
            return
        if self.db_operations.reject_if_active():
            return

        full = self._ask_for_full_export(context)
        if full is None:
            return
        if full and result.has_more_rows and result.continuation is None:
            context.set_status(
                "Full export is unavailable because the result cursor is no longer "
                "available; choose loaded rows instead"
            )
            return
        if full and result.continuation is not None and not is_database_connected(self.state.db):
            context.set_status("Full export is unavailable while disconnected; choose loaded rows instead")
            return

        label = self._format_label(format_id)
        try:
            self._preflight(format_id, options)
        except Exception as exc:
            context.set_status(f"{label} export failed: {short_error(exc)}")
            context.report_error(f"{label} export", exc)
            return
        path = prepare_export_path(
            context,
            f"Export result to {label}",
            default_result_filename(local_now(), format_id),
        )
        if path is None:
            return

        job = _ExportJob(
            format_id=format_id,
            context=context,
            options=options,
            path=path,
            result=result,
            export_result=self._copy_result_for_export(result),
            tab=self.state.active_tab,
            full=full,
            cancelled=Event(),
        )
        self._job = job
        if full and result.continuation is not None:
            self._fetch_next_page(job)
        else:
            self._start_writer(job)

    def _ask_for_full_export(self, context: PluginContext) -> bool | None:
        selected = self.dialogs.pick(
            _EXPORT_MODE_TITLE,
            list(_EXPORT_MODE_OPTIONS),
        )
        if selected is None:
            context.set_status("Export cancelled")
            return None
        return selected == 1

    @staticmethod
    def _format_label(format_id: str) -> str:
        return format_id.upper()

    @staticmethod
    def _preflight(format_id: str, options: object) -> None:
        if format_id == "csv":
            assert isinstance(options, CsvExportOptions)
            if len(options.separator) != 1:
                raise ValueError("CSV delimiter must be exactly one character")
            return
        if format_id == "html":
            assert isinstance(options, HtmlExportOptions)
            render_html_result(
                title="",
                columns=(),
                rows=(),
                has_more=False,
                theme=options.theme,
            )
            return
        if format_id == "xlsx":
            assert isinstance(options, XlsxExportOptions)
            preflight_xlsx_snapshot(options)
            return
        raise ValueError(f"Unsupported bundled export format: {format_id}")

    def _source_is_current(self, job: _ExportJob) -> bool:
        return job.tab in self.state.tabs and job.tab.active_result is job.result

    @staticmethod
    def _copy_result_for_export(result: QueryResult) -> QueryResult:
        """Create a private paging buffer without changing the visible grid."""
        return QueryResult(
            title=result.title,
            columns=list(result.columns),
            rows=[list(row) for row in result.rows],
            message=result.message,
            continuation=result.continuation,
            original_rows=[list(row) for row in result.original_rows],
            has_more_rows=result.has_more_rows,
        )

    def _fetch_next_page(self, job: _ExportJob) -> None:
        if self._job is not job or not self._source_is_current(job):
            self._abort_changed_result(job, None)
            return
        continuation = job.export_result.continuation
        if continuation is None:
            self._start_writer(job)
            return
        loaded_rows = len(job.export_result.rows)
        page_rows = self.state.config.max_rows
        label = f"Fetching rows for {self._format_label(job.format_id)} export ({loaded_rows:,} ready)"

        def fetch_page(db: Any, progress: Callable[..., None]) -> QueryResultPage:
            raise_if_export_cancelled(job.cancelled.is_set)
            progress(
                label,
                current=loaded_rows,
                total=None,
            )
            return db.fetch_more_rows(
                continuation,
                loaded_rows,
                page_rows=page_rows,
            )

        started = self.db_operations.start(
            "export-fetch",
            label,
            fetch_page,
            on_success=lambda page: self._page_loaded(job, page),
            on_error=lambda exc: self._page_failed(job, exc),
            progress_current=loaded_rows,
            progress_total=None,
            on_interrupt=job.cancelled.set,
            interrupt_database=True,
        )
        if not started:
            self._job = None

    def _page_loaded(self, job: _ExportJob, page: QueryResultPage) -> None:
        if self._job is not job or not self._source_is_current(job):
            self._abort_changed_result(job, page.continuation)
            return

        export_result = job.export_result
        export_result.rows.extend(page.rows)
        if page.original_rows:
            export_result.original_rows.extend(page.original_rows)
        elif export_result.original_rows:
            export_result.original_rows.extend([list(row) for row in page.rows])
        export_result.message = page.message
        export_result.continuation = page.continuation
        export_result.has_more_rows = page.has_more_rows
        job.result.continuation = page.continuation

        if not is_database_connected(self.state.db):
            self._job = None
            self.state.status = (
                f"Full export stopped after {len(export_result.rows):,} row(s) because "
                "the connection was lost; no file was written and the result is read-only"
            )
            return
        if job.cancelled.is_set():
            self._job = None
            self.state.status = (
                f"Export cancelled during row fetch after {len(export_result.rows):,} row(s); result is read-only"
            )
            return
        if export_result.continuation is not None:
            self._fetch_next_page(job)
            return
        self._start_writer(job)

    def _page_failed(self, job: _ExportJob, exc: Exception) -> None:
        self._job = None
        if job.cancelled.is_set():
            self.state.status = (
                f"Export cancelled during row fetch after {len(job.export_result.rows):,} row(s); result is read-only"
            )
            return
        self.presenter.handle_fetch_more_error(exc)

    def _abort_changed_result(
        self,
        job: _ExportJob,
        continuation: QueryResultContinuation | None,
    ) -> None:
        self._job = None
        token = continuation or job.export_result.continuation
        job.result.continuation = None
        job.export_result.continuation = None
        if token is not None:
            self.db_operations.submit_background(lambda db, progress: db.close_result_continuation(token))
        self.state.status = "Export cancelled because the source result changed"

    def _start_writer(self, job: _ExportJob) -> None:
        if self._job is not job or not self._source_is_current(job):
            self._abort_changed_result(job, None)
            return
        row_count = len(job.export_result.rows)
        label = f"Writing {self._format_label(job.format_id)} export"

        def write_export(db: Any, progress: Callable[..., None]) -> ResultSnapshot:
            current_snapshot = snapshot_result(
                job.export_result,
                cancelled=job.cancelled.is_set,
            )
            if current_snapshot is None:
                raise RuntimeError("No table result is available for export")
            last_bucket = -1

            def report(current: int, total: int) -> None:
                nonlocal last_bucket
                bucket = 100 if total == 0 else min(100, current * 100 // total)
                if bucket == last_bucket and current not in (0, total):
                    return
                last_bucket = bucket
                progress(label, current=current, total=total)

            self._write_snapshot(
                job,
                current_snapshot,
                report,
                job.cancelled.is_set,
            )
            return current_snapshot

        started = self.db_operations.start(
            "export-write",
            label,
            write_export,
            on_success=lambda written: self._write_succeeded(job, written),
            on_error=lambda exc: self._write_failed(job, exc),
            progress_current=0,
            progress_total=row_count,
            on_interrupt=job.cancelled.set,
            interrupt_database=False,
        )
        if not started:
            self._job = None

    @staticmethod
    def _write_snapshot(
        job: _ExportJob,
        snapshot: ResultSnapshot,
        progress: Callable[[int, int], None],
        cancelled: Callable[[], bool],
    ) -> None:
        if job.format_id == "csv":
            assert isinstance(job.options, CsvExportOptions)
            write_csv_snapshot(
                job.path,
                snapshot,
                job.options,
                on_progress=progress,
                cancelled=cancelled,
            )
            return
        if job.format_id == "html":
            assert isinstance(job.options, HtmlExportOptions)
            write_html_snapshot(
                job.path,
                snapshot,
                job.options,
                on_progress=progress,
                cancelled=cancelled,
            )
            return
        if job.format_id == "xlsx":
            assert isinstance(job.options, XlsxExportOptions)
            write_xlsx_snapshot(
                job.path,
                snapshot,
                job.options,
                on_progress=progress,
                cancelled=cancelled,
            )
            return
        raise ValueError(f"Unsupported bundled export format: {job.format_id}")

    def _write_succeeded(
        self,
        job: _ExportJob,
        snapshot: ResultSnapshot,
    ) -> None:
        self._job = None
        count = len(snapshot.rows)
        if not job.full:
            message = f"Exported {count:,} loaded row(s) to {job.path}"
            if snapshot.has_more:
                message += "; additional rows are available"
        else:
            message = f"Exported all {count:,} available row(s) to {job.path}"
        self.state.status = message

    def _write_failed(self, job: _ExportJob, exc: Exception) -> None:
        self._job = None
        if isinstance(exc, ExportCancelled) or job.cancelled.is_set():
            self.state.status = (
                "Export cancelled; destination unchanged; file cancellation did not "
                "interrupt Oracle or change transaction state | "
                f"{self._transaction_status()}"
            )
            return
        label = self._format_label(job.format_id)
        job.context.set_status(f"{label} export failed: {short_error(exc)}")
        job.context.report_error(f"{label} export", exc)

    def _transaction_status(self) -> str:
        db = self.state.db
        if bool(getattr(db, "autocommit", False)):
            return "autocommit remains enabled"
        if bool(getattr(db, "has_uncommitted_changes", False)):
            return "pre-existing pending transaction remains unresolved; commit or roll back explicitly"
        return "no pending transaction was tracked"
