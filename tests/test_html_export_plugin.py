from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest

import plsqlwks.exporting as exporting_module
from plsqlwks.html_exporting import DEFAULT_HTML_TITLE, render_html_result
from plsqlwks.plugins import PLUGIN_API_VERSION, ResultSnapshot
from plsqlwks.plugins import html_export


pytestmark = pytest.mark.plugin


class RecordingContext:
    """Small PluginContext test double with no App or database capabilities."""

    def __init__(
        self,
        results_dir: Path,
        snapshot: ResultSnapshot | None,
        *,
        insert_draft: bool = False,
        response: str | None = None,
        overwrite: bool = True,
        read_only: bool = False,
    ) -> None:
        self._results_dir = results_dir
        self.snapshot = snapshot
        self.insert_draft = insert_draft
        self.response = response
        self.overwrite = overwrite
        self.read_only = read_only
        self.statuses: list[str] = []
        self.prompts: list[tuple[str, str, bool]] = []
        self.confirmed_paths: list[Path] = []
        self.errors: list[tuple[str, Exception]] = []
        self.calls: list[str] = []

    @property
    def results_dir(self) -> Path:
        self.calls.append("results_dir")
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
        self.calls.append("status")
        self.statuses.append(message)

    def report_error(self, title: str, error: Exception) -> None:
        self.calls.append("error")
        self.errors.append((title, error))


class DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str | None]] = []
        self.text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.tags.append(tag)
        self.attributes.extend(attrs)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def snapshot(
    *,
    title: str = "Data",
    columns: tuple[str, ...] = ("VALUE",),
    rows: tuple[tuple[str, ...], ...] = (("1",),),
    has_more: bool = False,
) -> ResultSnapshot:
    return ResultSnapshot(title, columns, rows, has_more)


def test_plugin_metadata_uses_api_v1_without_shortcut():
    plugin = html_export.create_plugin(html_export.HtmlExportOptions())
    command = plugin.commands[0]

    assert PLUGIN_API_VERSION == 1
    assert (plugin.id, plugin.name, plugin.api_version) == (
        "html-export",
        "HTML result export",
        1,
    )
    assert (command.id, command.section, command.title) == (
        "export-loaded-rows",
        "Results",
        "Export loaded rows to HTML",
    )
    assert callable(command.handler)
    assert command.shortcut == ""
    assert command.keywords == "export html web browser table result loaded rows"


def test_html_export_options_have_documented_defaults():
    assert html_export.HtmlExportOptions() == html_export.HtmlExportOptions(
        null_value="",
        theme="bright",
        date_format="",
    )


def test_renderer_writes_complete_standalone_document_and_loaded_rows():
    document = render_html_result(
        title="Current data",
        columns=("ID", "Label"),
        rows=(("1", "first"), ("2", "second")),
        has_more=False,
    )

    assert document.startswith('<!doctype html>\n<html lang="en">\n<head>\n')
    assert '<meta charset="utf-8">' in document
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in document
    assert "<title>Current data</title>" in document
    assert "Current data" not in document.partition("<body>\n")[2]
    assert "<h1" not in document
    assert "2 loaded row(s)" in document
    assert document.index("  </div>\n") < document.index(
        '  <p class="summary">2 loaded row(s)</p>\n'
    )
    assert document.count('<th scope="col">') == 2
    assert document.count("<tbody>") == 1
    assert document.count("<td>") == 4
    assert "Additional rows are available" not in document
    assert document.endswith("</body>\n</html>\n")
    assert "\r" not in document
    assert "<script" not in document.lower()
    assert "http://" not in document and "https://" not in document


def test_renderer_selects_only_static_bright_or_dark_css_and_prints_bright():
    arguments = {
        "title": "Theme",
        "columns": ("VALUE",),
        "rows": (("value",),),
        "has_more": False,
    }

    default_document = render_html_result(**arguments)
    bright_document = render_html_result(**arguments, theme="bright")
    dark_document = render_html_result(**arguments, theme="dark")

    assert default_document == bright_document
    assert ":root { color-scheme: light; }" in bright_document
    assert "body { background: #fff; color: #202124; }" in bright_document
    assert ":root { color-scheme: dark; }" not in bright_document
    assert ":root { color-scheme: dark; }" in dark_document
    assert "body { background: #17191d; color: #f1f3f4; }" in dark_document
    assert "body { background: #fff; color: #202124; }" not in dark_document
    assert "body { margin: 0; background: #fff; color: #000; }" in dark_document


@pytest.mark.parametrize("theme", ["", "Bright", "night", "dark; body { display: none; }"])
def test_renderer_rejects_unknown_theme_before_returning_markup(theme):
    with pytest.raises(ValueError, match="theme must be 'bright' or 'dark'"):
        render_html_result(
            title="Theme",
            columns=("VALUE",),
            rows=(("value",),),
            has_more=False,
            theme=theme,
        )


def test_renderer_uses_fallback_title_and_keeps_headers_for_zero_rows():
    document = render_html_result(
        title="",
        columns=("A", "B"),
        rows=(),
        has_more=False,
    )

    assert f"<title>{DEFAULT_HTML_TITLE}</title>" in document
    assert DEFAULT_HTML_TITLE not in document.partition("<body>\n")[2]
    assert "<h1" not in document
    assert "0 loaded row(s)" in document
    assert '<th scope="col">A</th>' in document
    assert '<th scope="col">B</th>' in document
    assert "      <tbody>\n      </tbody>" in document
    assert "<td>" not in document


def test_renderer_reports_unexported_continuation_rows():
    document = render_html_result(
        title="Data",
        columns=("A",),
        rows=(("loaded",),),
        has_more=True,
    )

    assert "1 loaded row(s)" in document
    assert "Additional rows are available in PLSQLWKS and were not exported." in document
    assert document.index("  </div>\n") < document.index("1 loaded row(s)") < document.index(
        "Additional rows are available"
    )


@pytest.mark.parametrize(
    "value",
    [
        "ordinary",
        "comma,value",
        'double " quote',
        "single ' quote",
        "ampersand & value",
        "less < greater >",
        "embedded\nline",
        "embedded\r\nlines",
        "repeated   spaces",
        "Příliš žluťoučký kůň — 東京",
        "",
        "x" * 20_000,
    ],
)
def test_renderer_preserves_values_as_escaped_text(value):
    document = render_html_result(
        title="Values",
        columns=("VALUE",),
        rows=((value,),),
        has_more=False,
    )
    parser = DocumentParser()
    parser.feed(document)

    if value:
        assert value in parser.text
    else:
        assert "<td></td>" in document
    assert "white-space: pre-wrap" in document


def test_renderer_escapes_hostile_title_header_and_cell_without_active_content():
    hostile_title = '<script>alert(1)</script> & "title"'
    hostile_header = '</th><img src=x onerror=alert(1)><th>'
    hostile_cell = '</td><script>alert("x")</script><td>'
    already_escaped = '&lt;already escaped&gt; " onmouseover="alert(1)'
    document = render_html_result(
        title=hostile_title,
        columns=(hostile_header, "TEXT"),
        rows=((hostile_cell, already_escaped),),
        has_more=False,
    )
    parser = DocumentParser()
    parser.feed(document)

    assert hostile_title in parser.text
    assert hostile_header in parser.text
    assert hostile_cell in parser.text
    assert already_escaped in parser.text
    assert {"script", "img", "iframe", "form", "link"}.isdisjoint(parser.tags)
    assert all(not name.lower().startswith("on") for name, _ in parser.attributes)
    assert "&amp;lt;already escaped&amp;gt;" in document
    assert "&amp;amp;lt;already escaped" not in document
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in document
    assert "&lt;/td&gt;&lt;script&gt;" in document


@pytest.mark.parametrize(
    ("columns", "rows", "message"),
    [
        (("A", "B"), (("only one",),), "result row 1 has 1 value(s); expected 2"),
        (("A",), (("one", "extra"),), "result row 1 has 2 value(s); expected 1"),
    ],
)
def test_renderer_rejects_mismatched_row_width(columns, rows, message):
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        render_html_result(title="Bad", columns=columns, rows=rows, has_more=False)


def test_deterministic_default_filename_and_existing_context_contract(monkeypatch, tmp_path):
    fixed = datetime(2026, 7, 12, 9, 8, 7)
    monkeypatch.setattr(html_export, "local_now", lambda: fixed)
    context = RecordingContext(tmp_path, snapshot(), response=None)

    html_export.export_loaded_rows_to_html(context)

    assert context.prompts == [
        ("Export loaded rows to HTML", str(tmp_path / "result_20260712_090807.html"), True)
    ]
    assert context.statuses[-1] == "Export cancelled"
    assert context.calls[:3] == ["draft", "result", "results_dir"]


def test_explicit_options_are_captured_and_transform_only_cells(tmp_path):
    active = ResultSnapshot(
        "2026-07-13 <NULL>",
        ("2026-07-13", "<NULL>", "TIMESTAMP", "TEXT"),
        (
            (
                "2026-07-13",
                "<NULL>",
                "2026-07-13 14:15:16.123456+02:00",
                "prefix 2026-07-13",
            ),
        ),
        False,
    )
    context = RecordingContext(tmp_path, active, response="configured.html")
    plugin = html_export.create_plugin(
        html_export.HtmlExportOptions(
            null_value="(none)",
            theme="dark",
            date_format="%d.%m.%Y %H:%M:%S %z",
        )
    )

    plugin.commands[0].handler(context)

    document = (tmp_path / "configured.html").read_text(encoding="utf-8")
    assert "<title>2026-07-13 &lt;NULL&gt;</title>" in document
    assert '<th scope="col">2026-07-13</th>' in document
    assert '<th scope="col">&lt;NULL&gt;</th>' in document
    assert "<td>13.07.2026 00:00:00 </td>" in document
    assert "<td>(none)</td>" in document
    assert "<td>13.07.2026 14:15:16 +0200</td>" in document
    assert "<td>prefix 2026-07-13</td>" in document
    assert ":root { color-scheme: dark; }" in document


def test_empty_null_replacement_and_empty_date_format_are_supported(tmp_path):
    context = RecordingContext(
        tmp_path,
        snapshot(
            columns=("NULL_VALUE", "DATE_VALUE", "LITERAL"),
            rows=(("<NULL>", "2026-07-13", " <NULL> "),),
        ),
        response="empty-null.html",
    )

    html_export.export_loaded_rows_to_html(
        context,
        html_export.HtmlExportOptions(null_value="", date_format=""),
    )

    document = (tmp_path / "empty-null.html").read_text(encoding="utf-8")
    assert "<td></td>" in document
    assert "<td>2026-07-13</td>" in document
    assert "<td> &lt;NULL&gt; </td>" in document


def test_date_format_preserves_nonmatching_and_invalid_iso_display_values(tmp_path):
    original = (
        "prefix 2026-07-13",
        "2026-02-30",
        "2026-07-13T14:15:16",
        "2026-07-13 25:15:16",
        "ordinary",
    )
    context = RecordingContext(
        tmp_path,
        snapshot(columns=("A", "B", "C", "D", "E"), rows=(original,)),
        response="unchanged.html",
    )

    html_export.export_loaded_rows_to_html(
        context,
        html_export.HtmlExportOptions(date_format="%d/%m/%Y"),
    )

    document = (tmp_path / "unchanged.html").read_text(encoding="utf-8")
    for value in original:
        assert f"<td>{value}</td>" in document


def test_zero_argument_factory_reads_plugin_specific_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("PLSQLWKS_HTML_EXPORT_NULL_VALUE", "NULL")
    monkeypatch.setenv("PLSQLWKS_HTML_EXPORT_THEME", "dark")
    monkeypatch.setenv("PLSQLWKS_HTML_EXPORT_DATE_FORMAT", "%Y/%m/%d")
    plugin = html_export.create_plugin()
    context = RecordingContext(
        tmp_path,
        snapshot(columns=("NULL", "DATE"), rows=(("<NULL>", "2026-07-13"),)),
        response="environment.html",
    )

    plugin.commands[0].handler(context)

    document = (tmp_path / "environment.html").read_text(encoding="utf-8")
    assert "<td>NULL</td>" in document
    assert "<td>2026/07/13</td>" in document
    assert ":root { color-scheme: dark; }" in document


def test_unset_environment_uses_default_options(monkeypatch, tmp_path):
    for name in (
        "PLSQLWKS_HTML_EXPORT_NULL_VALUE",
        "PLSQLWKS_HTML_EXPORT_THEME",
        "PLSQLWKS_HTML_EXPORT_DATE_FORMAT",
    ):
        monkeypatch.delenv(name, raising=False)
    plugin = html_export.create_plugin()
    context = RecordingContext(
        tmp_path,
        snapshot(columns=("NULL", "DATE"), rows=(("<NULL>", "2026-07-13"),)),
        response="defaults.html",
    )

    plugin.commands[0].handler(context)

    document = (tmp_path / "defaults.html").read_text(encoding="utf-8")
    assert "<td></td>" in document
    assert "<td>2026-07-13</td>" in document
    assert ":root { color-scheme: light; }" in document


@pytest.mark.parametrize("response", [None, ""])
def test_cancelled_filename_prompt_writes_nothing(tmp_path, response):
    context = RecordingContext(tmp_path, snapshot(), response=response)

    html_export.export_loaded_rows_to_html(context)

    assert context.statuses[-1] == "Export cancelled"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("active", [None, ResultSnapshot("Message", (), (), False)])
def test_no_tabular_result_does_not_prompt_or_create_file(tmp_path, active):
    context = RecordingContext(tmp_path, active, response="unused.html")

    html_export.export_loaded_rows_to_html(context)

    assert context.statuses == ["No table result is available for export"]
    assert context.prompts == []
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_insert_draft_is_checked_before_snapshot_and_prompt(tmp_path):
    context = RecordingContext(
        tmp_path,
        snapshot(rows=(("temporary draft",),)),
        insert_draft=True,
        response="out.html",
    )

    html_export.export_loaded_rows_to_html(context)

    assert context.calls == ["draft", "status"]
    assert context.statuses == [
        "Export unavailable while an insert draft is active; commit or cancel the draft first"
    ]
    assert context.prompts == []


def test_relative_path_creates_parent_and_exports_exact_snapshot(tmp_path):
    active = ResultSnapshot(
        "Rows",
        ("NAME", "NOTE"),
        (("kůň", "first"), ("東京", "second")),
        True,
    )
    context = RecordingContext(tmp_path, active, response="nested/export.html")

    html_export.export_loaded_rows_to_html(context)

    path = (tmp_path / "nested" / "export.html").resolve()
    document = path.read_text(encoding="utf-8")
    assert "kůň" in document and "東京" in document
    assert document.count("<td>") == 4
    assert context.statuses[-1] == (
        f"Exported 2 loaded row(s) to {path}; additional rows are available"
    )
    assert context.snapshot is active
    assert active.rows == (("kůň", "first"), ("東京", "second"))
    assert active.has_more is True
    assert context.confirmed_paths == []


def test_absolute_path_and_explicit_filename_without_extension_are_accepted(tmp_path):
    path = (tmp_path / "absolute-result").resolve()
    context = RecordingContext(tmp_path / "other", snapshot(), response=str(path))

    html_export.export_loaded_rows_to_html(context)

    assert path.is_file()
    assert not path.with_suffix(".html").exists()
    assert context.statuses[-1] == f"Exported 1 loaded row(s) to {path}"


def test_path_expands_home_consistently(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    context = RecordingContext(tmp_path / "results", snapshot(), response="~/result.html")

    html_export.export_loaded_rows_to_html(context)

    assert (home / "result.html").is_file()


def test_overwrite_rejected_preserves_existing_destination(tmp_path):
    path = tmp_path / "out.html"
    old_bytes = b"old document\r\n"
    path.write_bytes(old_bytes)
    context = RecordingContext(tmp_path, snapshot(), response="out.html", overwrite=False)

    html_export.export_loaded_rows_to_html(context)

    assert path.read_bytes() == old_bytes
    assert context.confirmed_paths == [path.resolve()]
    assert context.statuses[-1] == "Export cancelled"
    assert context.errors == []


def test_overwrite_accepted_replaces_destination(tmp_path):
    path = tmp_path / "out.html"
    path.write_text("old", encoding="utf-8")
    context = RecordingContext(tmp_path, snapshot(), response="out.html", overwrite=True)

    html_export.export_loaded_rows_to_html(context)

    assert path.read_text(encoding="utf-8").startswith("<!doctype html>\n")
    assert context.confirmed_paths == [path.resolve()]


def test_invalid_environment_theme_is_command_failure_and_preserves_destination(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("PLSQLWKS_HTML_EXPORT_THEME", "user-supplied-css")

    # Creating the zero-argument factory is the loader/startup operation.  It
    # must capture, but not execute or validate, environment configuration.
    plugin = html_export.create_plugin()
    path = tmp_path / "out.html"
    old_bytes = b"existing document"
    path.write_bytes(old_bytes)
    context = RecordingContext(tmp_path, snapshot(), response="out.html", overwrite=True)

    plugin.commands[0].handler(context)

    assert path.read_bytes() == old_bytes
    assert context.confirmed_paths == [path.resolve()]
    assert context.statuses[-1] == (
        "HTML export failed: HTML export theme must be 'bright' or 'dark'"
    )
    assert len(context.errors) == 1
    assert context.errors[0][0] == "HTML export failed"
    assert isinstance(context.errors[0][1], ValueError)
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_row_validation_failure_preserves_destination_and_reports_html_error(tmp_path):
    path = tmp_path / "out.html"
    path.write_bytes(b"old")
    active = snapshot(columns=("A", "B"), rows=(("one",),), has_more=True)
    context = RecordingContext(tmp_path, active, response="out.html")

    html_export.export_loaded_rows_to_html(context)

    assert path.read_bytes() == b"old"
    assert context.statuses[-1].startswith("HTML export failed: result row 1")
    assert len(context.errors) == 1
    assert context.errors[0][0] == "HTML export failed"
    assert isinstance(context.errors[0][1], ValueError)
    assert context.snapshot is active
    assert active.has_more is True
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_temporary_file_creation_failure_is_reported_without_false_success(
    monkeypatch,
    tmp_path,
):
    context = RecordingContext(tmp_path, snapshot(), response="out.html")
    failure = OSError("temporary file unavailable\ninternal detail")

    def fail_temporary_file(*args, **kwargs):
        raise failure

    monkeypatch.setattr(exporting_module.tempfile, "NamedTemporaryFile", fail_temporary_file)

    html_export.export_loaded_rows_to_html(context)

    assert context.statuses[-1] == "HTML export failed: temporary file unavailable"
    assert context.errors == [("HTML export failed", failure)]
    assert not (tmp_path / "out.html").exists()


def test_close_failure_preserves_destination_and_cleans_temporary_file(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "out.html"
    old_bytes = b"existing document"
    path.write_bytes(old_bytes)
    context = RecordingContext(tmp_path, snapshot(), response="out.html", overwrite=True)
    real_named_temporary_file = exporting_module.tempfile.NamedTemporaryFile
    temporary_paths: list[Path] = []

    class FailAfterClose:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            entered = self.handle.__enter__()
            temporary_paths.append(Path(entered.name))
            return entered

        def __exit__(self, exc_type, exc_value, traceback):
            self.handle.__exit__(exc_type, exc_value, traceback)
            raise OSError("close failure")

    def fail_after_close(*args, **kwargs):
        return FailAfterClose(real_named_temporary_file(*args, **kwargs))

    monkeypatch.setattr(
        exporting_module.tempfile,
        "NamedTemporaryFile",
        fail_after_close,
    )

    html_export.export_loaded_rows_to_html(context)

    assert path.read_bytes() == old_bytes
    assert context.statuses[-1] == "HTML export failed: close failure"
    assert context.errors and context.errors[0][0] == "HTML export failed"
    assert temporary_paths and all(not temporary.exists() for temporary in temporary_paths)
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_path_preparation_failure_gets_html_specific_error(monkeypatch, tmp_path):
    context = RecordingContext(tmp_path, snapshot(), response="out.html")
    failure = OSError("cannot inspect destination")

    def fail_preparation(context, label, default_filename):
        raise failure

    monkeypatch.setattr(html_export, "prepare_result_export", fail_preparation)

    html_export.export_loaded_rows_to_html(context)

    assert context.statuses[-1] == "HTML export failed: cannot inspect destination"
    assert context.errors == [("HTML export failed", failure)]
    assert not (tmp_path / "out.html").exists()


def test_replace_failure_preserves_destination_and_cleans_temporary_file(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "out.html"
    path.write_bytes(b"old")
    context = RecordingContext(tmp_path, snapshot(), response="out.html")
    temporary_paths: list[Path] = []

    def fail_replace(source, destination):
        temporary_paths.append(Path(source))
        assert Path(destination) == path.resolve()
        raise OSError("replace failure")

    monkeypatch.setattr(exporting_module.os, "replace", fail_replace)

    html_export.export_loaded_rows_to_html(context)

    assert path.read_bytes() == b"old"
    assert context.statuses[-1] == "HTML export failed: replace failure"
    assert context.errors and context.errors[0][0] == "HTML export failed"
    assert temporary_paths and all(not temporary.exists() for temporary in temporary_paths)
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_write_failure_reports_complete_exception_and_bounds_status(monkeypatch, tmp_path):
    path = tmp_path / "out.html"
    path.write_bytes(b"previous destination")
    active = snapshot(rows=(("unchanged",),), has_more=True)
    context = RecordingContext(tmp_path, active, response="out.html")
    failure = OSError("x" * 500)

    def fail_write(path, writer):
        raise failure

    monkeypatch.setattr(html_export, "atomic_write_text", fail_write)

    html_export.export_loaded_rows_to_html(context)

    assert context.statuses[-1].startswith("HTML export failed: ")
    assert context.statuses[-1].endswith("…")
    assert len(context.statuses[-1]) <= len("HTML export failed: ") + 160
    assert context.errors == [("HTML export failed", failure)]
    assert path.read_bytes() == b"previous destination"
    assert context.snapshot is active
    assert active.rows == (("unchanged",),)


def test_handler_does_not_catch_base_exceptions(monkeypatch, tmp_path):
    context = RecordingContext(tmp_path, snapshot(), response="out.html")

    def interrupt_render(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(html_export, "render_html_result", interrupt_render)

    with pytest.raises(KeyboardInterrupt):
        html_export.export_loaded_rows_to_html(context)

    assert context.errors == []
    assert not (tmp_path / "out.html").exists()


def test_export_is_available_in_read_only_mode_and_uses_no_database_operations(tmp_path):
    context = RecordingContext(
        tmp_path,
        snapshot(),
        response="read-only.html",
        read_only=True,
    )

    html_export.create_plugin().commands[0].handler(context)

    assert (tmp_path / "read-only.html").is_file()
    assert context.statuses[-1].startswith("Exported 1 loaded row(s)")
