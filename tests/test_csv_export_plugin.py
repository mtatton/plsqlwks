from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from plsqlwks.plugins import ResultSnapshot
from plsqlwks.plugins import csv_export


pytestmark = pytest.mark.plugin


class RecordingContext:
    def __init__(
        self,
        results_dir: Path,
        snapshot: ResultSnapshot | None,
        *,
        insert_draft: bool = False,
        response: str | None = None,
        overwrite: bool = True,
    ) -> None:
        self._results_dir = results_dir
        self.snapshot = snapshot
        self.insert_draft = insert_draft
        self.response = response
        self.overwrite = overwrite
        self.statuses: list[str] = []
        self.prompts: list[tuple[str, str, bool]] = []
        self.confirmed_paths: list[Path] = []
        self.errors: list[tuple[str, Exception]] = []
        self.calls: list[str] = []

    @property
    def results_dir(self) -> Path:
        return self._results_dir

    def get_active_result(self) -> ResultSnapshot | None:
        self.calls.append("result")
        return self.snapshot

    def has_active_insert_draft(self) -> bool:
        self.calls.append("draft")
        return self.insert_draft

    def prompt_text(self, label: str, default: str = "", *, strip: bool = True) -> str | None:
        self.calls.append("prompt")
        self.prompts.append((label, default, strip))
        return self.response

    def confirm_overwrite(self, path: Path) -> bool:
        self.calls.append("confirm")
        self.confirmed_paths.append(path)
        return self.overwrite

    def set_status(self, message: str) -> None:
        self.statuses.append(message)

    def report_error(self, title: str, error: Exception) -> None:
        self.errors.append((title, error))


def snapshot(*, rows: tuple[tuple[str, ...], ...] = (("1",),), has_more: bool = False) -> ResultSnapshot:
    return ResultSnapshot("Data", ("VALUE",), rows, has_more)


def test_plugin_metadata_and_deterministic_default_filename(monkeypatch, tmp_path):
    fixed = datetime(2026, 7, 12, 9, 8, 7)
    monkeypatch.setattr(csv_export, "local_now", lambda: fixed)
    context = RecordingContext(tmp_path, snapshot(), response=None)

    csv_export.export_loaded_rows(context)

    plugin = csv_export.create_plugin()
    command = plugin.commands[0]
    assert (plugin.id, plugin.name, plugin.api_version) == ("csv-export", "CSV result export", 1)
    assert (command.id, command.section, command.title) == (
        "export-loaded-rows",
        "Results",
        "Export loaded rows to CSV",
    )
    assert command.shortcut == ""
    assert "csv" in command.keywords
    assert context.prompts == [
        ("Export loaded rows to CSV", str(tmp_path / "result_20260712_090807.csv"), True)
    ]


@pytest.mark.parametrize("response", [None, ""])
def test_cancelled_filename_prompt_writes_nothing(tmp_path, response):
    context = RecordingContext(tmp_path, snapshot(), response=response)

    csv_export.export_loaded_rows(context)

    assert context.statuses[-1] == "Export cancelled"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("active", [None, ResultSnapshot("Message", (), (), False)])
def test_no_tabular_result_does_not_prompt(tmp_path, active):
    context = RecordingContext(tmp_path, active, response="unused.csv")

    csv_export.export_loaded_rows(context)

    assert context.statuses == ["No table result is available for export"]
    assert context.prompts == []


def test_insert_draft_is_checked_before_result_and_does_not_prompt(tmp_path):
    context = RecordingContext(tmp_path, snapshot(rows=(("temporary",),)), insert_draft=True)

    csv_export.export_loaded_rows(context)

    assert context.calls == ["draft"]
    assert context.statuses == [
        "Export unavailable while an insert draft is active; commit or cancel the draft first"
    ]
    assert context.prompts == []


def test_relative_path_is_resolved_under_results_dir_and_writes_loaded_rows(tmp_path):
    context = RecordingContext(
        tmp_path,
        ResultSnapshot("Data", ("NAME", "NOTE"), (("kůň", 'a,b "quoted"'),), False),
        response="nested/export.csv",
    )

    csv_export.export_loaded_rows(context)

    path = (tmp_path / "nested" / "export.csv").resolve()
    assert path.read_text(encoding="utf-8") == 'NAME,NOTE\nkůň,"a,b ""quoted"""\n'
    assert context.statuses[-1] == f"Exported 1 loaded row(s) to {path}"
    assert context.confirmed_paths == []


def test_configured_separator_and_null_value_transform_display_rows(tmp_path):
    context = RecordingContext(
        tmp_path,
        ResultSnapshot(
            "Data",
            ("FIRST", "SECOND", "THIRD"),
            (("a;b", "<NULL>", ""),),
            False,
        ),
        response="configured.csv",
    )
    options = csv_export.CsvExportOptions(separator=";", null_value="")

    csv_export.export_loaded_rows(context, options)

    path = (tmp_path / "configured.csv").resolve()
    assert path.read_text(encoding="utf-8") == 'FIRST;SECOND;THIRD\n"a;b";;\n'


@pytest.mark.parametrize(
    ("null_value", "exported_null"),
    (("(none)", "(none)"), ("<NULL>", "<NULL>")),
)
def test_configured_custom_null_value_replaces_only_exact_display_token(
    tmp_path,
    null_value,
    exported_null,
):
    context = RecordingContext(
        tmp_path,
        ResultSnapshot(
            "Data",
            ("NULL_VALUE", "TEXT_VALUE", "SPACED_VALUE"),
            (("<NULL>", "literal NULL", " <NULL> "),),
            False,
        ),
        response="nulls.csv",
    )

    csv_export.export_loaded_rows(
        context,
        csv_export.CsvExportOptions(null_value=null_value),
    )

    path = (tmp_path / "nulls.csv").resolve()
    assert path.read_text(encoding="utf-8") == (
        "NULL_VALUE,TEXT_VALUE,SPACED_VALUE\n"
        f"{exported_null},literal NULL, <NULL> \n"
    )


def test_configured_date_format_handles_strict_iso_date_and_timestamp_shapes(tmp_path):
    context = RecordingContext(
        tmp_path,
        ResultSnapshot(
            "Data",
            ("DATE_VALUE", "TIMESTAMP_VALUE", "OFFSET_VALUE"),
            (("2026-07-12", "2026-07-12 09:08:07.123456", "2026-07-12 09:08:07+02:00"),),
            False,
        ),
        response="dates.csv",
    )

    csv_export.export_loaded_rows(
        context,
        csv_export.CsvExportOptions(date_format="%d.%m.%Y %H:%M:%S %z"),
    )

    path = (tmp_path / "dates.csv").resolve()
    assert path.read_text(encoding="utf-8") == (
        "DATE_VALUE,TIMESTAMP_VALUE,OFFSET_VALUE\n"
        "12.07.2026 00:00:00 ,12.07.2026 09:08:07 ,12.07.2026 09:08:07 +0200\n"
    )


def test_date_format_preserves_nonmatching_and_invalid_display_strings(tmp_path):
    original = (
        "prefix 2026-07-12",
        "2026-02-30",
        "2026-07-12T09:08:07",
        "2026-07-12 25:08:07",
        "ordinary",
    )
    context = RecordingContext(
        tmp_path,
        ResultSnapshot("Data", ("A", "B", "C", "D", "E"), (original,), False),
        response="unchanged.csv",
    )

    csv_export.export_loaded_rows(
        context,
        csv_export.CsvExportOptions(date_format="%d/%m/%Y"),
    )

    path = (tmp_path / "unchanged.csv").resolve()
    assert path.read_text(encoding="utf-8") == (
        "A,B,C,D,E\n"
        "prefix 2026-07-12,2026-02-30,2026-07-12T09:08:07,"
        "2026-07-12 25:08:07,ordinary\n"
    )


def test_default_options_write_null_as_empty_and_preserve_iso_display_values(tmp_path):
    context = RecordingContext(
        tmp_path,
        ResultSnapshot(
            "Data",
            ("NULL_VALUE", "DATE_VALUE", "TIMESTAMP_VALUE"),
            (("<NULL>", "2026-07-12", "2026-07-12 09:08:07"),),
            False,
        ),
        response="defaults.csv",
    )

    csv_export.export_loaded_rows(context)

    path = (tmp_path / "defaults.csv").resolve()
    assert path.read_text(encoding="utf-8") == (
        "NULL_VALUE,DATE_VALUE,TIMESTAMP_VALUE\n"
        ",2026-07-12,2026-07-12 09:08:07\n"
    )


def test_create_plugin_captures_options_without_changing_zero_argument_factory(tmp_path):
    configured = csv_export.create_plugin(
        csv_export.CsvExportOptions(separator=";", null_value="NULL")
    )
    context = RecordingContext(
        tmp_path,
        ResultSnapshot("Data", ("VALUE",), (("<NULL>",),), False),
        response="captured.csv",
    )

    configured.commands[0].handler(context)

    assert (tmp_path / "captured.csv").read_text(encoding="utf-8") == "VALUE\nNULL\n"
    assert csv_export.create_plugin().id == "csv-export"


def test_absolute_path_and_header_only_result_are_accepted(tmp_path):
    path = (tmp_path / "absolute.csv").resolve()
    context = RecordingContext(tmp_path / "other", snapshot(rows=()), response=str(path))

    csv_export.export_loaded_rows(context)

    assert path.read_text(encoding="utf-8") == "VALUE\n"
    assert context.statuses[-1] == f"Exported 0 loaded row(s) to {path}"


def test_continuation_adds_success_suffix_without_fetching_more(tmp_path):
    context = RecordingContext(tmp_path, snapshot(has_more=True), response="out.csv")

    csv_export.export_loaded_rows(context)

    path = (tmp_path / "out.csv").resolve()
    assert context.statuses[-1] == (
        f"Exported 1 loaded row(s) to {path}; additional rows are available"
    )
    assert context.calls == ["draft", "result", "prompt"]


def test_overwrite_rejected_preserves_existing_file(tmp_path):
    path = tmp_path / "out.csv"
    path.write_text("old", encoding="utf-8")
    context = RecordingContext(tmp_path, snapshot(), response="out.csv", overwrite=False)

    csv_export.export_loaded_rows(context)

    assert path.read_text(encoding="utf-8") == "old"
    assert context.confirmed_paths == [path.resolve()]
    assert context.statuses[-1] == "Export cancelled"


def test_overwrite_accepted_replaces_existing_file(tmp_path):
    path = tmp_path / "out.csv"
    path.write_text("old", encoding="utf-8")
    context = RecordingContext(tmp_path, snapshot(), response="out.csv", overwrite=True)

    csv_export.export_loaded_rows(context)

    assert path.read_text(encoding="utf-8") == "VALUE\n1\n"
    assert context.confirmed_paths == [path.resolve()]


def test_write_failure_reports_error_and_preserves_snapshot(monkeypatch, tmp_path):
    active = snapshot(rows=(("before",),), has_more=True)
    context = RecordingContext(tmp_path, active, response="out.csv")
    failure = OSError("disk full\ninternal detail")

    def fail_write(path, columns, rows, *, delimiter=","):
        raise failure

    monkeypatch.setattr(csv_export, "write_csv", fail_write)

    csv_export.export_loaded_rows(context)

    assert context.statuses[-1] == "CSV export failed: disk full"
    assert context.errors == [("CSV export", failure)]
    assert context.snapshot is active
    assert active.rows == (("before",),)
    assert active.has_more is True
    assert not (tmp_path / "out.csv").exists()


def test_write_failure_bounds_status_but_reports_complete_error(monkeypatch, tmp_path):
    context = RecordingContext(tmp_path, snapshot(), response="out.csv")
    failure = OSError("x" * 500)

    def fail_write(path, columns, rows, *, delimiter=","):
        raise failure

    monkeypatch.setattr(csv_export, "write_csv", fail_write)

    csv_export.export_loaded_rows(context)

    status = context.statuses[-1]
    assert status.startswith("CSV export failed: ")
    assert status.endswith("…")
    assert len(status) <= len("CSV export failed: ") + 160
    assert context.errors == [("CSV export", failure)]


def test_path_normalization_expands_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    assert csv_export.resolve_export_path("~/result.csv", tmp_path) == (home / "result.csv").resolve()
