from __future__ import annotations

from typing import Any, Callable, Protocol

from .db_worker import DbCommandHandle, DbSessionState, DbWorkerTask


class DatabaseWorkerPort(Protocol):
    @property
    def session_state(self) -> DbSessionState: ...

    @property
    def terminal(self) -> bool: ...

    def submit(
        self,
        task: DbWorkerTask,
        *,
        ignored: bool = False,
        background: bool = False,
    ) -> DbCommandHandle: ...

    def cancel_current_operation(self, command_id: int) -> bool: ...

    def shutdown(self, timeout: float | None = None) -> None: ...


DbTask = Callable[[Any, Callable[[str], None]], Any]


class DbOperationsPort(Protocol):
    @property
    def active(self) -> bool: ...

    @property
    def completion_target_was_active(self) -> bool: ...

    def reject_if_active(self) -> bool: ...

    def interrupt(self) -> None: ...

    def start(
        self,
        kind: str,
        label: str,
        task: DbTask,
        *,
        statement_start_line: int = 1,
        statement_start_col: int = 0,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        restore_active_tab: bool = True,
        source_text: str | None = None,
        statement_count: int = 1,
        replace_terminal_worker: bool = False,
    ) -> bool: ...

    def submit_background(self, task: DbTask) -> DbCommandHandle: ...


class DialogPort(Protocol):
    def prompt(self, label: str, default: str = "", strip: bool = True) -> str | None: ...

    def prompt_text_box(
        self,
        label: str,
        default: str = "",
        strip: bool = True,
    ) -> str | None: ...

    def pick(self, title: str, options: list[str]) -> int | None: ...
