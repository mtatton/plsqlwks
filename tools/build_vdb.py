#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sqlite3
import struct
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DIMS = 384
SKIP_DIRS = {
    ".git",
    ".agents",
    ".codex",
    "__pycache__",
    ".pytest_cache",
    "build",
    "dist",
    "vdb",
}
ROOT_DOCUMENTATION_FILES = {
    "ARCHITECTURE.md",
    "CHANGELOG.md",
    "COMPATIBILITY.md",
    "PLUGINS.md",
    "QUICKSTART.md",
    "README.md",
    "RELEASING.md",
}
DOCUMENTATION_SKIP_DIRS = SKIP_DIRS | {"workspace"}
TOKEN_RE = re.compile(r"[^\W\d]\w*", re.UNICODE)
MARKDOWN_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
MARKDOWN_CLOSING_SEQUENCE_RE = re.compile(r"[ \t]+#+[ \t]*$")
MARKDOWN_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


@dataclass(frozen=True)
class SourceChunk:
    collection: str
    file_path: str
    kind: str
    symbol: str
    start_line: int
    end_line: int
    text: str


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build separate local SQLite vector databases for source, tests, and documentation."
    )
    parser.add_argument("--root", default=".", help="Repository root to index.")
    parser.add_argument(
        "--out",
        default="vdb",
        help=(
            "Directory for source databases; test and documentation databases are written "
            "under its tests/ and docs/ subdirectories."
        ),
    )
    parser.add_argument("--dims", type=int, default=DEFAULT_DIMS, help="Embedding dimensions.")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    requested_out_dir = Path(args.out).expanduser()
    if requested_out_dir.is_symlink():
        raise SystemExit(f"Vector database output must not be a symbolic link: {requested_out_dir}")
    out_dir = requested_out_dir.resolve()
    test_out_dir = out_dir / "tests"
    documentation_out_dir = out_dir / "docs"

    validate_output_directory(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lock_path = out_dir / ".build.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise SystemExit(f"Vector database build is already active or left a stale lock: {lock_path}") from exc
    try:
        build_scope(root, out_dir, list(find_source_python_files(root)), args.dims, scope="source")
        build_scope(root, test_out_dir, list(find_test_python_files(root)), args.dims, scope="tests")
        build_documentation_scope(
            root,
            documentation_out_dir,
            list(find_documentation_files(root, excluded_root=out_dir)),
            args.dims,
        )
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


def build_scope(root: Path, out_dir: Path, files: list[Path], dims: int, scope: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[SourceChunk] = []
    for path in files:
        chunks.extend(chunk_python_file(path, root))

    symbol_chunks = [chunk for chunk in chunks if chunk.collection == "symbols"]
    chunk_db = out_dir / "python_chunks.sqlite"
    remove_legacy_symbol_database(out_dir / "python_symbols.sqlite")
    write_database(chunk_db, chunks, dims, root)
    write_manifest(out_dir / "manifest.json", root, files, chunks, symbol_chunks, dims, scope)
    write_readme(out_dir / "README.md", scope)

    scope_label = "test" if scope == "tests" else scope
    print(f"Indexed {len(files)} {scope_label} Python files")
    print(f"Wrote {len(chunks)} chunks ({len(symbol_chunks)} symbols) to {chunk_db}")


def validate_output_directory(out_dir: Path) -> None:
    if not out_dir.exists():
        return
    if not out_dir.is_dir() or out_dir.is_symlink():
        raise SystemExit(f"Vector database output must be a real directory: {out_dir}")
    manifest_path = out_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Refusing to write into an existing non-VDB directory: {out_dir}") from exc
    if manifest.get("format") != "plsqlwks-local-vdb-v1" or manifest.get("scope") != "source":
        raise SystemExit(f"Refusing to write into an unrecognized VDB directory: {out_dir}")
    generated_paths = (
        out_dir / "README.md",
        out_dir / "manifest.json",
        out_dir / "python_chunks.sqlite",
        out_dir / "python_symbols.sqlite",
        out_dir / "tests",
        out_dir / "tests" / "README.md",
        out_dir / "tests" / "manifest.json",
        out_dir / "tests" / "python_chunks.sqlite",
        out_dir / "tests" / "python_symbols.sqlite",
        out_dir / "docs",
        out_dir / "docs" / "README.md",
        out_dir / "docs" / "manifest.json",
        out_dir / "docs" / "documentation_chunks.sqlite",
    )
    if any(path.is_symlink() for path in generated_paths):
        raise SystemExit(f"Refusing symbolic links in VDB output: {out_dir}")


def remove_legacy_symbol_database(path: Path) -> None:
    if not path.exists():
        return
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            metadata = dict(connection.execute("select key, value from metadata"))
    except sqlite3.Error as exc:
        raise RuntimeError(f"Refusing to remove unrecognized legacy database: {path}") from exc
    if metadata.get("format") != "plsqlwks-local-vdb-v1":
        raise RuntimeError(f"Refusing to remove unrecognized legacy database: {path}")
    path.unlink()
    for suffix in ("-shm", "-wal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def build_documentation_scope(
    root: Path,
    out_dir: Path,
    files: list[Path],
    dims: int,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[SourceChunk] = []
    for path in files:
        chunks.extend(chunk_documentation_file(path, root))

    chunk_db = out_dir / "documentation_chunks.sqlite"
    write_database(chunk_db, chunks, dims, root)
    write_manifest(
        out_dir / "manifest.json",
        root,
        files,
        chunks,
        [],
        dims,
        "documentation",
        databases={"chunks": chunk_db.name},
    )
    write_readme(out_dir / "README.md", "documentation")

    print(f"Indexed {len(files)} documentation files")
    print(f"Wrote {len(chunks)} documentation chunks to {chunk_db}")


def find_source_python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        relative_path = path.relative_to(root)
        parts = set(relative_path.parts)
        if parts & SKIP_DIRS or any(part.endswith(".egg-info") for part in parts):
            continue
        if relative_path.parts[0] == "tests":
            continue
        yield path


def find_test_python_files(root: Path):
    test_root = root / "tests"
    for path in sorted(test_root.rglob("*.py")):
        parts = set(path.relative_to(root).parts)
        if parts & SKIP_DIRS or any(part.endswith(".egg-info") for part in parts):
            continue
        yield path


def find_documentation_files(root: Path, *, excluded_root: Path):
    resolved_root = root.resolve()
    resolved_excluded_root = excluded_root.resolve()
    candidates = [root / name for name in ROOT_DOCUMENTATION_FILES]
    documentation_root = root / "docs"
    if documentation_root.is_dir():
        candidates.extend(documentation_root.rglob("*.md"))
    for path in sorted(set(candidates)):
        relative_path = path.relative_to(root)
        parts = set(relative_path.parts)
        if parts & DOCUMENTATION_SKIP_DIRS or any(part.endswith(".egg-info") or part.startswith(".") for part in parts):
            continue
        if path.name.casefold() == "agents.md":
            continue
        if any(
            root.joinpath(*relative_path.parts[:index]).is_symlink() for index in range(1, len(relative_path.parts) + 1)
        ):
            continue
        resolved_path = path.resolve()
        if not resolved_path.is_relative_to(resolved_root):
            continue
        if resolved_excluded_root.is_relative_to(resolved_root) and resolved_path.is_relative_to(
            resolved_excluded_root
        ):
            continue
        if not path.is_file():
            continue
        yield path


def chunk_python_file(path: Path, root: Path) -> list[SourceChunk]:
    rel_path = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    chunks: list[SourceChunk] = [SourceChunk("chunks", rel_path, "file", rel_path, 1, max(1, len(lines)), text)]
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        chunks.append(
            SourceChunk("chunks", rel_path, "syntax_error", rel_path, exc.lineno or 1, exc.lineno or 1, str(exc))
        )
        return chunks

    overview = module_overview(tree, lines)
    if overview:
        chunks.append(
            SourceChunk("chunks", rel_path, "module_overview", rel_path, 1, len(overview), "\n".join(overview))
        )

    for node in tree.body:
        collect_symbol_chunks(node, rel_path, lines, chunks, parent="")
    return chunks


def chunk_documentation_file(path: Path, root: Path) -> list[SourceChunk]:
    rel_path = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    chunks = [
        SourceChunk(
            "documentation",
            rel_path,
            "file",
            rel_path,
            1,
            max(1, len(lines)),
            text,
        )
    ]
    if path.suffix.casefold() != ".md":
        return chunks

    headings = markdown_headings(lines)
    for index, (start_line, level, symbol) in enumerate(headings):
        end_line = len(lines)
        for next_start_line, next_level, _next_symbol in headings[index + 1 :]:
            if next_level <= level:
                end_line = next_start_line - 1
                break
        chunks.append(
            SourceChunk(
                "documentation",
                rel_path,
                "section",
                symbol,
                start_line,
                max(start_line, end_line),
                "\n".join(lines[start_line - 1 : end_line]),
            )
        )
    return chunks


def markdown_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    headings: list[tuple[int, int, str]] = []
    ancestors: list[tuple[int, str]] = []
    fence_character = ""
    fence_length = 0
    for line_number, line in enumerate(lines, start=1):
        fence_match = MARKDOWN_FENCE_RE.match(line)
        if fence_character:
            if fence_match:
                marker = fence_match.group(1)
                remainder = fence_match.group(2)
                if marker[0] == fence_character and len(marker) >= fence_length and not remainder.strip():
                    fence_character = ""
                    fence_length = 0
            continue
        if fence_match:
            marker = fence_match.group(1)
            remainder = fence_match.group(2)
            if marker[0] == "`" and "`" in remainder:
                continue
            fence_character = marker[0]
            fence_length = len(marker)
            continue

        heading_match = MARKDOWN_HEADING_RE.match(line)
        if heading_match is None:
            continue
        level = len(heading_match.group(1))
        raw_title = heading_match.group(2) or ""
        title = MARKDOWN_CLOSING_SEQUENCE_RE.sub("", raw_title).strip() or "(untitled)"
        while ancestors and ancestors[-1][0] >= level:
            ancestors.pop()
        ancestors.append((level, title))
        symbol = " > ".join(item[1] for item in ancestors)
        headings.append((line_number, level, symbol))
    return headings


def module_overview(tree: ast.Module, lines: list[str]) -> list[str]:
    selected: list[str] = []
    doc = ast.get_docstring(tree)
    if doc:
        selected.append(doc)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)):
            selected.extend(lines_for_node(node, lines))
    return selected[:80]


def collect_symbol_chunks(
    node: ast.AST,
    rel_path: str,
    lines: list[str],
    chunks: list[SourceChunk],
    parent: str,
) -> None:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        name = getattr(node, "name", "")
        symbol = f"{parent}.{name}" if parent else name
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        start_line = getattr(node, "lineno", 1)
        end_line = getattr(node, "end_lineno", start_line)
        source = "\n".join(lines[start_line - 1 : end_line])
        chunks.append(SourceChunk("symbols", rel_path, kind, symbol, start_line, end_line, source))
        for child in getattr(node, "body", []):
            collect_symbol_chunks(child, rel_path, lines, chunks, parent=symbol)


def lines_for_node(node: ast.AST, lines: list[str]) -> list[str]:
    start_line = getattr(node, "lineno", 1)
    end_line = getattr(node, "end_lineno", start_line)
    return lines[start_line - 1 : end_line]


def write_database(path: Path, chunks: list[SourceChunk], dims: int, root: Path) -> None:
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary_path.exists() or temporary_path.is_symlink():
        raise RuntimeError(f"Refusing to replace an existing VDB temporary file: {temporary_path}")
    conn = sqlite3.connect(temporary_path)
    completed = False
    try:
        conn.execute("pragma journal_mode=delete")
        conn.execute("create table metadata (key text primary key, value text not null)")
        conn.execute(
            """
            create table chunks (
              id integer primary key,
              collection text not null,
              file_path text not null,
              kind text not null,
              symbol text not null,
              start_line integer not null,
              end_line integer not null,
              token_count integer not null,
              text text not null
            )
            """
        )
        conn.execute(
            """
            create table vectors (
              chunk_id integer primary key references chunks(id) on delete cascade,
              dims integer not null,
              embedding blob not null
            )
            """
        )
        conn.execute("create index idx_chunks_file on chunks(file_path, start_line)")
        conn.execute("create index idx_chunks_symbol on chunks(symbol)")
        try:
            conn.execute(
                "create virtual table chunk_fts using fts5(file_path, symbol, kind, text, content='chunks', content_rowid='id')"
            )
            has_fts = True
        except sqlite3.OperationalError:
            has_fts = False

        conn.executemany(
            "insert into metadata(key, value) values (?, ?)",
            [
                ("format", "plsqlwks-local-vdb-v1"),
                ("embedding_model", "deterministic-hashed-token-vector"),
                ("dimensions", str(dims)),
                ("root", str(root)),
                ("created_at", datetime.now(timezone.utc).isoformat()),
                ("fts5", "1" if has_fts else "0"),
            ],
        )
        for chunk in chunks:
            tokens = tokenize_for_embedding(chunk)
            vector = embed_tokens(tokens, dims)
            cursor = conn.execute(
                """
                insert into chunks(collection, file_path, kind, symbol, start_line, end_line, token_count, text)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk.collection,
                    chunk.file_path,
                    chunk.kind,
                    chunk.symbol,
                    chunk.start_line,
                    chunk.end_line,
                    len(tokens),
                    chunk.text,
                ),
            )
            chunk_id = cursor.lastrowid
            conn.execute(
                "insert into vectors(chunk_id, dims, embedding) values (?, ?, ?)",
                (chunk_id, dims, pack_vector(vector)),
            )
            if has_fts:
                conn.execute(
                    "insert into chunk_fts(rowid, file_path, symbol, kind, text) values (?, ?, ?, ?, ?)",
                    (chunk_id, chunk.file_path, chunk.symbol, chunk.kind, chunk.text),
                )
        conn.commit()
        completed = True
    finally:
        conn.close()
        if not completed:
            remove_database_temporary_files(temporary_path)
    try:
        os.replace(temporary_path, path)
    except OSError:
        remove_database_temporary_files(temporary_path)
        raise


def remove_database_temporary_files(path: Path) -> None:
    path.unlink(missing_ok=True)
    for suffix in ("-journal", "-shm", "-wal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def tokenize_for_embedding(chunk: SourceChunk) -> list[str]:
    raw = f"{chunk.file_path} {chunk.kind} {chunk.symbol}\n{chunk.text}"
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(raw):
        token = match.group(0)
        tokens.extend(split_identifier(token))
    return [token for token in tokens if len(token) > 1]


def split_identifier(value: str) -> list[str]:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = value.replace("__", "_")
    return [part.lower() for part in re.split(r"_+|[^\w]+", value, flags=re.UNICODE) if part]


def embed_tokens(tokens: list[str], dims: int) -> list[float]:
    counts = Counter(tokens)
    vector = [0.0] * dims
    for token, count in counts.items():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "little", signed=False)
        index = value % dims
        sign = -1.0 if value & (1 << 63) else 1.0
        vector[index] += sign * (1.0 + count.bit_length())
    norm = sum(item * item for item in vector) ** 0.5
    if norm:
        vector = [item / norm for item in vector]
    return vector


def pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def write_manifest(
    path: Path,
    root: Path,
    files: list[Path],
    chunks: list[SourceChunk],
    symbol_chunks: list[SourceChunk],
    dims: int,
    scope: str,
    databases: dict[str, str] | None = None,
) -> None:
    data = {
        "format": "plsqlwks-local-vdb-v1",
        "scope": scope,
        "root": str(root),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "embedding_model": "deterministic-hashed-token-vector",
        "dimensions": dims,
        "files": len(files),
        "chunks": len(chunks),
        "symbols": len(symbol_chunks),
        "databases": databases or {"chunks": "python_chunks.sqlite"},
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def write_readme(path: Path, scope: str) -> None:
    if scope == "source":
        content = """# Source Code Vector Database

This directory is generated by `tools/build_vdb.py`.
It indexes Python source outside the repository's top-level `tests/` directory.
Test code is indexed separately under `tests/`.
Project documentation is indexed separately under `docs/`.
Together these scopes contain exactly three SQLite databases: one here for
source code, one under `tests/`, and one under `docs/`.

Files:

- `python_chunks.sqlite`: the source-only database containing file, module
  overview, class, function, and method chunks.
- `manifest.json`: generation metadata.

Search examples:

```bash
python3 tools/search_vdb.py "dbms output execution"
python3 tools/search_vdb.py "keyboard shortcuts" --collection symbols
```

Rebuild:

```bash
python3 tools/build_vdb.py
```
"""
    elif scope == "tests":
        content = """# Test Vector Database

This directory is generated by `tools/build_vdb.py`.
It indexes only Python files under the repository's top-level `tests/` directory.
Stored file paths remain relative to the repository root.

Files:

- `python_chunks.sqlite`: the test-only database containing file, module
  overview, class, function, and method chunks.
- `manifest.json`: generation metadata for the test scope.

Search examples:

```bash
python3 tools/search_vdb.py "integration fixtures" --db vdb/tests/python_chunks.sqlite
python3 tools/search_vdb.py "test worker cancellation" --db vdb/tests/python_chunks.sqlite --collection symbols
```

Rebuild source, test, and documentation databases together:

```bash
python3 tools/build_vdb.py
```
"""
    else:
        content = """# Documentation Vector Database

This directory is generated by `tools/build_vdb.py`.
It indexes explicitly selected public Markdown documentation as whole files and
heading-based sections. Agent instructions, arbitrary root notes, symlinks,
generated indexes, build output, and documentation outside the repository are
excluded.

Files:

- `documentation_chunks.sqlite`: documentation file and heading-section chunks.
- `manifest.json`: generation metadata for the documentation scope.

Search example:

```bash
python3 tools/search_vdb.py "self-hosted Oracle CI" --db vdb/docs/documentation_chunks.sqlite
```

Rebuild source, test, and documentation databases together:

```bash
python3 tools/build_vdb.py
```
"""
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
