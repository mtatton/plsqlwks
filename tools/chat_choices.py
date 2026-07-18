#!/usr/bin/env python3
"""Write explicit choices from local Codex chats as a private HTML report."""

from __future__ import annotations

import argparse
import html
import json
import os
import stat
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

SESSION_META_MARKER = b'"session_meta"'
REQUEST_USER_INPUT_MARKER = b'"request_user_input"'
FUNCTION_OUTPUT_MARKER = b'"function_call_output"'
USER_NOTE_PREFIX = "user_note:"

ICE_STYLESHEET = """\
:root {
  color-scheme: dark;
  --bg: #07111f;
  --surface: #0d1b2a;
  --surface-raised: #10243a;
  --text: #e6f4ff;
  --muted: #9bb7ce;
  --cyan: #67e8f9;
  --ice: #7dd3fc;
  --border: #264a64;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 16px/1.55 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
main { width: min(1100px, calc(100% - 2rem)); margin: 0 auto; padding: 2.5rem 0 4rem; }
h1, h2, h3, p { margin-top: 0; }
h1 { margin-bottom: .4rem; color: var(--cyan); font-size: clamp(1.9rem, 4vw, 3rem); }
.intro, .meta, .description, .empty, .scan-note { color: var(--muted); }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: .8rem; margin: 1.5rem 0; }
.metric, .empty, .scan-note {
  border: 1px solid var(--border);
  border-radius: .8rem;
  background: var(--surface);
  padding: 1rem;
}
.metric strong { display: block; color: var(--ice); font-size: 1.6rem; }
.chat { margin-top: 1rem; overflow: hidden; border: 1px solid var(--border); border-radius: .9rem; background: var(--surface); }
.chat > summary { cursor: pointer; padding: 1rem 1.1rem; color: var(--ice); font-weight: 700; }
.chat[open] > summary { border-bottom: 1px solid var(--border); }
.chat-meta { display: flex; flex-wrap: wrap; gap: .55rem 1rem; padding: .8rem 1.1rem 0; color: var(--muted); font-size: .9rem; }
.badge { border: 1px solid var(--border); border-radius: 999px; padding: .08rem .55rem; background: var(--surface-raised); }
.choices { display: grid; gap: .9rem; padding: 1rem; }
.choice { border: 1px solid var(--border); border-radius: .75rem; background: var(--surface-raised); padding: 1rem; }
.context { margin-bottom: .3rem; color: var(--cyan); font-size: .82rem; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
.choice h3 { margin-bottom: .45rem; font-size: 1.05rem; white-space: pre-wrap; }
.choice time { display: block; margin-bottom: .8rem; color: var(--muted); font-size: .82rem; }
.answer { margin-top: .65rem; border-left: .25rem solid var(--cyan); border-radius: .35rem; background: #0b2a3b; padding: .7rem .85rem; }
.answer.note { border-left-color: var(--ice); }
.answer-kind { color: var(--cyan); font-size: .78rem; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }
.answer-value, .description { margin: .2rem 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.session-id { color: var(--muted); overflow-wrap: anywhere; }
@media (max-width: 640px) { main { width: min(100% - 1rem, 1100px); padding-top: 1rem; } }
@media print {
  :root { color-scheme: light; }
  body { background: #fff; color: #000; }
  .chat, .choice, .metric, .empty, .scan-note, .answer { background: #fff; border-color: #777; }
  .intro, .meta, .description, .empty, .scan-note, .chat-meta, .session-id, .choice time { color: #333; }
  h1, .chat > summary, .context, .answer-kind, .metric strong { color: #075985; }
}
"""


class ChatChoiceError(RuntimeError):
    """A concise user-facing report error."""


@dataclass(frozen=True)
class Answer:
    text: str
    kind: str
    description: str = ""


@dataclass(frozen=True)
class Choice:
    call_id: str
    question_id: str
    header: str
    question: str
    answers: tuple[Answer, ...]
    answered_at: datetime
    order: int

    @property
    def identity(self) -> tuple[str, str]:
        return self.call_id, self.question_id


@dataclass(frozen=True)
class Transcript:
    path: Path
    session_id: str
    started_at: datetime
    archived: bool


@dataclass
class ChatChoices:
    transcript: Transcript
    choices: list[Choice] = field(default_factory=list)


@dataclass
class ScanStats:
    transcript_files: int = 0
    top_level_chats: int = 0
    subagent_chats: int = 0
    unreadable_files: int = 0
    malformed_records: int = 0
    unanswered_prompts: int = 0
    auto_resolved_prompts: int = 0
    duplicate_choices: int = 0

    @property
    def skipped_records(self) -> int:
        return self.unreadable_files + self.malformed_records


@dataclass(frozen=True)
class PendingRequest:
    call_id: str
    arguments: dict[str, Any]
    requested_at: datetime | None


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write explicit choices from local Codex chats as a standalone dark-ice HTML report."
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="Codex state directory (default: CODEX_HOME, then ~/.codex).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination .html file; an existing file is replaced atomically.",
    )
    return parser


def _json_mapping(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _warning(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _is_subagent(payload: dict[str, Any]) -> bool:
    if payload.get("parent_thread_id") not in (None, ""):
        return True
    if payload.get("thread_source") == "subagent":
        return True
    source = payload.get("source")
    return isinstance(source, dict) and "subagent" in source


def _read_transcript_metadata(path: Path, archived: bool, stats: ScanStats) -> Transcript | None:
    try:
        with path.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, 1):
                if SESSION_META_MARKER not in raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    stats.malformed_records += 1
                    _warning(f"skipped malformed session metadata in {path}:{line_number}")
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "session_meta" or not isinstance(record.get("payload"), dict):
                    continue
                payload = record["payload"]
                if _is_subagent(payload):
                    stats.subagent_chats += 1
                    return None
                session_id = payload.get("id") or payload.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    stats.malformed_records += 1
                    _warning(f"skipped session without an id: {path}")
                    return None
                started_at = _parse_timestamp(payload.get("timestamp")) or _parse_timestamp(record.get("timestamp"))
                if started_at is None:
                    started_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                return Transcript(path=path, session_id=session_id, started_at=started_at, archived=archived)
    except OSError as exc:
        stats.unreadable_files += 1
        _warning(f"could not read {path}: {exc}")
        return None
    stats.malformed_records += 1
    _warning(f"skipped transcript without session metadata: {path}")
    return None


def _discover_transcripts(codex_home: Path, stats: ScanStats) -> list[Transcript]:
    roots = ((codex_home / "sessions", False), (codex_home / "archived_sessions", True))
    existing_roots = [(root, archived) for root, archived in roots if root.is_dir()]
    if not existing_roots:
        raise ChatChoiceError(f"no Codex session directories found under {codex_home}")

    discovered: list[tuple[Path, bool]] = []
    seen_paths: set[Path] = set()
    for root, archived in existing_roots:
        for path in sorted(root.rglob("*.jsonl")):
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            discovered.append((path, archived))

    stats.transcript_files = len(discovered)
    transcripts = [
        transcript
        for path, archived in discovered
        if (transcript := _read_transcript_metadata(path, archived, stats)) is not None
    ]
    stats.top_level_chats = len(transcripts)
    return sorted(transcripts, key=lambda item: (item.started_at, item.archived, item.session_id, str(item.path)))


def _request_from_record(record: object) -> PendingRequest | None:
    if not isinstance(record, dict) or record.get("type") != "response_item":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "function_call" or payload.get("name") != "request_user_input":
        return None
    call_id = payload.get("call_id")
    arguments = _json_mapping(payload.get("arguments"))
    if not isinstance(call_id, str) or not call_id or arguments is None:
        return None
    return PendingRequest(call_id, arguments, _parse_timestamp(record.get("timestamp")))


def _answer_kind(answer: str, option_descriptions: dict[str, str]) -> Answer:
    if answer.startswith(USER_NOTE_PREFIX):
        return Answer(answer.removeprefix(USER_NOTE_PREFIX).lstrip(), "Note")
    if answer in option_descriptions:
        return Answer(answer, "Selected", option_descriptions[answer])
    return Answer(answer, "Custom answer")


def _answers_were_auto_resolved(request: PendingRequest, answered_at: datetime) -> bool | None:
    timeout = request.arguments.get("autoResolutionMs")
    if timeout is None:
        return False
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
        return None
    if request.requested_at is None:
        return None
    elapsed_ms = (answered_at - request.requested_at).total_seconds() * 1000
    return elapsed_ms >= timeout


def _choices_from_output(
    request: PendingRequest,
    output_record: dict[str, Any],
    *,
    first_order: int,
    stats: ScanStats,
) -> list[Choice]:
    payload = output_record.get("payload")
    if not isinstance(payload, dict):
        stats.malformed_records += 1
        return []
    output = _json_mapping(payload.get("output"))
    questions = request.arguments.get("questions")
    if output is None or not isinstance(questions, list):
        stats.malformed_records += 1
        return []
    answer_map = output.get("answers")
    if not isinstance(answer_map, dict) or not answer_map:
        stats.unanswered_prompts += 1
        return []

    answered_at = _parse_timestamp(output_record.get("timestamp")) or request.requested_at
    if answered_at is None:
        stats.malformed_records += 1
        return []
    auto_resolved = _answers_were_auto_resolved(request, answered_at)
    if auto_resolved is None:
        stats.malformed_records += 1
        return []
    if auto_resolved:
        stats.auto_resolved_prompts += 1
        return []

    choices: list[Choice] = []
    for question_index, question_value in enumerate(questions):
        if not isinstance(question_value, dict):
            stats.malformed_records += 1
            continue
        question_id = question_value.get("id")
        if not isinstance(question_id, str) or not question_id:
            stats.malformed_records += 1
            continue
        answer_value = answer_map.get(question_id)
        if not isinstance(answer_value, dict) or not isinstance(answer_value.get("answers"), list):
            continue
        raw_answers = answer_value["answers"]
        answers = [value for value in raw_answers if isinstance(value, str) and value]
        if not answers:
            continue

        option_descriptions: dict[str, str] = {}
        options = question_value.get("options")
        if isinstance(options, list):
            for option in options:
                if not isinstance(option, dict) or not isinstance(option.get("label"), str):
                    continue
                description = option.get("description")
                option_descriptions[option["label"]] = description if isinstance(description, str) else ""
        header = question_value.get("header")
        question = question_value.get("question")
        choices.append(
            Choice(
                call_id=request.call_id,
                question_id=question_id,
                header=header if isinstance(header, str) else "",
                question=question if isinstance(question, str) and question else question_id,
                answers=tuple(_answer_kind(answer, option_descriptions) for answer in answers),
                answered_at=answered_at,
                order=first_order + question_index,
            )
        )
    if not choices:
        stats.unanswered_prompts += 1
    return choices


def _read_choices(transcript: Transcript, stats: ScanStats) -> list[Choice]:
    pending: dict[str, PendingRequest] = {}
    choices: list[Choice] = []
    order = 0
    try:
        with transcript.path.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, 1):
                if REQUEST_USER_INPUT_MARKER in raw_line:
                    try:
                        record = json.loads(raw_line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        stats.malformed_records += 1
                        _warning(f"skipped malformed choice request in {transcript.path}:{line_number}")
                        continue
                    request = _request_from_record(record)
                    if request is not None:
                        pending[request.call_id] = request
                    continue
                if not pending or FUNCTION_OUTPUT_MARKER not in raw_line:
                    continue
                matching_ids = [call_id for call_id in pending if call_id.encode("utf-8") in raw_line]
                if not matching_ids:
                    continue
                try:
                    record = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    stats.malformed_records += 1
                    _warning(f"skipped malformed choice response in {transcript.path}:{line_number}")
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "response_item" or not isinstance(record.get("payload"), dict):
                    continue
                payload = record["payload"]
                if payload.get("type") != "function_call_output" or payload.get("call_id") not in pending:
                    continue
                request = pending.pop(payload["call_id"])
                extracted = _choices_from_output(request, record, first_order=order, stats=stats)
                choices.extend(extracted)
                order += max(1, len(extracted))
    except OSError as exc:
        stats.unreadable_files += 1
        _warning(f"could not read {transcript.path}: {exc}")
        return []
    stats.unanswered_prompts += len(pending)
    return choices


def scan_choices(codex_home: Path) -> tuple[list[ChatChoices], ScanStats]:
    stats = ScanStats()
    transcripts = _discover_transcripts(codex_home, stats)
    seen_choices: set[tuple[str, str]] = set()
    chats: list[ChatChoices] = []
    for transcript in transcripts:
        chat = ChatChoices(transcript)
        for choice in _read_choices(transcript, stats):
            if choice.identity in seen_choices:
                stats.duplicate_choices += 1
                continue
            seen_choices.add(choice.identity)
            chat.choices.append(choice)
        if chat.choices:
            chat.choices.sort(key=lambda item: (item.answered_at, item.order, item.call_id, item.question_id))
            chats.append(chat)
    chats.sort(key=lambda item: (item.transcript.started_at, item.transcript.session_id), reverse=True)
    return chats, stats


def _escaped(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_report(chats: Sequence[ChatChoices], stats: ScanStats, generated_at: datetime) -> str:
    total_choices = sum(len(chat.choices) for chat in chats)
    output: list[str] = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="utf-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        (
            "  <meta http-equiv=\"Content-Security-Policy\" "
            "content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        ),
        "  <title>Codex chat choices</title>",
        "  <style>",
        ICE_STYLESHEET.rstrip(),
        "  </style>",
        "</head>",
        "<body>",
        "<main>",
        "  <header>",
        "    <h1>Codex chat choices</h1>",
        (
            '    <p class="intro">Explicit selections from local active and archived Codex chats. '
            f'Generated <time datetime="{_escaped(generated_at.isoformat())}">{_escaped(_timestamp_text(generated_at))}</time>.</p>'
        ),
        "  </header>",
        '  <section class="summary-grid" aria-label="Report summary">',
        f'    <div class="metric"><strong>{total_choices}</strong> choice(s)</div>',
        f'    <div class="metric"><strong>{len(chats)}</strong> chat(s) with choices</div>',
        f'    <div class="metric"><strong>{stats.top_level_chats}</strong> top-level chat(s) scanned</div>',
        "  </section>",
    ]
    omitted = stats.auto_resolved_prompts + stats.unanswered_prompts + stats.duplicate_choices
    if stats.skipped_records or omitted:
        output.append(
            '  <p class="scan-note">'
            f"Skipped {stats.skipped_records} malformed/unreadable record(s), "
            f"{stats.auto_resolved_prompts} auto-resolved prompt(s), "
            f"{stats.unanswered_prompts} unanswered prompt(s), and "
            f"{stats.duplicate_choices} duplicate choice(s)."
            "</p>"
        )
    if not chats:
        output.append('  <p class="empty">No explicit user-selected choices were found.</p>')

    for chat_index, chat in enumerate(chats, 1):
        transcript = chat.transcript
        status = "Archived" if transcript.archived else "Active"
        started = _timestamp_text(transcript.started_at)
        output.extend(
            [
                f'  <details class="chat" id="chat-{chat_index}" open>',
                (
                    "    <summary>"
                    f"{_escaped(started)} · {len(chat.choices)} choice(s)"
                    "</summary>"
                ),
                '    <div class="chat-meta">',
                f'      <span class="badge">{status}</span>',
                f'      <span class="session-id">Session {_escaped(transcript.session_id)}</span>',
                "    </div>",
                '    <div class="choices">',
            ]
        )
        for choice in chat.choices:
            output.append('      <article class="choice">')
            if choice.header:
                output.append(f'        <div class="context">{_escaped(choice.header)}</div>')
            output.extend(
                [
                    f"        <h3>{_escaped(choice.question)}</h3>",
                    (
                        f'        <time datetime="{_escaped(choice.answered_at.isoformat())}">'
                        f"{_escaped(_timestamp_text(choice.answered_at))}</time>"
                    ),
                ]
            )
            for answer in choice.answers:
                note_class = " note" if answer.kind == "Note" else ""
                output.extend(
                    [
                        f'        <div class="answer{note_class}">',
                        f'          <span class="answer-kind">{_escaped(answer.kind)}</span>',
                        f'          <p class="answer-value">{_escaped(answer.text)}</p>',
                    ]
                )
                if answer.description:
                    output.append(f'          <p class="description">{_escaped(answer.description)}</p>')
                output.append("        </div>")
            output.append("      </article>")
        output.extend(["    </div>", "  </details>"])

    output.extend(["</main>", "</body>", "</html>", ""])
    return "\n".join(output)


def _preserve_existing_mode(path: Path, temporary_path: Path) -> None:
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return
    temporary_path.chmod(mode)


def atomic_write_report(path: Path, document: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor = -1
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as stream:
            file_descriptor = -1
            _write_document(stream, document)
        _preserve_existing_mode(path, temporary_path)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _write_document(stream: TextIO, document: str) -> None:
    stream.write(document)
    stream.flush()
    os.fsync(stream.fileno())


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def main(argv: Sequence[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    output_path = _absolute_path(args.output)
    if output_path.suffix.casefold() != ".html":
        print("chat choices: --output must name a .html file", file=sys.stderr)
        return 1
    if output_path.exists() and not output_path.is_file():
        print(f"chat choices: output is not a regular file: {output_path}", file=sys.stderr)
        return 1

    configured_home = args.codex_home or (Path(value) if (value := os.environ.get("CODEX_HOME")) else Path.home() / ".codex")
    codex_home = configured_home.expanduser()
    if not codex_home.is_dir():
        print(f"chat choices: Codex home is not a directory: {codex_home}", file=sys.stderr)
        return 1
    try:
        chats, stats = scan_choices(codex_home)
        total_choices = sum(len(chat.choices) for chat in chats)
        document = render_report(chats, stats, datetime.now(timezone.utc))
        atomic_write_report(output_path, document)
    except (ChatChoiceError, OSError) as exc:
        print(f"chat choices: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {total_choices} choice(s) from {len(chats)} chat(s) to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
