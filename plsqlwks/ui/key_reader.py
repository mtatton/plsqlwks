from __future__ import annotations

import curses

from .constants import ESC, ESC_SEQUENCE_TIMEOUT_MS
from .keys import decode_key_sequence, is_escape_key, normalize_key


class KeyReader:
    """Read and normalize curses keys, including raw escape sequences."""

    def __init__(self, screen: curses.window):
        self.screen = screen

    def read_key(
        self,
        window: curses.window | None = None,
        idle_timeout: int = 200,
    ) -> int | str:
        target = window or self.screen
        try:
            key = target.get_wch()
        except curses.error:
            return -1
        if not is_escape_key(key):
            return normalize_key(key)

        sequence = [key]
        target.timeout(ESC_SEQUENCE_TIMEOUT_MS)
        try:
            while len(sequence) < 16:
                try:
                    next_key = target.get_wch()
                except curses.error:
                    break
                if next_key == -1:
                    break
                sequence.append(next_key)
                decoded = decode_key_sequence(sequence)
                if decoded is not None:
                    return decoded
        finally:
            target.timeout(idle_timeout)
        return ESC

    def __call__(
        self,
        window: curses.window | None = None,
        idle_timeout: int = 200,
    ) -> int | str:
        return self.read_key(window, idle_timeout)
