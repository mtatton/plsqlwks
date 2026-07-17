from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import pytest_collection_modifyitems


class CollectionHook:
    def __init__(self) -> None:
        self.deselected: list[object] = []

    def pytest_deselected(self, items: list[object]) -> None:
        self.deselected.extend(items)


class CollectionItem:
    def __init__(self, *keywords: str) -> None:
        self.keywords = dict.fromkeys(keywords, True)
        self.markers: list[object] = []

    def add_marker(self, marker: object) -> None:
        self.markers.append(marker)


def invoke_collection_hook(
    items: list[CollectionItem] | None = None,
    *,
    markexpr: str = "",
) -> CollectionHook:
    hook = CollectionHook()
    config = SimpleNamespace(option=SimpleNamespace(markexpr=markexpr), hook=hook)
    pytest_collection_modifyitems(config, items or [])
    return hook


def test_explicit_oracle_opt_in_fails_when_credentials_are_missing(monkeypatch) -> None:
    monkeypatch.setenv("PLSQLWKS_TEST_ORACLE", "1")
    for name in ("ORACLE_USER", "ORACLE_DSN", "ORACLE_PASSWORD_FILE"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(pytest.UsageError, match="credentials are missing"):
        invoke_collection_hook()


@pytest.mark.parametrize("password_kind", ["missing", "directory", "empty"])
def test_explicit_oracle_opt_in_rejects_invalid_password_file(
    monkeypatch,
    tmp_path: Path,
    password_kind: str,
) -> None:
    password_file = tmp_path / "orapass"
    if password_kind == "directory":
        password_file.mkdir()
    elif password_kind == "empty":
        password_file.touch()
    monkeypatch.setenv("PLSQLWKS_TEST_ORACLE", "1")
    monkeypatch.setenv("ORACLE_USER", "test-user")
    monkeypatch.setenv("ORACLE_DSN", "test-dsn")
    monkeypatch.setenv("ORACLE_PASSWORD_FILE", str(password_file))

    with pytest.raises(pytest.UsageError, match="nonempty regular file"):
        invoke_collection_hook()


def test_explicit_oracle_opt_in_accepts_nonempty_password_file(monkeypatch, tmp_path: Path) -> None:
    password_file = tmp_path / "orapass"
    password_file.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("PLSQLWKS_TEST_ORACLE", "1")
    monkeypatch.setenv("ORACLE_USER", "test-user")
    monkeypatch.setenv("ORACLE_DSN", "test-dsn")
    monkeypatch.setenv("ORACLE_PASSWORD_FILE", str(password_file))

    invoke_collection_hook()


def test_oracle_matrix_tests_are_deselected_without_matrix_flag(monkeypatch) -> None:
    monkeypatch.delenv("PLSQLWKS_TEST_ORACLE", raising=False)
    monkeypatch.delenv("PLSQLWKS_TEST_ORACLE_MATRIX", raising=False)
    ordinary = CollectionItem("oracle")
    matrix = CollectionItem("oracle", "oracle_matrix")
    items = [ordinary, matrix]

    hook = invoke_collection_hook(items, markexpr="oracle")

    assert items == [ordinary]
    assert hook.deselected == [matrix]
    assert ordinary.markers


def test_oracle_matrix_flag_cannot_authorize_tests_without_base_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("PLSQLWKS_TEST_ORACLE", raising=False)
    monkeypatch.setenv("PLSQLWKS_TEST_ORACLE_MATRIX", "1")

    with pytest.raises(pytest.UsageError, match="requires PLSQLWKS_TEST_ORACLE=1"):
        invoke_collection_hook()
