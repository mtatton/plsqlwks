"""Supported public imports for the versioned PLSQLWKS Plugin API.

Loader and curses-host implementations are intentionally excluded.  Installed
plugins should import only the names listed in :data:`__all__`.
"""

from .api import (
    PLUGIN_API_VERSION,
    PLUGIN_ENTRY_POINT_GROUP,
    Plugin,
    PluginCommand,
    PluginContext,
    PluginFactory,
    PluginHandler,
    ResultSnapshot,
)


__all__ = [
    "PLUGIN_API_VERSION",
    "PLUGIN_ENTRY_POINT_GROUP",
    "Plugin",
    "PluginCommand",
    "PluginContext",
    "PluginFactory",
    "PluginHandler",
    "ResultSnapshot",
]
