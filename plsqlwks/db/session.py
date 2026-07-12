from __future__ import annotations

import oracledb
from typing import Any

from ..config import AppConfig
from .editing import EditingMixin
from .execution import ExecutionMixin
from .explain import ExplainMixin
from .metadata import MetadataMixin
from .transactions import TransactionMixin


class OracleWorkspace(
    TransactionMixin,
    ExecutionMixin,
    ExplainMixin,
    MetadataMixin,
    EditingMixin,
):
    def __init__(self, config: AppConfig):
        self.config = config
        self.connection: oracledb.Connection | None = None
        self.autocommit = config.autocommit
        self.read_only = config.read_only
        self.pending_rows_changed = 0
        self.pending_unknown_changes = False
        self._result_continuations: dict[str, Any] = {}

    def connect(self) -> None:
        from . import read_password

        password = read_password(self.config.password_file)
        self.close()
        connection = oracledb.connect(
            user=self.config.user,
            password=password,
            dsn=self.config.dsn,
        )
        self.connection = connection
        try:
            self.apply_autocommit()
            self.enable_dbms_output()
        except Exception:
            try:
                connection.close()
            except Exception:
                pass
            self.connection = None
            raise

    def close(self) -> None:
        connection = self.connection
        try:
            self.close_all_result_continuations()
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                self.connection = None
                self.clear_pending_transaction()

    def ensure_connected(self) -> oracledb.Connection:
        if self.connection is None:
            self.connect()
        assert self.connection is not None
        return self.connection

    def cancel_current_operation(self) -> bool:
        conn = self.connection
        if conn is None:
            return False
        cancel = getattr(conn, "cancel", None)
        if not callable(cancel):
            return False
        cancel()
        return True

    def set_autocommit(self, enabled: bool) -> None:
        if self.connection is not None and hasattr(self.connection, "autocommit"):
            self.connection.autocommit = enabled
        self.autocommit = enabled

    def set_read_only(self, enabled: bool) -> None:
        self.read_only = enabled

    def apply_autocommit(self) -> None:
        if self.connection is not None and hasattr(self.connection, "autocommit"):
            self.connection.autocommit = self.autocommit
