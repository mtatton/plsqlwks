"""Render immutable, display-ready query results as safe standalone HTML.

This module is deliberately independent of Oracle, curses, application state,
and the Plugin API.  Every value supplied by a result snapshot is treated as
untrusted text and escaped before it is inserted into the document.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from html import escape
from io import StringIO

from .exporting import raise_if_export_cancelled

DEFAULT_HTML_TITLE = "PLSQLWKS query result"

_COMMON_STYLESHEET = """\
:root { font-family: system-ui, sans-serif; }
body { margin: 1.5rem; line-height: 1.4; }
.notice { padding: 0.75rem; border: 1px solid currentColor; }
.table-container { max-width: 100%; overflow-x: auto; }
table { border-collapse: collapse; min-width: 100%; }
th, td { border: 1px solid; padding: 0.35rem 0.55rem; text-align: left; vertical-align: top; }
th { font-weight: 600; }
td { white-space: pre-wrap; }
"""

_BRIGHT_STYLESHEET = """\
:root { color-scheme: light; }
body { background: #fff; color: #202124; }
.notice { background: #fff8d6; }
th, td { border-color: #777; }
th { background: #f1f3f4; }
"""

_DARK_STYLESHEET = """\
:root { color-scheme: dark; }
body { background: #17191d; color: #f1f3f4; }
.notice { background: #302b19; }
th, td { border-color: #777c87; }
th { background: #292c33; }
"""

_PRINT_STYLESHEET = """\
@media print {
  :root { color-scheme: light; }
  body { margin: 0; background: #fff; color: #000; }
  .notice, th { background: #fff; color: #000; }
  th, td { border-color: #666; }
  .table-container { overflow: visible; }
  table { min-width: 0; }
  tr { break-inside: avoid; }
}
"""

_THEME_STYLESHEETS = {
    "bright": _BRIGHT_STYLESHEET,
    "dark": _DARK_STYLESHEET,
}


def _validate_row_widths(
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Reject rows that cannot be represented by the declared table shape."""
    expected = len(columns)
    for index, row in enumerate(rows, start=1):
        raise_if_export_cancelled(cancelled)
        actual = len(row)
        if actual != expected:
            raise ValueError(f"result row {index} has {actual} value(s); expected {expected}")


def render_html_result(
    *,
    title: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    has_more: bool,
    theme: str = "bright",
    on_progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """Return a complete HTML5 document for exactly the supplied rows.

    The result is deterministic apart from its inputs and uses LF line endings.
    Row widths and the theme name are validated before any markup is returned.
    The document title, column labels, and cells are escaped with attribute-safe
    HTML escaping even though result data is never placed in an attribute.  The
    result title is metadata only and is not repeated as a visible page heading.
    Themes select only bundled static CSS; result values can never affect CSS.
    """
    _validate_row_widths(columns, rows, cancelled)
    try:
        theme_stylesheet = _THEME_STYLESHEETS[theme]
    except KeyError:
        raise ValueError("HTML export theme must be 'bright' or 'dark'") from None
    display_title = title if title else DEFAULT_HTML_TITLE
    escaped_title = escape(display_title, quote=True)
    raise_if_export_cancelled(cancelled)
    if on_progress is not None:
        on_progress(0, len(rows))

    output = StringIO()
    output.write("<!doctype html>\n")
    output.write('<html lang="en">\n')
    output.write("<head>\n")
    output.write('  <meta charset="utf-8">\n')
    output.write('  <meta name="viewport" content="width=device-width, initial-scale=1">\n')
    output.write(f"  <title>{escaped_title}</title>\n")
    output.write("  <style>\n")
    output.write(_COMMON_STYLESHEET)
    output.write(theme_stylesheet)
    output.write(_PRINT_STYLESHEET)
    output.write("  </style>\n")
    output.write("</head>\n")
    output.write("<body>\n")
    output.write('  <div class="table-container">\n')
    output.write("    <table>\n")
    output.write("      <thead>\n")
    output.write("        <tr>\n")
    for column in columns:
        raise_if_export_cancelled(cancelled)
        output.write(f'          <th scope="col">{escape(column, quote=True)}</th>\n')
    output.write("        </tr>\n")
    output.write("      </thead>\n")
    output.write("      <tbody>\n")
    for row_index, row in enumerate(rows, start=1):
        raise_if_export_cancelled(cancelled)
        output.write("        <tr>\n")
        for value in row:
            raise_if_export_cancelled(cancelled)
            output.write(f"          <td>{escape(value, quote=True)}</td>\n")
        output.write("        </tr>\n")
        if on_progress is not None:
            on_progress(row_index, len(rows))
    output.write("      </tbody>\n")
    output.write("    </table>\n")
    output.write("  </div>\n")
    output.write(f'  <p class="summary">{len(rows)} row(s)</p>\n')
    if has_more:
        output.write('  <p class="notice">Additional rows are available in PLSQLWKS and were not exported.</p>\n')
    output.write("</body>\n")
    output.write("</html>\n")
    raise_if_export_cancelled(cancelled)
    return output.getvalue()
