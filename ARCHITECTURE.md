# Architecture

`plsqlwks` is a terminal-first application with one curses thread, one
long-lived database worker thread, and explicit boundaries around Oracle and
installed plugins.

## Runtime flow

```text
KeyReader
  -> InputController / CommandDispatcher
  -> focused controller
  -> DatabaseOperations
  -> DatabaseWorker (OracleWorkspace)
  -> completion event
  -> DatabaseOperations.poll
  -> ResultPresenter or operation callback
  -> UIState
  -> ViewportController
  -> Renderer
```

`ui.app.App` is the composition root and owns the event loop. `KeyReader`
normalizes terminal input, `InputController` routes it by focus, and
`CommandDispatcher` resolves stable command IDs. A focused controller validates
input and prepares an operation on the curses thread. Local document, editor,
and plugin actions may finish there without crossing the database boundary.

Database work goes through `DatabaseOperations`, which allows one foreground
operation and submits a database-only callable to `DatabaseWorker`. The worker
owns the single `OracleWorkspace`, connection, cursors, and continuation
registry; it executes commands in FIFO order and emits progress or completion
events. Polling those events on the curses thread updates `UIState` and invokes
`ResultPresenter` or an operation-specific callback. The presenter materializes
status, focus, errors, and per-tab results. On the next frame,
`ViewportController` prepares a layout snapshot and `Renderer` reads that
snapshot plus `UIState` to draw curses. The curses thread never calls Oracle
directly, and the worker never draws or prompts.

## Transaction state

The worker-owned `OracleWorkspace` is the detailed source of truth. It tracks
the connection, `autocommit`, the client-side `read_only` guardrail, known
pending row changes, and whether uncommitted work has an unknown size or
outcome. After every command the UI receives only an immutable
`DbSessionState(connected, autocommit, read_only, has_uncommitted_changes)`
snapshot.

- In manual mode, successful DML adds its row count and PL/SQL marks pending
  work as unknown. Rollback to a savepoint is also treated conservatively as
  unknown.
- Successful commit, rollback, and DDL clear tracked pending state. Driver
  transaction status may clear stale state or mark an otherwise untracked live
  transaction as unknown. State is cleared only after the driver operation
  succeeds.
- Changing from manual mode to autocommit and quitting with pending work require
  an explicit commit, rollback, or cancel decision. Reconnect may additionally
  discard a dead session. Interrupts and disconnects preserve conservative
  warnings; live cursor and editing handles are detached while already
  materialized rows remain viewable.

Database privileges remain the security boundary. Read-only mode only rejects
statements the client recognizes as writing.

## Plugin API boundary

The supported public surface is exactly the names exported by
`plsqlwks.plugins`. Installed factories are discovered through the
`plsqlwks.plugins` entry-point group, return `Plugin` and `PluginCommand`
metadata, and must match Plugin API version 1. The loader validates IDs,
versions, command uniqueness, and callability; a broken external plugin becomes
a startup warning instead of preventing the application from starting.

`PluginHost` and `UIPluginContext` are the sole UI adapters. A synchronous
handler can inspect an immutable snapshot of already loaded display rows, read
the results directory, detect an insert draft, prompt for text or overwrite
confirmation, set status, and report an error. A snapshot exposes no cursor or
continuation token, and only aligned immutable numeric source values may cross
alongside display text.

Plugin API version 1 does not expose the database worker, `OracleWorkspace`,
mutable results or `UIState`, curses objects, global shortcut registration,
lifecycle events, background jobs, settings schemas, hot reload, or
workspace-local executable modules. Plugins are trusted in-process Python code,
not a security sandbox. See [PLUGINS.md](PLUGINS.md) for the complete author
contract and examples.
