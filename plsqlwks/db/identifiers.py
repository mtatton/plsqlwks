from __future__ import annotations

import re
from dataclasses import dataclass

ORACLE_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]*")


@dataclass(frozen=True)
class OracleIdentifierToken:
    name: str
    quoted: bool
    end: int


def scan_oracle_identifier(
    text: str,
    start: int = 0,
) -> OracleIdentifierToken | None:
    """Parse one complete Oracle identifier token at *start*."""
    if start < 0 or start >= len(text):
        return None
    if text[start] != '"':
        match = ORACLE_IDENTIFIER_RE.match(text, start)
        if match is None:
            return None
        return OracleIdentifierToken(match.group(0).upper(), False, match.end())

    chars: list[str] = []
    idx = start + 1
    while idx < len(text):
        ch = text[idx]
        if ch != '"':
            chars.append(ch)
            idx += 1
            continue
        if idx + 1 < len(text) and text[idx + 1] == '"':
            chars.append('"')
            idx += 2
            continue
        return OracleIdentifierToken("".join(chars), True, idx + 1)
    return None


def normalize_identifier(identifier: str) -> str | None:
    """Return the exact dictionary name represented by a SQL identifier token."""
    text = identifier.strip()
    token = scan_oracle_identifier(text)
    if token is None or token.end != len(text) or not token.name:
        return None
    return token.name


def quote_identifier(name: str) -> str:
    if not name or "\x00" in name:
        raise ValueError("Oracle identifiers must not be empty or contain NUL")
    return '"' + name.replace('"', '""') + '"'


def render_identifier(name: str, reserved_words: set[str] | frozenset[str] = frozenset()) -> str:
    """Render an exact dictionary name without changing its meaning."""
    if ORACLE_IDENTIFIER_RE.fullmatch(name) and name == name.upper() and name.upper() not in reserved_words:
        return name
    return quote_identifier(name)
