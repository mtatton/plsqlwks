from __future__ import annotations

import curses


CTRL_Q = 17
CTRL_S = 19
CTRL_B = 2
CTRL_C = 3
CTRL_E = 5
CTRL_F = 6
CTRL_G = 7
CTRL_N = 14
CTRL_V = 22
CTRL_O = 15
CTRL_P = 16
CTRL_T = 20
CTRL_U = 21
CTRL_L = 12
CTRL_R = 18
CTRL_W = 23
CTRL_X = 24
CTRL_Y = 25
CTRL_Z = 26
TAB = 9
ESC = 27
ESC_SEQUENCE_TIMEOUT_MS = 100
UNDO_HISTORY_LIMIT = 200
XTERM_MODIFY_OTHER_KEYS_ENABLE = b"\x1b[>4;2m"
XTERM_MODIFY_OTHER_KEYS_RESET = b"\x1b[>4;0m"
KITTY_KEYBOARD_PROTOCOL_ENABLE = b"\x1b[>1u"
KITTY_KEYBOARD_PROTOCOL_RESET = b"\x1b[<1u"
EXTENDED_KEYBOARD_ENABLE = XTERM_MODIFY_OTHER_KEYS_ENABLE + KITTY_KEYBOARD_PROTOCOL_ENABLE
EXTENDED_KEYBOARD_RESET = KITTY_KEYBOARD_PROTOCOL_RESET + XTERM_MODIFY_OTHER_KEYS_RESET
KEY_CTRL_ENTER = 1_000_001
KEY_CTRL_HOME = 1_000_002
KEY_CTRL_END = 1_000_003
KEY_SHIFT_UP = 1_000_004
KEY_SHIFT_DOWN = 1_000_005
KEY_SHIFT_LEFT = 1_000_006
KEY_SHIFT_RIGHT = 1_000_007
KEY_CTRL_LEFT = 1_000_008
KEY_CTRL_RIGHT = 1_000_009
KEY_ALT_X = 1_000_010
KEY_SHIFT_HOME = 1_000_011
KEY_SHIFT_END = 1_000_012
KEY_CTRL_SHIFT_HOME = 1_000_013
KEY_CTRL_SHIFT_END = 1_000_014
KEY_CTRL_PAGEUP = 1_000_015
KEY_CTRL_PAGEDOWN = 1_000_016
KEY_CTRL_SHIFT_LEFT = 1_000_017
KEY_CTRL_SHIFT_RIGHT = 1_000_018
KEY_CTRL_ALT_C = 1_000_019
KEY_CTRL_ALT_R = 1_000_020
KEY_SHIFT_PAGEUP = 1_000_021
KEY_SHIFT_PAGEDOWN = 1_000_022
KEY_SHIFT_TAB = 1_000_023
KEY_CTRL_EQUALS = 1_000_024
KEY_CTRL_UP = 1_000_025
KEY_CTRL_DOWN = 1_000_026
KEY_ALT_R = 1_000_027
KEY_ALT_G = 1_000_028
KEY_CTRL_DELETE = 1_000_029
KEY_CTRL_BACKSPACE = 1_000_030
KEY_ALT_PLUS = 1_000_031
KEY_ALT_O = 1_000_032
KEY_ALT_DIGIT_BASE = 1_000_100


def curses_function_key(number: int) -> int:
    return getattr(curses, f"KEY_F{number}", curses.KEY_F0 + number)


FUNCTION_KEY_SEQUENCES = {
    "\x1bOP": curses_function_key(1),
    "\x1bOQ": curses_function_key(2),
    "\x1bOR": curses_function_key(3),
    "\x1bOS": curses_function_key(4),
    "\x1b[11~": curses_function_key(1),
    "\x1b[12~": curses_function_key(2),
    "\x1b[13~": curses_function_key(3),
    "\x1b[14~": curses_function_key(4),
    "\x1b[15~": curses_function_key(5),
    "\x1b[17~": curses_function_key(6),
    "\x1b[18~": curses_function_key(7),
    "\x1b[19~": curses_function_key(8),
    "\x1b[20~": curses_function_key(9),
    "\x1b[21~": curses_function_key(10),
    "\x1b[23~": curses_function_key(11),
    "\x1b[24~": curses_function_key(12),
    "\x1b[[A": curses_function_key(1),
    "\x1b[[B": curses_function_key(2),
    "\x1b[[C": curses_function_key(3),
    "\x1b[[D": curses_function_key(4),
    "\x1b[[E": curses_function_key(5),
}
FUNCTION_KEY_KEYNAMES: dict[str, int] = {}
for _number in range(1, 13):
    _key = curses_function_key(_number)
    for _name in (f"KEY_F{_number}", f"KEY_F({_number})", f"kf{_number}", f"F{_number}"):
        FUNCTION_KEY_KEYNAMES[_name] = _key
        FUNCTION_KEY_KEYNAMES[_name.upper()] = _key
        FUNCTION_KEY_KEYNAMES[_name.lower()] = _key
CTRL_ENTER_SEQUENCES = {
    "\n",
    "\x1b[13;5u",
    "\x1b[10;5u",
    "\x1b[77;5u",
    "\x1b[109;5u",
    "\x1b[74;5u",
    "\x1b[106;5u",
    "\x1b[27;5;13~",
    "\x1b[27;5;10~",
    "\x1b[27;5;77~",
    "\x1b[27;5;109~",
    "\x1b[27;5;74~",
    "\x1b[27;5;106~",
    "\x1b[13;5~",
    "\x1b[10;5~",
}
CTRL_EQUALS_SEQUENCES = {
    "\x1b[61;5u",
    "\x1b[27;5;61~",
}
CTRL_G_SEQUENCES = {
    "\x1b[103;5u",
    "\x1b[71;5u",
    "\x1b[27;5;103~",
    "\x1b[27;5;71~",
}
CTRL_HOME_SEQUENCES = {
    "\x1b[1;5H",
    "\x1b[5H",
    "\x1b[7;5~",
    "\x1b[27;5;72~",
    "\x1b[H;5",
}
CTRL_END_SEQUENCES = {
    "\x1b[1;5F",
    "\x1b[5F",
    "\x1b[4;5~",
    "\x1b[8;5~",
    "\x1b[27;5;70~",
    "\x1b[F;5",
}
CTRL_LEFT_SEQUENCES = {
    "\x1b[1;5D",
    "\x1b[5D",
    "\x1bOd",
    "\x1b[27;5;68~",
}
CTRL_RIGHT_SEQUENCES = {
    "\x1b[1;5C",
    "\x1b[5C",
    "\x1bOc",
    "\x1b[27;5;67~",
}
CTRL_UP_SEQUENCES = {
    "\x1b[1;5A",
    "\x1b[5A",
    "\x1bOa",
    "\x1b[27;5;65~",
}
CTRL_DOWN_SEQUENCES = {
    "\x1b[1;5B",
    "\x1b[5B",
    "\x1bOb",
    "\x1b[27;5;66~",
}
CTRL_SHIFT_LEFT_SEQUENCES = {
    "\x1b[1;6D",
    "\x1b[6D",
    "\x1b[27;6;68~",
}
CTRL_SHIFT_RIGHT_SEQUENCES = {
    "\x1b[1;6C",
    "\x1b[6C",
    "\x1b[27;6;67~",
}
CTRL_PAGEUP_SEQUENCES = {
    "\x1b[5;5~",
    "\x1b[27;5;53~",
}
CTRL_PAGEDOWN_SEQUENCES = {
    "\x1b[6;5~",
    "\x1b[27;5;54~",
}
CTRL_DELETE_SEQUENCES = {"\x1b[3;5~"}
CTRL_BACKSPACE_SEQUENCES = {
    "\x1b[127;5u",
    "\x1b[8;5u",
    "\x1b[27;5;127~",
    "\x1b[27;5;8~",
    "\x1b[127;5~",
}
INSERT_SEQUENCES = {"\x1b[2~"}
SHIFT_PAGE_SEQUENCES = {
    "\x1b[5;2~": KEY_SHIFT_PAGEUP,
    "\x1b[27;2;53~": KEY_SHIFT_PAGEUP,
    "\x1b[6;2~": KEY_SHIFT_PAGEDOWN,
    "\x1b[27;2;54~": KEY_SHIFT_PAGEDOWN,
}
SHIFT_TAB_SEQUENCES = {"\x1b[Z"}
ALT_X_SEQUENCES = {"\x1bx", "\x1bX"}
ALT_O_SEQUENCES = {
    "\x1bo",
    "\x1b[111;3u",
    "\x1b[79;3u",
    "\x1b[27;3;111~",
    "\x1b[27;3;79~",
}
ALT_R_SEQUENCES = {"\x1br", "\x1bR"}
ALT_G_SEQUENCES = {"\x1bg", "\x1bG"}
ALT_PLUS_SEQUENCES = {
    "\x1b+",
    "\x1b=",
    "\x1b[43;3u",
    "\x1b[61;3u",
    "\x1b[27;3;43~",
    "\x1b[27;3;61~",
}
CTRL_ALT_C_SEQUENCES = {
    "\x1b\x03",
    "\x1b[99;7u",
    "\x1b[67;7u",
    "\x1b[27;7;99~",
    "\x1b[27;7;67~",
}
CTRL_ALT_R_SEQUENCES = {
    "\x1b\x12",
    "\x1b[114;7u",
    "\x1b[82;7u",
    "\x1b[27;7;114~",
    "\x1b[27;7;82~",
}
CTRL_UP_KEYNAMES = {"kUP5", "KEY_CTRL_UP", "KEY_CONTROL_UP"}
CTRL_DOWN_KEYNAMES = {"kDN5", "KEY_CTRL_DOWN", "KEY_CONTROL_DOWN"}
CTRL_LEFT_KEYNAMES = {"kLFT5", "KEY_CLEFT", "KEY_CTRL_LEFT", "KEY_CONTROL_LEFT"}
CTRL_RIGHT_KEYNAMES = {"kRIT5", "KEY_CRIGHT", "KEY_CTRL_RIGHT", "KEY_CONTROL_RIGHT"}
CTRL_SHIFT_LEFT_KEYNAMES = {
    "kLFT6",
    "KEY_CSLEFT",
    "KEY_CTRL_SHIFT_LEFT",
    "KEY_CONTROL_SHIFT_LEFT",
}
CTRL_SHIFT_RIGHT_KEYNAMES = {
    "kRIT6",
    "KEY_CSRIGHT",
    "KEY_CTRL_SHIFT_RIGHT",
    "KEY_CONTROL_SHIFT_RIGHT",
}
CTRL_HOME_KEYNAMES = {"kHOM5", "KEY_CHOME", "KEY_CTRL_HOME", "KEY_CONTROL_HOME"}
CTRL_END_KEYNAMES = {"kEND5", "KEY_CEND", "KEY_CTRL_END", "KEY_CONTROL_END"}
CTRL_PAGEUP_KEYNAMES = {"kPRV5", "KEY_CPREVIOUS", "KEY_CTRL_PPAGE", "KEY_CTRL_PAGEUP", "KEY_CONTROL_PAGEUP"}
CTRL_PAGEDOWN_KEYNAMES = {"kNXT5", "KEY_CNEXT", "KEY_CTRL_NPAGE", "KEY_CTRL_PAGEDOWN", "KEY_CONTROL_PAGEDOWN"}
CTRL_DELETE_KEYNAMES = {"kDC5", "KEY_CDC", "KEY_CTRL_DELETE", "KEY_CONTROL_DELETE"}
CTRL_BACKSPACE_KEYNAMES = {"kBS5", "kbs5", "KEY_CBACKSPACE", "KEY_CTRL_BACKSPACE", "KEY_CONTROL_BACKSPACE"}
SHIFT_PAGEUP_KEYNAMES = {"kPRV", "KEY_SPREVIOUS", "KEY_SHIFT_PPAGE", "KEY_SHIFT_PAGEUP"}
SHIFT_PAGEDOWN_KEYNAMES = {"kNXT", "KEY_SNEXT", "KEY_SHIFT_NPAGE", "KEY_SHIFT_PAGEDOWN"}
SHIFT_TAB_KEYNAMES = {"kBTab", "kcbt", "KEY_BTAB", "KEY_BACKTAB", "KEY_SHIFT_TAB"}
INSERT_KEYNAMES = {"kich1", "kIC", "KEY_IC", "KEY_INSERT", "KEY_INS"}
SHIFT_ARROW_SEQUENCES = {
    "\x1b[1;2A": KEY_SHIFT_UP,
    "\x1b[1;2B": KEY_SHIFT_DOWN,
    "\x1b[1;2C": KEY_SHIFT_RIGHT,
    "\x1b[1;2D": KEY_SHIFT_LEFT,
    "\x1b[2A": KEY_SHIFT_UP,
    "\x1b[2B": KEY_SHIFT_DOWN,
    "\x1b[2C": KEY_SHIFT_RIGHT,
    "\x1b[2D": KEY_SHIFT_LEFT,
    "\x1b[a": KEY_SHIFT_UP,
    "\x1b[b": KEY_SHIFT_DOWN,
    "\x1b[c": KEY_SHIFT_RIGHT,
    "\x1b[d": KEY_SHIFT_LEFT,
}
SHIFT_BOUNDARY_SEQUENCES = {
    "\x1b[1;2H": KEY_SHIFT_HOME,
    "\x1b[2H": KEY_SHIFT_HOME,
    "\x1b[7;2~": KEY_SHIFT_HOME,
    "\x1b[27;2;72~": KEY_SHIFT_HOME,
    "\x1b[1;2F": KEY_SHIFT_END,
    "\x1b[2F": KEY_SHIFT_END,
    "\x1b[8;2~": KEY_SHIFT_END,
    "\x1b[27;2;70~": KEY_SHIFT_END,
}
CTRL_SHIFT_BOUNDARY_SEQUENCES = {
    "\x1b[1;6H": KEY_CTRL_SHIFT_HOME,
    "\x1b[6H": KEY_CTRL_SHIFT_HOME,
    "\x1b[7;6~": KEY_CTRL_SHIFT_HOME,
    "\x1b[27;6;72~": KEY_CTRL_SHIFT_HOME,
    "\x1b[1;6F": KEY_CTRL_SHIFT_END,
    "\x1b[6F": KEY_CTRL_SHIFT_END,
    "\x1b[8;6~": KEY_CTRL_SHIFT_END,
    "\x1b[27;6;70~": KEY_CTRL_SHIFT_END,
}
SHIFT_HOME_KEYNAMES = {"kHOM", "KEY_SHOME", "KEY_SHIFT_HOME"}
SHIFT_END_KEYNAMES = {"kEND", "KEY_SEND", "KEY_SHIFT_END"}
CTRL_SHIFT_HOME_KEYNAMES = {
    "kHOM6",
    "KEY_CS_HOME",
    "KEY_CSHOME",
    "KEY_CTRL_SHIFT_HOME",
    "KEY_CONTROL_SHIFT_HOME",
}
CTRL_SHIFT_END_KEYNAMES = {
    "kEND6",
    "KEY_CS_END",
    "KEY_CSEND",
    "KEY_CTRL_SHIFT_END",
    "KEY_CONTROL_SHIFT_END",
}
CURSES_SHIFT_KEYS: dict[int, int] = {
    getattr(curses, "KEY_SLEFT", -1): KEY_SHIFT_LEFT,
    getattr(curses, "KEY_SRIGHT", -1): KEY_SHIFT_RIGHT,
    getattr(curses, "KEY_SR", -1): KEY_SHIFT_UP,
    getattr(curses, "KEY_SF", -1): KEY_SHIFT_DOWN,
}
CURSES_SHIFT_KEYS.pop(-1, None)
CURSES_CTRL_KEYS: dict[int, int] = {
    getattr(curses, "KEY_CLEFT", -1): KEY_CTRL_LEFT,
    getattr(curses, "KEY_CRIGHT", -1): KEY_CTRL_RIGHT,
}
CURSES_CTRL_KEYS.pop(-1, None)
FOCUS_EDITOR = "editor"
FOCUS_RESULTS = "results"
FOCUS_BROWSER = "browser"
RESULT_GRID = "grid"
RESULT_ROW_DETAIL = "row_detail"
RESULT_STYLE_TEXT = "text"
RESULT_STYLE_HELP = "help"
RESULT_RATIO_EDITOR_FULLSCREEN = 0.0
RESULT_RATIO_GRID_SPLIT = 1 / 3
RESULT_RATIO_HALF = 0.5
RESULT_RATIO_EXPANDED = 0.7
RESULT_RATIO_FULLSCREEN = 1.0
RESULT_RATIO_EPSILON = 0.001
RESULT_PANE_LAYOUTS = (
    (RESULT_RATIO_EDITOR_FULLSCREEN, "editor fullscreen"),
    (RESULT_RATIO_GRID_SPLIT, "2/3 editor, 1/3 data grid"),
    (RESULT_RATIO_HALF, "half-screen"),
    (RESULT_RATIO_EXPANDED, "expanded"),
    (RESULT_RATIO_FULLSCREEN, "fullscreen"),
)
PLAN_CONNECTOR = "connector"
PLAN_OPERATION = "operation"
PLAN_OBJECT = "object"
PLAN_METRICS = "metrics"
PLAN_TEXT = "text"
HELP_BORDER = "border"
HELP_TITLE = "title"
HELP_SECTION = "section"
HELP_KEY = "key"
HELP_TEXT = "text"
HELP_TIP = "tip"
HELP_BOX_WIDTH = 78
COLOR_SYNTAX_KEYWORD = 6
COLOR_SYNTAX_STRING = 7
COLOR_SYNTAX_NUMBER = 8
COLOR_SYNTAX_COMMENT = 9
COLOR_SYNTAX_BIND = 10
COLOR_SYNTAX_OPERATOR = 11
COLOR_PLAN_CONNECTOR = 12
COLOR_PLAN_OPERATION = 13
COLOR_PLAN_OBJECT = 14
COLOR_PLAN_METRICS = 15
COLOR_PLAN_TEXT = 16
BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}"}
CLOSING_BRACKETS = {close: open_ for open_, close in BRACKET_PAIRS.items()}
BRACKET_CHARS = set(BRACKET_PAIRS) | set(CLOSING_BRACKETS)
SYNTAX_DEFAULT = "default"
SYNTAX_KEYWORD = "keyword"
SYNTAX_STRING = "string"
SYNTAX_NUMBER = "number"
SYNTAX_COMMENT = "comment"
SYNTAX_BIND = "bind"
SYNTAX_OPERATOR = "operator"

SYNTAX_COLOR_PAIR_BY_KIND = {
    SYNTAX_KEYWORD: COLOR_SYNTAX_KEYWORD,
    SYNTAX_STRING: COLOR_SYNTAX_STRING,
    SYNTAX_NUMBER: COLOR_SYNTAX_NUMBER,
    SYNTAX_COMMENT: COLOR_SYNTAX_COMMENT,
    SYNTAX_BIND: COLOR_SYNTAX_BIND,
    SYNTAX_OPERATOR: COLOR_SYNTAX_OPERATOR,
}

PLAN_COLOR_PAIR_BY_KIND = {
    PLAN_CONNECTOR: COLOR_PLAN_CONNECTOR,
    PLAN_OPERATION: COLOR_PLAN_OPERATION,
    PLAN_OBJECT: COLOR_PLAN_OBJECT,
    PLAN_METRICS: COLOR_PLAN_METRICS,
    PLAN_TEXT: COLOR_PLAN_TEXT,
}

_SYNTAX_COLOR_ATTRS = {
    SYNTAX_KEYWORD: (COLOR_SYNTAX_KEYWORD, curses.A_BOLD),
    SYNTAX_STRING: (COLOR_SYNTAX_STRING, curses.A_BOLD),
    SYNTAX_NUMBER: (COLOR_SYNTAX_NUMBER, curses.A_BOLD),
    SYNTAX_COMMENT: (COLOR_SYNTAX_COMMENT, curses.A_DIM),
    SYNTAX_BIND: (COLOR_SYNTAX_BIND, curses.A_BOLD),
    SYNTAX_OPERATOR: (COLOR_SYNTAX_OPERATOR, curses.A_BOLD),
}

_SYNTAX_FALLBACK_ATTRS = {
    SYNTAX_KEYWORD: curses.A_BOLD,
    SYNTAX_STRING: getattr(curses, "A_UNDERLINE", 0),
    SYNTAX_NUMBER: curses.A_BOLD,
    SYNTAX_COMMENT: curses.A_DIM,
    SYNTAX_BIND: curses.A_BOLD,
    SYNTAX_OPERATOR: curses.A_BOLD,
}


def syntax_color_palette(editor_colors: dict[str, int] | None = None) -> dict[int, int]:
    if getattr(curses, "COLORS", 0) >= 256:
        palette = {
            COLOR_SYNTAX_KEYWORD: 33,
            COLOR_SYNTAX_STRING: 114,
            COLOR_SYNTAX_NUMBER: 214,
            COLOR_SYNTAX_COMMENT: 244,
            COLOR_SYNTAX_BIND: 177,
            COLOR_SYNTAX_OPERATOR: 250,
        }
    elif getattr(curses, "COLORS", 0) >= 16:
        palette = {
            COLOR_SYNTAX_KEYWORD: 14,
            COLOR_SYNTAX_STRING: 10,
            COLOR_SYNTAX_NUMBER: 11,
            COLOR_SYNTAX_COMMENT: 12,
            COLOR_SYNTAX_BIND: 13,
            COLOR_SYNTAX_OPERATOR: 15,
        }
    else:
        comment_color = curses.COLOR_BLUE if getattr(curses, "COLORS", 0) >= 8 else curses.COLOR_CYAN
        palette = {
            COLOR_SYNTAX_KEYWORD: curses.COLOR_YELLOW,
            COLOR_SYNTAX_STRING: curses.COLOR_GREEN,
            COLOR_SYNTAX_NUMBER: curses.COLOR_CYAN,
            COLOR_SYNTAX_COMMENT: comment_color,
            COLOR_SYNTAX_BIND: getattr(curses, "COLOR_MAGENTA", curses.COLOR_RED),
            COLOR_SYNTAX_OPERATOR: curses.COLOR_WHITE,
        }
    color_count = getattr(curses, "COLORS", 0)
    for kind, color in (editor_colors or {}).items():
        pair_number = SYNTAX_COLOR_PAIR_BY_KIND.get(kind)
        if pair_number is not None and 0 <= color < color_count:
            palette[pair_number] = color
    return palette


def configured_plan_color_pairs(explain_colors: dict[str, int] | None = None) -> dict[str, tuple[int, int]]:
    color_count = getattr(curses, "COLORS", 0)
    pair_count = getattr(curses, "COLOR_PAIRS", 0)
    pairs: dict[str, tuple[int, int]] = {}
    for kind, color in (explain_colors or {}).items():
        pair_number = PLAN_COLOR_PAIR_BY_KIND.get(kind)
        if pair_number is not None and 0 <= color < color_count and pair_number < pair_count:
            pairs[kind] = (pair_number, color)
    return pairs


BROWSER_GROUP_LABELS = {
    "TABLE": "Tables",
    "VIEW": "Views",
    "PROCEDURE": "Procedures",
    "FUNCTION": "Functions",
    "PACKAGE": "Packages",
    "TRIGGER": "Triggers",
    "SEQUENCE": "Sequences",
    "INDEX": "Indexes",
    "SYNONYM": "Synonyms",
}
COMPLETION_SCHEMA_OBJECT_TYPES = ("TABLE", "VIEW", "PROCEDURE", "FUNCTION", "PACKAGE")
SQL_KEYWORDS = {
    "add",
    "alter",
    "and",
    "as",
    "asc",
    "begin",
    "between",
    "body",
    "by",
    "case",
    "commit",
    "connect",
    "create",
    "declare",
    "delete",
    "desc",
    "distinct",
    "drop",
    "else",
    "elsif",
    "end",
    "exception",
    "execute",
    "exists",
    "fetch",
    "for",
    "from",
    "function",
    "grant",
    "group",
    "having",
    "if",
    "in",
    "insert",
    "intersect",
    "into",
    "is",
    "join",
    "left",
    "like",
    "loop",
    "minus",
    "not",
    "null",
    "on",
    "or",
    "order",
    "over",
    "package",
    "partition",
    "procedure",
    "raise",
    "replace",
    "return",
    "right",
    "rollback",
    "select",
    "set",
    "start",
    "table",
    "then",
    "trigger",
    "type",
    "union",
    "update",
    "values",
    "view",
    "when",
    "where",
    "while",
    "with",
}
SQL_KEYWORDS.update(
    {
        "all",
        "any",
        "array",
        "at",
        "authid",
        "autonomous_transaction",
        "binary_double",
        "binary_float",
        "binary_integer",
        "blob",
        "boolean",
        "bulk",
        "char",
        "close",
        "clob",
        "collect",
        "constant",
        "continue",
        "current",
        "current_user",
        "cursor",
        "date",
        "definer",
        "deterministic",
        "double",
        "exit",
        "false",
        "forall",
        "goto",
        "immediate",
        "index",
        "interval",
        "json",
        "long",
        "nchar",
        "nocopy",
        "number",
        "nvarchar2",
        "of",
        "open",
        "out",
        "others",
        "parallel_enable",
        "pipelined",
        "pls_integer",
        "pragma",
        "raw",
        "real",
        "record",
        "ref",
        "returning",
        "rowid",
        "rowtype",
        "savepoint",
        "sqlcode",
        "sqlerrm",
        "subtype",
        "sys_refcursor",
        "timestamp",
        "true",
        "urowid",
        "using",
        "varchar",
        "varchar2",
        "xmltype",
    }
)
PLSQL_ATTRIBUTES = {
    "%bulk_exceptions",
    "%bulk_rowcount",
    "%found",
    "%isopen",
    "%notfound",
    "%rowcount",
    "%rowtype",
    "%type",
}


HELP_SECTIONS = [
    (
        "GLOBAL",
        [
            ("Alt-O", "Open commands menu"),
            ("F1", "Show this help"),
            ("F6", "Toggle DBMS_OUTPUT/results"),
            ("F7", "Cycle grid/editor/split layout"),
            ("F8", "Toggle grid/row detail"),
            ("F9", "Show/focus/hide schema browser"),
            ("F12", "Choose transaction mode"),
            ("Ctrl-Up/Down", "Scroll focused pane/output by line"),
            ("Ctrl-W", "Close current file tab"),
            ("Ctrl-PageUp/Down", "Page focused results; otherwise switch tabs"),
            ("Alt-1..Alt-9", "Jump to visible file tab"),
            ("Ctrl-Q", "Quit"),
            ("Ctrl-C while running", "Interrupt database operation"),
            ("Ctrl-Alt-C/R", "Commit / rollback transaction"),
        ],
    ),
    (
        "EDITOR",
        [
            ("Printable text", "Insert text"),
            ("Arrow keys", "Move cursor"),
            ("Shift-Arrow", "Select SQL text"),
            ("Home / End", "Line start/end"),
            ("Shift-Home/End", "Select to line start/end"),
            ("Ctrl-Home/End", "File start/end"),
            ("Ctrl-Shift-Home/End", "Select to file start/end"),
            ("PageUp/PageDown", "Move by page"),
            ("Shift-PageUp/Down", "Select by page"),
            ("Ctrl-Left/Right", "Move one word"),
            ("Ctrl-Shift-Left/Right", "Select one word"),
            ("Backspace / Delete", "Delete before/at cursor"),
            ("Ctrl-Backspace/Delete", "Delete previous/next word"),
            ("Enter", "Insert newline"),
            ("Tab", "Focus results/DBMS_OUTPUT"),
            ("Shift-Tab", "Autocomplete keywords/objects/columns"),
            ("F2 / Ctrl-S", "Save buffer"),
            ("F3 / Ctrl-O", "Open file"),
            ("F4", "New template"),
            ("F5/Ctrl-Enter/Alt-X", "Execute selection/current stmt"),
            ("F11", "Execute selection/buffer script"),
            ("Alt-G", "Generate SELECT/INSERT/UPDATE with columns"),
            ("Alt-+", "Refresh autocomplete cache"),
            ("Alt-R", "Rename current buffer"),
            ("Ctrl-T", "New file tab"),
            ("Ctrl-R", "Refresh workspace file list"),
            ("Ctrl-E", "Explain current statement"),
            ("Ctrl-B", "Toggle -- comment"),
            ("Ctrl-F", "Find literal text"),
            ("Ctrl-G", "Go to line"),
            ("Ctrl-N/P", "Next/previous search match"),
            ("Ctrl-U/L", "Upper/lowercase selection"),
            ("Ctrl-C/X/V", "Copy/cut/paste"),
            ("Ctrl-Z/Y", "Undo/redo"),
            ("Ctrl+=", "Reconnect"),
        ],
    ),
    (
        "RESULTS AND EXPLAIN PLAN",
        [
            ("Esc / Tab", "Return to editor"),
            ("Arrow keys", "Move through result cells"),
            ("PageUp/PageDown", "Move result page; fetch at loaded end"),
            ("Home / End", "First/last result column"),
            ("Ctrl-Home/End", "First/last result row"),
            ("F8", "Toggle grid/row detail"),
            ("F10", "View full selected cell"),
            ("Enter", "Edit ROWID-backed cell when available"),
            ("INS", "Prepare draft row for ROWID-backed insert"),
            ("Ctrl-Alt-C", "Insert active draft row"),
            ("Explain Up/Down", "Scroll explain-plan lines"),
            ("Explain PageUp/Down", "Scroll explain plan by page"),
            ("Explain Home/End", "First/last explain-plan line"),
        ],
    ),
    (
        "SCHEMA BROWSER",
        [
            ("Object groups", "Tables, views, procedures, functions, packages"),
            ("", "Triggers, sequences, indexes, synonyms"),
            ("Printable text", "Filter object names"),
            ("Backspace", "Delete final filter character"),
            ("Esc", "Clear filter or return to editor"),
            ("Up / Down", "Move through browser entries"),
            ("PageUp/PageDown", "Move through entries by page"),
            ("Enter", "Expand group or load definition"),
            ("Space", "Expand group or extend active filter"),
            ("Ctrl-R", "Refresh database objects"),
        ],
    ),
    (
        "CELL VIEWER",
        [
            ("Esc / Enter / F10", "Close viewer"),
            ("Up / Down", "Scroll cell text"),
            ("PageUp/PageDown", "Scroll cell text by page"),
            ("Home / End", "First/last cell-viewer line"),
        ],
    ),
    (
        "PROMPTS / PICKERS",
        [
            ("Printable text", "Type into prompts or filter pickers"),
            ("Backspace", "Delete prompt text or picker filter"),
            ("Enter", "Accept prompt or picker item"),
            ("Esc / Ctrl-Q", "Cancel prompt or picker"),
            ("Up / Down", "Move through picker options"),
            ("PageUp/PageDown", "Move through picker options by page"),
        ],
    ),
]
HELP_TIPS = [
    "Use / on a line by itself after PL/SQL blocks when running a script.",
    "Editable result grids require a ROWID-backed single-table query.",
    "Shift-Tab completes keywords, schema objects, and table/view columns.",
    "Read-only mode is a client-side guardrail; use a least-privileged database account for enforcement.",
]


TEMPLATES = {
    "SQL select": "select *\nfrom dual;\n",
    "Anonymous block": "begin\n  null;\nend;\n/\n",
    "Anonymous block with diagnostics": (
        "begin\n"
        "  null;\n"
        "exception\n"
        "  when others then\n"
        "    dbms_output.put_line(\n"
        "      'Error raised in: ' || nvl($$plsql_unit, '<anonymous>') ||\n"
        "      ' at line ' || $$plsql_line || ' - ' || sqlerrm\n"
        "    );\n"
        "    dbms_output.put_line(dbms_utility.format_error_backtrace);\n"
        "    raise;\n"
        "end;\n"
        "/\n"
    ),
    "Procedure": "create or replace procedure new_procedure as\nbegin\n  null;\nend;\n/\n",
    "Function": "create or replace function new_function return varchar2 as\nbegin\n  return 'ok';\nend;\n/\n",
    "Package": (
        "create or replace package new_package as\n"
        "  procedure run;\n"
        "end new_package;\n"
        "/\n\n"
        "create or replace package body new_package as\n"
        "  procedure run as\n"
        "  begin\n"
        "    null;\n"
        "  end run;\n"
        "end new_package;\n"
        "/\n"
    ),
}
