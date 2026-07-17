from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from ..config import save_autocommit
from ..db import TransactionReport
from .errors import short_error, wrap_error
from .ports import DbOperationsPort, DialogPort
from .results import (
    has_uncommitted_changes,
    is_autocommit_enabled,
    transaction_report_status,
)
from .state import UIState

if TYPE_CHECKING:
    from .result_presenter import ResultPresenter


@dataclass(frozen=True)
class TransactionCompletion:
    report: TransactionReport
    cleanup_error: Exception | None = None


@dataclass(frozen=True)
class ConnectionCompletion:
    old_session_close_error: Exception | None = None


@dataclass(frozen=True)
class TransactionModeChange:
    resolution: str | None
    report: TransactionReport | None = None
    transaction_error: Exception | None = None
    cleanup_error: Exception | None = None
    mode_error: Exception | None = None


class DatabaseSessionController:
    """Manage connection and transaction policy around database operations."""

    def __init__(
        self,
        state: UIState,
        db_operations: DbOperationsPort,
        dialogs: DialogPort,
        presenter: ResultPresenter,
    ) -> None:
        self.state = state
        self.db_operations = db_operations
        self.dialogs = dialogs
        self.presenter = presenter

    def try_connect(self, force: bool = False) -> None:
        if force:
            self.reconnect_database()
            return
        self._start_connect(force=False)

    def reconnect_database(self) -> None:
        if self.db_operations.reject_if_active():
            return
        resolution = self.prompt_pending_transaction(
            "Reconnect cancelled",
            allow_discard=True,
        )
        if resolution is None:
            return

        def reconnect_after_resolution() -> None:
            self._start_connect(force=True)

        if resolution == "commit":
            self.commit_transaction(
                after_success=reconnect_after_resolution,
                preserve_results_on_error=True,
            )
        elif resolution == "rollback":
            self.rollback_transaction(
                after_success=reconnect_after_resolution,
                preserve_results_on_error=True,
            )
        elif resolution in {"discard", "none"}:
            reconnect_after_resolution()
        else:
            self.state.status = "Reconnect cancelled"

    def _start_connect(self, *, force: bool) -> None:
        if self.db_operations.reject_if_active():
            return

        if force:
            self.presenter.close_all_result_continuations()
            self.presenter.invalidate_results_after_rollback()

        def connect(
            db: Any,
            progress: Callable[[str], None],
        ) -> ConnectionCompletion:
            close_error = None
            if force:
                try:
                    db.close()
                except Exception as exc:
                    close_error = exc
            db.ensure_connected()
            return ConnectionCompletion(close_error)

        def connected(result: ConnectionCompletion) -> None:
            self.state.status = f"Connected as {self.state.config.user}"
            if result.old_session_close_error is not None:
                self.state.status += (
                    " (warning: old session close failed: "
                    f"{short_error(result.old_session_close_error)})"
                )

        def connect_failed(exc: Exception) -> None:
            self.state.status = "Connection failed"
            self.presenter.set_results(
                ["ERROR connecting to Oracle:", *wrap_error(exc)]
            )

        self.db_operations.start(
            "connect",
            "Reconnecting to Oracle" if force else "Connecting to Oracle",
            connect,
            on_success=connected,
            on_error=connect_failed,
            replace_terminal_worker=True,
        )

    def choose_transaction_mode(self) -> None:
        if self.db_operations.reject_if_active():
            return
        current = "a" if is_autocommit_enabled(self.state.db) else "m"
        answer = self.dialogs.prompt(
            "Transaction mode (a=autocommit, m=manual)",
            current,
        )
        if answer is None or not answer:
            self.state.status = "Transaction mode unchanged"
            return
        normalized = answer.lower()
        if normalized.startswith("a"):
            resolution = None
            if (
                not is_autocommit_enabled(self.state.db)
                and has_uncommitted_changes(self.state.db)
            ):
                resolution = self.prompt_pending_transaction(
                    "Transaction mode unchanged"
                )
                if resolution is None:
                    return
            self.set_transaction_mode(True, resolution)
            return
        if normalized.startswith("m"):
            self.set_transaction_mode(False)
            return
        self.state.status = "Transaction mode unchanged"

    def prompt_pending_transaction(
        self,
        cancel_status: str,
        *,
        allow_discard: bool = False,
    ) -> str | None:
        if not has_uncommitted_changes(self.state.db):
            return "none"
        choices = "c=commit, r=rollback, x=cancel"
        if allow_discard:
            choices = "c=commit, r=rollback, d=discard session, x=cancel"
        answer = self.dialogs.prompt(
            f"Pending transaction: {choices}",
            "",
        )
        if answer is None:
            self.state.status = cancel_status
            return None
        normalized = answer.strip().lower()
        if normalized in {"c", "commit"}:
            return "commit"
        if normalized in {"r", "rollback"}:
            return "rollback"
        if allow_discard and normalized in {"d", "discard", "discard session"}:
            return "discard"
        self.state.status = cancel_status
        return None

    def set_transaction_mode(
        self,
        enabled: bool,
        resolution: str | None = None,
    ) -> bool:
        label = "autocommit" if enabled else "manual"

        def set_mode(
            db: Any,
            progress: Callable[[str], None],
        ) -> TransactionModeChange:
            report = None
            try:
                if resolution == "commit":
                    report = db.commit()
                elif resolution == "rollback":
                    report = db.rollback()
            except Exception as exc:
                return TransactionModeChange(resolution, transaction_error=exc)
            cleanup_error = None
            if resolution == "rollback":
                close_all = getattr(db, "close_all_result_continuations", None)
                if callable(close_all):
                    try:
                        close_all()
                    except Exception as exc:
                        cleanup_error = exc
            try:
                db.set_autocommit(enabled)
            except Exception as exc:
                return TransactionModeChange(
                    resolution,
                    report=report,
                    cleanup_error=cleanup_error,
                    mode_error=exc,
                )
            return TransactionModeChange(
                resolution,
                report=report,
                cleanup_error=cleanup_error,
            )

        def mode_set(result: TransactionModeChange) -> None:
            if result.resolution == "rollback" and result.report is not None:
                self.presenter.invalidate_results_after_rollback()
            if result.transaction_error is not None:
                action = "Commit" if result.resolution == "commit" else "Rollback"
                error_header = (
                    "ERROR committing transaction:"
                    if result.resolution == "commit"
                    else "ERROR rolling back transaction:"
                )
                self.state.status = f"{action} failed"
                self.presenter.set_results(
                    [error_header, *wrap_error(result.transaction_error)]
                )
                return
            if result.mode_error is not None:
                suffix = (
                    f" after {result.resolution}"
                    if result.resolution in {"commit", "rollback"}
                    else ""
                )
                self.state.status = f"Transaction mode change failed{suffix}"
                self.presenter.set_results(
                    [
                        "ERROR changing transaction mode:",
                        *wrap_error(result.mode_error),
                    ]
                )
                return
            persistence_error: BaseException | None = None
            persistence_interruption: BaseException | None = None
            try:
                save_autocommit(self.state.config, enabled)
            except Exception as exc:
                persistence_error = exc
            except BaseException as exc:
                persistence_error = exc
                persistence_interruption = exc
            self.state.status = f"Transaction mode: {label}"
            if persistence_interruption is not None:
                persistence_message = str(persistence_interruption) or type(
                    persistence_interruption
                ).__name__
                self.state.status += (
                    " (warning: live mode changed, preference save was "
                    f"interrupted; verify config: {persistence_message})"
                )
            elif persistence_error is not None:
                persistence_message = str(persistence_error) or type(
                    persistence_error
                ).__name__
                self.state.status += (
                    " (warning: live mode changed, preference was not saved: "
                    f"{persistence_message})"
                )
            if result.cleanup_error is not None:
                self.state.status += (
                    " (warning: result cleanup failed: "
                    f"{short_error(result.cleanup_error)})"
                )
            if persistence_interruption is not None:
                raise persistence_interruption

        def mode_failed(exc: Exception) -> None:
            self.state.status = "Transaction mode change failed"
            self.presenter.set_results(
                ["ERROR changing transaction mode:", *wrap_error(exc)]
            )

        return self.db_operations.start(
            "transaction-mode",
            f"Setting transaction mode: {label}",
            set_mode,
            on_success=mode_set,
            on_error=mode_failed,
        )

    def commit_transaction(
        self,
        after_success: Callable[[], None] | None = None,
        after_error: Callable[[], None] | None = None,
        *,
        preserve_results_on_error: bool = False,
    ) -> bool:
        if self.db_operations.reject_if_active():
            return False

        def committed(report: TransactionReport) -> None:
            self.state.status = transaction_report_status(
                "Committed transaction",
                report,
            )
            if after_success is not None:
                after_success()

        def commit_failed(exc: Exception) -> None:
            self.state.status = "Commit failed"
            self.presenter.set_results(
                ["ERROR committing transaction:", *wrap_error(exc)],
                clear_table=not preserve_results_on_error,
            )
            if after_error is not None:
                after_error()

        def commit(
            db: Any,
            progress: Callable[[str], None],
        ) -> TransactionReport:
            return db.commit()

        return self.db_operations.start(
            "commit",
            "Committing transaction",
            commit,
            on_success=committed,
            on_error=commit_failed,
        )

    def rollback_transaction(
        self,
        after_success: Callable[[], None] | None = None,
        after_error: Callable[[], None] | None = None,
        *,
        preserve_results_on_error: bool = False,
    ) -> bool:
        if self.db_operations.reject_if_active():
            return False

        def rollback(
            db: Any,
            progress: Callable[[str], None],
        ) -> TransactionCompletion:
            report = db.rollback()
            cleanup_error = None
            close_all = getattr(db, "close_all_result_continuations", None)
            if callable(close_all):
                try:
                    close_all()
                except Exception as exc:
                    cleanup_error = exc
            return TransactionCompletion(report, cleanup_error)

        def rolled_back(completion: TransactionCompletion) -> None:
            self.presenter.invalidate_results_after_rollback()
            self.state.status = transaction_report_status(
                "Rollback transaction",
                completion.report,
            )
            if completion.cleanup_error is not None:
                self.state.status += (
                    " (warning: result cleanup failed: "
                    f"{short_error(completion.cleanup_error)})"
                )
            if after_success is not None:
                after_success()

        def rollback_failed(exc: Exception) -> None:
            self.state.status = "Rollback failed"
            self.presenter.set_results(
                ["ERROR rolling back transaction:", *wrap_error(exc)],
                clear_table=not preserve_results_on_error,
            )
            if after_error is not None:
                after_error()

        return self.db_operations.start(
            "rollback",
            "Rolling back transaction",
            rollback,
            on_success=rolled_back,
            on_error=rollback_failed,
        )
