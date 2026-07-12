from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from importlib import import_module
from pathlib import Path

import pytest

from plsqlwks.plugins import PLUGIN_API_VERSION, ResultSnapshot
from plsqlwks.plugins import xlsx_export
from plsqlwks import xlsx_exporting


pytestmark = pytest.mark.plugin


class RecordingContext:
    """Minimal recording implementation of the existing PluginContext contract."""

    def __init__(
        self,
        results_dir: Path,
        active: ResultSnapshot | None,
        *,
        response: str | None = "result.xlsx",
        insert_draft: bool = False,
        overwrite: bool = False,
    ) -> None:
        self._results_dir = results_dir
        self.active = active
        self.response = response
        self.insert_draft = insert_draft
        self.overwrite = overwrite
        self.calls: list[str] = []
        self.prompt_defaults: list[str] = []
        self.confirmed_paths: list[Path] = []
        self.statuses: list[str] = []
        self.errors: list[tuple[str, Exception]] = []

    @property
    def results_dir(self) -> Path:
        self.calls.append("results_dir")
        return self._results_dir

    def get_active_result(self) -> ResultSnapshot | None:
        self.calls.append("result")
        return self.active

    def has_active_insert_draft(self) -> bool:
        self.calls.append("draft")
        return self.insert_draft

    def prompt_text(
        self,
        label: str,
        default: str = "",
        *,
        strip: bool = True,
    ) -> str | None:
        assert label == "Export loaded rows to XLSX"
        assert strip is True
        self.calls.append("prompt")
        self.prompt_defaults.append(default)
        return self.response

    def confirm_overwrite(self, path: Path) -> bool:
        self.calls.append("overwrite")
        self.confirmed_paths.append(path)
        return self.overwrite

    def set_status(self, message: str) -> None:
        self.calls.append("status")
        self.statuses.append(message)

    def report_error(self, title: str, error: Exception) -> None:
        self.calls.append("error")
        self.errors.append((title, error))


def snapshot(
    *,
    columns: tuple[str, ...] = ("VALUE",),
    rows: tuple[tuple[str, ...], ...] = (("one",),),
    has_more: bool = False,
    title: str = "Loaded result",
) -> ResultSnapshot:
    return ResultSnapshot(title, columns, rows, has_more)


def load_workbook(path: Path):
    """Load openpyxl only inside optional test execution, not at collection."""
    return import_module("openpyxl").load_workbook(path, data_only=False)


def test_plugin_metadata_options_and_api_version():
    plugin = xlsx_export.create_plugin(xlsx_export.XlsxExportOptions())
    command = plugin.commands[0]

    assert PLUGIN_API_VERSION == 1
    assert plugin.id == "xlsx-export"
    assert plugin.name == "XLSX result export"
    assert plugin.api_version == 1
    assert command.id == "export-loaded-rows"
    assert command.section == "Results"
    assert command.title == "Export loaded rows to XLSX"
    assert command.shortcut == ""
    assert {"xlsx", "excel", "spreadsheet"} <= set(command.keywords.split())
    assert xlsx_export.XlsxExportOptions() == xlsx_export.XlsxExportOptions(
        null_value="<NULL>",
        theme="bright",
        date_format="",
    )
    with pytest.raises(FrozenInstanceError):
        plugin.id = "changed"  # type: ignore[misc]


def test_deterministic_default_filename_and_context_contract(monkeypatch, tmp_path):
    fixed = datetime(2026, 7, 13, 1, 2, 3)
    monkeypatch.setattr(xlsx_export, "local_now", lambda: fixed)
    context = RecordingContext(tmp_path, snapshot(), response=None)

    xlsx_export.export_loaded_rows_to_xlsx(context)

    assert context.prompt_defaults == [str(tmp_path / "result_20260713_010203.xlsx")]
    assert context.statuses == ["Export cancelled"]
    assert context.calls == ["draft", "result", "results_dir", "prompt", "status"]


def test_writes_exact_loaded_rows_as_formula_safe_strings_with_options(tmp_path):
    active = snapshot(
        columns=("=HEADER", "NULL", "DATE", "TEXT"),
        rows=(("=1+1", "<NULL>", "2026-07-13", "Žluťoučký\ntext"),),
        has_more=True,
        title="Current result",
    )
    destination = tmp_path / "nested" / "loaded.xlsx"
    context = RecordingContext(tmp_path, active, response="nested/loaded.xlsx")
    options = xlsx_export.XlsxExportOptions(
        null_value="(null)",
        theme="dark",
        date_format="%d.%m.%Y",
    )

    xlsx_export.export_loaded_rows_to_xlsx(context, options)

    workbook = load_workbook(destination)
    try:
        sheet = workbook.active
        assert sheet.title == "Query result"
        assert workbook.properties.title == "Current result"
        assert sheet.max_row == 2
        assert sheet.max_column == 4
        assert [cell.value for cell in sheet[1]] == ["=HEADER", "NULL", "DATE", "TEXT"]
        assert [cell.value for cell in sheet[2]] == [
            "=1+1",
            "(null)",
            "13.07.2026",
            "Žluťoučký\ntext",
        ]
        assert all(cell.data_type == "s" for row in sheet.iter_rows() for cell in row)
        assert all(cell.font.bold for cell in sheet[1])
        assert not any(cell.font.bold for cell in sheet[2])
        assert sheet["A1"].fill.fgColor.rgb != sheet["A2"].fill.fgColor.rgb
    finally:
        workbook.close()

    assert context.statuses == [
        f"Exported 1 loaded row(s) to {destination.resolve()}; additional rows are available"
    ]
    assert context.errors == []
    assert context.active is active
    assert active.rows[0][0] == "=1+1"


def test_zero_rows_produces_header_only_workbook_and_plain_success(tmp_path):
    destination = tmp_path / "empty.xlsx"
    context = RecordingContext(
        tmp_path,
        snapshot(columns=("A", "B"), rows=()),
        response=str(destination),
    )

    xlsx_export.export_loaded_rows_to_xlsx(context)

    workbook = load_workbook(destination)
    try:
        sheet = workbook.active
        assert sheet.max_row == 1
        assert [cell.value for cell in sheet[1]] == ["A", "B"]
    finally:
        workbook.close()
    assert context.statuses == [f"Exported 0 loaded row(s) to {destination.resolve()}"]


@pytest.mark.parametrize("value", ["=1+1", "+SUM(A1:A2)", "-2+3", "@command"])
def test_formula_like_values_are_always_literal_strings(tmp_path, value):
    destination = tmp_path / "literal.xlsx"

    xlsx_exporting.write_xlsx_result(
        destination,
        title="literal values",
        columns=(value,),
        rows=((value,),),
    )

    workbook = load_workbook(destination)
    try:
        assert workbook.active["A1"].value == value
        assert workbook.active["A1"].data_type == "s"
        assert workbook.active["A2"].value == value
        assert workbook.active["A2"].data_type == "s"
    finally:
        workbook.close()


@pytest.mark.parametrize("response", [None, ""])
def test_cancelled_prompt_writes_nothing(tmp_path, response):
    context = RecordingContext(tmp_path, snapshot(), response=response)

    xlsx_export.export_loaded_rows_to_xlsx(context)

    assert context.statuses == ["Export cancelled"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("active", [None, ResultSnapshot("text", (), (), False)])
def test_no_table_result_does_not_prompt(tmp_path, active):
    context = RecordingContext(tmp_path, active)

    xlsx_export.export_loaded_rows_to_xlsx(context)

    assert context.statuses == ["No table result is available for export"]
    assert "prompt" not in context.calls


def test_insert_draft_precedes_snapshot_and_prompt(tmp_path):
    context = RecordingContext(tmp_path, snapshot(), insert_draft=True)

    xlsx_export.export_loaded_rows_to_xlsx(context)

    assert context.calls == ["draft", "status"]
    assert context.statuses == [
        "Export unavailable while an insert draft is active; "
        "commit or cancel the draft first"
    ]


def test_overwrite_rejection_preserves_old_file(tmp_path):
    destination = tmp_path / "existing.xlsx"
    previous = b"old workbook bytes"
    destination.write_bytes(previous)
    context = RecordingContext(tmp_path, snapshot(), response="existing.xlsx")

    xlsx_export.export_loaded_rows_to_xlsx(context)

    assert destination.read_bytes() == previous
    assert context.confirmed_paths == [destination.resolve()]
    assert context.statuses == ["Export cancelled"]


def test_overwrite_acceptance_replaces_old_file(tmp_path):
    destination = tmp_path / "existing.xlsx"
    destination.write_bytes(b"old")
    context = RecordingContext(
        tmp_path,
        snapshot(),
        response=str(destination),
        overwrite=True,
    )

    xlsx_export.export_loaded_rows_to_xlsx(context)

    assert destination.read_bytes().startswith(b"PK")
    assert context.errors == []


def test_absolute_path_and_filename_without_extension_are_accepted(tmp_path):
    destination = tmp_path / "elsewhere" / "explicit-name"
    context = RecordingContext(tmp_path / "results", snapshot(), response=str(destination))

    xlsx_export.export_loaded_rows_to_xlsx(context)

    assert destination.is_file()
    assert not destination.with_suffix(".xlsx").exists()


def test_zero_argument_factory_captures_environment_options(monkeypatch, tmp_path):
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_NULL_VALUE", "NULL")
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_THEME", "dark")
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_DATE_FORMAT", "%Y/%m/%d")
    plugin = xlsx_export.create_plugin()
    destination = tmp_path / "configured.xlsx"
    context = RecordingContext(
        tmp_path,
        snapshot(columns=("N", "D"), rows=(("<NULL>", "2026-07-13"),)),
        response="configured.xlsx",
    )

    plugin.commands[0].handler(context)

    workbook = load_workbook(destination)
    try:
        assert [cell.value for cell in workbook.active[2]] == ["NULL", "2026/07/13"]
    finally:
        workbook.close()


def test_missing_optional_dependency_is_reported_without_damaging_destination(
    monkeypatch,
    tmp_path,
):
    destination = tmp_path / "existing.xlsx"
    previous = b"previous"
    destination.write_bytes(previous)
    context = RecordingContext(
        tmp_path,
        snapshot(),
        response="existing.xlsx",
        overwrite=True,
    )

    def missing_backend(name: str):
        if name.startswith("openpyxl"):
            raise ImportError("not installed")
        return import_module(name)

    monkeypatch.setattr(xlsx_exporting, "import_module", missing_backend)

    xlsx_export.export_loaded_rows_to_xlsx(context)

    assert destination.read_bytes() == previous
    assert context.statuses == [
        "XLSX export failed: XLSX export requires the optional 'openpyxl' package"
    ]
    assert context.errors and context.errors[0][0] == "XLSX export failed"


@pytest.mark.parametrize(
    ("columns", "rows", "message"),
    [
        (("A", "B"), (("one",),), "expected 2"),
        (("A",), (("one", "two"),), "expected 1"),
    ],
)
def test_mismatched_rows_fail_before_atomic_replacement(
    monkeypatch,
    tmp_path,
    columns,
    rows,
    message,
):
    destination = tmp_path / "existing.xlsx"
    destination.write_bytes(b"old")
    context = RecordingContext(
        tmp_path,
        snapshot(columns=columns, rows=rows),
        response="existing.xlsx",
        overwrite=True,
    )
    monkeypatch.setattr(
        xlsx_exporting,
        "_load_openpyxl",
        lambda: pytest.fail("backend loaded before validation"),
    )

    xlsx_export.export_loaded_rows_to_xlsx(context)

    assert destination.read_bytes() == b"old"
    assert message in context.statuses[-1]
    assert context.errors and context.errors[0][0] == "XLSX export failed"


def test_theme_dimensions_and_cell_limits_validate_before_writing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        xlsx_exporting,
        "_load_openpyxl",
        lambda: pytest.fail("backend loaded before validation"),
    )

    with pytest.raises(ValueError, match="theme must be"):
        xlsx_exporting.write_xlsx_result(
            tmp_path / "theme.xlsx",
            title="x",
            columns=("A",),
            rows=(),
            theme="custom-css",
        )
    with pytest.raises(ValueError, match="at most 2 columns"):
        monkeypatch.setattr(xlsx_exporting, "EXCEL_MAX_COLUMNS", 2)
        xlsx_exporting.write_xlsx_result(
            tmp_path / "columns.xlsx",
            title="x",
            columns=("A", "B", "C"),
            rows=(),
        )
    monkeypatch.setattr(xlsx_exporting, "EXCEL_MAX_COLUMNS", 16_384)
    monkeypatch.setattr(xlsx_exporting, "EXCEL_MAX_DATA_ROWS", 1)
    with pytest.raises(ValueError, match="at most 1 loaded data rows"):
        xlsx_exporting.write_xlsx_result(
            tmp_path / "rows.xlsx",
            title="x",
            columns=("A",),
            rows=(("1",), ("2",)),
        )
    monkeypatch.setattr(xlsx_exporting, "EXCEL_MAX_DATA_ROWS", 1_048_575)
    monkeypatch.setattr(xlsx_exporting, "EXCEL_MAX_CELL_CHARACTERS", 3)
    with pytest.raises(ValueError, match="cell limit of 3"):
        xlsx_exporting.write_xlsx_result(
            tmp_path / "cell.xlsx",
            title="x",
            columns=("ABCD",),
            rows=(),
        )


def test_write_failure_preserves_destination_and_snapshot(monkeypatch, tmp_path):
    destination = tmp_path / "existing.xlsx"
    destination.write_bytes(b"previous")
    active = snapshot(rows=(("unchanged",),), has_more=True)
    context = RecordingContext(
        tmp_path,
        active,
        response="existing.xlsx",
        overwrite=True,
    )
    failure = OSError("disk full\ninternal detail")
    monkeypatch.setattr(xlsx_export, "write_xlsx_result", lambda *args, **kwargs: (_ for _ in ()).throw(failure))

    xlsx_export.export_loaded_rows_to_xlsx(context)

    assert destination.read_bytes() == b"previous"
    assert context.statuses == ["XLSX export failed: disk full"]
    assert context.errors == [("XLSX export failed", failure)]
    assert context.active is active
    assert active.rows == (("unchanged",),)
    assert active.has_more is True


def test_handler_does_not_catch_base_exceptions(monkeypatch, tmp_path):
    context = RecordingContext(tmp_path, snapshot())

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(xlsx_export, "write_xlsx_result", interrupt)

    with pytest.raises(KeyboardInterrupt):
        xlsx_export.export_loaded_rows_to_xlsx(context)
