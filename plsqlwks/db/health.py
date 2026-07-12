from __future__ import annotations

from ..config import AppConfig


def workspace_health(config: AppConfig) -> list[str]:
    messages: list[str] = []
    if not config.password_file.exists():
        messages.append(f"Password file is missing: {config.password_file}")
    for path in (config.sql_dir, config.plsql_dir, config.results_dir):
        if not path.exists():
            messages.append(f"Will create: {path}")
    return messages
