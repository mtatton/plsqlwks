from __future__ import annotations

import argparse
import curses
from pathlib import Path

from ..config import AppConfig, load_config
from ..db import OracleWorkspace, workspace_health
from ..workspace import ensure_workspace, list_workspace_files
from .keys import configure_utf8_locale, disable_extended_keyboard_reporting, enable_extended_keyboard_reporting
from .db_worker import DatabaseWorker
from .state import UIState
from .app_render import AppRenderMixin
from .app_db import AppDbMixin
from .app_input import AppInputMixin
from .app_tabs_browser import AppTabsBrowserMixin
from .app_results import AppResultsMixin
from .app_editor import AppEditorMixin
from .app_files import AppFilesMixin

def main(argv: list[str] | None = None) -> None:
    configure_utf8_locale()
    args = parse_args(argv)
    config = load_config(workspace=args.workspace, autocommit=args.autocommit, read_only=args.read_only)
    ensure_workspace(config)
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


class App(
    AppRenderMixin,
    AppDbMixin,
    AppInputMixin,
    AppTabsBrowserMixin,
    AppResultsMixin,
    AppEditorMixin,
    AppFilesMixin,
):
    def __init__(self, screen: curses.window, config: AppConfig):
        self.screen = screen
        self.db_worker = DatabaseWorker(OracleWorkspace(config))
        self.state = UIState(config=config, db=self.db_worker.session_state)
        self.state.files = list_workspace_files(config)
        self.running = True
        self.message_lines: list[str] = []
        self.draw_offset_x = 0
        self.syntax_colors_enabled = False
        self.explain_color_kinds_enabled: set[str] = set()

    def run(self) -> None:
        self.show_cursor()
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
            self.init_colors()
            self.restore_session_tabs()
            self.show_help(
                [*self.state.config.startup_warnings, *workspace_health(self.state.config)],
                focus_results=False,
            )
            self.try_connect()
            while self.running:
                self.poll_db_operation()
                self.draw()
                key = self.read_key()
                if key != -1:
                    self.handle_key(key)
                self.poll_db_operation()
        finally:
            shutdown_timeout = 5.0 if self.state.db_operation is not None else None
            try:
                try:
                    if self.state.db_operation is not None:
                        try:
                            self.db_worker.cancel_current_operation(
                                self.state.db_operation.handle.command_id
                            )
                        except Exception:
                            pass
                    if shutdown_timeout is None:
                        self.wait_for_db_operation()
                    else:
                        self.wait_for_db_operation(timeout=shutdown_timeout)
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
                        self.close_all_result_continuations()
                    finally:
                        if shutdown_timeout is None:
                            self.shutdown_database_worker()
                        else:
                            self.shutdown_database_worker(timeout=shutdown_timeout)
