"""Discover and validate built-in and installed Plugin API implementations.

Built-ins load first and are treated as application programming: an invalid
built-in raises immediately.  External entry points load in deterministic name
order.  A discovery, import, factory, or validation failure skips only the
affected external plugin and becomes a concise startup warning.

This module is an internal registry implementation, not part of the public
``plsqlwks.plugins`` import surface.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import metadata
from typing import Any

from ._result_export import BuiltinExportHandler
from .api import (
    PLUGIN_API_VERSION,
    PLUGIN_ENTRY_POINT_GROUP,
    Plugin,
    PluginCommand,
    PluginFactory,
)
from .csv_export import CsvExportOptions
from .csv_export import create_plugin as create_csv_export_plugin
from .html_export import create_plugin as create_html_export_plugin
from .xlsx_export import create_plugin as create_xlsx_export_plugin

_PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")


@dataclass(frozen=True)
class PluginRegistry:
    """Validated plugins and non-fatal warnings collected during discovery."""

    plugins: tuple[Plugin, ...]
    warnings: tuple[str, ...]


def _installed_entry_points() -> tuple[metadata.EntryPoint, ...]:
    """Return installed plugin entry points in a Python 3.10-compatible order."""
    discovered: Any = metadata.entry_points()
    if hasattr(discovered, "select"):
        selected = discovered.select(group=PLUGIN_ENTRY_POINT_GROUP)
    else:  # pragma: no cover - retained for the older mapping-style API
        selected = discovered.get(PLUGIN_ENTRY_POINT_GROUP, ())
    return tuple(sorted(selected, key=lambda entry_point: (entry_point.name, entry_point.value)))


def _require_nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty")
    return value


def _validate_plugin(plugin: object) -> tuple[Plugin, tuple[tuple[str, str], ...]]:
    """Validate one factory result and return its effective command keys."""
    if not isinstance(plugin, Plugin):
        raise TypeError("factory must return Plugin")
    if plugin.api_version != PLUGIN_API_VERSION:
        raise ValueError(f"plugin {plugin.id!r} uses API version {plugin.api_version}; expected {PLUGIN_API_VERSION}")
    if not isinstance(plugin.id, str) or _PLUGIN_ID_PATTERN.fullmatch(plugin.id) is None:
        raise ValueError(f"plugin id {plugin.id!r} is invalid")
    _require_nonempty(plugin.name, "plugin name")
    if not isinstance(plugin.commands, tuple):
        raise TypeError("plugin commands must be a tuple")

    command_ids: set[str] = set()
    command_keys: list[tuple[str, str]] = []
    for command in plugin.commands:
        if not isinstance(command, PluginCommand):
            raise TypeError("plugin commands must contain PluginCommand values")
        command_id = _require_nonempty(command.id, "command id")
        if command_id in command_ids:
            raise ValueError(f"plugin {plugin.id!r} has duplicate command id {command_id!r}")
        command_ids.add(command_id)
        _require_nonempty(command.section, f"command {command_id!r} section")
        _require_nonempty(command.title, f"command {command_id!r} title")
        if not callable(command.handler):
            raise TypeError(f"command {command_id!r} handler must be callable")
        command_keys.append((plugin.id, command_id))
    return plugin, tuple(command_keys)


def _register_plugin(
    candidate: object,
    plugins: list[Plugin],
    plugin_ids: set[str],
    command_keys: set[tuple[str, str]],
) -> None:
    """Validate and append a plugin while enforcing registry-wide uniqueness."""
    plugin, candidate_keys = _validate_plugin(candidate)
    if plugin.id in plugin_ids:
        raise ValueError(f"duplicate plugin id {plugin.id!r}")
    duplicate_key = next((key for key in candidate_keys if key in command_keys), None)
    if duplicate_key is not None:
        raise ValueError(f"duplicate plugin command key {duplicate_key[0]!r}:{duplicate_key[1]!r}")

    plugins.append(plugin)
    plugin_ids.add(plugin.id)
    command_keys.update(candidate_keys)


def _entry_point_name(entry_point: object) -> str:
    name = getattr(entry_point, "name", "<unknown>")
    return name if isinstance(name, str) and name else "<unknown>"


def _concise_error(error: Exception) -> str:
    message = " ".join(str(error).split())
    if len(message) > 200:
        message = f"{message[:197]}..."
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def load_plugin_registry(
    *,
    builtin_factories: Iterable[PluginFactory] | None = None,
    entry_points: Iterable[metadata.EntryPoint] | None = None,
    csv_export_options: CsvExportOptions | None = None,
    csv_export_enabled: bool = True,
    html_export_enabled: bool = True,
    xlsx_export_enabled: bool = True,
    host_export: BuiltinExportHandler | None = None,
) -> PluginRegistry:
    """Load built-in plugins, then isolate failures from installed entry points.

    ``builtin_factories`` and ``entry_points`` are injectable to keep discovery
    and validation testable.  In normal use they default to the application
    built-ins and the ``plsqlwks.plugins`` entry-point group respectively.
    The exporter options configure only the default built-in factories.  They
    are deliberately ignored when callers supply their own built-in factories.
    """
    plugins: list[Plugin] = []
    warnings: list[str] = []
    plugin_ids: set[str] = set()
    command_keys: set[tuple[str, str]] = set()

    if builtin_factories is None:
        default_factories: list[PluginFactory] = []
        if csv_export_enabled:
            default_factories.append(
                lambda: create_csv_export_plugin(
                    csv_export_options,
                    host_export=host_export,
                )
            )
        if html_export_enabled:
            default_factories.append(lambda: create_html_export_plugin(host_export=host_export))
        if xlsx_export_enabled:
            default_factories.append(lambda: create_xlsx_export_plugin(host_export=host_export))
        factories = tuple(default_factories)
    else:
        factories = tuple(builtin_factories)
    for factory in factories:
        if not callable(factory):
            raise TypeError("built-in plugin factory must be callable")
        _register_plugin(factory(), plugins, plugin_ids, command_keys)

    try:
        external_entry_points = (
            _installed_entry_points()
            if entry_points is None
            else tuple(sorted(entry_points, key=lambda item: (item.name, item.value)))
        )
    except Exception as error:
        warnings.append(f"External plugin discovery failed: {_concise_error(error)}")
        return PluginRegistry(tuple(plugins), tuple(warnings))

    for entry_point in external_entry_points:
        try:
            factory = entry_point.load()
            if not callable(factory):
                raise TypeError("entry point must resolve to a callable")
            _register_plugin(factory(), plugins, plugin_ids, command_keys)
        except Exception as error:
            warnings.append(f"External plugin {_entry_point_name(entry_point)!r} skipped: {_concise_error(error)}")

    return PluginRegistry(tuple(plugins), tuple(warnings))
