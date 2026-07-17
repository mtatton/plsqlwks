from __future__ import annotations

import curses
import locale
import os
import sys
from typing import Any

from .constants import *

def configure_utf8_locale() -> None:
    os.environ.setdefault("LANG", "C.UTF-8")
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        for candidate in ("C.UTF-8", "en_US.UTF-8", "UTF-8"):
            try:
                locale.setlocale(locale.LC_ALL, candidate)
                return
            except locale.Error:
                continue


def write_terminal_sequence(sequence: bytes, stream: Any | None = None) -> bool:
    if stream is None:
        try:
            fd = os.open("/dev/tty", os.O_WRONLY | getattr(os, "O_NOCTTY", 0))
            try:
                offset = 0
                while offset < len(sequence):
                    written = os.write(fd, sequence[offset:])
                    if written <= 0:
                        return False
                    offset += written
                return True
            finally:
                os.close(fd)
        except Exception:
            stream = getattr(sys, "__stdout__", sys.stdout)

    if stream is None:
        return False
    target = stream
    try:
        binary_target = getattr(target, "buffer", None)
        if binary_target is not None:
            binary_target.write(sequence)
            binary_target.flush()
            return True
        try:
            target.write(sequence)
        except TypeError:
            target.write(sequence.decode("ascii"))
        target.flush()
        return True
    except Exception:
        return False


def enable_extended_keyboard_reporting(stream: Any | None = None) -> bool:
    return write_terminal_sequence(EXTENDED_KEYBOARD_ENABLE, stream)


def disable_extended_keyboard_reporting(stream: Any | None = None) -> bool:
    return write_terminal_sequence(EXTENDED_KEYBOARD_RESET, stream)


def decode_key_sequence(sequence: list[int | str]) -> int | None:
    text = "".join(key_to_text(key) for key in sequence)
    if text in FUNCTION_KEY_SEQUENCES:
        return FUNCTION_KEY_SEQUENCES[text]
    if text in CTRL_ENTER_SEQUENCES:
        return KEY_CTRL_ENTER
    if text in CTRL_EQUALS_SEQUENCES:
        return KEY_CTRL_EQUALS
    if text in CTRL_G_SEQUENCES:
        return CTRL_G
    if text in CTRL_HOME_SEQUENCES:
        return KEY_CTRL_HOME
    if text in CTRL_END_SEQUENCES:
        return KEY_CTRL_END
    if text in CTRL_LEFT_SEQUENCES:
        return KEY_CTRL_LEFT
    if text in CTRL_RIGHT_SEQUENCES:
        return KEY_CTRL_RIGHT
    if text in CTRL_UP_SEQUENCES:
        return KEY_CTRL_UP
    if text in CTRL_DOWN_SEQUENCES:
        return KEY_CTRL_DOWN
    if text in CTRL_SHIFT_LEFT_SEQUENCES:
        return KEY_CTRL_SHIFT_LEFT
    if text in CTRL_SHIFT_RIGHT_SEQUENCES:
        return KEY_CTRL_SHIFT_RIGHT
    if text in CTRL_PAGEUP_SEQUENCES:
        return KEY_CTRL_PAGEUP
    if text in CTRL_PAGEDOWN_SEQUENCES:
        return KEY_CTRL_PAGEDOWN
    if text in CTRL_DELETE_SEQUENCES:
        return KEY_CTRL_DELETE
    if text in CTRL_BACKSPACE_SEQUENCES:
        return KEY_CTRL_BACKSPACE
    if text in INSERT_SEQUENCES:
        return curses.KEY_IC
    if text in SHIFT_TAB_SEQUENCES:
        return KEY_SHIFT_TAB
    if text in SHIFT_PAGE_SEQUENCES:
        return SHIFT_PAGE_SEQUENCES[text]
    if text in CTRL_SHIFT_BOUNDARY_SEQUENCES:
        return CTRL_SHIFT_BOUNDARY_SEQUENCES[text]
    if text in SHIFT_BOUNDARY_SEQUENCES:
        return SHIFT_BOUNDARY_SEQUENCES[text]
    if text in SHIFT_ARROW_SEQUENCES:
        return SHIFT_ARROW_SEQUENCES[text]
    if len(text) == 2 and text.startswith("\x1b") and text[1] in "123456789":
        return alt_digit_key(int(text[1]))
    if text in ALT_X_SEQUENCES:
        return KEY_ALT_X
    if text in ALT_O_SEQUENCES:
        return KEY_ALT_O
    if text in ALT_R_SEQUENCES:
        return KEY_ALT_R
    if text in ALT_G_SEQUENCES:
        return KEY_ALT_G
    if text in ALT_PLUS_SEQUENCES:
        return KEY_ALT_PLUS
    if text in CTRL_ALT_C_SEQUENCES:
        return KEY_CTRL_ALT_C
    if text in CTRL_ALT_R_SEQUENCES:
        return KEY_CTRL_ALT_R
    return None


def alt_digit_key(digit: int) -> int:
    return KEY_ALT_DIGIT_BASE + digit


def alt_digit_from_key(key: int | str) -> int | None:
    if isinstance(key, int) and KEY_ALT_DIGIT_BASE + 1 <= key <= KEY_ALT_DIGIT_BASE + 9:
        return key - KEY_ALT_DIGIT_BASE
    return None


def key_to_text(key: int | str) -> str:
    if isinstance(key, str):
        return key
    if 0 <= key <= 255:
        return chr(key)
    return ""


def is_escape_key(key: int | str) -> bool:
    return key == ESC or key == "\x1b"


def normalize_key(key: int | str) -> int | str:
    if key == "\n" or key == 10:
        return KEY_CTRL_ENTER
    if key == "\r" or key == 13 or key == curses.KEY_ENTER:
        return 13
    if isinstance(key, int):
        normalized = normalize_curses_keyname(curses_keyname(key))
        if normalized is not None:
            return normalized
    if isinstance(key, int) and key in CURSES_CTRL_KEYS:
        return CURSES_CTRL_KEYS[key]
    if isinstance(key, int) and key in CURSES_SHIFT_KEYS:
        return CURSES_SHIFT_KEYS[key]
    if isinstance(key, str) and len(key) == 1 and (ord(key) < 32 or ord(key) == 127):
        return ord(key)
    return key


def curses_keyname(key: int) -> str:
    try:
        raw = curses.keyname(key)
    except Exception:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("ascii", "replace")
    return str(raw)


def normalize_curses_keyname(name: str) -> int | None:
    if not name:
        return None
    if name in FUNCTION_KEY_KEYNAMES:
        return FUNCTION_KEY_KEYNAMES[name]
    if name in CTRL_SHIFT_HOME_KEYNAMES:
        return KEY_CTRL_SHIFT_HOME
    if name in CTRL_SHIFT_END_KEYNAMES:
        return KEY_CTRL_SHIFT_END
    if name in SHIFT_HOME_KEYNAMES:
        return KEY_SHIFT_HOME
    if name in SHIFT_END_KEYNAMES:
        return KEY_SHIFT_END
    if name in CTRL_HOME_KEYNAMES:
        return KEY_CTRL_HOME
    if name in CTRL_END_KEYNAMES:
        return KEY_CTRL_END
    if name in CTRL_UP_KEYNAMES:
        return KEY_CTRL_UP
    if name in CTRL_DOWN_KEYNAMES:
        return KEY_CTRL_DOWN
    if name in CTRL_LEFT_KEYNAMES:
        return KEY_CTRL_LEFT
    if name in CTRL_RIGHT_KEYNAMES:
        return KEY_CTRL_RIGHT
    if name in CTRL_SHIFT_LEFT_KEYNAMES:
        return KEY_CTRL_SHIFT_LEFT
    if name in CTRL_SHIFT_RIGHT_KEYNAMES:
        return KEY_CTRL_SHIFT_RIGHT
    if name in CTRL_PAGEUP_KEYNAMES:
        return KEY_CTRL_PAGEUP
    if name in CTRL_PAGEDOWN_KEYNAMES:
        return KEY_CTRL_PAGEDOWN
    if name in CTRL_DELETE_KEYNAMES:
        return KEY_CTRL_DELETE
    if name in CTRL_BACKSPACE_KEYNAMES:
        return KEY_CTRL_BACKSPACE
    if name in SHIFT_PAGEUP_KEYNAMES:
        return KEY_SHIFT_PAGEUP
    if name in SHIFT_PAGEDOWN_KEYNAMES:
        return KEY_SHIFT_PAGEDOWN
    if name in SHIFT_TAB_KEYNAMES:
        return KEY_SHIFT_TAB
    if name in INSERT_KEYNAMES:
        return curses.KEY_IC
    upper_name = name.upper()
    if upper_name in FUNCTION_KEY_KEYNAMES:
        return FUNCTION_KEY_KEYNAMES[upper_name]
    if upper_name in {value.upper() for value in CTRL_SHIFT_HOME_KEYNAMES}:
        return KEY_CTRL_SHIFT_HOME
    if upper_name in {value.upper() for value in CTRL_SHIFT_END_KEYNAMES}:
        return KEY_CTRL_SHIFT_END
    if upper_name in {value.upper() for value in SHIFT_HOME_KEYNAMES if value.startswith("KEY_")}:
        return KEY_SHIFT_HOME
    if upper_name in {value.upper() for value in SHIFT_END_KEYNAMES if value.startswith("KEY_")}:
        return KEY_SHIFT_END
    if upper_name in {value.upper() for value in CTRL_HOME_KEYNAMES}:
        return KEY_CTRL_HOME
    if upper_name in {value.upper() for value in CTRL_END_KEYNAMES}:
        return KEY_CTRL_END
    if upper_name in {value.upper() for value in CTRL_UP_KEYNAMES}:
        return KEY_CTRL_UP
    if upper_name in {value.upper() for value in CTRL_DOWN_KEYNAMES}:
        return KEY_CTRL_DOWN
    if upper_name in {value.upper() for value in CTRL_LEFT_KEYNAMES}:
        return KEY_CTRL_LEFT
    if upper_name in {value.upper() for value in CTRL_RIGHT_KEYNAMES}:
        return KEY_CTRL_RIGHT
    if upper_name in {value.upper() for value in CTRL_SHIFT_LEFT_KEYNAMES}:
        return KEY_CTRL_SHIFT_LEFT
    if upper_name in {value.upper() for value in CTRL_SHIFT_RIGHT_KEYNAMES}:
        return KEY_CTRL_SHIFT_RIGHT
    if upper_name in {value.upper() for value in CTRL_PAGEUP_KEYNAMES}:
        return KEY_CTRL_PAGEUP
    if upper_name in {value.upper() for value in CTRL_PAGEDOWN_KEYNAMES}:
        return KEY_CTRL_PAGEDOWN
    if upper_name in {value.upper() for value in CTRL_DELETE_KEYNAMES}:
        return KEY_CTRL_DELETE
    if upper_name in {value.upper() for value in CTRL_BACKSPACE_KEYNAMES}:
        return KEY_CTRL_BACKSPACE
    if upper_name in {value.upper() for value in SHIFT_PAGEUP_KEYNAMES if value.startswith("KEY_")}:
        return KEY_SHIFT_PAGEUP
    if upper_name in {value.upper() for value in SHIFT_PAGEDOWN_KEYNAMES if value.startswith("KEY_")}:
        return KEY_SHIFT_PAGEDOWN
    if upper_name in {value.upper() for value in SHIFT_TAB_KEYNAMES if value.startswith("KEY_")}:
        return KEY_SHIFT_TAB
    if upper_name in {value.upper() for value in INSERT_KEYNAMES if value.startswith("KEY_")}:
        return curses.KEY_IC
    return None
