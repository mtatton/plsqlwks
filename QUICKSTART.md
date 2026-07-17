# PLSQLWKS quick start

This guide goes from a source checkout to a first Oracle query in about five
minutes. It then covers a few useful query workflows, the most common startup
problems, and a small installed plugin. For the complete key reference and
runtime behavior, see [README.md](README.md); for the full extension contract,
see [PLUGINS.md](PLUGINS.md).

## 1. Check the prerequisites

You need:

- Python 3.10 or newer with the standard-library `curses` module
- a terminal with at least 80 columns and 24 rows
- an Oracle username, password, and Easy Connect service name
- network access to the Oracle listener

The DSN in this guide has the form `host:port/service_name`, for example
`db.example.test:1521/freepdb1`. Ask the database administrator for the exact
value when in doubt; a service name is not necessarily the same as an Oracle
SID.

## 2. Install PLSQLWKS

From a checkout of the repository, create an isolated environment and install
the application:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install .
```

Confirm that the console command is available:

```bash
plsqlwks --help
plsqlwks --version
```

For development, replace the final install command with
`python3 -m pip install -e '.[dev]'`.

## 3. Configure the Oracle connection

The following Bash example prompts without echoing the password, creates a
private password file, and exports the connection for the current shell. Change
the user and DSN to match the target database.

```bash
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/plsqlwks"
mkdir -p "$config_dir"
chmod 700 "$config_dir"
umask 077
IFS= read -r -s -p "Oracle password: " oracle_password
printf '\n'
printf '%s' "$oracle_password" > "$config_dir/orapass"
unset oracle_password

export ORACLE_USER='hr'
export ORACLE_PASSWORD_FILE="$config_dir/orapass"
export ORACLE_DSN='127.0.0.1:1521/free'
```

PLSQLWKS preserves spaces in the password and removes only final CR/LF line
endings. Keep the password file private; on POSIX systems its mode should be
`0600`.

These variables override the built-in defaults. To keep them across terminal
sessions, add only the three `export` lines to the shell profile; do not put the
password itself in the profile.

## 4. Run the first query

Start in manual transaction mode:

```bash
plsqlwks --manual
```

The header should say `db connected`. Enter this query in the upper editor:

```sql
select
    sys_context('USERENV', 'SESSION_USER') as session_user,
    sys_context('USERENV', 'DB_NAME') as database_name,
    to_char(systimestamp, 'YYYY-MM-DD HH24:MI:SS TZH:TZM') as database_time
from dual;
```

Press `F5`, `Ctrl-Enter`, or `Alt-X`. The lower pane shows one row from the
connected database. Press `Tab` to focus the grid, arrow keys to move between
cells, and `Tab` again to return to the editor. Press `Ctrl-Q` to quit.

![PLSQLWKS editor and result workspace](https://gitlab.com/unununu/plsqlwks/-/raw/main/img/preview.png)

The result is a connection check that does not depend on application tables or
sample schemas. The displayed user and database name should match the intended
target before running any write statement.

## Practical query examples

### Prompt for a bind value

Execute this statement with `F5`:

```sql
select :name as supplied_name,
       'Hello, ' || :name as greeting
from dual;
```

PLSQLWKS prompts once for `:name` and passes the answer as an Oracle bind value.
Set `[database] remember_bind_values = yes` in the active `config.ini` if the
same bind should be prefilled later in the application session.

### Run a script and inspect DBMS_OUTPUT

Use `F11` for a multi-statement script:

```sql
begin
    dbms_output.put_line(
        'Connected as ' || sys_context('USERENV', 'SESSION_USER')
    );
end;
/

select count(*) as visible_objects
from user_objects;
```

`F11` runs the whole buffer (or the selection). Press `F6` to switch between
normal results and captured `DBMS_OUTPUT`.

### Page through a larger result

If the connected schema has the Oracle HR sample tables, try:

```sql
select employee_id, first_name, last_name, department_id
from employees
order by employee_id;
```

`PLSQLWKS_MAX_ROWS` controls the number of rows loaded per page. With the result
grid focused, use `Ctrl-PageDown` and `Ctrl-PageUp` to page. The status and
result label say when more rows are available.

### Export query rows

With a table result visible, press `Alt-O`, type `export`, and choose CSV, HTML,
or XLSX. A second picker defaults to **Loaded rows only**, which writes exactly
the rows already in the grid without another database fetch. Choose **All
available rows** to fetch continuation pages up to a fixed 10,000-row total.
The status bar shows fetch and write progress; press `Ctrl-C` to cancel either
phase without installing a partial destination file. Completed fetch pages stay
visible but become read-only after an interrupted database fetch. Output names
default to the workspace `results/` directory. XLSX support remains optional
and requires `python3 -m pip install 'plsqlwks[xlsx]'`.

### Commit or roll back deliberately

Fresh workspaces use manual transactions. After a DML statement, use
`Ctrl-Alt-C` to commit or `Ctrl-Alt-R` to roll back. `F12` changes transaction
mode. The `--read-only` option adds a client-side guardrail against statements
that appear to write, but database privileges remain the security boundary.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `No module named _curses` or `curses` | Use a Python build and terminal that provide curses. On native Windows, a supported WSL environment is usually the simplest route. |
| `Password file is missing` | Check `ORACLE_PASSWORD_FILE`, create the file, and restart. The path must name a nonempty regular file. |
| Password permission warning | Run `chmod 600 "$ORACLE_PASSWORD_FILE"`; also keep its parent directory private. |
| `ORA-01017` | Recheck `ORACLE_USER`, rewrite the password file without adding unintended characters, and confirm that the account is valid for this service. |
| `ORA-12514` or `ORA-12154` | Recheck the service name in `ORACLE_DSN`. Easy Connect uses `host:port/service_name`. |
| `ORA-12541`, timeout, or connection refused | Check host, port, listener availability, VPN, firewall, and DNS from the same terminal. |
| Header says `db disconnected` after a network problem | Loaded rows remain viewable. Restore connectivity and press `Ctrl+=` to reconnect; if manual work was pending, treat its outcome as unknown until verified. |
| `Ctrl-Enter` inserts a newline or is not recognized | Terminal key encodings vary. Use `F5` or `Alt-X`, which run the same current-statement action. |
| Box drawing or non-ASCII text is garbled | Start from a UTF-8 locale such as `C.UTF-8`; check the available names with `locale -a`. |
| The UI is clipped or a picker will not open | Enlarge the terminal to at least 80 columns by 24 rows. |
| A write statement is rejected in read-only mode | Restart with `--read-write` only if the account and task are intended to allow writes. |
| XLSX export reports that `openpyxl` is missing | Install the optional extra with `python3 -m pip install 'plsqlwks[xlsx]'`, then restart. |

Press `F1` at any time for the in-application key guide. Connection errors and
plugin failures are reported in the lower pane; the final status line provides
the shorter summary.

## Develop a small command plugin

Plugin API v1 loads trusted Python packages in the PLSQLWKS process. This
example adds two commands: one reports the loaded-row count, and the other
prompts for a label. It needs no dependency beyond PLSQLWKS.

Create this layout outside the PLSQLWKS repository:

```text
result-tools/
  pyproject.toml
  src/
    result_tools/
      __init__.py
      plugin.py
```

Use this `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "plsqlwks-result-tools"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["plsqlwks>=0.1.8"]

[project.entry-points."plsqlwks.plugins"]
result-tools = "result_tools.plugin:create_plugin"

[tool.setuptools.packages.find]
where = ["src"]
```

Leave `src/result_tools/__init__.py` empty and put this in
`src/result_tools/plugin.py`:

```python
from plsqlwks.plugins import Plugin, PluginCommand, PluginContext


def show_loaded_count(context: PluginContext) -> None:
    result = context.get_active_result()
    if result is None:
        context.set_status("No table result is active")
        return
    suffix = "; more rows exist" if result.has_more else ""
    context.set_status(f"{len(result.rows)} loaded row(s){suffix}")


def label_loaded_result(context: PluginContext) -> None:
    result = context.get_active_result()
    if result is None:
        context.set_status("No table result is active")
        return
    label = context.prompt_text("Result label", result.title)
    if label is not None:
        context.set_status(f"{label}: {len(result.rows)} loaded row(s)")


def create_plugin() -> Plugin:
    return Plugin(
        id="result-tools",
        name="Result tools",
        commands=(
            PluginCommand(
                id="show-loaded-count",
                section="Results",
                title="Show loaded row count",
                handler=show_loaded_count,
                keywords="plugin snapshot rows",
            ),
            PluginCommand(
                id="label-loaded-result",
                section="Results",
                title="Label loaded result",
                handler=label_loaded_result,
                keywords="plugin prompt snapshot",
            ),
        ),
    )
```

Install it into the same virtual environment and perform a factory smoke test:

```bash
python3 -m pip install -e /path/to/result-tools
python3 -c "from result_tools.plugin import create_plugin; print(create_plugin())"
```

Restart PLSQLWKS, open `Alt-O`, and search for `loaded`. Plugin factories are
discovered only at startup. A bad plugin is skipped with a startup warning, and
an exception from a command is shown in the UI rather than ending the session.

Handlers receive an immutable snapshot of already loaded display rows and a
small UI-mediated context. API v1 intentionally does not expose database
execution, Oracle handles, mutable application state, curses drawing, global
shortcuts, background jobs, lifecycle hooks, or hot reload. Installed plugins
are not sandboxed, so install them only from sources you trust. Continue with
[PLUGINS.md](PLUGINS.md) for export-plugin examples, validation rules, optional
dependencies, and plugin test commands.
