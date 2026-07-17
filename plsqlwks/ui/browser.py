from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..db import SCHEMA_OBJECT_TYPES
from .constants import *
from .display import clip_text, display_width

if TYPE_CHECKING:
    from .state import FileTab

@dataclass(frozen=True)
class BrowserEntry:
    kind: str
    label: str
    object_type: str
    object_name: str = ""

def browser_panel_width(total_width: int) -> int:
    if total_width <= 40:
        return max(12, total_width // 3)
    return min(38, max(24, total_width // 4))


def flatten_browser_entries(
    objects: dict[str, list[str]],
    expanded: set[str],
    filter_text: str = "",
) -> list[BrowserEntry]:
    entries: list[BrowserEntry] = []
    query = filter_text.casefold()
    for object_type in SCHEMA_OBJECT_TYPES:
        names = sorted(objects.get(object_type, []))
        if query:
            names = [name for name in names if query in name.casefold()]
            if not names:
                continue
        label = BROWSER_GROUP_LABELS.get(object_type, object_type.title())
        entries.append(BrowserEntry(kind="group", label=f"{label} ({len(names)})", object_type=object_type))
        if query or object_type in expanded:
            entries.extend(
                BrowserEntry(kind="object", label=name, object_type=object_type, object_name=name)
                for name in names
            )
    return entries


def clamp_browser_row(row: int, entries: list[BrowserEntry]) -> int:
    if not entries:
        return 0
    return min(max(row, 0), len(entries) - 1)


def browser_entry_text(entry: BrowserEntry, expanded: set[str], filter_text: str = "") -> str:
    if entry.kind == "group":
        if filter_text:
            return entry.label
        marker = "[-]" if entry.object_type in expanded else "[+]"
        return f"{marker} {entry.label}"
    return f"    {entry.label}"


def file_source_key(path: Path) -> str:
    return f"file:{path.expanduser().resolve()}"


def template_source_key(name: str) -> str:
    return f"template:{name}"


def clamp_tab_index(index: int, tabs: list[FileTab]) -> int:
    if not tabs:
        return 0
    return min(max(index, 0), len(tabs) - 1)


def tab_display_title(tab: FileTab) -> str:
    buffer = tab.buffer
    if buffer.title:
        return buffer.title
    if buffer.path:
        return buffer.path.name
    return "untitled.sql"


def format_tab_label(tab: FileTab) -> str:
    title = tab_display_title(tab)
    dirty = "*" if tab.buffer.dirty else ""
    return f"{title}{dirty}"


def visible_tab_labels(tabs: list[FileTab], scroll: int, width: int) -> list[tuple[int, int, str]]:
    if width <= 0 or not tabs:
        return []
    scroll = clamp_tab_index(scroll, tabs)
    visible: list[tuple[int, int, str]] = []
    used = 0
    for idx in range(scroll, len(tabs)):
        if len(visible) >= 9:
            break
        number = len(visible) + 1
        raw_label = format_tab_label(tabs[idx])
        remaining = width - used
        min_width = display_width(f"[{number} ]")
        if remaining <= min_width and visible:
            break
        label_width = max(1, remaining - min_width)
        label = clip_text(raw_label, label_width)
        text_width = display_width(f"[{number} {label}]")
        if visible and used + text_width > width:
            break
        visible.append((idx, number, label))
        used += text_width + 1
    return visible


def schema_object_title(user: str, object_type: str, object_name: str) -> str:
    return f"schema://{user.upper()}/{object_type.upper()}/{object_name.upper()}.sql"
