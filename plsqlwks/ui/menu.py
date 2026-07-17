from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class TreeMenuItem(Protocol):
    @property
    def section(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def shortcut(self) -> str: ...

    @property
    def keywords(self) -> str: ...


@dataclass(frozen=True)
class TreeMenuSection:
    name: str
    item_indexes: tuple[int, ...]


@dataclass(frozen=True)
class TreeMenuRow:
    kind: str
    section: str
    item_index: int | None = None
    expanded: bool = True
    visible_count: int = 0
    total_count: int = 0


def tree_menu_sections(items: Sequence[TreeMenuItem]) -> tuple[TreeMenuSection, ...]:
    order: list[str] = []
    indexes_by_section: dict[str, list[int]] = {}
    for idx, item in enumerate(items):
        if item.section not in indexes_by_section:
            order.append(item.section)
            indexes_by_section[item.section] = []
        indexes_by_section[item.section].append(idx)
    return tuple(TreeMenuSection(section, tuple(indexes_by_section[section])) for section in order)


def tree_menu_search_text(item: TreeMenuItem) -> str:
    return " ".join(
        part
        for part in (
            item.section,
            item.title,
            item.shortcut,
            item.keywords,
        )
        if part
    ).casefold()


def tree_menu_item_matches_filter(item: TreeMenuItem, terms: list[str]) -> bool:
    return all(term in tree_menu_search_text(item) for term in terms)


def filtered_tree_menu_indexes(items: Sequence[TreeMenuItem], filter_text: str) -> list[int]:
    terms = filter_text.casefold().split()
    if not terms:
        return list(range(len(items)))
    return [idx for idx, item in enumerate(items) if tree_menu_item_matches_filter(item, terms)]


def tree_menu_rows(
    items: Sequence[TreeMenuItem],
    filter_text: str,
    expanded_sections: set[str],
) -> list[TreeMenuRow]:
    terms = filter_text.casefold().split()
    filtering = bool(terms)
    rows: list[TreeMenuRow] = []
    for section in tree_menu_sections(items):
        visible_indexes = tuple(idx for idx in section.item_indexes if tree_menu_item_matches_filter(items[idx], terms))
        if filtering and not visible_indexes:
            continue
        expanded = filtering or section.name in expanded_sections
        rows.append(
            TreeMenuRow(
                "section",
                section.name,
                expanded=expanded,
                visible_count=len(visible_indexes),
                total_count=len(section.item_indexes),
            )
        )
        if expanded:
            rows.extend(
                TreeMenuRow(
                    "item",
                    section.name,
                    item_index=idx,
                    visible_count=len(visible_indexes),
                    total_count=len(section.item_indexes),
                )
                for idx in visible_indexes
            )
    return rows


def tree_menu_row_label(
    row: TreeMenuRow,
    items: Sequence[TreeMenuItem],
    shortcut_width: int,
) -> str:
    if row.item_index is None:
        marker = "[-]" if row.expanded else "[+]"
        count = (
            f"{row.visible_count}/{row.total_count}" if row.visible_count != row.total_count else str(row.total_count)
        )
        return f"{marker} {row.section} ({count})"

    item = items[row.item_index]
    shortcut = item.shortcut.ljust(shortcut_width)
    return f"    {item.title}  {shortcut}"


def first_tree_menu_row_index(rows: Sequence[TreeMenuRow]) -> int:
    for idx, row in enumerate(rows):
        if row.item_index is not None:
            return idx
    return 0


def tree_menu_section_row_index(rows: Sequence[TreeMenuRow], section: str) -> int | None:
    for idx, row in enumerate(rows):
        if row.item_index is None and row.section == section:
            return idx
    return None
