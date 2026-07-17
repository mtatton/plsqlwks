from __future__ import annotations

from dataclasses import dataclass

from .menu import filtered_tree_menu_indexes, tree_menu_search_text


@dataclass(frozen=True)
class CommandMenuItem:
    section: str
    title: str
    shortcut: str
    handler: str
    keywords: str = ""


COMMAND_MENU_ITEMS = (
    CommandMenuItem("Application", "Show help", "F1", "show_help", "keyboard shortcuts"),
    CommandMenuItem("Application", "Quit", "Ctrl-Q", "request_quit", "exit close"),
    CommandMenuItem("Layout", "Toggle DBMS_OUTPUT/results", "F6", "toggle_dbms_output_view", "output transcript"),
    CommandMenuItem(
        "Layout", "Cycle result pane layout", "F7", "toggle_result_pane_size", "split fullscreen editor grid"
    ),
    CommandMenuItem("Layout", "Toggle grid/row detail", "F8", "toggle_result_mode", "results detail"),
    CommandMenuItem("Layout", "Show/focus/hide schema browser", "F9", "toggle_browser", "objects tree"),
    CommandMenuItem("Database", "Choose transaction mode", "F12", "choose_transaction_mode", "autocommit manual"),
    CommandMenuItem(
        "Database",
        "Interrupt running database operation",
        "Ctrl-C while running",
        "interrupt_db_operation",
        "cancel stop query plsql",
    ),
    CommandMenuItem("Database", "Commit transaction", "Ctrl-Alt-C", "commit_or_insert_draft", "save changes"),
    CommandMenuItem("Database", "Rollback transaction", "Ctrl-Alt-R", "rollback_transaction", "undo database changes"),
    CommandMenuItem("Database", "Reconnect", "Ctrl+=", "reconnect_database", "connect"),
    CommandMenuItem("File", "Save buffer", "F2 / Ctrl-S", "save_buffer", "write"),
    CommandMenuItem("File", "Open file", "F3 / Ctrl-O", "open_file", "workspace"),
    CommandMenuItem("File", "New template", "F4", "new_template", "create"),
    CommandMenuItem("File", "Rename current buffer", "Alt-R", "rename_current_buffer", "save as"),
    CommandMenuItem("File", "New file tab", "Ctrl-T", "new_tab", "tab"),
    CommandMenuItem("File", "Close current file tab", "Ctrl-W", "close_active_tab", "tab"),
    CommandMenuItem("File", "Refresh workspace file list", "Ctrl-R", "refresh_workspace_file_list", "files"),
    CommandMenuItem(
        "Editor", "Execute selection/current statement", "F5 / Ctrl-Enter / Alt-X", "run_current_statement", "run sql"
    ),
    CommandMenuItem("Editor", "Execute selection/buffer script", "F11", "run_script", "run sql script"),
    CommandMenuItem("Editor", "Explain current statement", "Ctrl-E", "explain_current_statement", "plan"),
    CommandMenuItem(
        "Editor",
        "Generate SQL with columns",
        "Alt-G",
        "generate_sql_with_columns",
        "table view select insert update",
    ),
    CommandMenuItem(
        "Editor", "Refresh autocomplete cache", "Alt-+", "refresh_autocomplete_cache", "completion metadata"
    ),
    CommandMenuItem("Editor", "Autocomplete", "Shift-Tab", "autocomplete_editor", "complete"),
    CommandMenuItem("Editor", "Toggle line comment", "Ctrl-B", "toggle_current_line_comment", "comment uncomment"),
    CommandMenuItem("Editor", "Find text", "Ctrl-F", "prompt_search", "search"),
    CommandMenuItem("Editor", "Go to line", "Ctrl-G", "prompt_go_to_line", "jump"),
    CommandMenuItem("Editor", "Next search match", "Ctrl-N", "repeat_search_forward", "find search"),
    CommandMenuItem("Editor", "Previous search match", "Ctrl-P", "repeat_search_backward", "find search"),
    CommandMenuItem(
        "Editor",
        "Next execution diagnostic",
        "Command menu",
        "next_execution_diagnostic",
        "error warning compile location",
    ),
    CommandMenuItem(
        "Editor",
        "Previous execution diagnostic",
        "Command menu",
        "previous_execution_diagnostic",
        "error warning compile location",
    ),
    CommandMenuItem("Editor", "Uppercase selection", "Ctrl-U", "uppercase_selection", "case"),
    CommandMenuItem("Editor", "Lowercase selection", "Ctrl-L", "lowercase_selection", "case"),
    CommandMenuItem("Editor", "Copy selection", "Ctrl-C", "copy_selection", "clipboard"),
    CommandMenuItem("Editor", "Cut selection", "Ctrl-X", "cut_selection", "clipboard"),
    CommandMenuItem("Editor", "Paste clipboard", "Ctrl-V", "paste_clipboard", "clipboard"),
    CommandMenuItem("Editor", "Undo", "Ctrl-Z", "undo_buffer", "history"),
    CommandMenuItem("Editor", "Redo", "Ctrl-Y", "redo_buffer", "history"),
    CommandMenuItem("Results", "Focus result table", "Tab", "enter_results_focus", "grid"),
    CommandMenuItem(
        "Results",
        "Copy selected cell",
        "Ctrl-C",
        "copy_selected_result_cell",
        "cell clipboard",
    ),
    CommandMenuItem("Results", "View selected cell", "F10", "view_selected_result_cell", "cell"),
    CommandMenuItem("Results", "Start insert draft row", "Ins", "start_insert_draft_row", "row insert"),
    CommandMenuItem("Schema", "Refresh schema browser", "Ctrl-R", "refresh_browser", "objects metadata"),
)


def command_menu_search_text(command: CommandMenuItem) -> str:
    return tree_menu_search_text(command)


def filtered_command_indexes(commands: tuple[CommandMenuItem, ...], filter_text: str) -> list[int]:
    return filtered_tree_menu_indexes(commands, filter_text)


def command_menu_label(command: CommandMenuItem, section_width: int, shortcut_width: int) -> str:
    section = command.section.ljust(section_width)
    shortcut = command.shortcut.ljust(shortcut_width)
    return f"{section}  {command.title}  {shortcut}"
