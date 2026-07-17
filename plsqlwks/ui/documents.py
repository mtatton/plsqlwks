from __future__ import annotations

import configparser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import AppConfig, SessionTab, save_session_tabs
from ..workspace import list_workspace_files
from .browser import (
    clamp_tab_index,
    file_source_key,
    tab_display_title,
    template_source_key,
    visible_tab_labels,
)
from .buffer import (
    Buffer,
    FileVersion,
    FileVersionConflictError,
    read_file_version,
)
from .constants import FOCUS_EDITOR, FOCUS_RESULTS, TEMPLATES
from .errors import short_error, wrap_error
from .ports import DbOperationsPort, DialogPort
from .sql import looks_like_plsql
from .state import FileTab, UIState


class DocumentResultPort(Protocol):
    def set_results(self, lines: list[str], clear_table: bool = True) -> None: ...

    def close_tab_result_continuations(self, tab: FileTab) -> None: ...


ListWorkspaceFiles = Callable[[AppConfig], list[Path]]
WriteSessionTabs = Callable[[AppConfig, Sequence[SessionTab], int], None]


@dataclass(frozen=True)
class _SaveTarget:
    path: Path
    source_key: str
    version: FileVersion


class DocumentController:
    def __init__(
        self,
        state: UIState,
        dialogs: DialogPort,
        db_operations: DbOperationsPort,
        presenter: DocumentResultPort,
        *,
        list_files: ListWorkspaceFiles = list_workspace_files,
        write_session_tabs: WriteSessionTabs = save_session_tabs,
    ) -> None:
        self.state = state
        self.dialogs = dialogs
        self.db_operations = db_operations
        self.presenter = presenter
        self.list_files = list_files
        self.write_session_tabs = write_session_tabs

    def restore_session_tabs(self) -> None:
        restored: list[FileTab] = []
        restored_by_source: dict[str, int] = {}
        restored_active: int | None = None
        for saved_index, saved_tab in enumerate(self.state.config.session_tabs):
            try:
                path = Path(saved_tab.path).expanduser().resolve()
                if not path.is_file():
                    continue
                source_key = file_source_key(path)
                duplicate_index = restored_by_source.get(source_key)
                if duplicate_index is not None:
                    if saved_index == self.state.config.active_session_tab:
                        restored_active = duplicate_index
                    continue
                buffer = Buffer()
                buffer.load(path, record_undo=False)
            except (OSError, RuntimeError, TypeError, UnicodeError, ValueError):
                continue
            if (
                isinstance(saved_tab.row, int)
                and not isinstance(saved_tab.row, bool)
                and isinstance(saved_tab.col, int)
                and not isinstance(saved_tab.col, bool)
                and 0 <= saved_tab.row < len(buffer.lines)
                and 0 <= saved_tab.col <= len(buffer.lines[saved_tab.row])
            ):
                buffer.row = saved_tab.row
                buffer.col = saved_tab.col
            restored_index = len(restored)
            restored_by_source[source_key] = restored_index
            restored.append(FileTab(buffer=buffer, source_key=source_key))
            if saved_index == self.state.config.active_session_tab:
                restored_active = restored_index
        if not restored:
            return
        self.state.tabs = restored
        self.state.active_tab_idx = restored_active if restored_active is not None else 0
        self.state.tab_scroll = 0
        self.state.focus = FOCUS_EDITOR

    def refresh_workspace_file_list(self) -> None:
        self.state.files = self.list_files(self.state.config)
        self.state.status = "File list refreshed"

    def persist_session_tabs(self) -> bool:
        saved_tabs: list[SessionTab] = []
        active_tab = 0
        for tab_index, tab in enumerate(self.state.tabs):
            path = tab.buffer.path
            if path is None:
                continue
            if tab_index == self.state.active_tab_idx:
                active_tab = len(saved_tabs)
            saved_tabs.append(SessionTab(path=path, row=tab.buffer.row, col=tab.buffer.col))
        try:
            self.write_session_tabs(self.state.config, saved_tabs, active_tab)
        except (configparser.Error, OSError, RuntimeError, UnicodeError):
            return False
        return True

    def save_buffer(self) -> bool:
        buffer = self.state.buffer
        try:
            if buffer.path is None:
                target = self._prompt_save_as_target(str(self.default_buffer_path()))
            else:
                target = self._external_change_save_target(buffer)
            if target is None:
                return False
            saved_target = self._save_with_conflict_resolution(buffer, target)
            if saved_target is None:
                return False
            self.state.active_tab.source_key = saved_target.source_key
            self._refresh_files_after_write(f"Saved {saved_target.path}")
            return True
        except Exception as exc:
            self.state.status = "Save failed"
            self.presenter.set_results(["ERROR saving file:", *wrap_error(exc)])
            return False

    def prompt_save_as_path(
        self,
        default: str,
        *,
        current_path: Path | None = None,
    ) -> Path | None:
        target = self._prompt_save_as_target(
            default,
            current_path=current_path,
        )
        return target.path if target is not None else None

    def _prompt_save_as_target(
        self,
        default: str,
        *,
        current_path: Path | None = None,
    ) -> _SaveTarget | None:
        name = self.dialogs.prompt("Save as", default)
        if not name:
            self.state.status = "Save cancelled"
            return None
        path = Path(name).expanduser()
        source_key = file_source_key(path)
        existing = self.find_tab_by_source_key(source_key)
        if existing is not None and existing != self.state.active_tab_idx:
            self.state.status = "Save failed: file is already open in another tab"
            return None
        return self._confirm_save_target(path, current_path, source_key)

    def external_change_save_path(self, buffer: Buffer) -> Path | None:
        target = self._external_change_save_target(buffer)
        return target.path if target is not None else None

    def _external_change_save_target(self, buffer: Buffer) -> _SaveTarget | None:
        path = buffer.path
        if path is None:
            return None
        source_key = file_source_key(path)
        version = read_file_version(path)
        change = buffer.external_file_change(version)
        if change is None:
            return _SaveTarget(path, source_key, version)
        description = "changed" if change == "modified" else "was deleted"
        answer = self.dialogs.prompt(
            f"File {description} on disk. overwrite/save-as/cancel? o/s/c",
            "",
        )
        if answer and answer.lower().startswith("o"):
            return _SaveTarget(path, source_key, version)
        if answer and answer.lower().startswith("s"):
            return self._prompt_save_as_target(str(path))
        self.state.status = "Save cancelled"
        return None

    def default_buffer_path(self) -> Path:
        default_dir = (
            self.state.config.plsql_dir if looks_like_plsql(self.state.buffer.text()) else self.state.config.sql_dir
        )
        return default_dir / "scratch.sql"

    def confirm_file_overwrite(self, path: Path, current_path: Path | None) -> bool:
        source_key = file_source_key(path)
        return self._confirm_save_target(path, current_path, source_key) is not None

    def rename_current_buffer(self) -> bool:
        buffer = self.state.buffer
        default = str(buffer.path if buffer.path is not None else self.default_buffer_path())
        name = self.dialogs.prompt("Rename as", default)
        if not name:
            self.state.status = "Rename cancelled"
            return False
        path = Path(name).expanduser()
        source_key = file_source_key(path)
        existing = self.find_tab_by_source_key(source_key)
        if existing is not None and existing != self.state.active_tab_idx:
            self.state.status = "Rename failed: file is already open in another tab"
            return False
        if buffer.path is not None and source_key == file_source_key(buffer.path):
            try:
                target = self._external_change_save_target(buffer)
            except Exception as exc:
                self.state.status = "Rename failed"
                self.presenter.set_results(["ERROR renaming buffer:", *wrap_error(exc)])
                return False
            if target is None:
                return False
        else:
            try:
                target = self._confirm_save_target(path, buffer.path, source_key)
            except Exception as exc:
                self.state.status = "Rename failed"
                self.presenter.set_results(["ERROR renaming buffer:", *wrap_error(exc)])
                return False
            if target is None:
                return False

        old_path = buffer.path
        old_title = buffer.title
        old_dirty = buffer.dirty
        old_source_key = self.state.active_tab.source_key
        try:
            saved_target = self._save_with_conflict_resolution(buffer, target)
            if saved_target is None:
                return False
            self.state.active_tab.source_key = saved_target.source_key
            self._refresh_files_after_write(f"Renamed buffer to {saved_target.path}")
            return True
        except Exception as exc:
            buffer.path = old_path
            buffer.title = old_title
            buffer.dirty = old_dirty
            self.state.active_tab.source_key = old_source_key
            self.state.status = "Rename failed"
            self.presenter.set_results(["ERROR renaming buffer:", *wrap_error(exc)])
            return False

    def _confirm_save_target(
        self,
        path: Path,
        current_path: Path | None,
        source_key: str,
    ) -> _SaveTarget | None:
        version = read_file_version(path)
        same_as_current = current_path is not None and source_key == file_source_key(current_path)
        if not same_as_current and version.exists:
            answer = self.dialogs.prompt("Overwrite existing file? y/n", "")
            if not answer or not answer.lower().startswith("y"):
                self.state.status = "Overwrite cancelled"
                return None
        return _SaveTarget(path, source_key, version)

    def _save_with_conflict_resolution(
        self,
        buffer: Buffer,
        target: _SaveTarget,
    ) -> _SaveTarget | None:
        while True:
            try:
                buffer.save(
                    target.path,
                    expected_file_version=target.version,
                )
                return target
            except FileVersionConflictError as exc:
                replacement = self._resolve_file_version_conflict(exc)
                if replacement is None:
                    return None
                target = replacement

    def _resolve_file_version_conflict(
        self,
        conflict: FileVersionConflictError,
    ) -> _SaveTarget | None:
        if conflict.expected.exists and not conflict.actual.exists:
            description = "was deleted"
        elif not conflict.expected.exists and conflict.actual.exists:
            description = "was created"
        else:
            description = "changed"
        answer = self.dialogs.prompt(
            f"File {description} before save. overwrite/save-as/cancel? o/s/c",
            "",
        )
        if answer and answer.lower().startswith("o"):
            return _SaveTarget(
                conflict.path,
                file_source_key(conflict.path),
                conflict.actual,
            )
        if answer and answer.lower().startswith("s"):
            return self._prompt_save_as_target(str(conflict.path))
        self.state.status = "Save cancelled"
        return None

    def _refresh_files_after_write(self, success_status: str) -> None:
        try:
            self.state.files = self.list_files(self.state.config)
        except Exception as exc:
            self.state.status = f"{success_status} (warning: file list refresh failed: {short_error(exc)})"
            return
        self.state.status = success_status

    def open_file(self) -> None:
        self.state.files = self.list_files(self.state.config)
        if not self.state.files:
            self.state.status = "No workspace files"
            return
        choice = self.dialogs.pick("Open file", [str(path) for path in self.state.files])
        if choice is None:
            self.state.status = "Open cancelled"
            return
        try:
            path = self.state.files[choice]
            source_key = file_source_key(path)
            existing = self.find_tab_by_source_key(source_key)
            if existing is not None:
                self.switch_to_tab(existing, f"Switched to {path}")
                self.state.focus = FOCUS_EDITOR
                return
            buffer = Buffer()
            buffer.load(path, record_undo=False)
            self.new_tab(FileTab(buffer=buffer, source_key=source_key), f"Opened {path}")
        except Exception as exc:
            self.state.status = "Open failed"
            self.presenter.set_results(["ERROR opening file:", *wrap_error(exc)])

    def new_template(self) -> None:
        names = list(TEMPLATES)
        choice = self.dialogs.pick("Template", names)
        if choice is None:
            self.state.status = "Template cancelled"
            return
        name = names[choice]
        source_key = template_source_key(name)
        for idx, tab in enumerate(self.state.tabs):
            if tab.source_key == source_key and not tab.buffer.dirty:
                self.switch_to_tab(idx, f"Switched to {name} template")
                self.state.focus = FOCUS_EDITOR
                return
        buffer = Buffer()
        buffer.set_text(TEMPLATES[name], title=f"{name}.sql", dirty=False, record_undo=False)
        self.new_tab(FileTab(buffer=buffer, source_key=source_key), f"Inserted {name} template")

    def new_tab(self, tab: FileTab | None = None, status: str = "New tab") -> None:
        self.state.tabs.append(tab or FileTab())
        self.state.active_tab_idx = len(self.state.tabs) - 1
        self.state.focus = FOCUS_EDITOR
        self.state.status = status

    def switch_tab(self, delta: int) -> None:
        self.state.ensure_tab()
        if len(self.state.tabs) <= 1:
            self.state.status = "Only one tab"
            return
        self.switch_to_tab((self.state.active_tab_idx + delta) % len(self.state.tabs))

    def switch_to_tab(self, index: int, status: str | None = None) -> None:
        self.state.ensure_tab()
        self.state.active_tab_idx = clamp_tab_index(index, self.state.tabs)
        if self.state.focus == FOCUS_RESULTS and self.state.active_result is None and self.state.explain_result is None:
            self.state.focus = FOCUS_EDITOR
        title = tab_display_title(self.state.active_tab)
        self.state.status = status or (f"Tab {self.state.active_tab_idx + 1}/{len(self.state.tabs)}: {title}")

    def switch_to_visible_tab_number(self, number: int) -> None:
        if not 1 <= number <= 9:
            return
        target = self.state.tab_scroll + number - 1
        if target >= len(self.state.tabs):
            self.state.status = "No such visible tab"
            return
        self.switch_to_tab(target)

    def find_tab_by_source_key(self, source_key: str) -> int | None:
        for idx, tab in enumerate(self.state.tabs):
            if tab.source_key == source_key:
                return idx
        return None

    def close_active_tab(self) -> None:
        if self.db_operations.reject_if_active():
            return
        self.state.ensure_tab()
        idx = self.state.active_tab_idx
        tab = self.state.active_tab
        title = tab_display_title(tab)
        if tab.buffer.dirty and not self.confirm_dirty_tab(idx, "Close"):
            return
        self.presenter.close_tab_result_continuations(tab)
        self.state.tabs.pop(idx)
        if not self.state.tabs:
            self.state.tabs.append(FileTab())
            self.state.active_tab_idx = 0
            self.state.tab_scroll = 0
            self.state.focus = FOCUS_EDITOR
            self.state.status = "Closed tab; new empty tab"
            return
        self.state.active_tab_idx = min(idx, len(self.state.tabs) - 1)
        self.state.tab_scroll = min(self.state.tab_scroll, self.state.active_tab_idx)
        if self.state.focus == FOCUS_RESULTS and self.state.active_result is None and self.state.explain_result is None:
            self.state.focus = FOCUS_EDITOR
        self.state.status = f"Closed {title}"

    def confirm_dirty_tab(self, index: int, action: str) -> bool:
        self.switch_to_tab(index, status=None)
        title = tab_display_title(self.state.active_tab)
        answer = self.dialogs.prompt(f"Save changes to {title}? y/n/c", "")
        if answer is None or not answer or answer.lower().startswith("c"):
            self.state.status = f"{action} cancelled"
            return False
        if answer.lower().startswith("n"):
            return True
        if answer.lower().startswith("y"):
            if self.save_buffer():
                return True
            self.state.status = f"{action} cancelled"
            return False
        self.state.status = f"{action} cancelled"
        return False

    def confirm_quit(self) -> bool:
        if self.db_operations.active:
            self.state.status = "Quit unavailable while database operation is running"
            return False
        original_idx = self.state.active_tab_idx
        for idx, tab in enumerate(list(self.state.tabs)):
            if tab.buffer.dirty and not self.confirm_dirty_tab(idx, "Quit"):
                self.state.active_tab_idx = min(original_idx, len(self.state.tabs) - 1)
                return False
        self.state.active_tab_idx = min(original_idx, len(self.state.tabs) - 1)
        return True

    def ensure_active_tab_visible(self, width: int) -> None:
        self.state.ensure_tab()
        active = self.state.active_tab_idx
        if active < self.state.tab_scroll:
            self.state.tab_scroll = active
        while active not in [
            idx
            for idx, _, _ in visible_tab_labels(
                self.state.tabs,
                self.state.tab_scroll,
                width,
            )
        ]:
            if self.state.tab_scroll >= active:
                break
            self.state.tab_scroll += 1
