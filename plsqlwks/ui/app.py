from __future__ import annotations

import argparse
import curses
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from ..config import AppConfig, load_config
from ..db import OracleWorkspace, workspace_health
from ..plugins.csv_export import CsvExportOptions
from ..plugins.loader import load_plugin_registry
from ..workspace import ensure_workspace, list_workspace_files
from .application_controller import ApplicationController
from .catalog import BrowserController, CatalogService
from .command_dispatcher import CommandDispatcher
from .db_operations import DatabaseOperations
from .db_session import DatabaseSessionController
from .db_worker import DatabaseWorker
from .dialogs import DialogService
from .documents import DocumentController
from .editor_controller import CompletionController, EditorController
from .input_controller import InputController
from .key_reader import KeyReader
from .keys import (
    configure_utf8_locale,
    disable_extended_keyboard_reporting,
    enable_extended_keyboard_reporting,
)
from .plugin_host import PluginHost, UIPluginContext, snapshot_result
from .query_controller import QueryController
from .renderer import Renderer
from .result_controller import ResultController
from .result_presenter import ResultPresenter
from .state import UIState
from .viewport import ViewportController


def main(argv: list[str] | None = None) -> None:
    configure_utf8_locale()
    args = parse_args(argv)
    stored_config = load_config(workspace=args.workspace)
    ensure_workspace(stored_config)
    config = replace(
        stored_config,
        autocommit=stored_config.autocommit if args.autocommit is None else args.autocommit,
        read_only=stored_config.read_only if args.read_only is None else args.read_only,
    )
    curses.wrapper(lambda screen: App(screen, config).run())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the plsqlwks terminal workspace.")
    parser.add_argument(
        "--workspace",
        type=Path,
        metavar="PATH",
        help="Use PATH as the workspace (overrides PLSQLWKS_WORKSPACE).",
    )
    transaction = parser.add_mutually_exclusive_group()
    transaction.add_argument(
        "--manual",
        dest="autocommit",
        action="store_false",
        help="Start in manual transaction mode.",
    )
    transaction.add_argument(
        "--autocommit",
        dest="autocommit",
        action="store_true",
        help="Start in autocommit transaction mode.",
    )
    access = parser.add_mutually_exclusive_group()
    access.add_argument(
        "--read-only",
        dest="read_only",
        action="store_true",
        help=(
            "Enable a client-side guardrail against statements that appear to write; "
            "this is not a security boundary."
        ),
    )
    access.add_argument(
        "--read-write",
        dest="read_only",
        action="store_false",
        help="Allow statements that can write to the database.",
    )
    parser.set_defaults(autocommit=None, read_only=None)
    return parser.parse_args(argv)


class App:
    """Compose the ncurses UI services and own the terminal event loop."""

    def __init__(self, screen: curses.window, config: AppConfig):
        self.screen = screen
        self.db_worker = DatabaseWorker(OracleWorkspace(config))
        self.state = UIState(config=config, db=self.db_worker.session_state)
        self.state.files = list_workspace_files(config)

        self.key_reader = KeyReader(screen)
        self.dialogs = DialogService(screen, self.state, self.key_reader)
        self.db_operations = DatabaseOperations(
            self.state,
            self.db_worker,
            worker_factory=self._create_replacement_database_worker,
        )
        self.result_presenter = ResultPresenter(
            self.state,
            self.db_operations,
            screen_width=lambda: self.screen.getmaxyx()[1],
        )
        self.db_operations.set_result_handler(
            self.result_presenter.apply_db_operation_result
        )
        self.documents = DocumentController(
            self.state,
            self.dialogs,
            self.db_operations,
            self.result_presenter,
        )
        self.catalog = CatalogService(
            self.state,
            self.db_operations,
            self.result_presenter,
        )
        self.browser = BrowserController(
            self.state,
            self.catalog,
            self.documents,
            self.db_operations,
            self.result_presenter,
        )
        self.editor = EditorController(self.state, self.dialogs)
        self.completion = CompletionController(
            self.state,
            self.dialogs,
            self.db_operations,
            self.catalog,
            self.browser,
        )
        self.database = DatabaseSessionController(
            self.state,
            self.db_operations,
            self.dialogs,
            self.result_presenter,
        )
        self.query = QueryController(
            self.state,
            self.db_operations,
            self.dialogs,
            self.result_presenter,
        )
        self.results = ResultController(
            self.state,
            self.dialogs,
            self.db_operations,
            self.result_presenter,
        )
        self.viewport = ViewportController(screen, self.state, self.results)
        self.renderer = Renderer(
            screen,
            self.state,
            self.documents,
            self.browser,
            self.results,
        )
        self.application = ApplicationController(
            self.state,
            self.documents,
            self.database,
            self.results,
        )

        csv_export_options = CsvExportOptions(
            separator=config.csv_export_separator,
            null_value=config.csv_export_null_value,
            date_format=config.csv_export_date_format,
            protect_formulas=config.csv_export_protect_formulas,
        )
        self._plugin_host = PluginHost(
            load_plugin_registry(
                csv_export_options=csv_export_options,
                csv_export_enabled=config.csv_export_enabled,
                html_export_enabled=config.html_export_enabled,
                xlsx_export_enabled=config.xlsx_export_enabled,
            ),
            self._create_plugin_context,
        )
        self.command_menu_items = self._plugin_host.command_menu_items
        self._plugin_startup_warnings = self._plugin_host.startup_warnings
        self.dispatcher = CommandDispatcher(
            self._built_in_actions(),
            self._plugin_host,
        )
        self.input = InputController(
            self.state,
            self.db_operations,
            self.application,
            self.dispatcher,
            self.dialogs,
            self.documents,
            self.viewport,
            self.browser,
            self.results,
            self.editor,
            self.command_menu_items,
        )

    def _create_replacement_database_worker(
        self,
        previous_session: object,
    ) -> DatabaseWorker:
        workspace = OracleWorkspace(self.state.config)
        workspace.autocommit = bool(
            getattr(previous_session, "autocommit", workspace.autocommit)
        )
        workspace.read_only = bool(
            getattr(previous_session, "read_only", workspace.read_only)
        )
        worker = DatabaseWorker(workspace)
        self.db_worker = worker
        return worker

    def _built_in_actions(self) -> dict[str, Callable[[], object]]:
        return {
            "show_help": self.result_presenter.show_help,
            "request_quit": self.application.request_quit,
            "toggle_dbms_output_view": self.results.toggle_dbms_output_view,
            "toggle_result_pane_size": self.viewport.toggle_result_pane_size,
            "toggle_result_mode": self.results.toggle_result_mode,
            "toggle_browser": self.browser.toggle_browser,
            "choose_transaction_mode": self.database.choose_transaction_mode,
            "interrupt_db_operation": self.db_operations.interrupt,
            "commit_or_insert_draft": self.application.commit_or_insert_draft,
            "rollback_transaction": self.database.rollback_transaction,
            "reconnect_database": self.database.reconnect_database,
            "save_buffer": self.documents.save_buffer,
            "open_file": self.documents.open_file,
            "new_template": self.documents.new_template,
            "rename_current_buffer": self.documents.rename_current_buffer,
            "new_tab": self.documents.new_tab,
            "close_active_tab": self.documents.close_active_tab,
            "refresh_workspace_file_list": self.documents.refresh_workspace_file_list,
            "run_current_statement": self.query.run_current_statement,
            "run_script": self.query.run_script,
            "explain_current_statement": self.query.explain_current_statement,
            "generate_sql_with_columns": self.completion.generate_sql_with_columns,
            "refresh_autocomplete_cache": self.completion.refresh_autocomplete_cache,
            "autocomplete_editor": self.completion.autocomplete_editor,
            "toggle_current_line_comment": self.editor.toggle_current_line_comment,
            "prompt_search": self.editor.prompt_search,
            "prompt_go_to_line": self.editor.prompt_go_to_line,
            "repeat_search_forward": self.editor.repeat_search_forward,
            "repeat_search_backward": self.editor.repeat_search_backward,
            "next_execution_diagnostic": self.query.next_execution_diagnostic,
            "previous_execution_diagnostic": self.query.previous_execution_diagnostic,
            "uppercase_selection": self.editor.uppercase_selection,
            "lowercase_selection": self.editor.lowercase_selection,
            "copy_selection": self.editor.copy_selection,
            "cut_selection": self.editor.cut_selection,
            "paste_clipboard": self.editor.paste_clipboard,
            "undo_buffer": self.editor.undo_buffer,
            "redo_buffer": self.editor.redo_buffer,
            "enter_results_focus": self.results.enter_results_focus,
            "copy_selected_result_cell": self.results.copy_selected_result_cell,
            "view_selected_result_cell": self.results.view_selected_result_cell,
            "start_insert_draft_row": self.results.start_insert_draft_row,
            "refresh_browser": self.browser.refresh_browser,
        }

    def _create_plugin_context(self) -> UIPluginContext:
        insert_draft = self.results.active_insert_draft() is not None
        result_snapshot = None if insert_draft else snapshot_result(self.state.active_result)
        return UIPluginContext(
            self.state.config.results_dir,
            result_snapshot=result_snapshot,
            insert_draft=insert_draft,
            prompt=lambda label, default, strip: self.dialogs.prompt_text_box(
                label,
                default,
                strip,
            ),
            set_status=lambda message: setattr(self.state, "status", message),
            set_results=lambda lines, clear_table: self.result_presenter.set_results(
                lines,
                clear_table=clear_table,
            ),
        )

    def run(self) -> None:
        self.renderer.show_cursor()
        self.screen.keypad(True)
        self.screen.leaveok(False)
        self.screen.timeout(200)
        try:
            curses.raw()
        except curses.error:
            pass
        try:
            curses.nonl()
        except curses.error:
            pass
        extended_keyboard_enabled = enable_extended_keyboard_reporting()
        try:
            self.renderer.init_colors()
            self.documents.restore_session_tabs()
            self.result_presenter.show_help(
                [
                    *self.state.config.startup_warnings,
                    *self._plugin_startup_warnings,
                    *workspace_health(self.state.config),
                ],
                focus_results=False,
            )
            self.database.try_connect()
            while self.application.running:
                self.db_operations.poll()
                self.renderer.draw()
                key = self.key_reader.read_key()
                if key != -1:
                    self.input.handle_key(key)
                self.db_operations.poll()
        finally:
            shutdown_timeout = 5.0 if self.db_operations.active else None
            try:
                try:
                    if self.db_operations.active:
                        try:
                            operation = self.state.db_operation
                            if operation is not None:
                                self.db_worker.cancel_current_operation(
                                    operation.handle.command_id
                                )
                        except Exception:
                            pass
                    self.db_operations.wait(timeout=shutdown_timeout)
                finally:
                    if extended_keyboard_enabled:
                        disable_extended_keyboard_reporting()
            finally:
                try:
                    try:
                        curses.nl()
                    except curses.error:
                        pass
                    try:
                        curses.noraw()
                    except curses.error:
                        pass
                finally:
                    try:
                        self.result_presenter.close_all_result_continuations()
                    finally:
                        self.db_operations.shutdown(timeout=shutdown_timeout)
