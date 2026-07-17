from __future__ import annotations

from collections.abc import Callable, Mapping

from .commands import COMMAND_MENU_ITEMS
from .plugin_host import PluginHost


class CommandDispatcher:
    """Resolve stable command IDs without reaching through the application object."""

    def __init__(
        self,
        actions: Mapping[str, Callable[[], object]],
        plugin_host: PluginHost,
    ) -> None:
        self._actions = dict(actions)
        self._plugin_host = plugin_host
        expected = {item.handler for item in COMMAND_MENU_ITEMS}
        missing = expected - self._actions.keys()
        unexpected = self._actions.keys() - expected
        if missing or unexpected:
            details: list[str] = []
            if missing:
                details.append(f"missing: {', '.join(sorted(missing))}")
            if unexpected:
                details.append(f"unexpected: {', '.join(sorted(unexpected))}")
            raise ValueError(f"invalid built-in command mapping ({'; '.join(details)})")

    def execute(self, handler_id: str) -> bool:
        if self._plugin_host.execute(handler_id):
            return True
        action = self._actions.get(handler_id)
        if action is None:
            return False
        action()
        return True
