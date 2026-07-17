from __future__ import annotations

from collections.abc import Callable
from typing import Any

import plsqlwks.ui as ui
from plsqlwks.plugins.loader import PluginRegistry
from plsqlwks.ui.app import App as CursesApp
from plsqlwks.ui.application_controller import ApplicationController
from plsqlwks.ui.catalog import BrowserController, CatalogService
from plsqlwks.ui.command_dispatcher import CommandDispatcher
from plsqlwks.ui.db_operations import DatabaseOperations
from plsqlwks.ui.db_session import DatabaseSessionController
from plsqlwks.ui.dialogs import DialogService
from plsqlwks.ui.documents import DocumentController
from plsqlwks.ui.editor_controller import CompletionController, EditorController
from plsqlwks.ui.input_controller import InputController
from plsqlwks.ui.key_reader import KeyReader
from plsqlwks.ui.plugin_host import PluginHost
from plsqlwks.ui.query_controller import QueryController
from plsqlwks.ui.renderer import Renderer
from plsqlwks.ui.result_controller import ResultController
from plsqlwks.ui.result_presenter import ResultPresenter
from plsqlwks.ui.viewport import ViewportController


class _Screen:
    def getmaxyx(self) -> tuple[int, int]:
        return (24, 120)


_METHOD_OWNERS = {
    # Terminal and dialogs.
    "read_key": "key_reader",
    "prompt": "dialogs",
    "draw_prompt_line": "dialogs",
    "prompt_text_box": "dialogs",
    "pick": "dialogs",
    "pick_command_menu": "dialogs",
    "show_cell_viewer": "dialogs",
    "refresh_modal_background": "dialogs",
    # Database coordination.
    "reject_if_db_operation_active": "db_operations",
    "interrupt_db_operation": "db_operations",
    "poll_db_operation": "db_operations",
    "wait_for_db_operation": "db_operations",
    "shutdown_database_worker": "db_operations",
    "try_connect": "database",
    "reconnect_database": "database",
    "choose_transaction_mode": "database",
    "prompt_pending_transaction": "database",
    "set_transaction_mode": "database",
    "commit_transaction": "database",
    "rollback_transaction": "database",
    # Query execution and presentation.
    "bind_names_for_statements": "query",
    "remembered_bind_value": "query",
    "remember_bind_values": "query",
    "prompt_bind_values_for_statements": "query",
    "bind_values_for_statement": "query",
    "execute_statement_with_bind_values": "query",
    "explain_statement_with_bind_values": "query",
    "run_current_statement": "query",
    "selected_script": "query",
    "run_selected_script": "query",
    "explain_current_statement": "query",
    "run_script": "query",
    "navigate_execution_diagnostic": "query",
    "next_execution_diagnostic": "query",
    "previous_execution_diagnostic": "query",
    "apply_db_operation_result": "result_presenter",
    "finish_execution": "result_presenter",
    "apply_fetch_more_result": "result_presenter",
    "handle_fetch_more_error": "result_presenter",
    "refresh_result_summary_line": "result_presenter",
    "close_tab_result_continuations": "result_presenter",
    "close_all_result_continuations": "result_presenter",
    "release_result_continuation": "result_presenter",
    "invalidate_results_after_rollback": "result_presenter",
    "run_with_errors": "result_presenter",
    "handle_execution_error": "result_presenter",
    "render_results": "result_presenter",
    "show_explain_result": "result_presenter",
    "handle_explain_error": "result_presenter",
    "set_results": "result_presenter",
    "show_help": "result_presenter",
    # Documents, browser, and editor.
    "restore_session_tabs": "documents",
    "persist_session_tabs": "documents",
    "refresh_workspace_file_list": "documents",
    "save_buffer": "documents",
    "default_buffer_path": "documents",
    "confirm_file_overwrite": "documents",
    "rename_current_buffer": "documents",
    "open_file": "documents",
    "new_template": "documents",
    "new_tab": "documents",
    "switch_tab": "documents",
    "switch_to_tab": "documents",
    "switch_to_visible_tab_number": "documents",
    "find_tab_by_source_key": "documents",
    "close_active_tab": "documents",
    "confirm_dirty_tab": "documents",
    "confirm_quit": "documents",
    "ensure_active_tab_visible": "documents",
    "toggle_browser": "browser",
    "refresh_browser": "browser",
    "browser_entries": "browser",
    "set_browser_filter": "browser",
    "handle_browser_key": "browser",
    "active_browser_entry": "browser",
    "activate_browser_entry": "browser",
    "toggle_browser_group_at_cursor": "browser",
    "toggle_browser_group": "browser",
    "load_schema_object": "browser",
    "ensure_browser_selection_visible": "browser",
    "prompt_search": "editor",
    "repeat_search": "editor",
    "repeat_search_forward": "editor",
    "repeat_search_backward": "editor",
    "prompt_go_to_line": "editor",
    "move_to_search_match": "editor",
    "edit_key": "editor",
    "move_editor_cursor": "editor",
    "copy_selection": "editor",
    "cut_selection": "editor",
    "paste_clipboard": "editor",
    "toggle_current_line_comment": "editor",
    "transform_selection_case": "editor",
    "transform_selection_sql_code_case": "editor",
    "uppercase_selection": "editor",
    "lowercase_selection": "editor",
    "undo_buffer": "editor",
    "redo_buffer": "editor",
    "generate_sql_with_columns": "completion",
    "finish_generate_sql_with_columns": "completion",
    "capture_editor_context": "completion",
    "editor_context_is_current": "completion",
    "default_generate_sql_table": "completion",
    "autocomplete_editor": "completion",
    "load_completion_metadata_if_needed": "completion",
    "completion_candidates": "completion",
    "completion_schema_objects": "completion",
    "completion_columns": "completion",
    "refresh_autocomplete_cache": "completion",
    "apply_completion": "completion",
    # Results and layout.
    "enter_results_focus": "results",
    "leave_results_focus": "results",
    "toggle_result_mode": "results",
    "toggle_dbms_output_view": "results",
    "handle_results_key": "results",
    "active_insert_draft": "results",
    "selected_insert_draft": "results",
    "start_insert_draft_row": "results",
    "edit_insert_draft_cell": "results",
    "cancel_insert_draft_if_selected": "results",
    "remove_insert_draft": "results",
    "discard_insert_draft": "results",
    "commit_insert_draft_if_active": "results",
    "fetch_next_result_page_if_needed": "results",
    "handle_explain_results_key": "results",
    "move_result_selection": "results",
    "clamp_result_selection": "results",
    "apply_result_position": "results",
    "ensure_selected_row_visible": "results",
    "ensure_selected_column_visible": "results",
    "ensure_selected_detail_field_visible": "results",
    "update_result_status": "results",
    "update_explain_status": "results",
    "edit_selected_result_cell": "results",
    "prompt_cell_edit_value": "results",
    "copy_selected_result_cell": "results",
    "view_selected_result_cell": "results",
    "toggle_result_pane_size": "viewport",
    "set_result_pane_ratio": "viewport",
    "enter_result_grid_fullscreen": "viewport",
    "scroll_focused_window": "viewport",
    "current_pane_sizes": "viewport",
    "scroll_editor_window": "viewport",
    "scroll_browser_window": "viewport",
    "scroll_results_window": "viewport",
    "scroll_results_page": "viewport",
    "scroll_explain_window": "viewport",
    "scroll_result_grid_window": "viewport",
    "scroll_result_detail_window": "viewport",
    "scroll_text_results_window": "viewport",
    "scroll_dbms_output_window": "viewport",
    "update_line_scroll_status": "viewport",
    # Rendering and input dispatch.
    "init_colors": "renderer",
    "draw": "renderer",
    "draw_header": "renderer",
    "draw_tab_bar": "renderer",
    "draw_browser": "renderer",
    "draw_editor": "renderer",
    "draw_editor_line": "renderer",
    "draw_editor_bracket_overlays": "renderer",
    "syntax_attr": "renderer",
    "draw_results": "renderer",
    "draw_dbms_output": "renderer",
    "draw_text_results": "renderer",
    "draw_help_results": "renderer",
    "draw_help_line": "renderer",
    "help_attr": "renderer",
    "draw_explain_plan": "renderer",
    "draw_explain_plan_line": "renderer",
    "explain_plan_attr": "renderer",
    "configured_explain_plan_attr": "renderer",
    "draw_result_grid": "renderer",
    "draw_result_detail": "renderer",
    "draw_table_line": "renderer",
    "draw_table_separator": "renderer",
    "draw_status": "renderer",
    "addstr": "renderer",
    "move_cursor": "renderer",
    "show_cursor": "renderer",
    "open_commands_menu": "input",
    "focused_results_are_scrollable": "input",
    "handle_key": "input",
    "request_quit": "application",
    "commit_or_insert_draft": "application",
}

_RENAMED_METHODS = {
    "reject_if_db_operation_active": "reject_if_active",
    "interrupt_db_operation": "interrupt",
    "poll_db_operation": "poll",
    "wait_for_db_operation": "wait",
    "shutdown_database_worker": "shutdown",
}


class ServiceHarness:
    """Test-only composition of the real UI services around fakes."""

    def _wire(self) -> None:
        if self.__dict__.get("_wired") or self.__dict__.get("_wiring"):
            return
        object.__setattr__(self, "_wiring", True)
        screen = self.__dict__.get("screen") or _Screen()
        state = self.__dict__.get("state")
        if state is None:
            raise RuntimeError("set harness.state before using UI services")

        key_reader = KeyReader(screen)
        dialogs = DialogService(screen, state, key_reader)
        db_operations = DatabaseOperations(state)
        result_presenter = ResultPresenter(
            state,
            db_operations,
            screen_width=lambda: screen.getmaxyx()[1],
        )
        db_operations.set_result_handler(result_presenter.apply_db_operation_result)
        documents = DocumentController(
            state,
            dialogs,
            db_operations,
            result_presenter,
            list_files=lambda config: ui.list_workspace_files(config),
        )
        catalog = CatalogService(state, db_operations, result_presenter)
        browser = BrowserController(
            state,
            catalog,
            documents,
            db_operations,
            result_presenter,
        )
        editor = EditorController(
            state,
            dialogs,
            copy_to_clipboard=lambda text: ui.copy_to_system_clipboard(text),
            paste_from_clipboard=lambda text: ui.paste_from_clipboard(text),
        )
        completion = CompletionController(
            state,
            dialogs,
            db_operations,
            catalog,
            browser,
        )
        database = DatabaseSessionController(
            state,
            db_operations,
            dialogs,
            result_presenter,
        )
        query = QueryController(state, db_operations, dialogs, result_presenter)
        results = ResultController(
            state,
            dialogs,
            db_operations,
            result_presenter,
            copy_to_clipboard=lambda text: ui.copy_to_system_clipboard(text),
        )
        viewport = ViewportController(screen, state, results)
        renderer = Renderer(screen, state, documents, browser, results)
        renderer.draw_offset_x = self.__dict__.get("_draw_offset_x", 0)
        renderer.syntax_colors_enabled = self.__dict__.get(
            "_syntax_colors_enabled",
            False,
        )
        renderer.explain_color_kinds_enabled = self.__dict__.get(
            "_explain_color_kinds_enabled",
            set(),
        )
        application = ApplicationController(state, documents, database, results)

        for name, value in {
            "screen": screen,
            "key_reader": key_reader,
            "dialogs": dialogs,
            "db_operations": db_operations,
            "result_presenter": result_presenter,
            "documents": documents,
            "catalog": catalog,
            "browser": browser,
            "editor": editor,
            "completion": completion,
            "database": database,
            "query": query,
            "results": results,
            "viewport": viewport,
            "renderer": renderer,
            "application": application,
        }.items():
            object.__setattr__(self, name, value)
        application.running = self.__dict__.get("_running", True)

        plugin_host = PluginHost(
            PluginRegistry((), ()),
            lambda: CursesApp._create_plugin_context(self),
        )
        object.__setattr__(self, "_plugin_host", plugin_host)
        object.__setattr__(self, "command_menu_items", plugin_host.command_menu_items)
        dispatcher = CommandDispatcher(
            CursesApp._built_in_actions(self),
            plugin_host,
        )
        object.__setattr__(self, "dispatcher", dispatcher)
        object.__setattr__(
            self,
            "input",
            InputController(
                state,
                db_operations,
                application,
                dispatcher,
                dialogs,
                documents,
                viewport,
                browser,
                results,
                editor,
                plugin_host.command_menu_items,
            ),
        )
        object.__setattr__(self, "_plugin_startup_warnings", ())
        object.__setattr__(self, "_wired", True)
        object.__setattr__(self, "_wiring", False)

    def _service_method(self, name: str) -> Callable[..., Any]:
        self._wire()
        owner = _METHOD_OWNERS[name]
        method_name = _RENAMED_METHODS.get(name, name)
        return getattr(getattr(self, owner), method_name)

    def __getattr__(self, name: str) -> Any:
        if name in _METHOD_OWNERS:
            return self._service_method(name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _METHOD_OWNERS and "state" in self.__dict__:
            method = self._service_method(name)
            setattr(method.__self__, method.__name__, value)
            dispatcher = self.__dict__.get("dispatcher")
            if dispatcher is not None and name in dispatcher._actions:
                dispatcher._actions[name] = value
            return
        if name == "screen" and self.__dict__.get("_wired"):
            object.__setattr__(self, name, value)
            self.key_reader.screen = value
            self.dialogs.screen = value
            self.viewport.screen = value
            self.renderer.screen = value
            return
        object.__setattr__(self, name, value)

    @property
    def running(self) -> bool:
        if self.__dict__.get("_wired"):
            return self.application.running
        return self.__dict__.get("_running", True)

    @running.setter
    def running(self, value: bool) -> None:
        object.__setattr__(self, "_running", value)
        if self.__dict__.get("_wired"):
            self.application.running = value

    @property
    def draw_offset_x(self) -> int:
        self._wire()
        return self.renderer.draw_offset_x

    @draw_offset_x.setter
    def draw_offset_x(self, value: int) -> None:
        if self.__dict__.get("_wired"):
            self.renderer.draw_offset_x = value
        else:
            object.__setattr__(self, "_draw_offset_x", value)

    @property
    def syntax_colors_enabled(self) -> bool:
        self._wire()
        return self.renderer.syntax_colors_enabled

    @syntax_colors_enabled.setter
    def syntax_colors_enabled(self, value: bool) -> None:
        if self.__dict__.get("_wired"):
            self.renderer.syntax_colors_enabled = value
        else:
            object.__setattr__(self, "_syntax_colors_enabled", value)

    @property
    def explain_color_kinds_enabled(self) -> set[str]:
        self._wire()
        return self.renderer.explain_color_kinds_enabled

    @explain_color_kinds_enabled.setter
    def explain_color_kinds_enabled(self, value: set[str]) -> None:
        if self.__dict__.get("_wired"):
            self.renderer.explain_color_kinds_enabled = value
        else:
            object.__setattr__(self, "_explain_color_kinds_enabled", value)

    def db_operation_active(self) -> bool:
        self._wire()
        return self.db_operations.active

    def start_db_operation(self, *args: Any, **kwargs: Any) -> bool:
        self._wire()
        return self.db_operations.start(*args, **kwargs)

    def run(self) -> None:
        self._wire()
        CursesApp.run(self)


def _delegate(name: str) -> Callable[..., Any]:
    def delegated(self: ServiceHarness, *args: Any, **kwargs: Any) -> Any:
        return self._service_method(name)(*args, **kwargs)

    delegated.__name__ = name
    return delegated


for _name in _METHOD_OWNERS:
    if not hasattr(ServiceHarness, _name):
        setattr(ServiceHarness, _name, _delegate(_name))
