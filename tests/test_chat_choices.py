from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tools import chat_choices

ROOT = Path(__file__).resolve().parents[1]
CHAT_CHOICES = ROOT / "tools" / "chat_choices.py"


def _record(timestamp: str, record_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {"timestamp": timestamp, "type": record_type, "payload": payload}


def _session_meta(
    session_id: str,
    timestamp: str,
    *,
    parent_thread_id: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": session_id,
        "session_id": session_id,
        "timestamp": timestamp,
        "source": "cli",
        "thread_source": "user" if parent_thread_id is None else "subagent",
    }
    if parent_thread_id is not None:
        payload["parent_thread_id"] = parent_thread_id
        payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent_thread_id}}}
    return _record(timestamp, "session_meta", payload)


def _request(
    call_id: str,
    timestamp: str,
    questions: list[dict[str, object]],
    *,
    auto_resolution_ms: int | None = None,
) -> dict[str, object]:
    arguments: dict[str, object] = {"questions": questions}
    if auto_resolution_ms is not None:
        arguments["autoResolutionMs"] = auto_resolution_ms
    return _record(
        timestamp,
        "response_item",
        {
            "type": "function_call",
            "name": "request_user_input",
            "call_id": call_id,
            "arguments": json.dumps(arguments),
            "metadata": {"format_variant": True},
        },
    )


def _output(call_id: str, timestamp: str, answers: dict[str, list[str]]) -> dict[str, object]:
    return _record(
        timestamp,
        "response_item",
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(
                {"answers": {question_id: {"answers": values} for question_id, values in answers.items()}}
            ),
        },
    )


def _question(
    question_id: str,
    question: str,
    *options: tuple[str, str],
    header: str = "Choice",
) -> dict[str, object]:
    return {
        "header": header,
        "id": question_id,
        "question": question,
        "options": [{"label": label, "description": description} for label, description in options],
    }


def _write_transcript(
    codex_home: Path,
    name: str,
    records: list[dict[str, object] | str],
    *,
    archived: bool = False,
) -> Path:
    root = codex_home / ("archived_sessions" if archived else "sessions")
    path = root / "2026" / "07" / "17" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [record if isinstance(record, str) else json.dumps(record, ensure_ascii=False) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_scan_correlates_choices_and_preserves_question_answer_order(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    first_question = _question(
        "format",
        "Which format?",
        ("HTML", "Standalone report"),
        ("Text", "Plain text"),
        header="Output",
    )
    second_question = _question(
        "theme",
        "Which theme?",
        ("Deep ice", "Navy and cyan"),
        ("Bright", "White background"),
        header="Theme",
    )
    _write_transcript(
        codex_home,
        "root",
        [
            _session_meta("session-root", "2026-07-17T10:00:00.000Z"),
            _request("call-choice", "2026-07-17T10:01:00.000Z", [first_question, second_question]),
            _record(
                "2026-07-17T10:01:01.000Z",
                "response_item",
                {"type": "function_call_output", "call_id": "unrelated", "output": "SECRET TOOL OUTPUT"},
            ),
            _output(
                "call-choice",
                "2026-07-17T10:01:02.000Z",
                {
                    "theme": ["Deep ice", "user_note: Keep it calm"],
                    "format": ["HTML"],
                },
            ),
        ],
    )

    chats, stats = chat_choices.scan_choices(codex_home)

    assert stats.top_level_chats == 1
    assert len(chats) == 1
    assert [choice.question_id for choice in chats[0].choices] == ["format", "theme"]
    assert chats[0].choices[0].answers == (
        chat_choices.Answer("HTML", "Selected", "Standalone report"),
    )
    assert chats[0].choices[1].answers == (
        chat_choices.Answer("Deep ice", "Selected", "Navy and cyan"),
        chat_choices.Answer("Keep it calm", "Note"),
    )


def test_scan_includes_archives_excludes_subagents_and_deduplicates_forks(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    question = _question("mode", "Choose mode", ("Safe", "Use safeguards"), ("Fast", "Move quickly"))
    shared = [
        _request("call-shared", "2026-07-17T10:01:00.000Z", [question]),
        _output("call-shared", "2026-07-17T10:01:01.000Z", {"mode": ["Safe"]}),
    ]
    _write_transcript(
        codex_home,
        "old-active",
        [_session_meta("old-active", "2026-07-17T10:00:00.000Z"), *shared],
    )
    _write_transcript(
        codex_home,
        "new-archive",
        [
            _session_meta("new-archive", "2026-07-17T12:00:00.000Z"),
            *shared,
            _request("call-custom", "2026-07-17T12:01:00.000Z", [question]),
            _output(
                "call-custom",
                "2026-07-17T12:01:01.000Z",
                {"mode": ["None of the above", "user_note: Deliberate custom value"]},
            ),
        ],
        archived=True,
    )
    _write_transcript(
        codex_home,
        "subagent",
        [_session_meta("child", "2026-07-17T13:00:00.000Z", parent_thread_id="old-active"), *shared],
    )

    chats, stats = chat_choices.scan_choices(codex_home)

    assert stats.transcript_files == 3
    assert stats.top_level_chats == 2
    assert stats.subagent_chats == 1
    assert stats.duplicate_choices == 1
    assert [chat.transcript.session_id for chat in chats] == ["new-archive", "old-active"]
    assert [choice.call_id for choice in chats[0].choices] == ["call-custom"]
    assert chats[0].choices[0].answers == (
        chat_choices.Answer("None of the above", "Custom answer"),
        chat_choices.Answer("Deliberate custom value", "Note"),
    )


def test_scan_skips_auto_resolved_unanswered_dangling_and_malformed_records(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    question = _question("mode", "Choose mode", ("Safe", "Use safeguards"), ("Fast", "Move quickly"))
    _write_transcript(
        codex_home,
        "edge-cases",
        [
            _session_meta("edge-cases", "2026-07-17T10:00:00.000Z"),
            _request(
                "call-user",
                "2026-07-17T10:01:00.000Z",
                [question],
                auto_resolution_ms=60_000,
            ),
            _output("call-user", "2026-07-17T10:01:02.000Z", {"mode": ["Fast"]}),
            _request(
                "call-timeout",
                "2026-07-17T10:02:00.000Z",
                [question],
                auto_resolution_ms=60_000,
            ),
            _output("call-timeout", "2026-07-17T10:03:00.000Z", {"mode": ["Safe"]}),
            _request("call-empty", "2026-07-17T10:04:00.000Z", [question]),
            _output("call-empty", "2026-07-17T10:04:01.000Z", {}),
            _request("call-dangling", "2026-07-17T10:05:00.000Z", [question]),
            '{"type":"response_item","payload":{"type":"function_call","name":"request_user_input"',
        ],
    )

    chats, stats = chat_choices.scan_choices(codex_home)

    assert [choice.call_id for choice in chats[0].choices] == ["call-user"]
    assert stats.auto_resolved_prompts == 1
    assert stats.unanswered_prompts == 2
    assert stats.malformed_records == 1


class ReportParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str | None]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attributes.extend(attrs)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def test_report_is_private_standalone_escaped_deep_navy_ice_html(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    question = _question(
        "unsafe",
        "Keep <script>alert('question')</script> & Unicode Žluťoučký?",
        ("<b>Ice</b>", "Description </p><img src=https://example.invalid>"),
        header="<Output>",
    )
    _write_transcript(
        codex_home,
        "privacy",
        [
            _session_meta("privacy-session", "2026-07-17T10:00:00.000Z"),
            _record(
                "2026-07-17T10:00:01.000Z",
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "FULL USER MESSAGE SECRET"}],
                },
            ),
            _request("call-unsafe", "2026-07-17T10:01:00.000Z", [question]),
            _output(
                "call-unsafe",
                "2026-07-17T10:01:01.000Z",
                {"unsafe": ["<b>Ice</b>", "user_note: line one\nline two"]},
            ),
            _record(
                "2026-07-17T10:02:00.000Z",
                "response_item",
                {"type": "custom_tool_call_output", "call_id": "tool", "output": "UNRELATED TOOL SECRET"},
            ),
        ],
    )
    chats, stats = chat_choices.scan_choices(codex_home)

    document = chat_choices.render_report(chats, stats, datetime(2026, 7, 17, 12, tzinfo=timezone.utc))
    parser = ReportParser()
    parser.feed(document)
    text = "".join(parser.text)

    assert document.startswith("<!doctype html>\n<html lang=\"en\">\n")
    assert document.endswith("</html>\n")
    assert ":root {\n  color-scheme: dark;" in document
    for color in ("#07111f", "#0d1b2a", "#10243a", "#e6f4ff", "#9bb7ce", "#67e8f9", "#7dd3fc", "#264a64"):
        assert color in document
    assert "script" not in parser.tags
    assert not any(name in {"src", "href"} for name, _value in parser.attributes)
    assert "FULL USER MESSAGE SECRET" not in document
    assert "UNRELATED TOOL SECRET" not in document
    assert "<script>alert('question')</script>" not in document
    assert "Keep <script>alert('question')</script> & Unicode Žluťoučký?" in text
    assert "<b>Ice</b>" in text
    assert "line one\nline two" in text


def test_report_has_empty_state_for_valid_home_without_choices(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    _write_transcript(
        codex_home,
        "empty",
        [_session_meta("empty", "2026-07-17T10:00:00.000Z")],
    )

    chats, stats = chat_choices.scan_choices(codex_home)
    document = chat_choices.render_report(chats, stats, datetime(2026, 7, 17, 12, tzinfo=timezone.utc))

    assert chats == []
    assert "No explicit user-selected choices were found." in document
    assert "<strong>0</strong> choice(s)" in document


def test_cli_uses_codex_home_writes_atomically_and_prints_summary(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    question = _question("theme", "Choose theme", ("Ice", "Deep navy"), ("Bright", "White"))
    _write_transcript(
        codex_home,
        "cli",
        [
            _session_meta("cli", "2026-07-17T10:00:00.000Z"),
            _request("call-cli", "2026-07-17T10:01:00.000Z", [question]),
            _output("call-cli", "2026-07-17T10:01:01.000Z", {"theme": ["Ice"]}),
        ],
    )
    output = tmp_path / "report.html"
    output.write_text("old report", encoding="utf-8")
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)

    completed = subprocess.run(
        [sys.executable, str(CHAT_CHOICES), "--output", str(output)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.stderr == ""
    assert completed.stdout == f"Wrote 1 choice(s) from 1 chat(s) to {output}\n"
    assert output.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert "old report" not in output.read_text(encoding="utf-8")


def test_cli_rejects_missing_home_and_non_html_output_without_replacing_destination(tmp_path: Path) -> None:
    output = tmp_path / "report.html"
    output.write_text("keep me", encoding="utf-8")

    missing_home = subprocess.run(
        [
            sys.executable,
            str(CHAT_CHOICES),
            "--codex-home",
            str(tmp_path / "missing"),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    bad_extension = subprocess.run(
        [
            sys.executable,
            str(CHAT_CHOICES),
            "--codex-home",
            str(tmp_path),
            "--output",
            str(tmp_path / "report.txt"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert missing_home.returncode == 1
    assert "Codex home is not a directory" in missing_home.stderr
    assert output.read_text(encoding="utf-8") == "keep me"
    assert bad_extension.returncode == 1
    assert "--output must name a .html file" in bad_extension.stderr


def test_atomic_write_failure_preserves_existing_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "report.html"
    output.write_text("existing", encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("replacement failed")

    monkeypatch.setattr(chat_choices.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replacement failed"):
        chat_choices.atomic_write_report(output, "new")

    assert output.read_text(encoding="utf-8") == "existing"
    assert list(tmp_path.glob(".report.html.*.tmp")) == []
