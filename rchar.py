from __future__ import annotations

import curses
import locale
import os
import select
import sys
import time
import unicodedata

from plsqlwks.ui.keys import disable_extended_keyboard_reporting, enable_extended_keyboard_reporting


SEQUENCE_TIMEOUT_SECONDS = 0.1
MAX_SEQUENCE_BYTES = 64
RESULT_DISPLAY_SECONDS = 1.5


def read_keyboard_sequence(fd: int) -> bytes:
    data = bytearray(os.read(fd, 1))
    while len(data) < MAX_SEQUENCE_BYTES:
        ready, _, _ = select.select([fd], [], [], SEQUENCE_TIMEOUT_SECONDS)
        if not ready:
            break
        chunk = os.read(fd, MAX_SEQUENCE_BYTES - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def escaped_control_char(ch: str) -> str:
    codepoint = ord(ch)
    if codepoint <= 0xFF:
        return f"\\x{codepoint:02x}"
    if codepoint <= 0xFFFF:
        return f"\\u{codepoint:04x}"
    return f"\\U{codepoint:08x}"


def python_string_literal(text: str) -> str:
    parts = ['"']
    for ch in text:
        if ch == "\\":
            parts.append("\\\\")
        elif ch == '"':
            parts.append('\\"')
        elif unicodedata.category(ch)[0] == "C":
            parts.append(escaped_control_char(ch))
        else:
            parts.append(ch)
    parts.append('"')
    return "".join(parts)


def decode_terminal_text(data: bytes) -> str:
    encoding = sys.stdin.encoding or sys.getdefaultencoding()
    return data.decode(encoding, "surrogateescape")


def byte_summary(data: bytes) -> str:
    return " ".join(f"{value:02x}" for value in data)


def sequence_description(data: bytes) -> str:
    if data == b"\n":
        return "Ctrl-Enter (LF / \\x0a)"
    if data == b"\r":
        return "Enter (CR / \\x0d)"
    return "ANSI sequence"


def render_line(screen: curses.window, row: int, text: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if row < 0 or row >= height or width <= 1:
        return
    try:
        screen.addnstr(row, 0, text.ljust(width - 1), width - 1, attr)
    except curses.error:
        pass


def render_waiting(screen: curses.window) -> None:
    screen.erase()
    render_line(screen, 0, "ANSI keyboard sequence capture", curses.A_BOLD)
    render_line(screen, 2, "Press one key, shortcut, or terminal key sequence.")
    render_line(screen, 4, "The captured sequence will be printed after the curses screen closes.")
    screen.refresh()


def render_result(screen: curses.window, data: bytes) -> None:
    literal = python_string_literal(decode_terminal_text(data))
    screen.erase()
    render_line(screen, 0, "ANSI keyboard sequence capture", curses.A_BOLD)
    render_line(screen, 2, "String literal:")
    render_line(screen, 3, literal, curses.A_BOLD)
    render_line(screen, 5, "Detected:")
    render_line(screen, 6, sequence_description(data))
    render_line(screen, 8, "Bytes:")
    render_line(screen, 9, byte_summary(data))
    render_line(screen, 11, "Printed to stdout.")
    screen.refresh()


def run_curses_app(screen: curses.window) -> bytes:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.raw()
    except curses.error:
        pass
    screen.keypad(True)
    screen.nodelay(False)
    render_waiting(screen)

    extended_keyboard_enabled = enable_extended_keyboard_reporting()
    try:
        data = read_keyboard_sequence(sys.stdin.fileno())
        render_result(screen, data)
        time.sleep(RESULT_DISPLAY_SECONDS)
        return data
    finally:
        if extended_keyboard_enabled:
            disable_extended_keyboard_reporting()
        try:
            curses.noraw()
        except curses.error:
            pass


def main() -> int:
    if not sys.stdin.isatty():
        data = sys.stdin.buffer.read()
    else:
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass
        data = curses.wrapper(run_curses_app)

    if not data:
        return 1

    print(python_string_literal(decode_terminal_text(data)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
