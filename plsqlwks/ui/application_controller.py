from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .state import UIState


class DocumentLifecyclePort(Protocol):
    def confirm_quit(self) -> bool: ...

    def persist_session_tabs(self) -> bool: ...


class DatabaseSessionPort(Protocol):
    def prompt_pending_transaction(
        self,
        cancel_status: str,
        *,
        allow_discard: bool = False,
    ) -> str | None: ...

    def commit_transaction(
        self,
        after_success: Callable[[], None] | None = None,
        after_error: Callable[[], None] | None = None,
        *,
        preserve_results_on_error: bool = False,
    ) -> bool: ...

    def rollback_transaction(
        self,
        after_success: Callable[[], None] | None = None,
        after_error: Callable[[], None] | None = None,
        *,
        preserve_results_on_error: bool = False,
    ) -> bool: ...


class ResultCommitPort(Protocol):
    def commit_insert_draft_if_active(self) -> bool: ...


class ApplicationController:
    def __init__(
        self,
        state: UIState,
        documents: DocumentLifecyclePort,
        database: DatabaseSessionPort,
        results: ResultCommitPort,
    ) -> None:
        self.state = state
        self.documents = documents
        self.database = database
        self.results = results

    @property
    def running(self) -> bool:
        return self.state.application.running

    @running.setter
    def running(self, value: bool) -> None:
        self.state.application.running = value

    @property
    def quit_pending(self) -> bool:
        return self.state.application.quit_pending

    @quit_pending.setter
    def quit_pending(self, value: bool) -> None:
        self.state.application.quit_pending = value

    def request_quit(self) -> None:
        if not self.documents.confirm_quit():
            return
        resolution = self.database.prompt_pending_transaction("Quit cancelled")
        if resolution is None:
            return

        self.quit_pending = True

        def finish_quit() -> None:
            self.quit_pending = False
            self.documents.persist_session_tabs()
            self.running = False

        def cancel_quit() -> None:
            self.quit_pending = False

        if resolution == "commit":
            if not self.database.commit_transaction(finish_quit, cancel_quit):
                cancel_quit()
        elif resolution == "rollback":
            if not self.database.rollback_transaction(finish_quit, cancel_quit):
                cancel_quit()
        else:
            finish_quit()

    def commit_or_insert_draft(self) -> None:
        if self.results.commit_insert_draft_if_active():
            return
        self.database.commit_transaction()
