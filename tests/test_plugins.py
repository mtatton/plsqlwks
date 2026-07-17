from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

import plsqlwks.plugins as public_plugins
import plsqlwks.plugins._result_export as result_export_helpers
import plsqlwks.plugins.loader as loader_module
from plsqlwks.exporting import ExportCancelled
from plsqlwks.plugins import (
    PLUGIN_API_VERSION,
    PLUGIN_ENTRY_POINT_GROUP,
    Plugin,
    PluginCommand,
    PluginContext,
    PluginFactory,
    PluginHandler,
    ResultSnapshot,
)
from plsqlwks.plugins.csv_export import CsvExportOptions
from plsqlwks.plugins.loader import load_plugin_registry

pytestmark = pytest.mark.plugin


def _handler(context: PluginContext) -> None:
    context.set_status("done")


def _plugin(
    plugin_id: str,
    *,
    command_id: str = "run",
    api_version: int = PLUGIN_API_VERSION,
    handler: Any = _handler,
) -> Plugin:
    return Plugin(
        id=plugin_id,
        name=f"Plugin {plugin_id}",
        commands=(
            PluginCommand(
                id=command_id,
                section="Testing",
                title=f"Run {plugin_id}",
                handler=handler,
            ),
        ),
        api_version=api_version,
    )


class FakeEntryPoint:
    def __init__(self, name: str, value: str, loader: Callable[[], object]):
        self.name = name
        self.value = value
        self._loader = loader

    def load(self) -> object:
        return self._loader()


def _entry_point(name: str, plugin: Plugin, *, value: str | None = None) -> FakeEntryPoint:
    return FakeEntryPoint(name, value or f"example.{name}:create_plugin", lambda: lambda: plugin)


def test_public_plugin_api_exports_only_supported_names():
    expected = {
        "PLUGIN_API_VERSION",
        "PLUGIN_ENTRY_POINT_GROUP",
        "Plugin",
        "PluginCommand",
        "PluginContext",
        "PluginFactory",
        "PluginHandler",
        "ResultSnapshot",
    }

    assert set(public_plugins.__all__) == expected
    assert PLUGIN_API_VERSION == 1
    assert PLUGIN_ENTRY_POINT_GROUP == "plsqlwks.plugins"
    assert PluginFactory is not None
    assert PluginHandler is not None

    snapshot = ResultSnapshot("Result", ("ID",), (("1",),), False)
    assert snapshot.numeric_values == ()
    assert ResultSnapshot.__match_args__ == (
        "title",
        "columns",
        "rows",
        "has_more",
    )
    with pytest.raises(TypeError):
        ResultSnapshot("Result", ("ID",), (("1",),), False, ((1,),))  # type: ignore[misc]  # reason: verifies the private field rejects positional input
    with pytest.raises(FrozenInstanceError):
        snapshot.title = "Changed"  # type: ignore[misc]  # reason: verifies immutable result snapshots reject mutation


def test_shared_export_formatting_stops_between_cells_when_cancelled(monkeypatch):
    formatted: list[str] = []
    cancelled = False

    def format_value(value: str, *, null_value: str, date_format: str) -> str:
        nonlocal cancelled
        formatted.append(value)
        cancelled = True
        return value

    monkeypatch.setattr(result_export_helpers, "format_export_value", format_value)

    with pytest.raises(ExportCancelled):
        result_export_helpers.format_export_rows(
            (("first", "second"),),
            null_value="",
            date_format="",
            cancelled=lambda: cancelled,
        )

    assert formatted == ["first"]


def test_builtin_plugins_are_registered_csv_then_html_then_xlsx():
    registry = load_plugin_registry(entry_points=())

    assert [plugin.id for plugin in registry.plugins] == [
        "csv-export",
        "html-export",
        "xlsx-export",
    ]
    html_plugin = registry.plugins[1]
    assert html_plugin.name == "HTML result export"
    assert html_plugin.api_version == PLUGIN_API_VERSION == 1
    assert len(html_plugin.commands) == 1
    command = html_plugin.commands[0]
    assert command.id == "export-loaded-rows"
    assert command.section == "Results"
    assert command.title == "Export result to HTML"
    assert command.shortcut == ""
    assert "html" in command.keywords.split()
    xlsx_plugin = registry.plugins[2]
    assert xlsx_plugin.name == "XLSX result export"
    assert xlsx_plugin.api_version == PLUGIN_API_VERSION == 1
    assert len(xlsx_plugin.commands) == 1
    command = xlsx_plugin.commands[0]
    assert command.id == "export-loaded-rows"
    assert command.section == "Results"
    assert command.title == "Export result to XLSX"
    assert command.shortcut == ""
    assert "xlsx" in command.keywords.split()
    assert registry.warnings == ()


def test_builtin_export_coordinator_is_private_and_not_given_to_external_plugins():
    calls: list[tuple[str, object, object]] = []

    def host_export(format_id: str, context: object, options: object) -> None:
        calls.append((format_id, context, options))

    registry = load_plugin_registry(entry_points=(), host_export=host_export)
    context = object()

    for plugin in registry.plugins:
        plugin.commands[0].handler(context)  # type: ignore[arg-type]  # reason: verifies the host withholds its private PluginContext from external handlers

    assert [format_id for format_id, _context, _options in calls] == [
        "csv",
        "html",
        "xlsx",
    ]
    assert all(seen_context is context for _format, seen_context, _options in calls)
    assert PLUGIN_API_VERSION == 1


def test_external_entry_point_factories_load_in_deterministic_order():
    entry_points = (
        _entry_point("zeta", _plugin("zeta")),
        _entry_point("alpha", _plugin("alpha-b"), value="example.z:create_plugin"),
        _entry_point("alpha", _plugin("alpha-a"), value="example.a:create_plugin"),
    )

    registry = load_plugin_registry(entry_points=entry_points)

    assert [plugin.id for plugin in registry.plugins] == [
        "csv-export",
        "html-export",
        "xlsx-export",
        "alpha-a",
        "alpha-b",
        "zeta",
    ]


@pytest.mark.parametrize(
    ("disabled_option", "expected_ids"),
    (
        ("csv_export_enabled", ["html-export", "xlsx-export"]),
        ("html_export_enabled", ["csv-export", "xlsx-export"]),
        ("xlsx_export_enabled", ["csv-export", "html-export"]),
    ),
)
def test_builtin_export_plugins_can_be_disabled_independently(
    disabled_option,
    expected_ids,
):
    registry = load_plugin_registry(entry_points=(), **{disabled_option: False})

    assert [plugin.id for plugin in registry.plugins] == expected_ids
    assert registry.warnings == ()


def test_all_disabled_builtins_are_not_invoked_and_external_plugins_still_load(
    monkeypatch,
):
    def fail_factory(*args, **kwargs):
        pytest.fail("disabled built-in factory was invoked")

    monkeypatch.setattr(loader_module, "create_csv_export_plugin", fail_factory)
    monkeypatch.setattr(loader_module, "create_html_export_plugin", fail_factory)
    monkeypatch.setattr(loader_module, "create_xlsx_export_plugin", fail_factory)

    registry = load_plugin_registry(
        entry_points=(_entry_point("external", _plugin("external")),),
        csv_export_enabled=False,
        html_export_enabled=False,
        xlsx_export_enabled=False,
    )

    assert [plugin.id for plugin in registry.plugins] == ["external"]
    assert registry.warnings == ()


@pytest.mark.parametrize(
    ("plugin", "warning_text"),
    [
        (_plugin("future", api_version=2), "API version 2"),
        (_plugin("Invalid ID"), "plugin id 'Invalid ID' is invalid"),
        (_plugin("bad-handler", handler=None), "handler must be callable"),
    ],
)
def test_invalid_external_plugins_are_skipped_with_useful_warnings(plugin, warning_text):
    registry = load_plugin_registry(entry_points=(_entry_point("invalid", plugin),))

    assert [item.id for item in registry.plugins] == [
        "csv-export",
        "html-export",
        "xlsx-export",
    ]
    assert len(registry.warnings) == 1
    assert "External plugin 'invalid' skipped" in registry.warnings[0]
    assert warning_text in registry.warnings[0]
    assert "Traceback" not in registry.warnings[0]


def test_duplicate_plugin_id_is_skipped_without_removing_first_plugin():
    registry = load_plugin_registry(
        entry_points=(
            _entry_point("first", _plugin("duplicate")),
            _entry_point("second", _plugin("duplicate")),
        )
    )

    assert [plugin.id for plugin in registry.plugins] == [
        "csv-export",
        "html-export",
        "xlsx-export",
        "duplicate",
    ]
    assert len(registry.warnings) == 1
    assert "duplicate plugin id 'duplicate'" in registry.warnings[0]


def test_duplicate_command_id_rejects_plugin_transactionally():
    duplicate_commands = Plugin(
        id="duplicate-commands",
        name="Duplicate commands",
        commands=(
            PluginCommand("same", "Testing", "First", _handler),
            PluginCommand("same", "Testing", "Second", _handler),
        ),
    )
    valid_after = _plugin("valid-after")

    registry = load_plugin_registry(
        entry_points=(
            _entry_point("bad", duplicate_commands),
            _entry_point("valid", valid_after),
        )
    )

    assert [plugin.id for plugin in registry.plugins] == [
        "csv-export",
        "html-export",
        "xlsx-export",
        "valid-after",
    ]
    assert "duplicate command id 'same'" in registry.warnings[0]


def test_broken_external_plugin_does_not_block_valid_plugin():
    def fail_to_load() -> object:
        raise RuntimeError("cannot import package\nwithout optional component")

    registry = load_plugin_registry(
        entry_points=(
            FakeEntryPoint("broken", "broken:create_plugin", fail_to_load),
            _entry_point("valid", _plugin("valid")),
        )
    )

    assert [plugin.id for plugin in registry.plugins] == [
        "csv-export",
        "html-export",
        "xlsx-export",
        "valid",
    ]
    assert registry.warnings == (
        "External plugin 'broken' skipped: RuntimeError: cannot import package without optional component",
    )


def test_noncallable_external_entry_point_is_skipped():
    registry = load_plugin_registry(entry_points=(FakeEntryPoint("value", "example:value", lambda: object()),))

    assert [plugin.id for plugin in registry.plugins] == [
        "csv-export",
        "html-export",
        "xlsx-export",
    ]
    assert "entry point must resolve to a callable" in registry.warnings[0]


def test_invalid_builtin_plugin_fails_fast():
    def invalid_factory() -> Plugin:
        return _plugin("Invalid ID")

    with pytest.raises(ValueError, match="plugin id 'Invalid ID' is invalid"):
        load_plugin_registry(builtin_factories=(invalid_factory,), entry_points=())


def test_explicit_builtin_factories_ignore_default_builtin_options():
    def explicit_factory() -> Plugin:
        return _plugin("explicit")

    registry = load_plugin_registry(
        builtin_factories=(explicit_factory,),
        entry_points=(),
        csv_export_options=CsvExportOptions(separator=";"),
        csv_export_enabled=False,
        html_export_enabled=False,
        xlsx_export_enabled=False,
    )

    assert [plugin.id for plugin in registry.plugins] == ["explicit"]
