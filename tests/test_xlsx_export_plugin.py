from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from zipfile import ZipFile

import pytest

from plsqlwks import xlsx_exporting
from plsqlwks.exporting import ExportCancelled
from plsqlwks.plugins import PLUGIN_API_VERSION, ResultSnapshot, xlsx_export

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
    numeric_values: tuple[tuple[Decimal | int | float | None, ...], ...] = (),
) -> ResultSnapshot:
    return ResultSnapshot(
        title,
        columns,
        rows,
        has_more,
        numeric_values=numeric_values,
    )


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
    assert command.title == "Export result to XLSX"
    assert command.shortcut == ""
    assert {"xlsx", "excel", "spreadsheet"} <= set(command.keywords.split())
    assert xlsx_export.XlsxExportOptions() == xlsx_export.XlsxExportOptions(
        null_value="",
        theme="bright",
        date_format="",
        auto_filter=True,
        auto_width=True,
        freeze_top_row=True,
    )
    with pytest.raises(FrozenInstanceError):
        plugin.id = "changed"  # type: ignore[misc]  # reason: verifies frozen plugin metadata rejects mutation


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
        auto_filter=False,
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
        assert sheet.auto_filter.ref is None
        assert sheet.freeze_panes == "A2"
    finally:
        workbook.close()

    assert context.statuses == [f"Exported 1 loaded row(s) to {destination.resolve()}; additional rows are available"]
    assert context.errors == []
    assert context.active is active
    assert active.rows[0][0] == "=1+1"


def test_genuine_numeric_values_export_as_numbers_without_coercing_text(tmp_path):
    active = snapshot(
        columns=(
            "FIXED_DECIMAL",
            "INTEGER",
            "FLOAT",
            "TEXT_CODE",
            "LONG_NUMBER",
            "DATE",
            "BOOLEAN",
            "FORMULA",
            "NULL_VALUE",
        ),
        rows=(
            (
                "10.50",
                "42",
                "1.25",
                "007",
                "1234567890123456",
                "2026-07-13",
                "True",
                "=1+1",
                "<NULL>",
            ),
        ),
        numeric_values=(
            (
                Decimal("10.50"),
                42,
                1.25,
                None,
                Decimal("1234567890123456"),
                None,
                None,
                None,
                None,
            ),
        ),
    )
    destination = tmp_path / "typed.xlsx"
    context = RecordingContext(tmp_path, active, response=str(destination))
    options = xlsx_export.XlsxExportOptions(
        null_value="0",
        date_format="%d.%m.%Y",
    )

    xlsx_export.export_loaded_rows_to_xlsx(context, options)

    workbook = load_workbook(destination)
    try:
        sheet = workbook.active
        assert [cell.value for cell in sheet[2]] == [
            10.5,
            42,
            1.25,
            "007",
            "1234567890123456",
            "13.07.2026",
            "True",
            "=1+1",
            "0",
        ]
        assert [cell.data_type for cell in sheet[2]] == [
            "n",
            "n",
            "n",
            "s",
            "s",
            "s",
            "s",
            "s",
            "s",
        ]
        assert sheet["A2"].number_format == "0.00"
        assert sheet["H2"].value == "=1+1"
        assert sheet["H2"].data_type == "s"
        assert sheet.auto_filter.ref == "A1:I2"
    finally:
        workbook.close()


def test_column_widths_use_maximum_header_or_data_content(tmp_path):
    destination = tmp_path / "fitted.xlsx"

    xlsx_exporting.write_xlsx_result(
        destination,
        title="fitted columns",
        columns=(
            "LONG HEADER",
            "D",
            "UNICODE",
            "WIDE",
            "MULTILINE",
            "",
            "CAPPED",
        ),
        rows=(
            (
                "x",
                "medium",
                "Žluťoučký",
                "表value",
                "short\r\nlongest line",
                "",
                "x" * 100,
            ),
            ("short", "longest value", "", "", "", "", "short"),
        ),
    )

    workbook = load_workbook(destination)
    try:
        sheet = workbook.active
        dimensions = [sheet.column_dimensions[letter] for letter in "ABCDEFG"]
        margin = 17 / 7
        assert [dimension.width for dimension in dimensions] == pytest.approx(
            [
                15.03246 + margin,
                10.628 + margin,
                10.79916 + margin,
                7.56187 + margin,
                12.06709 + margin,
                3 + margin,
                60 + margin,
            ]
        )
        assert all(dimension.customWidth for dimension in dimensions)
        assert not sheet["A1"].alignment.wrap_text
        assert not sheet["A2"].alignment.wrap_text
        assert sheet["E2"].alignment.wrap_text is True
        assert not sheet["G1"].alignment.wrap_text
        assert sheet["G2"].alignment.wrap_text is True
    finally:
        workbook.close()


def test_proportional_width_uses_visual_maximum_for_header_and_data(tmp_path):
    destination = tmp_path / "proportional.xlsx"

    xlsx_exporting.write_xlsx_result(
        destination,
        title="proportional columns",
        columns=("PRIMARY_SETTLEMENT_ACCOUNT", "i" * 20, "W" * 8, "I", "I"),
        rows=(
            ("i" * 27, "W" * 8, "i" * 20, "Ω" * 4, "W" * 35),
            ("W" * 13, "short", "short", "", "short"),
        ),
    )

    workbook = load_workbook(destination)
    try:
        sheet = workbook.active
        margin = 17 / 7
        assert [sheet.column_dimensions[letter].width for letter in "ABCDE"] == pytest.approx(
            [
                31.77408 + margin,
                14.04 + margin,
                17.4612 + margin,
                4 + margin,
                60 + margin,
            ]
        )
        assert not sheet["E2"].alignment.wrap_text
    finally:
        workbook.close()


@pytest.mark.parametrize(
    ("auto_filter", "expected_header_width"),
    [(True, 15.03246), (False, 12.03246)],
)
def test_auto_filter_adds_three_units_only_to_header_width_candidate(
    tmp_path,
    auto_filter,
    expected_header_width,
):
    destination = tmp_path / f"header-filter-{auto_filter}.xlsx"

    xlsx_exporting.write_xlsx_result(
        destination,
        title="header filter allowance",
        columns=("LONG HEADER", "D"),
        rows=(("x", "longest value"),),
        auto_filter=auto_filter,
    )

    workbook = load_workbook(destination)
    try:
        sheet = workbook.active
        margin = 17 / 7
        assert [sheet.column_dimensions[letter].width for letter in "AB"] == (
            pytest.approx([expected_header_width + margin, 10.628 + margin])
        )
    finally:
        workbook.close()


def test_wrapping_starts_above_sixty_units_per_cell(tmp_path):
    destination = tmp_path / "wrap-boundary.xlsx"

    xlsx_exporting.write_xlsx_result(
        destination,
        title="wrap boundary",
        columns=("DATA", "H" * 61, "EXACT"),
        rows=(
            ("x" * 60, "short", "x" * 60),
            ("x" * 61, "short", "short"),
        ),
    )

    workbook = load_workbook(destination)
    try:
        sheet = workbook.active
        margin = 17 / 7
        assert [sheet.column_dimensions[letter].width for letter in "ABC"] == pytest.approx(
            [52.155 + margin, 60 + margin, 51.3 + margin]
        )
        assert not sheet["A1"].alignment.wrap_text
        assert not sheet["A2"].alignment.wrap_text
        assert sheet["A3"].alignment.wrap_text is True
        assert sheet["B1"].alignment.wrap_text is True
        assert not sheet["B2"].alignment.wrap_text
        assert not sheet["B3"].alignment.wrap_text
        assert not sheet["C2"].alignment.wrap_text
    finally:
        workbook.close()


def test_auto_width_can_be_disabled_without_affecting_wrapping_or_filter(tmp_path):
    destination = tmp_path / "default-widths.xlsx"
    context = RecordingContext(
        tmp_path,
        snapshot(
            columns=("LONG_VALUE", "SECOND"),
            rows=(("x" * 61, "short"),),
        ),
        response=str(destination),
    )

    xlsx_export.export_loaded_rows_to_xlsx(
        context,
        xlsx_export.XlsxExportOptions(auto_width=False),
    )

    with ZipFile(destination) as archive:
        worksheet_xml = archive.read("xl/worksheets/sheet1.xml")
    assert b"<cols" not in worksheet_xml

    workbook = load_workbook(destination)
    try:
        sheet = workbook.active
        assert list(sheet.column_dimensions) == []
        assert sheet["A2"].alignment.wrap_text is True
        assert sheet.auto_filter.ref == "A1:B2"
        assert sheet.freeze_panes == "A2"
    finally:
        workbook.close()


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
        assert [sheet.column_dimensions[letter].width for letter in "AB"] == pytest.approx(
            [4.17626 + 17 / 7, 4.10519 + 17 / 7]
        )
        assert sheet.auto_filter.ref == "A1:B1"
        assert sheet.freeze_panes == "A2"
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


def test_numeric_precision_boundary_uses_significant_digits(tmp_path):
    destination = tmp_path / "numeric-precision.xlsx"

    xlsx_exporting.write_xlsx_result(
        destination,
        title="numeric precision",
        columns=("FIFTEEN", "SIXTEEN", "TRAILING_ZERO"),
        rows=(("123456789012345", "1234567890123456", "123456789012345.0"),),
        numeric_values=(
            (
                Decimal("123456789012345"),
                Decimal("1234567890123456"),
                Decimal("123456789012345.0"),
            ),
        ),
    )

    workbook = load_workbook(destination)
    try:
        sheet = workbook.active
        assert sheet["A2"].value == 123456789012345
        assert sheet["A2"].data_type == "n"
        assert sheet["B2"].value == "1234567890123456"
        assert sheet["B2"].data_type == "s"
        assert sheet["C2"].value == 123456789012345
        assert sheet["C2"].data_type == "n"
        assert sheet["C2"].number_format == "0.0"
        assert sheet.column_dimensions["A"].width == pytest.approx(15 + 17 / 7)
    finally:
        workbook.close()


def test_fixed_scale_number_format_respects_excel_character_limit(tmp_path):
    destination = tmp_path / "fixed-scale-limit.xlsx"
    accepted = "1." + "0" * 253
    accepted_format = "0." + "0" * 253
    rejected = "1." + "0" * 254

    xlsx_exporting.write_xlsx_result(
        destination,
        title="fixed scale limit",
        columns=("ACCEPTED", "REJECTED"),
        rows=((accepted, rejected),),
        numeric_values=((Decimal(accepted), Decimal(rejected)),),
    )

    workbook = load_workbook(destination)
    try:
        sheet = workbook.active
        assert sheet["A2"].value == 1
        assert sheet["A2"].data_type == "n"
        assert sheet["A2"].number_format == accepted_format
        assert len(sheet["A2"].number_format) == 255
        assert sheet["B2"].value == rejected
        assert sheet["B2"].data_type == "s"
    finally:
        workbook.close()


@pytest.mark.parametrize(
    ("display", "numeric_value"),
    [
        ("1E+309", Decimal("1E+309")),
        ("1E-309", Decimal("1E-309")),
        ("NaN", Decimal("NaN")),
        ("Infinity", Decimal("Infinity")),
        ("inf", float("inf")),
        ("nan", float("nan")),
    ],
)
def test_out_of_range_and_nonfinite_numeric_values_fall_back_to_text(
    tmp_path,
    display,
    numeric_value,
):
    destination = tmp_path / "unsupported-number.xlsx"

    xlsx_exporting.write_xlsx_result(
        destination,
        title="unsupported number",
        columns=("VALUE",),
        rows=((display,),),
        numeric_values=((numeric_value,),),
    )

    workbook = load_workbook(destination)
    try:
        cell = workbook.active["A2"]
        assert cell.value == display
        assert cell.data_type == "s"
    finally:
        workbook.close()


@pytest.mark.parametrize(
    ("rows", "numeric_values"),
    [
        ((("1", "2"),), ((1,),)),
        ((("1", "2"),), ((1, 2), (3, 4))),
        ((("1", "2"),), ((2, None),)),
        ((("True", "2"),), ((True, None),)),
    ],
)
def test_malformed_or_mismatched_numeric_values_fall_back_to_text(
    tmp_path,
    rows,
    numeric_values,
):
    destination = tmp_path / "invalid-numeric-values.xlsx"

    xlsx_exporting.write_xlsx_result(
        destination,
        title="invalid numeric values",
        columns=("A", "B"),
        rows=rows,
        numeric_values=numeric_values,
    )

    workbook = load_workbook(destination)
    try:
        assert [cell.value for cell in workbook.active[2]] == list(rows[0])
        assert [cell.data_type for cell in workbook.active[2]] == ["s", "s"]
    finally:
        workbook.close()


def test_auto_filter_covers_header_and_loaded_rows_through_multiletter_column(
    tmp_path,
):
    destination = tmp_path / "filtered.xlsx"
    columns = tuple(f"COLUMN_{index}" for index in range(1, 28))
    rows = (
        tuple(f"first-{index}" for index in range(1, 28)),
        tuple(f"second-{index}" for index in range(1, 28)),
    )

    xlsx_exporting.write_xlsx_result(
        destination,
        title="filtered values",
        columns=columns,
        rows=rows,
        auto_filter=True,
    )

    workbook = load_workbook(destination)
    try:
        sheet = workbook.active
        assert sheet.auto_filter.ref == "A1:AA3"
        assert not any(sheet.row_dimensions[index].hidden for index in (2, 3))
    finally:
        workbook.close()


def test_auto_filter_can_be_disabled_for_direct_writer(tmp_path):
    destination = tmp_path / "unfiltered.xlsx"

    xlsx_exporting.write_xlsx_result(
        destination,
        title="unfiltered values",
        columns=("A", "B"),
        rows=(("one", "two"),),
        auto_filter=False,
    )

    workbook = load_workbook(destination)
    try:
        assert workbook.active.auto_filter.ref is None
        assert workbook.active.freeze_panes == "A2"
    finally:
        workbook.close()


def test_freeze_top_row_can_be_disabled_for_direct_writer(tmp_path):
    destination = tmp_path / "scrolling-header.xlsx"

    xlsx_exporting.write_xlsx_result(
        destination,
        title="scrolling header",
        columns=("A", "B"),
        rows=(("one", "two"),),
        freeze_top_row=False,
    )

    workbook = load_workbook(destination)
    try:
        assert workbook.active.freeze_panes is None
        assert workbook.active.auto_filter.ref == "A1:B2"
    finally:
        workbook.close()


def test_auto_filter_ignores_columnless_direct_writer_result(tmp_path):
    destination = tmp_path / "columnless.xlsx"

    xlsx_exporting.write_xlsx_result(
        destination,
        title="columnless result",
        columns=(),
        rows=(),
        auto_filter=True,
    )

    workbook = load_workbook(destination)
    try:
        assert workbook.active.auto_filter.ref is None
        assert workbook.active.freeze_panes is None
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
    assert context.statuses == ["Export unavailable while an insert draft is active; commit or cancel the draft first"]


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
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_AUTO_FILTER", "off")
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_AUTO_WIDTH", "off")
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_FREEZE_TOP_ROW", "off")
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
        assert workbook.active.auto_filter.ref is None
        assert list(workbook.active.column_dimensions) == []
        assert workbook.active.freeze_panes is None
    finally:
        workbook.close()


def test_unset_environment_uses_default_options(monkeypatch, tmp_path):
    for name in (
        "PLSQLWKS_XLSX_EXPORT_NULL_VALUE",
        "PLSQLWKS_XLSX_EXPORT_THEME",
        "PLSQLWKS_XLSX_EXPORT_DATE_FORMAT",
        "PLSQLWKS_XLSX_EXPORT_AUTO_FILTER",
        "PLSQLWKS_XLSX_EXPORT_AUTO_WIDTH",
        "PLSQLWKS_XLSX_EXPORT_FREEZE_TOP_ROW",
    ):
        monkeypatch.delenv(name, raising=False)
    plugin = xlsx_export.create_plugin()
    destination = tmp_path / "defaults.xlsx"
    context = RecordingContext(
        tmp_path,
        snapshot(columns=("NULL", "DATE"), rows=(("<NULL>", "2026-07-13"),)),
        response="defaults.xlsx",
    )

    plugin.commands[0].handler(context)

    workbook = load_workbook(destination)
    try:
        sheet = workbook.active
        assert [cell.value for cell in sheet[2]] == [None, "2026-07-13"]
        assert sheet["A2"].fill.fgColor.rgb.endswith("FFFFFF")
        assert sheet.auto_filter.ref == "A1:B2"
        assert list(sheet.column_dimensions) == ["A", "B"]
        assert sheet.freeze_panes == "A2"
    finally:
        workbook.close()


@pytest.mark.parametrize(
    ("environment_value", "expected_ref"),
    [
        ("1", "A1:A2"),
        (" yes ", "A1:A2"),
        ("TRUE", "A1:A2"),
        ("On", "A1:A2"),
        ("0", None),
        (" no ", None),
        ("FALSE", None),
        ("Off", None),
    ],
)
def test_environment_auto_filter_boolean_values(
    monkeypatch,
    tmp_path,
    environment_value,
    expected_ref,
):
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_AUTO_FILTER", environment_value)
    plugin = xlsx_export.create_plugin()
    destination = tmp_path / "environment-filter.xlsx"
    context = RecordingContext(
        tmp_path,
        snapshot(),
        response=str(destination),
    )

    plugin.commands[0].handler(context)

    workbook = load_workbook(destination)
    try:
        assert workbook.active.auto_filter.ref == expected_ref
    finally:
        workbook.close()


def test_malformed_environment_auto_filter_value_uses_enabled_default(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_AUTO_FILTER", "sometimes")
    plugin = xlsx_export.create_plugin()
    destination = tmp_path / "fallback-filter.xlsx"
    context = RecordingContext(
        tmp_path,
        snapshot(),
        response=str(destination),
    )

    plugin.commands[0].handler(context)

    workbook = load_workbook(destination)
    try:
        assert workbook.active.auto_filter.ref == "A1:A2"
    finally:
        workbook.close()


@pytest.mark.parametrize(
    ("configured_value", "later_value", "expected_dimensions"),
    [
        ("true", "false", ["A"]),
        ("false", "true", []),
    ],
)
def test_environment_auto_width_boolean_is_captured_by_factory(
    monkeypatch,
    tmp_path,
    configured_value,
    later_value,
    expected_dimensions,
):
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_AUTO_WIDTH", configured_value)
    plugin = xlsx_export.create_plugin()
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_AUTO_WIDTH", later_value)
    destination = tmp_path / "environment-width.xlsx"
    context = RecordingContext(
        tmp_path,
        snapshot(),
        response=str(destination),
    )

    plugin.commands[0].handler(context)

    workbook = load_workbook(destination)
    try:
        assert list(workbook.active.column_dimensions) == expected_dimensions
    finally:
        workbook.close()


def test_malformed_environment_auto_width_value_uses_enabled_default(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_AUTO_WIDTH", "sometimes")
    plugin = xlsx_export.create_plugin()
    destination = tmp_path / "fallback-width.xlsx"
    context = RecordingContext(
        tmp_path,
        snapshot(),
        response=str(destination),
    )

    plugin.commands[0].handler(context)

    workbook = load_workbook(destination)
    try:
        assert list(workbook.active.column_dimensions) == ["A"]
    finally:
        workbook.close()


@pytest.mark.parametrize(
    ("configured_value", "later_value", "expected_pane"),
    [("on", "off", "A2"), ("off", "on", None)],
)
def test_environment_freeze_top_row_boolean_is_captured_by_factory(
    monkeypatch,
    tmp_path,
    configured_value,
    later_value,
    expected_pane,
):
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_FREEZE_TOP_ROW", configured_value)
    plugin = xlsx_export.create_plugin()
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_FREEZE_TOP_ROW", later_value)
    destination = tmp_path / "environment-freeze.xlsx"
    context = RecordingContext(tmp_path, snapshot(), response=str(destination))

    plugin.commands[0].handler(context)

    workbook = load_workbook(destination)
    try:
        assert workbook.active.freeze_panes == expected_pane
    finally:
        workbook.close()


def test_malformed_environment_freeze_top_row_value_uses_enabled_default(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_FREEZE_TOP_ROW", "sometimes")
    plugin = xlsx_export.create_plugin()
    destination = tmp_path / "fallback-freeze.xlsx"
    context = RecordingContext(tmp_path, snapshot(), response=str(destination))

    plugin.commands[0].handler(context)

    workbook = load_workbook(destination)
    try:
        assert workbook.active.freeze_panes == "A2"
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
    assert context.statuses == ["XLSX export failed: XLSX export requires the optional 'openpyxl' package"]
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


def test_cancellable_xlsx_writer_preserves_existing_destination(tmp_path):
    destination = tmp_path / "cancelled.xlsx"
    destination.write_bytes(b"old")
    cancelled = False

    def progress(current: int, total: int) -> None:
        nonlocal cancelled
        if current == 1:
            cancelled = True

    with pytest.raises(ExportCancelled):
        xlsx_export.write_xlsx_snapshot(
            destination,
            snapshot(rows=(("one",), ("two",))),
            xlsx_export.XlsxExportOptions(),
            on_progress=progress,
            cancelled=lambda: cancelled,
        )

    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []
