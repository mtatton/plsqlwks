from __future__ import annotations

import re
from dataclasses import dataclass

from ..db.identifiers import quote_identifier
from .buffer import Buffer, is_word_char, position_to_text_index
from .constants import COMPLETION_SCHEMA_OBJECT_TYPES, SQL_KEYWORDS
from .sql import COMPLETION_KIND_ORDER, sql_identifier


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
    prefix_quoted: bool = False
    qualifier_quoted: bool = False


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


@dataclass(frozen=True)
class _LineIdentifier:
    start: int
    end: int
    name: str
    quoted: bool


def _line_identifiers(line: str, end: int) -> list[_LineIdentifier]:
    tokens: list[_LineIdentifier] = []
    idx = 0
    while idx < end:
        if line[idx] == '"':
            start = idx
            chars: list[str] = []
            idx += 1
            while idx < end:
                if line[idx] != '"':
                    chars.append(line[idx])
                    idx += 1
                    continue
                if idx + 1 < end and line[idx + 1] == '"':
                    chars.append('"')
                    idx += 2
                    continue
                idx += 1
                break
            tokens.append(_LineIdentifier(start, idx, "".join(chars), True))
            continue
        if line[idx].isalpha():
            start = idx
            idx += 1
            while idx < end and is_word_char(line[idx]):
                idx += 1
            tokens.append(_LineIdentifier(start, idx, line[start:idx], False))
            continue
        idx += 1
    return tokens


def completion_context_for_buffer(buffer: Buffer, statement: str = "") -> CompletionContext:
    row, col = buffer.clamp_position(buffer.row, buffer.col)
    line = buffer.lines[row]
    tokens = _line_identifiers(line, col)
    current = tokens[-1] if tokens and tokens[-1].end == col else None
    start_col = current.start if current is not None else col
    prefix = current.name if current is not None else ""
    prefix_quoted = current.quoted if current is not None else False
    qualifier = None
    qualifier_quoted = False
    dot_col = start_col - 1
    while dot_col >= 0 and line[dot_col].isspace():
        dot_col -= 1
    if dot_col >= 0 and line[dot_col] == ".":
        qualifier_end = dot_col
        while qualifier_end > 0 and line[qualifier_end - 1].isspace():
            qualifier_end -= 1
        qualifier_token = next(
            (token for token in reversed(tokens) if token.end == qualifier_end),
            None,
        )
        if qualifier_token is not None:
            qualifier = qualifier_token.name
            qualifier_quoted = qualifier_token.quoted
    return CompletionContext(
        row=row,
        start_col=start_col,
        end_col=col,
        prefix=prefix,
        qualifier=qualifier,
        statement=statement,
        prefix_quoted=prefix_quoted,
        qualifier_quoted=qualifier_quoted,
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
    quoted: bool = False,
) -> list[CompletionCandidate]:
    if not prefix:
        return []
    normalized_prefix = prefix.casefold()
    candidates: list[CompletionCandidate] = []
    for object_type in COMPLETION_SCHEMA_OBJECT_TYPES:
        kind = object_type.lower()
        for object_name in schema_objects.get(object_type, []):
            exact_name = str(object_name)
            if exact_name.casefold().startswith(normalized_prefix):
                text = quote_identifier(exact_name) if quoted else sql_identifier(exact_name)
                candidates.append(CompletionCandidate(text, completion_label(text, kind), kind))
    return candidates


def column_completion_candidates(
    columns: list[str],
    prefix: str,
    source: str,
    quoted: bool = False,
) -> list[CompletionCandidate]:
    normalized_prefix = prefix.casefold()
    candidates: list[CompletionCandidate] = []
    for column in columns:
        exact_name = str(column)
        if exact_name.casefold().startswith(normalized_prefix):
            text = quote_identifier(exact_name) if quoted else sql_identifier(exact_name)
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
    *,
    quoted: bool = False,
) -> str | None:
    normalized = qualifier if quoted else qualifier.upper()
    if normalized in references:
        return references[normalized]
    for object_type in ("TABLE", "VIEW"):
        names = [str(name) for name in schema_objects.get(object_type, [])]
        if normalized in names:
            return normalized
    if quoted:
        return None
    if normalized and all(is_word_char(ch) for ch in qualifier):
        return normalized
    return None


def dedupe_completion_candidates(candidates: list[CompletionCandidate]) -> list[CompletionCandidate]:
    deduped: list[CompletionCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate.kind, candidate.insert_text)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return sorted(
        deduped,
        key=lambda candidate: COMPLETION_KIND_ORDER.get(candidate.kind, 99),
    )
