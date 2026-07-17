from __future__ import annotations

import csv
import os
import stat
from io import StringIO
from pathlib import Path

import pytest

import plsqlwks.exporting as exporting_module
from plsqlwks.exporting import (
    ExportCancelled,
    atomic_write_binary,
    atomic_write_text,
    preserve_existing_posix_permissions,
    raise_if_export_cancelled,
    write_csv,
)


def test_atomic_write_binary_creates_parent_and_writes_exact_bytes(tmp_path):
    path = tmp_path / "nested" / "result.bin"
    content = b"PK\x03\x04\x00binary\xff"

    atomic_write_binary(path, lambda stream: stream.write(content))

    assert path.read_bytes() == content
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("kind", ["text", "binary"])
def test_atomic_writes_preserve_existing_destination_permissions(tmp_path, kind):
    path = tmp_path / f"result.{kind}"
    path.write_bytes(b"old")
    path.chmod(0o640)

    if kind == "text":
        atomic_write_text(path, lambda stream: stream.write("new"))
    else:
        atomic_write_binary(path, lambda stream: stream.write(b"new"))

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_atomic_write_keeps_new_destination_private(tmp_path):
    path = tmp_path / "new.txt"

    atomic_write_text(path, lambda stream: stream.write("private"))

    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_atomic_write_binary_closes_temporary_file_before_replace(tmp_path, monkeypatch):
    path = tmp_path / "result.bin"
    real_named_temporary_file = exporting_module.tempfile.NamedTemporaryFile
    real_replace = exporting_module.os.replace
    temporary_handles = []

    def record_temporary_file(*args, **kwargs):
        handle = real_named_temporary_file(*args, **kwargs)
        temporary_handles.append(handle)
        return handle

    def assert_closed_then_replace(source, destination):
        assert temporary_handles[-1].file.closed is True
        real_replace(source, destination)

    monkeypatch.setattr(
        exporting_module.tempfile,
        "NamedTemporaryFile",
        record_temporary_file,
    )
    monkeypatch.setattr(exporting_module.os, "replace", assert_closed_then_replace)

    atomic_write_binary(path, lambda stream: stream.write(b"complete"))

    assert path.read_bytes() == b"complete"


def test_atomic_write_binary_guard_runs_after_close_and_before_replace(tmp_path, monkeypatch):
    path = tmp_path / "result.bin"
    path.write_bytes(b"old")
    real_replace = exporting_module.os.replace
    events: list[str] = []

    def before_replace():
        events.append("guard")
        assert path.read_bytes() == b"old"

    def replace(source, destination):
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(exporting_module.os, "replace", replace)

    atomic_write_binary(path, lambda stream: stream.write(b"new"), before_replace=before_replace)

    assert events == ["guard", "replace"]
    assert path.read_bytes() == b"new"


def test_atomic_write_binary_guard_failure_cleans_temp_and_preserves_destination(tmp_path):
    path = tmp_path / "result.bin"
    path.write_bytes(b"old")

    with pytest.raises(RuntimeError, match="guard rejected"):
        atomic_write_binary(
            path,
            lambda stream: stream.write(b"new"),
            before_replace=lambda: (_ for _ in ()).throw(RuntimeError("guard rejected")),
        )

    assert path.read_bytes() == b"old"
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


@pytest.mark.parametrize("kind", ["text", "binary"])
def test_atomic_writers_preserve_creation_failure_without_cleanup_error(tmp_path, monkeypatch, kind):
    def fail_create(*_args, **_kwargs):
        raise OSError("temporary creation failed")

    monkeypatch.setattr(exporting_module.tempfile, "NamedTemporaryFile", fail_create)
    writer = atomic_write_text if kind == "text" else atomic_write_binary

    with pytest.raises(OSError, match="temporary creation failed"):
        writer(tmp_path / f"result.{kind}", lambda _stream: None)


def test_atomic_cleanup_failure_does_not_mask_original_writer_error(tmp_path, monkeypatch):
    real_unlink = Path.unlink

    def fail_temp_unlink(path, *args, **kwargs):
        if path.suffix == ".tmp":
            real_unlink(path, *args, **kwargs)
            raise OSError("ignored cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temp_unlink)

    with pytest.raises(RuntimeError, match="writer failed"):
        atomic_write_text(
            tmp_path / "result.txt",
            lambda _stream: (_ for _ in ()).throw(RuntimeError("writer failed")),
        )


def test_permission_preservation_returns_without_touching_paths_off_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(exporting_module.os, "name", "nt")

    preserve_existing_posix_permissions(tmp_path / "missing", tmp_path / "also-missing")


def test_raise_if_export_cancelled_handles_absent_false_and_true_callbacks():
    raise_if_export_cancelled(None)
    raise_if_export_cancelled(lambda: False)
    with pytest.raises(ExportCancelled, match="Export cancelled"):
        raise_if_export_cancelled(lambda: True)


def test_atomic_write_binary_cleans_temp_and_preserves_destination_on_base_exception(
    tmp_path,
):
    path = tmp_path / "result.bin"
    path.write_bytes(b"old")

    class StopWriting(BaseException):
        pass

    def interrupt(stream):
        stream.write(b"partial")
        raise StopWriting

    with pytest.raises(StopWriting):
        atomic_write_binary(path, interrupt)

    assert path.read_bytes() == b"old"
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_atomic_write_binary_cleans_temp_and_preserves_destination_on_replace_failure(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "result.bin"
    path.write_bytes(b"old")
    temporary_paths: list[Path] = []

    def fail_replace(source, destination):
        temporary_paths.append(Path(source))
        assert Path(source).parent == path.parent
        assert Path(destination) == path
        raise OSError("replace failure")

    monkeypatch.setattr(exporting_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failure"):
        atomic_write_binary(path, lambda stream: stream.write(b"new"))

    assert path.read_bytes() == b"old"
    assert temporary_paths and all(not temporary.exists() for temporary in temporary_paths)
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_atomic_write_text_creates_parent_and_writes_utf8_with_exact_newlines(tmp_path):
    path = tmp_path / "nested" / "result.txt"

    atomic_write_text(path, lambda stream: stream.write("Příliš\nsecond\r\n"))

    assert path.read_bytes() == "Příliš\nsecond\r\n".encode()
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_atomic_write_text_cleans_temp_and_preserves_destination_on_base_exception(tmp_path):
    path = tmp_path / "result.txt"
    path.write_text("old", encoding="utf-8")

    class StopWriting(BaseException):
        pass

    def interrupt(stream):
        stream.write("partial")
        raise StopWriting

    with pytest.raises(StopWriting):
        atomic_write_text(path, interrupt)

    assert path.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_atomic_write_text_guard_runs_before_replace_and_preserves_destination(
    tmp_path,
):
    path = tmp_path / "result.txt"
    path.write_text("external", encoding="utf-8")
    checks: list[str] = []

    def reject_replace():
        checks.append(path.read_text(encoding="utf-8"))
        raise RuntimeError("target changed")

    with pytest.raises(RuntimeError, match="target changed"):
        atomic_write_text(
            path,
            lambda stream: stream.write("local"),
            before_replace=reject_replace,
        )

    assert checks == ["external"]
    assert path.read_text(encoding="utf-8") == "external"
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_write_csv_uses_standard_escaping_utf8_and_deterministic_newlines(tmp_path):
    path = tmp_path / "nested" / "values.csv"

    write_csv(
        path,
        ("KIND", "VALUE"),
        (
            ("ordinary", "value"),
            ("empty", ""),
            ("comma", "a,b"),
            ("quote", 'a"b'),
            ("lf", "a\nb"),
            ("cr", "a\rb"),
            ("crlf", "a\r\nb"),
            ("unicode", "Příliš žluťoučký kůň"),
        ),
    )

    assert (
        path.read_bytes()
        == (
            "KIND,VALUE\n"
            "ordinary,value\n"
            "empty,\n"
            'comma,"a,b"\n'
            'quote,"a""b"\n'
            'lf,"a\nb"\n'
            'cr,"a\rb"\n'
            'crlf,"a\r\nb"\n'
            "unicode,Příliš žluťoučký kůň\n"
        ).encode()
    )


def test_write_csv_writes_header_only_for_zero_rows(tmp_path):
    path = tmp_path / "header.csv"

    write_csv(path, ("A", "B"), ())

    assert path.read_text(encoding="utf-8") == "A,B\n"


def test_write_csv_accepts_a_custom_single_character_delimiter(tmp_path):
    path = tmp_path / "semicolon.csv"

    write_csv(path, ("A", "B"), (("a;b", "value"),), delimiter=";")

    assert path.read_text(encoding="utf-8") == 'A;B\n"a;b";value\n'


def test_write_csv_formula_protection_is_opt_in(tmp_path):
    path = tmp_path / "formula.csv"

    write_csv(path, ("=HEADER",), (("=1+1",),))

    assert path.read_text(encoding="utf-8") == "=HEADER\n=1+1\n"


def test_write_csv_formula_protection_quotes_fields_and_contains_breakout_text(tmp_path):
    path = tmp_path / "protected.csv"

    write_csv(
        path,
        ("=HEADER", "PLAIN"),
        (('=1+2";=1+2', "safe"),),
        delimiter=";",
        protect_formulas=True,
    )

    assert path.read_text(encoding="utf-8") == ('"\t=HEADER";"PLAIN"\n"\t=1+2"";=1+2";"safe"\n')


@pytest.mark.parametrize(
    "prefix",
    ("=", "+", "-", "@", "\t", "\r", "\n", "\0", "＝", "＋", "－", "＠"),
)
def test_write_csv_formula_protection_covers_spreadsheet_prefixes(tmp_path, prefix):
    path = tmp_path / "protected-prefix.csv"

    write_csv(
        path,
        (f"{prefix}HEADER", "SAFE"),
        ((f"{prefix}VALUE", "ordinary"),),
        protect_formulas=True,
    )

    if prefix == "\0":
        assert path.read_bytes() == (b'"\t\0HEADER","SAFE"\n"\t\0VALUE","ordinary"\n')
        return
    with path.open(encoding="utf-8", newline="") as handle:
        exported = list(csv.reader(handle))
    assert exported == [
        [f"\t{prefix}HEADER", "SAFE"],
        [f"\t{prefix}VALUE", "ordinary"],
    ]


def test_write_csv_delegates_serialization_to_atomic_text_writer(tmp_path, monkeypatch):
    calls: list[tuple[Path, str]] = []

    def record_atomic_write(path, writer, *, before_replace=None):
        stream = StringIO(newline="")
        writer(stream)
        if before_replace is not None:
            before_replace()
        calls.append((path, stream.getvalue()))

    monkeypatch.setattr(exporting_module, "atomic_write_text", record_atomic_write)
    path = tmp_path / "delegated.csv"

    write_csv(path, ("A", "B"), (("a,b", 'a"b'),))

    assert calls == [(path, 'A,B\n"a,b","a""b"\n')]


@pytest.mark.parametrize("delimiter", ["", "::"])
def test_write_csv_rejects_delimiters_that_are_not_one_character(tmp_path, delimiter):
    path = tmp_path / "invalid.csv"

    with pytest.raises(ValueError, match="exactly one character"):
        write_csv(path, ("A",), (), delimiter=delimiter)

    assert not path.exists()


def test_write_csv_preserves_existing_destination_and_cleans_temp_on_row_failure(tmp_path):
    path = tmp_path / "result.csv"
    path.write_text("old data\n", encoding="utf-8")

    def failing_rows():
        yield ("new data",)
        raise OSError("row failure")

    with pytest.raises(OSError, match="row failure"):
        write_csv(path, ("VALUE",), failing_rows())

    assert path.read_text(encoding="utf-8") == "old data\n"
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_write_csv_preserves_existing_destination_and_cleans_temp_on_replace_failure(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "result.csv"
    path.write_text("old data\n", encoding="utf-8")
    temporary_paths: list[Path] = []

    def fail_replace(source, destination):
        temporary_paths.append(Path(source))
        assert Path(destination) == path
        raise OSError("replace failure")

    monkeypatch.setattr(exporting_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failure"):
        write_csv(path, ("VALUE",), (("new data",),))

    assert path.read_text(encoding="utf-8") == "old data\n"
    assert temporary_paths and all(not temporary.exists() for temporary in temporary_paths)
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


@pytest.mark.parametrize("cancel_after", [0, 2, 3])
def test_write_csv_cancellation_is_checked_before_rows_and_replacement(tmp_path, cancel_after):
    path = tmp_path / "cancelled.csv"
    path.write_text("old\n", encoding="utf-8")
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks > cancel_after

    with pytest.raises(ExportCancelled):
        write_csv(
            path,
            ("VALUE",),
            (("one",), ("two",)),
            cancelled=cancelled,
            total_rows=-5,
        )

    assert path.read_text(encoding="utf-8") == "old\n"
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_write_csv_reports_explicit_total_before_and_after_each_row(tmp_path):
    progress = []

    write_csv(
        tmp_path / "progress.csv",
        ("VALUE",),
        (("one",), ("two",)),
        total_rows=5,
        on_progress=lambda current, total: progress.append((current, total)),
    )

    assert progress == [(0, 5), (1, 5), (2, 5)]


def test_write_csv_allows_rows_without_a_header(tmp_path):
    path = tmp_path / "headerless.csv"

    write_csv(path, (), (("one", "two"),))

    assert path.read_text(encoding="utf-8") == "one,two\n"
