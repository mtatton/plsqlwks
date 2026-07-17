from __future__ import annotations

from dataclasses import dataclass
import re
import traceback

from ..db import OracleCompilationError, OracleExecutionError
from .buffer import Buffer
from .display import wrap_display_text

@dataclass(frozen=True)
class ErrorLocation:
    line: int
    column: int = 1


@dataclass(frozen=True)
class ParsedErrorLocation:
    location: ErrorLocation
    priority: int
    unit: str | None = None
    external: bool = False


@dataclass(frozen=True)
class PlsqlDiagnostic:
    unit: str | None
    line: int
    message: str
    source: str

LINE_COLUMN_RE = re.compile(r"\bline\s+(\d+)\s*,\s*column\s+(\d+)", re.IGNORECASE)
ORA_06512_LINE_RE = re.compile(r"\bORA-06512:\s+at\s+line\s+(\d+)\b", re.IGNORECASE)
ORA_06512_NAMED_LINE_RE = re.compile(
    r'\bORA-06512:\s+at\s+"(?P<unit>[^"]+)"\s*,\s*line\s+(?P<line>\d+)\b',
    re.IGNORECASE,
)
SQLERRM_DIAGNOSTIC_RE = re.compile(
    r"\bError raised in:\s*(?P<unit>.*?)\s+at line\s+(?P<line>\d+)\s*-\s*(?P<message>.*)",
    re.IGNORECASE,
)
ORACLE_CANCEL_RE = re.compile(r"\bORA-01013\b|user requested cancel", re.IGNORECASE)
ERROR_LOCATION_PRIORITY_EXACT = 0
ERROR_LOCATION_PRIORITY_COMPILE_ERROR = -1
ERROR_LOCATION_PRIORITY_OFFSET = 1
ERROR_LOCATION_PRIORITY_FALLBACK = 2


def parse_error_locations(text: str) -> list[ErrorLocation]:
    return [candidate.location for candidate in parse_error_location_candidates(text)]


def parse_error_location_candidates(text: str) -> list[ParsedErrorLocation]:
    candidates: list[ParsedErrorLocation] = []
    best_priority: dict[tuple[int, int], int] = {}

    def add(
        location: ErrorLocation,
        priority: int,
        *,
        unit: str | None = None,
        external: bool = False,
    ) -> None:
        key = (location.line, location.column)
        current_priority = best_priority.get(key)
        if current_priority is not None and current_priority <= priority:
            return
        best_priority[key] = priority
        candidates[:] = [candidate for candidate in candidates if candidate.location != location]
        candidates.append(
            ParsedErrorLocation(
                location,
                priority,
                unit=unit,
                external=external,
            )
        )

    for match in LINE_COLUMN_RE.finditer(text):
        add(ErrorLocation(line=int(match.group(1)), column=int(match.group(2))), ERROR_LOCATION_PRIORITY_EXACT)
    for diagnostic in parse_plsql_diagnostics(text):
        add(
            ErrorLocation(line=diagnostic.line, column=1),
            ERROR_LOCATION_PRIORITY_FALLBACK,
            unit=diagnostic.unit,
            external=not is_local_plsql_unit(diagnostic.unit),
        )
    for match in ORA_06512_NAMED_LINE_RE.finditer(text):
        add(
            ErrorLocation(line=int(match.group("line")), column=1),
            ERROR_LOCATION_PRIORITY_FALLBACK,
            unit=match.group("unit"),
            external=True,
        )
    for match in ORA_06512_LINE_RE.finditer(text):
        add(ErrorLocation(line=int(match.group(1)), column=1), ERROR_LOCATION_PRIORITY_FALLBACK)
    return candidates


def is_local_plsql_unit(unit: str | None) -> bool:
    if unit is None:
        return True
    normalized = unit.strip().casefold().replace(" ", "")
    return normalized in {"", "<anonymous>", "anonymous", "anonymousblock"}


def parse_plsql_diagnostics(text: str) -> list[PlsqlDiagnostic]:
    diagnostics: list[PlsqlDiagnostic] = []
    seen: set[tuple[str | None, int, str]] = set()
    for match in SQLERRM_DIAGNOSTIC_RE.finditer(text):
        unit = match.group("unit").strip() or None
        line = int(match.group("line"))
        message = match.group("message").strip()
        key = (unit, line, message)
        if key in seen:
            continue
        seen.add(key)
        diagnostics.append(PlsqlDiagnostic(unit=unit, line=line, message=message, source=match.group(0)))
    return diagnostics


def execution_error_texts(exc: Exception) -> list[str]:
    texts = [exception_text(exc)]
    if isinstance(exc, OracleExecutionError):
        texts.extend(exc.dbms_output)
    return [text for text in texts if text]


def execution_error_diagnostics(exc: Exception) -> list[PlsqlDiagnostic]:
    diagnostics: list[PlsqlDiagnostic] = []
    seen: set[tuple[str | None, int, str]] = set()
    for text in execution_error_texts(exc):
        for diagnostic in parse_plsql_diagnostics(text):
            key = (diagnostic.unit, diagnostic.line, diagnostic.message)
            if key in seen:
                continue
            seen.add(key)
            diagnostics.append(diagnostic)
    return diagnostics


def is_execution_interrupted(exc: Exception) -> bool:
    return ORACLE_CANCEL_RE.search(exception_text(exc)) is not None


def execution_error_lines(exc: Exception) -> list[str]:
    lines = ["ERROR executing statement:"]
    detail = exception_text(exc)
    if detail:
        lines.append("Oracle error:")
        for raw_line in detail.splitlines():
            lines.extend(wrap_display_text(raw_line, 120) or [""])

    diagnostics = execution_error_diagnostics(exc)
    if diagnostics:
        lines.append("Diagnostics:")
        for diagnostic in diagnostics:
            unit = diagnostic.unit or "PL/SQL"
            message = f"{unit} line {diagnostic.line}"
            if diagnostic.message:
                message = f"{message}: {diagnostic.message}"
            lines.extend(wrap_display_text(message, 120) or [""])

    if isinstance(exc, OracleExecutionError):
        if exc.dbms_output:
            lines.append("DBMS_OUTPUT:")
            for output_line in exc.dbms_output:
                for raw_line in output_line.splitlines() or [""]:
                    lines.extend(wrap_display_text(raw_line, 120) or [""])
        if exc.dbms_output_error:
            lines.append("DBMS_OUTPUT read failed:")
            lines.extend(wrap_display_text(exc.dbms_output_error, 120) or [""])
        if exc.warnings:
            lines.append("Warnings:")
            for warning in exc.warnings:
                lines.extend(wrap_display_text(warning, 120) or [""])
    return lines


def short_execution_error_message(exc: Exception) -> str:
    diagnostics = execution_error_diagnostics(exc)
    if diagnostics:
        diagnostic = diagnostics[0]
        if diagnostic.message:
            return diagnostic.message
        unit = diagnostic.unit or "PL/SQL"
        return f"{unit} line {diagnostic.line}"
    for line in exception_text(exc).splitlines():
        text = line.strip()
        if not text:
            continue
        for marker in ("ORA-", "PLS-"):
            marker_index = text.find(marker)
            if marker_index >= 0:
                return text[marker_index:]
        return text
    return ""


def _mapped_error_location(
    candidate: ParsedErrorLocation,
    statement_start_line: int,
    statement_start_col: int = 0,
) -> ParsedErrorLocation:
    start_line = max(1, statement_start_line)
    start_col = max(0, statement_start_col)
    location = candidate.location
    mapped_line = start_line + max(1, location.line) - 1
    mapped_column = max(1, location.column)
    if max(1, location.line) == 1:
        mapped_column += start_col
    mapped = ErrorLocation(line=mapped_line, column=mapped_column)
    return ParsedErrorLocation(
        mapped,
        candidate.priority,
        unit=candidate.unit,
        external=candidate.external,
    )


def _execution_error_location_candidates(exc: Exception) -> list[ParsedErrorLocation]:
    candidates: list[ParsedErrorLocation] = []
    best_priority: dict[tuple[int, int], int] = {}

    def add_candidate(candidate: ParsedErrorLocation) -> None:
        location = candidate.location
        key = (location.line, location.column)
        current_priority = best_priority.get(key)
        if current_priority is not None and current_priority <= candidate.priority:
            return
        best_priority[key] = candidate.priority
        candidates[:] = [existing for existing in candidates if existing.location != location]
        candidates.append(candidate)

    original = exc.original if isinstance(exc, OracleExecutionError) else exc
    if isinstance(original, OracleCompilationError):
        for diagnostic in original.diagnostics:
            priority = (
                ERROR_LOCATION_PRIORITY_COMPILE_ERROR
                if diagnostic.severity.upper() == "ERROR"
                else ERROR_LOCATION_PRIORITY_EXACT
            )
            add_candidate(
                ParsedErrorLocation(
                    ErrorLocation(diagnostic.line, diagnostic.position),
                    priority,
                )
            )

    for text in execution_error_texts(exc):
        for candidate in parse_error_location_candidates(text):
            add_candidate(candidate)

    offset_location = execution_error_offset_location(exc)
    if offset_location is not None:
        add_candidate(ParsedErrorLocation(offset_location, ERROR_LOCATION_PRIORITY_OFFSET))
    return candidates


def first_document_error_location(
    exc: Exception,
    statement_start_line: int = 1,
    statement_start_col: int = 0,
) -> ErrorLocation | None:
    candidates = _execution_error_location_candidates(exc)
    candidates = [candidate for candidate in candidates if not candidate.external]
    if not candidates:
        return None
    mapped = [
        _mapped_error_location(candidate, statement_start_line, statement_start_col)
        for candidate in candidates
    ]
    return min(
        mapped,
        key=lambda candidate: (candidate.priority, candidate.location.line, candidate.location.column),
    ).location


def document_error_locations(
    exc: Exception,
    statement_start_line: int = 1,
    statement_start_col: int = 0,
) -> list[ErrorLocation]:
    """Return ordered, deduplicated locations belonging to the executed source."""
    candidates = [
        _mapped_error_location(
            candidate,
            statement_start_line,
            statement_start_col,
        )
        for candidate in _execution_error_location_candidates(exc)
        if not candidate.external
    ]
    candidates.sort(
        key=lambda candidate: (
            candidate.priority,
            candidate.location.line,
            candidate.location.column,
        )
    )
    locations: list[ErrorLocation] = []
    seen: set[tuple[int, int]] = set()
    for candidate in candidates:
        key = (candidate.location.line, candidate.location.column)
        if key in seen:
            continue
        seen.add(key)
        locations.append(candidate.location)
    return locations


def document_error_diagnostics(
    exc: Exception,
    statement_start_line: int = 1,
    statement_start_col: int = 0,
) -> list[tuple[ErrorLocation, str]]:
    """Return local error locations with the most specific message for each."""
    locations = document_error_locations(
        exc,
        statement_start_line,
        statement_start_col,
    )
    if not locations:
        return []

    messages: dict[tuple[int, int], str] = {}
    message_priorities: dict[tuple[int, int], int] = {}

    def mapped_key(location: ErrorLocation) -> tuple[int, int]:
        mapped = _mapped_error_location(
            ParsedErrorLocation(location, ERROR_LOCATION_PRIORITY_EXACT),
            statement_start_line,
            statement_start_col,
        ).location
        return mapped.line, mapped.column

    original = exc.original if isinstance(exc, OracleExecutionError) else exc
    if isinstance(original, OracleCompilationError):
        for diagnostic in original.diagnostics:
            severity = diagnostic.severity.capitalize()
            text = diagnostic.text.strip()
            message = f"{severity}: {text}" if text else severity
            key = mapped_key(ErrorLocation(diagnostic.line, diagnostic.position))
            priority = (
                ERROR_LOCATION_PRIORITY_COMPILE_ERROR
                if diagnostic.severity.upper() == "ERROR"
                else ERROR_LOCATION_PRIORITY_EXACT
            )
            current_priority = message_priorities.get(key)
            if current_priority is None or priority < current_priority:
                messages[key] = message
                message_priorities[key] = priority

    for text in execution_error_texts(exc):
        lines = text.splitlines()
        for index, line in enumerate(lines):
            for match in LINE_COLUMN_RE.finditer(line):
                detail = line[match.end() :].lstrip(" :-\t")
                if not detail:
                    for following in lines[index + 1 :]:
                        detail = following.strip()
                        if detail and LINE_COLUMN_RE.search(detail) is None:
                            break
                        detail = ""
                if not detail:
                    continue
                key = mapped_key(
                    ErrorLocation(int(match.group(1)), int(match.group(2)))
                )
                messages.setdefault(key, detail)
        for parsed_diagnostic in parse_plsql_diagnostics(text):
            if (
                not is_local_plsql_unit(parsed_diagnostic.unit)
                or not parsed_diagnostic.message
            ):
                continue
            messages.setdefault(
                mapped_key(ErrorLocation(parsed_diagnostic.line, 1)),
                parsed_diagnostic.message,
            )

    fallback = short_execution_error_message(exc)
    return [
        (location, messages.get((location.line, location.column), fallback))
        for location in locations
    ]


def execution_error_offset_location(exc: Exception) -> ErrorLocation | None:
    if not isinstance(exc, OracleExecutionError) or not exc.statement:
        return None
    offset = oracle_error_offset(exc.original)
    if offset is None:
        return None
    return statement_offset_location(exc.statement, offset)


def oracle_error_offset(exc: Exception) -> int | None:
    sources = [exc]
    args = getattr(exc, "args", ())
    if args:
        sources.append(args[0])
    for source in sources:
        offset = getattr(source, "offset", None)
        if offset is None:
            continue
        try:
            parsed = int(offset)
        except (TypeError, ValueError):
            continue
        # python-oracledb defaults offset to 0, so use only concrete parse offsets.
        if parsed > 0:
            return parsed
    return None


def statement_offset_location(statement: str, offset: int) -> ErrorLocation:
    clamped = min(max(offset, 0), len(statement))
    preceding = statement[:clamped]
    line = preceding.count("\n") + 1
    last_newline = preceding.rfind("\n")
    if last_newline < 0:
        column = len(preceding) + 1
    else:
        column = len(preceding) - last_newline
    return ErrorLocation(line=line, column=column)


def exception_text(exc: Exception) -> str:
    if isinstance(exc, OracleExecutionError):
        return exception_text(exc.original)
    return "".join(traceback.format_exception_only(type(exc), exc)).strip() or str(exc)


def move_buffer_to_error(buffer: Buffer, location: ErrorLocation) -> ErrorLocation:
    row = min(max(location.line - 1, 0), len(buffer.lines) - 1)
    col = min(max(location.column - 1, 0), len(buffer.lines[row]))
    buffer.clear_selection()
    buffer.row = row
    buffer.col = col
    if buffer.scroll > row:
        buffer.scroll = row
    return ErrorLocation(line=row + 1, column=col + 1)

def wrap_error(exc: Exception) -> list[str]:
    detail = exception_text(exc)
    return wrap_display_text(detail, 120) or [str(exc)]


def short_error(exc: Exception) -> str:
    lines = wrap_error(exc)
    return lines[0] if lines else str(exc)
