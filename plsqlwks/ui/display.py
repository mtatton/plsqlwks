from __future__ import annotations

import unicodedata

LINE_BREAKS = ("\r", "\n")


def is_printable_text(text: str) -> bool:
    return bool(text) and not any(unicodedata.category(ch)[0] == "C" for ch in text)


def filtered_picker_indexes(options: list[str], filter_text: str) -> list[int]:
    query = filter_text.casefold()
    if not query:
        return list(range(len(options)))
    return [idx for idx, option in enumerate(options) if query in option.casefold()]


def clamp_picker_selection(selected: int, option_count: int) -> int:
    if option_count <= 0:
        return 0
    return min(max(selected, 0), option_count - 1)


def cell_width(ch: str) -> int:
    if not ch:
        return 0
    if ch == "\t":
        return 4
    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch)[0] == "C":
        return 0
    if unicodedata.east_asian_width(ch) in ("F", "W"):
        return 2
    return 1


def escaped_control_char(ch: str) -> str:
    codepoint = ord(ch)
    if codepoint <= 0xFF:
        return f"\\x{codepoint:02x}"
    if codepoint <= 0xFFFF:
        return f"\\u{codepoint:04x}"
    return f"\\U{codepoint:08x}"


def display_units(text: str) -> list[str]:
    units: list[str] = []
    in_line_break = False
    for ch in text:
        if ch in LINE_BREAKS:
            if not in_line_break:
                units.append(" ")
            in_line_break = True
            continue
        in_line_break = False
        units.append(escaped_control_char(ch) if ch != "\t" and unicodedata.category(ch)[0] == "C" else ch)
    return units


def display_unit_width(text: str) -> int:
    if len(text) != 1:
        return len(text)
    return cell_width(text)


def display_width(text: str) -> int:
    return sum(display_unit_width(unit) for unit in display_units(text))


def clip_text(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    used = 0
    clipped: list[str] = []
    for unit in display_units(text):
        width = display_unit_width(unit)
        if used + width > max_width:
            break
        clipped.append(unit)
        used += width
    return "".join(clipped)


def fit_text(text: str, width: int) -> str:
    clipped = clip_text(text, width)
    return clipped + " " * max(0, width - display_width(clipped))


def display_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def wrap_display_line(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current: list[str] = []
    used = 0
    for unit in display_units(text):
        unit_width = display_unit_width(unit)
        if current and used + unit_width > width:
            lines.append("".join(current))
            current = []
            used = 0
        if unit_width > width:
            continue
        current.append(unit)
        used += unit_width
    if current:
        lines.append("".join(current))
    return lines


def wrap_display_text(text: str, width: int) -> list[str]:
    if width <= 0:
        return [""]
    if not text:
        return []
    lines: list[str] = []
    for line in display_lines(text):
        if line:
            lines.extend(wrap_display_line(line, width) or [""])
        else:
            lines.append("")
    return lines
