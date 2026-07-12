from __future__ import annotations

from dataclasses import dataclass
import re

from .buffer import Buffer, is_word_char, position_to_text_index
from .browser import BrowserEntry
from .constants import COMPLETION_SCHEMA_OBJECT_TYPES, FOCUS_BROWSER, SQL_KEYWORDS
from .sql import (
    COMPLETION_KIND_ORDER,
    selected_browser_table_name,
    statement_table_references,
)

@dataclass(frozen=True)
class SearchMatch:
    start_row: int
    start_col: int
    end_row: int
    end_col: int
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class CompletionContext:
    row: int
    start_col: int
    end_col: int
    prefix: str
    qualifier: str | None = None
    statement: str = ""


@dataclass(frozen=True)
class CompletionCandidate:
    insert_text: str
    label: str
    kind: str
    source: str = ""

def find_search_matches(lines: list[str], query: str) -> list[SearchMatch]:
    if not query or "\n" in query:
        return []
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    matches: list[SearchMatch] = []
    line_offset = 0
    for row, line in enumerate(lines):
        for match in pattern.finditer(line):
            start_col, end_col = match.span()
            matches.append(
                SearchMatch(
                    start_row=row,
                    start_col=start_col,
                    end_row=row,
                    end_col=end_col,
                    start_offset=line_offset + start_col,
                    end_offset=line_offset + end_col,
                )
            )
        line_offset += len(line) + 1
    return matches


def search_match_index(matches: list[SearchMatch], start_offset: int, direction: int) -> tuple[int, bool] | None:
    if not matches:
        return None
    if direction >= 0:
        for idx, match in enumerate(matches):
            if match.start_offset >= start_offset:
                return idx, False
        return 0, True
    for idx in range(len(matches) - 1, -1, -1):
        if matches[idx].start_offset < start_offset:
            return idx, False
    return len(matches) - 1, True


def search_navigation_offset(buffer: Buffer, direction: int) -> int:
    selected = buffer.selection_range()
    if direction < 0 and selected is not None:
        return position_to_text_index(buffer.lines, selected[0][0], selected[0][1])
    return position_to_text_index(buffer.lines, buffer.row, buffer.col)


def select_search_match(buffer: Buffer, match: SearchMatch) -> None:
    buffer.row, buffer.col = buffer.clamp_position(match.end_row, match.end_col)
    buffer.selection_anchor = buffer.clamp_position(match.start_row, match.start_col)


def search_status(query: str, match_index: int, match_count: int, wrapped: bool) -> str:
    suffix = " (wrapped)" if wrapped else ""
    return f'Found "{query}" {match_index + 1}/{match_count}{suffix}'

def completion_context_for_buffer(buffer: Buffer, statement: str = "") -> CompletionContext:
    row, col = buffer.clamp_position(buffer.row, buffer.col)
    line = buffer.lines[row]
    start_col = col
    while start_col > 0 and is_word_char(line[start_col - 1]):
        start_col -= 1
    qualifier = None
    if start_col > 0 and line[start_col - 1] == ".":
        qualifier_end = start_col - 1
        qualifier_start = qualifier_end
        while qualifier_start > 0 and is_word_char(line[qualifier_start - 1]):
            qualifier_start -= 1
        if qualifier_start < qualifier_end:
            qualifier = line[qualifier_start:qualifier_end]
    return CompletionContext(
        row=row,
        start_col=start_col,
        end_col=col,
        prefix=line[start_col:col],
        qualifier=qualifier,
        statement=statement,
    )


def keyword_completion_text(keyword: str, prefix: str) -> str:
    if prefix.isupper():
        return keyword.upper()
    if prefix.islower():
        return keyword.lower()
    if prefix[:1].isupper() and prefix[1:].islower():
        return keyword.capitalize()
    return keyword.lower()


def completion_label(insert_text: str, kind: str, source: str = "") -> str:
    suffix = f" {source}" if source else ""
    return f"{insert_text} [{kind}{suffix}]"


def keyword_completion_candidates(prefix: str) -> list[CompletionCandidate]:
    if not prefix:
        return []
    lowered = prefix.lower()
    candidates: list[CompletionCandidate] = []
    for keyword in sorted(SQL_KEYWORDS):
        if keyword.startswith(lowered):
            insert_text = keyword_completion_text(keyword, prefix)
            candidates.append(CompletionCandidate(insert_text, completion_label(insert_text, "keyword"), "keyword"))
    return candidates


def object_completion_candidates(
    schema_objects: dict[str, list[str]],
    prefix: str,
) -> list[CompletionCandidate]:
    if not prefix:
        return []
    normalized_prefix = prefix.upper()
    candidates: list[CompletionCandidate] = []
    for object_type in COMPLETION_SCHEMA_OBJECT_TYPES:
        kind = object_type.lower()
        for object_name in schema_objects.get(object_type, []):
            text = str(object_name).upper()
            if text.startswith(normalized_prefix):
                candidates.append(CompletionCandidate(text, completion_label(text, kind), kind))
    return candidates


def column_completion_candidates(
    columns: list[str],
    prefix: str,
    source: str,
) -> list[CompletionCandidate]:
    normalized_prefix = prefix.upper()
    candidates: list[CompletionCandidate] = []
    for column in columns:
        text = str(column).upper()
        if text.startswith(normalized_prefix):
            candidates.append(CompletionCandidate(text, completion_label(text, "column", source), "column", source))
    return candidates

def ordered_reference_tables(references: dict[str, str]) -> list[str]:
    tables: list[str] = []
    for table_name in references.values():
        if table_name not in tables:
            tables.append(table_name)
    return tables


def resolve_completion_qualifier(
    qualifier: str,
    references: dict[str, str],
    schema_objects: dict[str, list[str]],
) -> str | None:
    normalized = qualifier.upper()
    if normalized in references:
        return references[normalized]
    for object_type in ("TABLE", "VIEW"):
        if normalized in {name.upper() for name in schema_objects.get(object_type, [])}:
            return normalized
    if normalized and all(is_word_char(ch) for ch in normalized):
        return normalized
    return None


def dedupe_completion_candidates(candidates: list[CompletionCandidate]) -> list[CompletionCandidate]:
    deduped: list[CompletionCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate.kind, candidate.insert_text.upper())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return sorted(
        deduped,
        key=lambda candidate: (
            COMPLETION_KIND_ORDER.get(candidate.kind, 99),
            candidate.insert_text.upper(),
            candidate.source.upper(),
        ),
    )
