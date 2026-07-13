# plsqlwks

An ncurses SQL and PL/SQL workspace for Oracle databases.

![PLSQLWKS PREVIEW](https://gitlab.com/unununu/plsqlwks/-/raw/main/img/preview.png)

## Connection

The default connection is:

- user: `hr`
- password file: the `orapass` file in the platform user-config directory
- service: `free` on `127.0.0.1:1521`

On Linux, fresh defaults are `~/.config/plsqlwks/orapass` for the password,
`~/.config/plsqlwks/config.ini` for settings, and `~/.local/share/plsqlwks`
for the workspace. macOS and Windows use their corresponding user config and
data directories. An existing `/tmp/orapass` remains a compatibility fallback,
but the app warns about that legacy location and about POSIX permissions other
than `0600`.

Override the defaults with environment variables:

```bash
export ORACLE_USER=hr
export ORACLE_PASSWORD_FILE=~/.config/plsqlwks/orapass
export ORACLE_DSN='127.0.0.1:1521/free'
export PLSQLWKS_WORKSPACE=/path/to/workspace
export PLSQLWKS_MAX_ROWS=200
export PLSQLWKS_ARRAYSIZE=100
```

Password files preserve leading and trailing spaces; only final CR/LF line
endings are removed. On POSIX systems, create the file for the current user and
protect it with `chmod 600`.

`PLSQLWKS_MAX_ROWS` sets the maximum number of rows in each result page, while
`PLSQLWKS_ARRAYSIZE` sets the Oracle cursor array size. Both values must be
positive integers.

## Install

Python 3.10 or newer is required, along with a compatible terminal and a Python
build that provides the standard-library `curses` module.

For development, install the package in editable mode:

```bash
python3 -m pip install -e '.[dev]'
```

`oracledb` is the only runtime dependency. Supporting functionality uses the Python standard library.

For a regular local install, use:

```bash
python3 -m pip install .
```

Run the default test and lint checks from a development checkout with:

```bash
python3 -m pytest -q
python3 -m ruff check .
```

Plugin API and built-in plugin tests are optional and are not part of the
default core test run. Run them with either opt-in form:

```bash
PLSQLWKS_TEST_PLUGINS=1 python3 -m pytest -q
python3 -m pytest -q -m plugin
```

Each built-in plugin has an optional dependency manifest under
`plugin-requirements/<plugin-id>/requirements.txt`. The CSV and HTML export
plugins have no additional dependencies. XLSX export uses `openpyxl`, which is
kept out of the PLSQLWKS runtime dependencies and can be installed with:

```bash
python3 -m pip install -r plugin-requirements/xlsx-export/requirements.txt
```

Oracle integration tests require the connection environment variables shown
above and an accessible password file. Enable every test group with:

```bash
PLSQLWKS_TEST_ORACLE=1 PLSQLWKS_TEST_PLUGINS=1 PLSQLWKS_TEST_PTY=1 PLSQLWKS_TEST_SLOW=1 \
  python3 -m pytest -q -rs
```

## Run

After installation, start the workspace with:

```bash
plsqlwks
```

Select a workspace for one invocation with:

```bash
plsqlwks --workspace /path/to/workspace
```

The CLI option takes precedence over `PLSQLWKS_WORKSPACE`. For compatibility, a
source checkout that already contains a configured `workspace/` continues to
use it and displays a migration notice; files are never moved automatically.

Choose the initial transaction mode for one invocation with `--manual` or
`--autocommit`. These options override `[database] autocommit` from the active
`config.ini`:

```bash
plsqlwks --manual
plsqlwks --autocommit
```

You can also run it directly as a module from the source tree:

```bash
python3 -m plsqlwks
```

The app opens a split-screen terminal workspace:

- editor on top
- results/messages below
- command/status bar at the bottom

The terminal UI uses the active locale for keyboard input and display. If that
locale cannot be initialized, the app tries `C.UTF-8`, `en_US.UTF-8`, and
`UTF-8`. A valid plain `C` locale remains active, so configure a UTF-8 locale in
the shell when non-ASCII input or display is required.

Oracle operations run serially on one persistent background worker. The UI
continues to redraw and accept input while an operation runs, but it rejects a
second database operation until the active one finishes. `Ctrl-C` requests
interruption of the active operation. Reconnecting closes open result pages and
clears query results loaded by the previous connection. If a manual transaction
has pending changes, reconnect first asks whether to commit, roll back, discard
the session, or cancel. Commit or rollback must succeed before reconnect closes
the old connection. If either action fails, plsqlwks does not close the current
connection or clear its loaded results; reconnect can be tried again with an
explicit discard. Discard closes the session without resolving the transaction
explicitly, so Oracle rolls its uncommitted work back.
If the old connection is already dead and reports an error while closing,
plsqlwks still attempts the replacement connection and reports the close error
as a warning after a successful reconnect.

After a successful quit, plsqlwks records the open file-backed tabs, active tab, and cursor positions in the managed `[session.tabs]` section of `config.ini`. Fresh platform-default workspaces use the user-config directory; explicitly selected and legacy workspaces keep `config.ini` inside the workspace for compatibility. On the next start the app tries each saved path independently, silently skips files that are missing or unreadable, and leaves the initial empty tab in place when none can be opened. A saved cursor position that no longer exists in its file starts at the beginning instead. Untitled, template, and generated schema tabs are not persisted because they do not have a file to reopen.

## Keys

### Global

| Key | Action |
| --- | --- |
| `Alt-O` | Open the top-left commands menu |
| `F1` | Help |
| `F6` | Switch between DBMS_OUTPUT and normal results |
| `F7` | Cycle fullscreen grid, fullscreen editor, and 2/3 editor + 1/3 grid |
| `F8` | Toggle result grid / row-detail output |
| `F9` | Show/focus/hide schema browser |
| `F12` | Choose autocommit or manual transaction mode |
| `Ctrl-Up` / `Ctrl-Down` | Scroll the focused pane or visible DBMS_OUTPUT by one line |
| `Ctrl-W` | Close current file tab |
| `Ctrl-PageUp` / `Ctrl-PageDown` | Scroll focused results by one page; otherwise switch file tabs |
| `Alt-1`..`Alt-9` | Jump to visible file tab |
| `Ctrl-Q` | Quit |
| `Ctrl-C` while running | Interrupt the active database operation |
| `Ctrl-Alt-C` | Insert the active result-grid draft, or commit when no draft is active |
| `Ctrl-Alt-R` | Roll back the current transaction |

### Editor

| Key | Action |
| --- | --- |
| Printable text | Insert text |
| Arrow keys | Move the cursor |
| `Shift-Arrow` | Select SQL text |
| `Home` / `End` | Move to the beginning/end of the current line |
| `Shift-Home` / `Shift-End` | Select to the beginning/end of the current line |
| `Ctrl-Home` / `Ctrl-End` | Move to the beginning/end of the editor buffer |
| `Ctrl-Shift-Home` / `Ctrl-Shift-End` | Select to the beginning/end of the editor buffer |
| `PageUp` / `PageDown` | Move by page |
| `Shift-PageUp` / `Shift-PageDown` | Select by page |
| `Ctrl-Left` / `Ctrl-Right` | Move one word |
| `Ctrl-Shift-Left` / `Ctrl-Shift-Right` | Select one word |
| `Backspace` | Delete before the cursor |
| `Delete` | Delete at the cursor |
| `Ctrl-Backspace` / `Ctrl-Delete` | Delete the previous/next word |
| `Enter` | Insert a new line |
| `Tab` | Focus the results or DBMS_OUTPUT pane |
| `Shift-Tab` | Autocomplete keywords, schema objects, and columns |
| `F2` / `Ctrl-S` | Save buffer |
| `F3` / `Ctrl-O` | Open file |
| `F4` | New template |
| `F5` / `Ctrl-Enter` / `Alt-X` | Execute selected SQL or current statement |
| `F11` | Execute selected SQL or whole buffer as a script |
| `Alt-G` | Generate SELECT, INSERT, or UPDATE with table columns |
| `Alt-+` | Refresh autocomplete metadata cache |
| `Alt-R` | Rename current buffer |
| `Ctrl-T` | New file tab |
| `Ctrl-R` | Refresh workspace file list |
| `Ctrl-E` | Explain current statement |
| `Ctrl-B` | Toggle `-- ` comment on the current line or selected lines |
| `Ctrl-F` | Find literal text |
| `Ctrl-G` | Go to line |
| `Ctrl-N` / `Ctrl-P` | Move to the next / previous search occurrence |
| `Ctrl-U` | Uppercase selected SQL code |
| `Ctrl-L` | Lowercase selected SQL code |
| `Ctrl-C` | Copy selected text |
| `Ctrl-X` | Cut selected text |
| `Ctrl-V` | Paste clipboard text |
| `Ctrl-Z` / `Ctrl-Y` | Undo / redo |
| `Ctrl+=` | Reconnect |

Use `/` on a line by itself after PL/SQL objects or anonymous blocks when running a script.
Execution and explain errors report the mapped editor line and column, then move the cursor to that location when Oracle provides one.
When executed or explained SQL contains bind variables such as `:id`, plsqlwks opens a text box for each value and sends the answers as Oracle bind parameters.
Unquoted bind names are case-insensitive, so `:id` and `:ID` share one prompt and value. Quoted bind names remain case-sensitive.
Set `[database] remember_bind_values = yes` in the active `config.ini` to prefill future bind prompts with values entered earlier in the same app session.

Editor search is literal and case-insensitive. `Ctrl-F` prompts for text and selects the found occurrence; `Ctrl-N` and `Ctrl-P` repeat the last search forward or backward with wraparound.

Editor autocomplete is available with `Shift-Tab`. It completes PL/SQL keywords,
current-schema object names, and table/view columns from cached or lazily loaded
database metadata; multiple matches open a picker. Database-object and column
completion currently supports conventional unquoted identifiers; quoted
mixed-case names are not preserved. Use `Alt-+` to reload schema-object metadata
and clear cached column metadata.

Editor syntax and explain-plan colors can be overridden in the active `config.ini` with color names or numeric curses color indexes. Unsupported values fall back to the built-in palette for the current terminal:

```ini
[editor.colors]
keyword = bright-cyan
string = green
number = orange
comment = blue
bind = bright-magenta
operator = white

[explain.colors]
connector = cyan
operation = bright-yellow
object = green
metrics = gray
text = white
```

The bottom status bar starts with `[ ]` when there is no observed uncommitted work and `[*]` when manual mode has pending changes. Commit and rollback messages include the local timestamp and the tracked row count, for example `Committed transaction, 2026-06-12 10:12:15, 7 row(s) changed`. Direct DML and ROWID grid edits contribute exact row counts; PL/SQL blocks can mark the transaction as pending with an unknown row count. Quitting or switching from manual mode to autocommit while changes are pending asks whether to commit, roll back, or cancel. The selected transaction mode is stored under `[database] autocommit` in the active `config.ini`.

Read-only mode is a client-side guardrail that rejects statements which the SQL scanner recognizes as database-writing. Select it with `--read-only`, `--read-write`, or `[database] read_only` in `config.ini`. It is not a security boundary: a function invoked by an otherwise valid `SELECT` can perform work outside the visible statement, including an autonomous transaction. Use an Oracle account with only the required privileges when writes must be prevented by the database. In read-only mode, `Ctrl-E` explains `SELECT` and `WITH` statements by opening a cursor and reading `DBMS_XPLAN.DISPLAY_CURSOR`; direct `EXPLAIN PLAN` remains disabled because it writes to `PLAN_TABLE`.

### Results And Explain Plan

| Key | Action |
| --- | --- |
| `Esc` / `Tab` | Return to the editor |
| Arrow keys | Move through result-grid cells |
| `PageUp` / `PageDown` | Move by a visible page in the result grid |
| `Home` / `End` | Move to the first/last result-grid column |
| `Ctrl-Home` / `Ctrl-End` | Move to the first/last result-grid row |
| `F8` | Toggle result grid / row-detail output |
| `F10` | View the full selected result cell |
| `Enter` | Edit the selected ROWID-backed result cell when available |
| `INS` | Prepare a draft insert row for ROWID-backed results |
| `Ctrl-Alt-C` | Insert the active draft row, or commit the transaction when no draft is active |
| `Up` / `Down` | Scroll explain-plan lines |
| `PageUp` / `PageDown` | Scroll explain-plan lines by page |
| `Home` / `End` | Move to the first/last explain-plan line |

When more result rows are available, `PageDown` at the loaded end fetches and
appends the next result page. For simple queries against one conventional
unquoted current-schema table that include `ROWID`, press `Enter` on a directly
selected unquoted table column to update it by rowid. Press `INS` in the grid to
prepare a draft row at the top of the grid, edit its cells with `Enter`, use
`Ctrl-Alt-C` to insert it, or use `Esc` to cancel it.
Database null values are displayed and entered as `<NULL>` in editable results. Plain `NULL` is stored as literal text.
Grid edits compare the selected cell's originally loaded typed value as well as
the `ROWID`. If another session changes that cell or deletes the row first, the
edit is rejected and the query must be refreshed. Changes to other cells in the
same row do not cause a conflict. NUMBER input uses decimal notation with a
period; DATE and TIMESTAMP input uses ISO
`YYYY-MM-DD[ HH:MM:SS[.ffffff]]`; RAW and BLOB input uses hexadecimal bytes.
Character and CLOB input is preserved as entered. Types that cannot be compared
without losing information, including time-zone timestamps and timestamps with
precision above six, are read-only in the grid.
Displayed CLOB and BLOB values are limited to the first 65,536 characters or
bytes and include a truncation marker with the full size. Truncated LOB cells
cannot be edited safely. Schema-browser DDL is always read in full.
`F7` cycles a grid fullscreen view that starts with the table header on the first terminal line and uses the last line for data, an editor-only fullscreen view, and a split layout with 2/3 editor and 1/3 data grid.

To export the active table, use one of these command paths:

- `Alt-O -> Results -> Export loaded rows to CSV`
- `Alt-O -> Results -> Export loaded rows to HTML`
- `Alt-O -> Results -> Export loaded rows to XLSX`

All three built-in Plugin API commands write the columns and exactly the rows
currently loaded in the grid; they never fetch additional result pages.
Relative names and the timestamped default name are resolved under the active
workspace's `results/` directory; absolute paths are also accepted, and an
existing file requires confirmation before replacement. Commit or cancel an
active insert draft before exporting so its temporary row cannot be included.
Each enabled export remains available in read-only mode because it does not
execute SQL or change a transaction.

The bundled exporters can be enabled independently in the active `config.ini`:

```ini
[plugin.csv-export]
enabled = yes

[plugin.html-export]
enabled = yes

[plugin.xlsx-export]
enabled = yes
```

Set an exporter's `enabled` value to `no` to omit its command from the
**Results** menu. A missing or malformed value defaults to enabled so existing
configurations keep all three commands. These switches affect only the bundled
exporters, not installed entry-point plugins, and take effect after restarting
PLSQLWKS.

The HTML command writes a standalone UTF-8 HTML5 document with column headings
and table rows, followed by the loaded-row count and any additional-row notice.
The result title is used only as browser-tab document metadata, not as a visible
heading. Document titles, column headers, and cell values are escaped as
untrusted text. The document contains a static embedded stylesheet, no
JavaScript, and no external resources. It reports when more rows are available
in PLSQLWKS but does not fetch them, and it does not open a browser after export.

The XLSX command writes one `Query result` worksheet with a header row and the
currently loaded display rows. Genuine source numbers become native Excel
numeric cells when they fit Excel's range and 15-significant-digit precision;
fixed-point scale such as `10.50` is preserved. Numeric-looking character data
and unsafe-precision numbers remain exact text and can still receive Excel's
number-as-text warning. Headers and other values, including text beginning with
`=`, `+`, `-`, or `@`, remain literal strings and are never interpreted as
spreadsheet formulas. The workbook contains no macros, external links, or
fetched continuation rows and is not opened after export. XLSX support is
optional and requires `openpyxl>=3.1` from the plugin's requirements file shown
above. Each column uses the larger of its bold column name or widest data value,
estimated with Calibri 11-compatible proportional glyph widths, clamped from 3
through 60 units, and given a 17-pixel fit margin. Wrapping remains based on
logical visual length: values over 60 visual units or containing explicit line
breaks are wrapped.

The HTML plugin accepts three plugin-owned environment settings. They are
captured when PLSQLWKS loads the plugin:

```bash
PLSQLWKS_HTML_EXPORT_NULL_VALUE="(null)"
PLSQLWKS_HTML_EXPORT_THEME="dark"
PLSQLWKS_HTML_EXPORT_DATE_FORMAT="%d.%m.%Y"
```

`PLSQLWKS_HTML_EXPORT_NULL_VALUE` replaces the exact `<NULL>` grid display
token and defaults to empty; set it to `<NULL>` or another marker to keep NULL
values visible. `PLSQLWKS_HTML_EXPORT_THEME` is `bright` (the default) or
`dark`; both select only bundled static CSS, and printing uses a readable
bright palette. `PLSQLWKS_HTML_EXPORT_DATE_FORMAT` is empty by default and
otherwise uses Python `strftime` syntax with the same conservative ISO-display
matching described for CSV below. These settings affect only generated HTML
and do not expand Plugin API v1.

The XLSX plugin has equivalent plugin-owned environment settings, captured when
PLSQLWKS loads it:

```bash
PLSQLWKS_XLSX_EXPORT_NULL_VALUE="(null)"
PLSQLWKS_XLSX_EXPORT_THEME="dark"
PLSQLWKS_XLSX_EXPORT_DATE_FORMAT="%d.%m.%Y"
PLSQLWKS_XLSX_EXPORT_AUTO_FILTER="no"
PLSQLWKS_XLSX_EXPORT_AUTO_WIDTH="no"
```

`PLSQLWKS_XLSX_EXPORT_NULL_VALUE` replaces the exact `<NULL>` display token and
defaults to empty; set it to `<NULL>` or another marker to keep NULL values
visible. `PLSQLWKS_XLSX_EXPORT_THEME` selects the bundled `bright` (default) or
`dark` cell styles. `PLSQLWKS_XLSX_EXPORT_DATE_FORMAT` is empty by default and
otherwise applies Python `strftime` directives to the same strict ISO-shaped
display strings as CSV and HTML. Formatted date and text values remain literal
spreadsheet strings rather than formulas or typed Excel dates. The
`PLSQLWKS_XLSX_EXPORT_AUTO_FILTER` setting enables Excel's column filter
controls by default. It accepts case-insensitive, whitespace-tolerant `1`,
`yes`, `true`, or `on` to enable them and `0`, `no`, `false`, or `off` to
disable them; an unset or malformed value falls back to enabled. When enabled,
the filter range spans exactly the header and currently loaded rows, without
applying filter criteria or initially hiding any rows.
`PLSQLWKS_XLSX_EXPORT_AUTO_WIDTH` controls proportional sizing from the widest
header or loaded data value and uses the same boolean syntax. When filtering is
enabled, the header candidate includes three extra character units for the filter
dropdown. Auto-width defaults to enabled; disabling it leaves Excel's default
column widths while preserving the existing multiline and over-60-unit cell
wrapping. These settings belong to the built-in XLSX plugin and do not expand
Plugin API v1.

The built-in CSV export formatting can also be customized in the active
`config.ini`:

```ini
[plugin.csv-export]
separator = ,
null_value =
date_format =
```

`separator` must be one character and defaults to a comma. `null_value`
defaults to empty, replacing the exact `<NULL>` display value with an empty CSV
field. Set it to `<NULL>` or another marker to retain a visible value.
`date_format` defaults to empty, which preserves displayed date values. When
set, it uses Python `strftime` syntax and formats only calendar-valid,
full-string ISO display values shaped as `YYYY-MM-DD` or
`YYYY-MM-DD HH:MM:SS[.digits][+/-HH:MM]`, with one to six fractional digits;
other text is exported unchanged. This deliberately strict heuristic cannot
identify non-ISO or otherwise preformatted database date text, while matching
text-column values are indistinguishable from dates and are formatted too.

### Schema Browser

The `F9` schema browser groups tables, views, procedures, functions, packages, triggers, sequences, indexes, and private synonyms from the current schema. Type while the browser is focused to filter object names with a case-insensitive substring match; groups without matches are hidden and matching groups expand automatically without changing their saved expansion state.

Definition loading supports conventional unquoted identifiers. Quoted
mixed-case objects can appear in the browser but their definitions cannot
currently be loaded from it.

| Key | Action |
| --- | --- |
| Printable text | Filter object names |
| `Backspace` | Delete the final filter character |
| `Esc` | Clear a nonempty filter, or return to the editor when the filter is empty |
| `Up` / `Down` | Move through browser entries |
| `PageUp` / `PageDown` | Move through browser entries by page |
| `Enter` | Expand a group or load an object definition |
| `Space` | Expand/collapse a group when the filter is empty, or add a space to the active filter |
| `Ctrl-R` | Refresh database objects |

### Cell Viewer

| Key | Action |
| --- | --- |
| `Esc` / `Enter` / `F10` | Close the cell viewer |
| `Up` / `Down` | Scroll cell text |
| `PageUp` / `PageDown` | Scroll cell text by page |
| `Home` / `End` | Move to the first/last cell-viewer line |

### Prompts And Pickers

| Key | Action |
| --- | --- |
| Printable text | Type into prompts or filter picker options |
| `Backspace` | Delete before the prompt cursor or picker filter |
| `Enter` | Accept the prompt or selected picker item |
| `Esc` / `Ctrl-Q` | Cancel the prompt or picker |
| `Up` / `Down` | Move through picker options |
| `PageUp` / `PageDown` | Move through picker options by page |

## Workspace Layout

```text
workspace/
  sql/       saved SQL files
  plsql/     saved PL/SQL files
  results/   CSV, HTML, XLSX, and other result output
```

The first launch creates the workspace folders and starter SQL/PLSQL files.

## Plugin API

Plugin API version 1 is a deliberately small, command-only extension point.
See [PLUGINS.md](PLUGINS.md) for the focused plugin author and testing guide.
PLSQLWKS uses that same API for its built-in loaded-row CSV, HTML, and XLSX
exporters; adding formats requires no direct App integration or public API
expansion.
Installed Python packages can add commands to the `Alt-O` menu through the
standard `plsqlwks.plugins` entry-point group:

```toml
[project.entry-points."plsqlwks.plugins"]
example = "example_package.plugin:create_plugin"
```

A minimal plugin factory is:

```python
from plsqlwks.plugins import Plugin, PluginCommand, PluginContext


def show_loaded_count(context: PluginContext) -> None:
    result = context.get_active_result()
    count = len(result.rows) if result is not None else 0
    context.set_status(f"{count} row(s) are currently loaded")


def create_plugin() -> Plugin:
    return Plugin(
        id="example",
        name="Example commands",
        commands=(
            PluginCommand(
                id="show-loaded-count",
                section="Results",
                title="Show loaded row count",
                handler=show_loaded_count,
            ),
        ),
    )
```

The entry point must resolve to a zero-argument callable returning `Plugin`.
Handlers receive only `PluginContext`: it provides an immutable snapshot of the
active tabular result, the results directory, insert-draft detection, text and
overwrite prompts, status updates, and UI error reporting. API v1 does not
provide database execution, mutable results, the application or UI state,
keyboard registration, drawing, events, lifecycle hooks, background jobs,
settings schemas, hot reload, or workspace-local executable plugins.

The `[plugin.csv-export]` section and the documented HTML/XLSX environment
variables configure only their corresponding PLSQLWKS-supplied plugins. Plugin
API v1 does not provide a generic settings schema or pass these values to
installed third-party plugins.

Installed plugins are trusted, in-process Python code. They are not sandboxed;
install plugins only from sources you trust.
