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
starts. Its optional `numeric_values` matrix carries only aligned source
`Decimal`, `int`, and `float` values; other cells are `None`, and legacy
snapshots may omit the matrix. No raw rows or Oracle handles cross the plugin
boundary. `has_more` reports that additional rows exist without exposing a
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

The bundled CSV, HTML, and XLSX commands are normal built-in `Plugin` objects.
Their standalone handlers use only the same loaded snapshot exposed by API v1.
In the application, a private host coordinator adds the loaded/full choice,
background progress, and cancellation without exposing Oracle handles or
widening Plugin API v1. When enabled, the commands are registered in that order
in the **Results** section and have no global keyboard shortcuts.

### Exporter availability

Each bundled exporter has an independent switch in the active `config.ini`:

```ini
[plugin.csv-export]
enabled = yes

[plugin.html-export]
enabled = yes

[plugin.xlsx-export]
enabled = yes
```

Set `enabled = no` to omit that exporter's command from the **Results** menu.
A missing or malformed value defaults to enabled. These switches control only
the bundled exporters; installed entry-point plugins are still discovered and
loaded normally. Restart PLSQLWKS after changing an availability switch.

Each command opens an **Export rows** picker. **Loaded rows only (default)** is
selected first and never fetches a continuation page. **All available rows
(keep the result grid unchanged)** fetches every continuation page into a
private export buffer with no 10,000-row cap; the grid retains only the rows it
already displayed. Escape cancels the picker. The status bar shows the prepared
row count while fetching and determinate write progress. `Ctrl-C` cancels either
active phase: an interrupted fetch preserves the original grid rows but detaches
the cursor and editing state, while a cancelled file phase leaves Oracle and
transaction state alone. Atomic replacement leaves an existing destination
unchanged in either case. An active insert draft must first be committed or
cancelled.

### CSV export

Choose **Alt-O -> Results -> Export result to CSV** to export the rows selected
by the shared mode picker. The command proposes a timestamped name in the workspace
`results/` directory, accepts relative or absolute paths, and asks before
replacing an existing file. It writes UTF-8 CSV with a header and the selected
display rows. Export is available in read-only mode.

The shared CSV writer writes a temporary sibling file and atomically replaces
the destination after a successful close. A failed write therefore does not
turn an existing destination into an apparently successful partial export.

### HTML export

Choose **Alt-O -> Results -> Export result to HTML** to write a complete,
standalone UTF-8 HTML5 document for the selected rows. The command
proposes a timestamped `.html` name in the workspace `results/` directory,
accepts relative or absolute paths, and asks before replacing an existing file.
It writes the headers and selected display rows, followed by the exported-row
count and a notice when additional rows remain available. The result title is
retained only as browser-tab document metadata and is not repeated as a visible
heading.

The result-derived document title, column names, and cell values are escaped as
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
  `<NULL>` display token. It defaults to empty and may be set to `<NULL>` or
  another marker.
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

Choose **Alt-O -> Results -> Export result to XLSX** to write one `Query result`
worksheet containing a header and the selected result rows.
The command proposes a timestamped `.xlsx` name in the workspace `results/`
directory, accepts relative or absolute paths, and asks before replacing an
existing file. The format writer starts no subprocess and does not open the
resulting workbook. The command remains available in read-only mode.

Headers and text cells are explicitly stored as strings. Genuine source
numbers become native Excel numeric cells only when they are finite, within
Excel's supported range, and contain at most 15 significant digits. Fixed-point
formats preserve visible scale such as `10.50`; excess-precision values and
numeric-looking character data remain exact text and may retain Excel's
number-as-text warning. Values beginning with `=`, `+`, `-`, or `@` remain
literal result text and are never interpreted as spreadsheet formulas. The
workbook contains no macros or external links. Its snapshot title is stored
only as document metadata; result data remains confined to the single
worksheet. Each column uses the larger of its bold column name or widest data
value, estimated with Calibri 11-compatible proportional glyph widths and
clamped from 3 through 60 units. A 17-pixel fit margin prevents spreadsheet
font rendering from clipping the last characters. Wrapping remains based on
logical visual length: values over 60 visual units or containing explicit line
breaks are wrapped.

XLSX generation uses the optional `openpyxl>=3.1` dependency. It is not a base
PLSQLWKS runtime dependency. Install a distribution with XLSX support through
the standard extra:

```bash
python3 -m pip install 'plsqlwks[xlsx]'
```

For an editable development checkout, use `python3 tools/dev.py install --xlsx`. The
repository keeps `plugin-requirements/xlsx-export/requirements.txt` as a
compatible fallback for existing source-checkout automation.

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
PLSQLWKS_XLSX_EXPORT_AUTO_FILTER="no"
PLSQLWKS_XLSX_EXPORT_AUTO_WIDTH="no"
PLSQLWKS_XLSX_EXPORT_FREEZE_TOP_ROW="no"
```

- `PLSQLWKS_XLSX_EXPORT_NULL_VALUE` replaces values exactly equal to the grid's
  `<NULL>` display token. It defaults to empty and may be set to `<NULL>` or
  another marker.
- `PLSQLWKS_XLSX_EXPORT_THEME` selects the bundled `bright` or `dark` cell
  styles and defaults to `bright`.
- `PLSQLWKS_XLSX_EXPORT_DATE_FORMAT` accepts Python `strftime` directives and
  defaults to empty, preserving displayed values.
- `PLSQLWKS_XLSX_EXPORT_AUTO_FILTER` controls Excel's column filter controls.
  It defaults to enabled and accepts case-insensitive, whitespace-tolerant
  `1`, `yes`, `true`, or `on` to enable them and `0`, `no`, `false`, or `off`
  to disable them. An unset or malformed value falls back to enabled.
- `PLSQLWKS_XLSX_EXPORT_AUTO_WIDTH` controls proportional column sizing from
  the widest header or exported data value. With filtering enabled, the header
  candidate includes three extra character units for the filter dropdown. It
  accepts the same boolean values, defaults to enabled, and leaves Excel's
  default column widths when disabled.
- `PLSQLWKS_XLSX_EXPORT_FREEZE_TOP_ROW` keeps the first-row header visible
  while scrolling. It accepts the same boolean values, defaults to enabled,
  and leaves the worksheet unfrozen when disabled.

When enabled, the auto-filter covers exactly the header row and exported rows.
It applies no filter criteria, so no data rows are initially
hidden. Disabling automatic widths does not change cell wrapping: multiline
values and values above the 60-character-unit wrapping threshold still wrap.
Freezing is independent of the auto-filter and automatic-width settings.

NULL and date transformations follow the same display-string limitations as
CSV and HTML and remain literal spreadsheet strings. Numeric provenance is
independent of these settings. These variables configure only the bundled XLSX
plugin and are not a generic Plugin API v1 settings mechanism. Code embedding
its factory may pass an immutable `XlsxExportOptions` value; installed
entry-point factories remain zero-argument callables.

### CSV export configuration

The built-in CSV plugin reads these host-owned formatting settings from the
active `config.ini`:

```ini
[plugin.csv-export]
separator = ,
null_value =
date_format =
protect_formulas = no
```

- `separator` is one character and defaults to `,`.
- `null_value` is written for values that exactly equal the result grid's
  `<NULL>` display token. It defaults to empty and may be set to `<NULL>` or
  another marker.
- `date_format` uses Python `strftime` directives. It defaults to empty, which
  preserves the displayed value.
- `protect_formulas` accepts normal INI boolean values and defaults to `no`.
  When enabled, the plugin protects both headers and formatted data fields
  that begin with spreadsheet formula-triggering ASCII, control, or full-width
  characters. It prefixes those values with a tab and quotes every field so
  the tab remains inside its CSV cell. Missing or malformed values fall back
  to `no`.

Date formatting is deliberately conservative because Plugin API snapshots
contain display strings rather than Oracle type metadata. It is applied only
to valid, full-string ISO display values shaped as `YYYY-MM-DD` or
`YYYY-MM-DD HH:MM:SS[.digits][+/-HH:MM]`, with one to six fractional digits.
Invalid dates, ISO values using other shapes, and all nonmatching text remain
unchanged. A character value with the same valid shape is indistinguishable
from a date and is formatted too. The directives supported by `strftime` can
vary slightly by platform.

Formula protection is intended for human spreadsheet viewing. It deliberately
turns leading signed values such as `-42` into text and exposes the added tab
to programmatic CSV readers. There is no universal neutralization across all
spreadsheet applications or save/re-open workflows, so use this opt-in mode
for the target spreadsheet workflow rather than as a lossless round trip.

This section configures only the PLSQLWKS-supplied plugin. Plugin API v1 has no
generic configuration schema and does not expose these settings to installed
third-party plugins.

## Optional plugin tests

Plugin-specific tests are excluded from the default core test run. Run the
deterministic plugin profile explicitly with:

```bash
python3 tools/dev.py test plugins
```

Compatibility manifests for repository plugins remain under
`plugin-requirements/<plugin-id>/requirements.txt`. The built-in `csv-export`
and `html-export` plugins have no extra dependencies beyond PLSQLWKS and the
Python standard library. The built-in `xlsx-export` plugin uses the standard
`xlsx` extra for `openpyxl>=3.1`; its manifest is retained as a synchronized
source-checkout fallback.
