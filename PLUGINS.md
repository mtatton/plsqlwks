# PLSQLWKS plugins

PLSQLWKS Plugin API version 1 is a small, command-only extension point. A plugin
can add commands to the Alt-O menu and use the limited `PluginContext` passed to
its command handler. Installed plugins are trusted, in-process Python code. They
are **not sandboxed** and should be installed only from sources you trust.

## API version 1

Import supported types and constants from `plsqlwks.plugins`:

- `Plugin` describes a plugin and its commands.
- `PluginCommand` describes one searchable Alt-O menu command.
- `PluginContext` is the host interface supplied to a command handler.
- `ResultSnapshot` is an immutable, tuple-based copy of the active table result.
- `PluginHandler` and `PluginFactory` are the corresponding callable types.
- `PLUGIN_API_VERSION` and `PLUGIN_ENTRY_POINT_GROUP` identify the contract.

`PluginContext` can read the configured results directory, inspect the active
result snapshot, detect an active result-grid insert draft, open a text prompt,
request overwrite confirmation, set status text, and report an error through
the UI. A snapshot contains exactly the display rows loaded when the command
starts. `has_more` reports that additional rows exist without exposing a
continuation or a way to fetch them.

API v1 does not provide database execution, mutable query results, application
or UI state, curses drawing, arbitrary shortcuts, event or lifecycle hooks,
background jobs, generic third-party plugin settings, hot reload, or
workspace-local Python-file loading.

## Registering an installed plugin

Expose a zero-argument factory through the `plsqlwks.plugins` entry-point group:

```toml
[project.entry-points."plsqlwks.plugins"]
example = "example_package.plugin:create_plugin"
```

A minimal `example_package/plugin.py` is:

```python
from plsqlwks.plugins import Plugin, PluginCommand, PluginContext


def show_results_directory(context: PluginContext) -> None:
    context.set_status(f"Results directory: {context.results_dir}")


def create_plugin() -> Plugin:
    return Plugin(
        id="example",
        name="Example commands",
        commands=(
            PluginCommand(
                id="show-results-directory",
                section="Workspace",
                title="Show results directory",
                handler=show_results_directory,
                keywords="example results directory",
            ),
        ),
    )
```

Plugin IDs must match the safe pattern `^[a-z][a-z0-9_.-]*$` and are globally
unique. Command IDs must be nonempty and unique within a plugin. The API version
must match the host version. Built-in plugins load first, followed by installed
entry points in deterministic name order. A broken or incompatible installed
plugin is skipped and reported as a startup warning rather than preventing
PLSQLWKS from starting. An ordinary exception raised by a command is reported
through the UI instead of terminating the application.

## Built-in result exports

The bundled CSV, HTML, and XLSX commands are normal built-in `Plugin` objects
using the same API v1 context available to installed plugins. They are
registered in that order in the **Results** section and have no global keyboard
shortcuts. None is an App method, and adding export formats requires no public
API expansion.

### CSV export

Choose **Alt-O -> Results -> Export loaded rows to CSV** to export the active
table snapshot. The command proposes a timestamped name in the workspace
`results/` directory, accepts relative or absolute paths, and asks before
replacing an existing file. It writes UTF-8 CSV with a header and exactly the
currently loaded display rows; it never fetches a continuation page. Export is
available in read-only mode. An active insert draft must first be committed or
cancelled so its temporary row cannot be exported.

The shared CSV writer writes a temporary sibling file and atomically replaces
the destination after a successful close. A failed write therefore does not
turn an existing destination into an apparently successful partial export.

### HTML export

Choose **Alt-O -> Results -> Export loaded rows to HTML** to write a complete,
standalone UTF-8 HTML5 document for the active table snapshot. The command
proposes a timestamped `.html` name in the workspace `results/` directory,
accepts relative or absolute paths, and asks before replacing an existing file.
It writes the visible result title, loaded-row count, headers, and exactly the
currently loaded display rows. A notice identifies when additional rows remain
available, but the plugin never fetches them.

All result-derived titles, column names, and cell values are escaped as
untrusted text. The document has a small static embedded stylesheet and no
JavaScript, event handlers, forms, frames, images, remote URLs, or external
resources. Cell whitespace remains readable, and wide tables scroll
horizontally. The command does not launch a browser.

Like CSV export, HTML export is available in read-only mode and rejects an
active insert draft before reading the result or opening a filename prompt. It
uses the existing context methods for draft detection, immutable snapshot
access, the results directory, text and overwrite prompts, status updates, and
error reporting. Atomic replacement keeps a previous destination intact when
rendering, writing, or replacement fails.

### HTML export configuration

The built-in HTML plugin captures these optional environment variables when
PLSQLWKS loads it:

```bash
PLSQLWKS_HTML_EXPORT_NULL_VALUE="(null)"
PLSQLWKS_HTML_EXPORT_THEME="dark"
PLSQLWKS_HTML_EXPORT_DATE_FORMAT="%d.%m.%Y"
```

- `PLSQLWKS_HTML_EXPORT_NULL_VALUE` replaces values exactly equal to the grid's
  `<NULL>` display token. Its default is `<NULL>`, and an empty value is valid.
- `PLSQLWKS_HTML_EXPORT_THEME` selects the bundled static `bright` or `dark`
  stylesheet. It defaults to `bright`; print rules remain bright and readable.
- `PLSQLWKS_HTML_EXPORT_DATE_FORMAT` accepts Python `strftime` directives. It
  defaults to empty, preserving displayed values.

The HTML date option uses the same strict ISO-display matching documented for
CSV: snapshots have display strings, not Oracle type metadata, so invalid or
nonmatching text is preserved and a text value having the recognized shape is
indistinguishable from a date. These are settings of the bundled plugin, not a
generic Plugin API v1 configuration facility. Code embedding the built-in
factory may instead pass an immutable `HtmlExportOptions` instance explicitly;
installed third-party entry-point factories remain zero-argument callables.

### XLSX export

Choose **Alt-O -> Results -> Export loaded rows to XLSX** to write one `Query
result` worksheet containing a header and exactly the loaded result snapshot.
The command proposes a timestamped `.xlsx` name in the workspace `results/`
directory, accepts relative or absolute paths, and asks before replacing an
existing file. It does not fetch continuation rows, access the database, start
a subprocess, or open the resulting workbook. It remains available in
read-only mode and rejects an active insert draft before reading the snapshot
or prompting.

Every header and cell value is explicitly stored as a string. Values beginning
with `=`, `+`, `-`, or `@` therefore remain literal result text and are never
interpreted as spreadsheet formulas. The workbook contains no macros or
external links. Its snapshot title is stored only as document metadata; result
data remains confined to the single worksheet.

XLSX generation uses the optional `openpyxl>=3.1` dependency. It is not a
PLSQLWKS runtime dependency. Install the built-in plugin's manifest before
using or testing the command:

```bash
python3 -m pip install -r plugin-requirements/xlsx-export/requirements.txt
```

If the dependency is unavailable, invoking the command reports a concise XLSX
export error through the normal Plugin API UI boundary instead of preventing
PLSQLWKS from starting.

### XLSX export configuration

The built-in XLSX plugin captures these optional environment variables when
PLSQLWKS loads it:

```bash
PLSQLWKS_XLSX_EXPORT_NULL_VALUE="(null)"
PLSQLWKS_XLSX_EXPORT_THEME="dark"
PLSQLWKS_XLSX_EXPORT_DATE_FORMAT="%d.%m.%Y"
```

- `PLSQLWKS_XLSX_EXPORT_NULL_VALUE` replaces values exactly equal to the grid's
  `<NULL>` display token. Its default is `<NULL>`, and an empty value is valid.
- `PLSQLWKS_XLSX_EXPORT_THEME` selects the bundled `bright` or `dark` cell
  styles and defaults to `bright`.
- `PLSQLWKS_XLSX_EXPORT_DATE_FORMAT` accepts Python `strftime` directives and
  defaults to empty, preserving displayed values.

NULL and date transformations follow the same display-string limitations as
CSV and HTML. The resulting values are still stored as literal spreadsheet
strings. These variables configure only the bundled XLSX plugin and are not a
generic Plugin API v1 settings mechanism. Code embedding its factory may pass
an immutable `XlsxExportOptions` value; installed entry-point factories remain
zero-argument callables.

### CSV export configuration

The built-in plugin reads these host-owned settings from the active
`config.ini`:

```ini
[plugin.csv-export]
separator = ,
null_value = <NULL>
date_format =
```

- `separator` is one character and defaults to `,`.
- `null_value` is written for values that exactly equal the result grid's
  `<NULL>` display token. It defaults to `<NULL>` and may be empty.
- `date_format` uses Python `strftime` directives. It defaults to empty, which
  preserves the displayed value.

Date formatting is deliberately conservative because Plugin API snapshots
contain display strings rather than Oracle type metadata. It is applied only
to valid, full-string ISO display values shaped as `YYYY-MM-DD` or
`YYYY-MM-DD HH:MM:SS[.digits][+/-HH:MM]`, with one to six fractional digits.
Invalid dates, ISO values using other shapes, and all nonmatching text remain
unchanged. A character value with the same valid shape is indistinguishable
from a date and is formatted too. The directives supported by `strftime` can
vary slightly by platform.

This section configures only the PLSQLWKS-supplied plugin. Plugin API v1 has no
generic configuration schema and does not expose these settings to installed
third-party plugins.

## Optional plugin tests

Plugin-specific tests are excluded from the default core test run. Run them
explicitly with either:

```bash
python3 -m pytest -q -m plugin
PLSQLWKS_TEST_PLUGINS=1 python3 -m pytest -q
```

Additional requirements for each repository plugin belong in
`plugin-requirements/<plugin-id>/requirements.txt`. Install the relevant file
before its optional tests. The built-in `csv-export` plugin has no extra
dependencies beyond PLSQLWKS and the Python standard library; neither does the
built-in `html-export` plugin. The built-in `xlsx-export` plugin requires
`openpyxl>=3.1`; it remains outside the core runtime dependency set.
