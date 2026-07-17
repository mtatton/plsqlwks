from __future__ import annotations

import textwrap
from dataclasses import dataclass

from .constants import (
    HELP_BORDER,
    HELP_BOX_WIDTH,
    HELP_KEY,
    HELP_SECTION,
    HELP_SECTIONS,
    HELP_TEXT,
    HELP_TIP,
    HELP_TIPS,
    HELP_TITLE,
)


@dataclass(frozen=True)
class HelpSegment:
    text: str
    kind: str = HELP_TEXT


@dataclass(frozen=True)
class HelpLine:
    segments: list[HelpSegment]

    @property
    def text(self) -> str:
        return "".join(segment.text for segment in self.segments)


def clip_ascii_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[:width]


def help_border_line() -> HelpLine:
    return HelpLine([HelpSegment("+" + "-" * (HELP_BOX_WIDTH - 2) + "+", HELP_BORDER)])


def help_box_line(text: str = "", kind: str = HELP_TEXT) -> HelpLine:
    inner_width = HELP_BOX_WIDTH - 2
    clipped = clip_ascii_text(text, inner_width)
    return HelpLine(
        [
            HelpSegment("|", HELP_BORDER),
            HelpSegment(clipped.ljust(inner_width), kind),
            HelpSegment("|", HELP_BORDER),
        ]
    )


def help_wrapped_box_lines(text: str, kind: str = HELP_TEXT) -> list[HelpLine]:
    inner_width = HELP_BOX_WIDTH - 2
    wrapped = textwrap.wrap(text, width=inner_width, break_long_words=True) or [""]
    return [help_box_line(line, kind) for line in wrapped]


def help_center_line(text: str, kind: str = HELP_TITLE) -> HelpLine:
    inner_width = HELP_BOX_WIDTH - 2
    clipped = clip_ascii_text(text, inner_width)
    left = max(0, (inner_width - len(clipped)) // 2)
    right = max(0, inner_width - len(clipped) - left)
    return HelpLine(
        [
            HelpSegment("|", HELP_BORDER),
            HelpSegment(" " * left, HELP_TEXT),
            HelpSegment(clipped, kind),
            HelpSegment(" " * right, HELP_TEXT),
            HelpSegment("|", HELP_BORDER),
        ]
    )


def help_section_line(title: str) -> HelpLine:
    inner_width = HELP_BOX_WIDTH - 2
    label = f" {clip_ascii_text(title, inner_width - 2)} "
    filler = "-" * max(0, inner_width - len(label))
    return HelpLine(
        [
            HelpSegment("|", HELP_BORDER),
            HelpSegment(label, HELP_SECTION),
            HelpSegment(filler, HELP_BORDER),
            HelpSegment("|", HELP_BORDER),
        ]
    )


def help_row_line(key: str, description: str) -> HelpLine:
    inner_width = HELP_BOX_WIDTH - 2
    indent = "  "
    key_width = 24
    gap = "  "
    description_width = max(0, inner_width - len(indent) - key_width - len(gap))
    key_text = clip_ascii_text(key, key_width).ljust(key_width)
    description_text = clip_ascii_text(description, description_width).ljust(description_width)
    return HelpLine(
        [
            HelpSegment("|", HELP_BORDER),
            HelpSegment(indent, HELP_TEXT),
            HelpSegment(key_text, HELP_KEY),
            HelpSegment(gap, HELP_TEXT),
            HelpSegment(description_text, HELP_TEXT),
            HelpSegment("|", HELP_BORDER),
        ]
    )


def build_help_lines(workspace_messages: list[str] | None = None) -> list[HelpLine]:
    lines = [
        help_border_line(),
        help_center_line("PLSQLWKS HELP"),
        help_center_line("F1 opens this page  |  Esc/Tab returns from result focus", HELP_TIP),
        help_border_line(),
    ]
    if workspace_messages:
        lines.append(help_section_line("WORKSPACE"))
        for message in workspace_messages:
            lines.extend(help_wrapped_box_lines(f"  {message}", HELP_TIP))
        lines.append(help_box_line())
    for section, rows in HELP_SECTIONS:
        lines.append(help_section_line(section))
        for key, description in rows:
            lines.append(help_row_line(key, description))
        lines.append(help_box_line())
    lines.append(help_section_line("TIPS"))
    for tip in HELP_TIPS:
        lines.append(help_box_line(f"  {tip}", HELP_TIP))
    lines.append(help_border_line())
    return lines


HELP_LINES = build_help_lines()
HELP = [line.text for line in HELP_LINES]
