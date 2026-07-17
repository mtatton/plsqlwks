from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_VDB = ROOT / "tools" / "build_vdb.py"
SEARCH_VDB = ROOT / "tools" / "search_vdb.py"


def build_tiny_tree(tmp_path: Path) -> tuple[Path, Path, str]:
    project = tmp_path / "project"
    files = {
        "app.py": "def application():\n    return 'source'\n",
        "package/module.py": "def module_function():\n    return 'source'\n",
        "package/tests/test_internal.py": "def internal_test_helper():\n    return 'source'\n",
        "tests/test_app.py": "def test_application():\n    assert True\n",
        "tests/support/helper.py": "def test_helper():\n    return 'test'\n",
        "tests/__pycache__/ignored.py": "def ignored():\n    return None\n",
        "README.md": (
            "# Project\n\n"
            "Overview.\n\n"
            "```python\n"
            "# Not a heading\n"
            "```not-a-closing-fence\n"
            "# Still not a heading\n"
            "```\n\n"
            "## Setup\n\n"
            "Install safely.\n\n"
            "### Local\n\n"
            "Run locally.\n\n"
            "## C#\n\n"
            "Language heading.\n"
        ),
        "docs/guide.md": "# Guide\n\nPublic guidance.\n",
        "docs/nested/reference.md": "# Reference\n\nPublic reference.\n",
        "docs/reference.rst": "Reference\n=========\n\nNot an indexed format.\n",
        "AGENTS.md": "# Private agent instructions\n",
        "PRIVATE.md": "# Arbitrary root note\n",
        ".agents/internal.md": "# Internal tooling data\n",
        "build/generated.md": "# Generated build output\n",
        "vdb/generated.md": "# Generated vector data\n",
        "notes.txt": "Documentation-like text outside the supported formats.\n",
    }
    for relative_path, content in files.items():
        path = project / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    external_document = tmp_path / "external-private.md"
    external_document.write_text("# External private document\n", encoding="utf-8")
    try:
        (project / "docs" / "external.md").symlink_to(external_document)
        (project / "docs" / "guide-alias.md").symlink_to(project / "docs" / "guide.md")
    except OSError:
        pass

    out_dir = tmp_path / "custom-index"
    completed = subprocess.run(
        [
            sys.executable,
            str(BUILD_VDB),
            "--root",
            str(project),
            "--out",
            str(out_dir),
            "--dims",
            "16",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return project, out_dir, completed.stdout


def database_file_paths(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("select distinct file_path from chunks")}


def database_collections(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("select distinct collection from chunks")}


def database_journal_mode(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("pragma journal_mode").fetchone()[0]


def test_build_vdb_keeps_source_test_and_documentation_files_disjoint(tmp_path: Path) -> None:
    _, out_dir, stdout = build_tiny_tree(tmp_path)

    source_paths = database_file_paths(out_dir / "python_chunks.sqlite")
    test_paths = database_file_paths(out_dir / "tests" / "python_chunks.sqlite")
    documentation_paths = database_file_paths(out_dir / "docs" / "documentation_chunks.sqlite")

    assert source_paths == {"app.py", "package/module.py", "package/tests/test_internal.py"}
    assert test_paths == {"tests/support/helper.py", "tests/test_app.py"}
    assert source_paths.isdisjoint(test_paths)
    assert source_paths.isdisjoint(documentation_paths)
    assert test_paths.isdisjoint(documentation_paths)
    assert all(path.startswith("tests/") for path in test_paths)
    assert documentation_paths == {
        "README.md",
        "docs/guide.md",
        "docs/nested/reference.md",
    }
    assert database_collections(out_dir / "python_chunks.sqlite") == {"chunks", "symbols"}
    assert database_collections(out_dir / "tests" / "python_chunks.sqlite") == {
        "chunks",
        "symbols",
    }
    assert database_collections(out_dir / "docs" / "documentation_chunks.sqlite") == {"documentation"}
    assert database_journal_mode(out_dir / "python_chunks.sqlite") == "delete"
    assert database_journal_mode(out_dir / "tests" / "python_chunks.sqlite") == "delete"
    assert database_journal_mode(out_dir / "docs" / "documentation_chunks.sqlite") == "delete"
    assert {path.relative_to(out_dir).as_posix() for path in out_dir.rglob("*.sqlite")} == {
        "docs/documentation_chunks.sqlite",
        "python_chunks.sqlite",
        "tests/python_chunks.sqlite",
    }
    assert "Indexed 3 source Python files" in stdout
    assert "Indexed 2 test Python files" in stdout
    assert "Indexed 3 documentation files" in stdout


def test_build_vdb_writes_scope_specific_manifests_and_readmes(tmp_path: Path) -> None:
    project, out_dir, _ = build_tiny_tree(tmp_path)

    source_manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    test_manifest = json.loads((out_dir / "tests" / "manifest.json").read_text(encoding="utf-8"))
    documentation_manifest = json.loads((out_dir / "docs" / "manifest.json").read_text(encoding="utf-8"))

    assert source_manifest["scope"] == "source"
    assert source_manifest["root"] == str(project.resolve())
    assert source_manifest["dimensions"] == 16
    assert source_manifest["files"] == 3
    assert source_manifest["symbols"] > 0
    assert source_manifest["databases"] == {"chunks": "python_chunks.sqlite"}
    assert test_manifest["scope"] == "tests"
    assert test_manifest["root"] == str(project.resolve())
    assert test_manifest["dimensions"] == 16
    assert test_manifest["files"] == 2
    assert test_manifest["symbols"] > 0
    assert test_manifest["databases"] == {"chunks": "python_chunks.sqlite"}
    assert documentation_manifest["scope"] == "documentation"
    assert documentation_manifest["root"] == str(project.resolve())
    assert documentation_manifest["dimensions"] == 16
    assert documentation_manifest["files"] == 3
    assert documentation_manifest["symbols"] == 0
    assert documentation_manifest["databases"] == {"chunks": "documentation_chunks.sqlite"}

    source_readme = (out_dir / "README.md").read_text(encoding="utf-8")
    test_readme = (out_dir / "tests" / "README.md").read_text(encoding="utf-8")
    documentation_readme = (out_dir / "docs" / "README.md").read_text(encoding="utf-8")
    assert "Source Code Vector Database" in source_readme
    assert "exactly three SQLite databases" in source_readme
    assert "Test code is indexed separately" in source_readme
    assert "python_symbols.sqlite" not in source_readme
    assert "Test Vector Database" in test_readme
    assert "only Python files under" in test_readme
    assert "vdb/tests/python_chunks.sqlite" in test_readme
    assert "python_symbols.sqlite" not in test_readme
    assert "Documentation Vector Database" in documentation_readme
    assert "vdb/docs/documentation_chunks.sqlite" in documentation_readme


def test_build_vdb_chunks_markdown_by_heading_without_indexing_fences(
    tmp_path: Path,
) -> None:
    _, out_dir, _ = build_tiny_tree(tmp_path)

    with sqlite3.connect(out_dir / "docs" / "documentation_chunks.sqlite") as connection:
        sections = connection.execute(
            """
            select symbol, start_line, end_line, text
            from chunks
            where file_path = 'README.md' and kind = 'section'
            order by start_line
            """
        ).fetchall()

    assert [(symbol, start_line, end_line) for symbol, start_line, end_line, _ in sections] == [
        ("Project", 1, 21),
        ("Project > Setup", 11, 18),
        ("Project > Setup > Local", 15, 18),
        ("Project > C#", 19, 21),
    ]
    assert all("Not a heading" not in symbol for symbol, _, _, _ in sections)
    assert all("Still not a heading" not in symbol for symbol, _, _, _ in sections)
    assert "# Not a heading" in sections[0][3]


def test_build_vdb_excludes_custom_output_and_documentation_database_is_searchable(
    tmp_path: Path,
) -> None:
    project, out_dir, _ = build_tiny_tree(tmp_path)
    internal_out = project / "docs" / "generated-index"

    subprocess.run(
        [
            sys.executable,
            str(BUILD_VDB),
            "--root",
            str(project),
            "--out",
            str(internal_out),
            "--dims",
            "16",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    internal_paths = database_file_paths(internal_out / "docs" / "documentation_chunks.sqlite")
    assert internal_paths == {"README.md", "docs/guide.md", "docs/nested/reference.md"}

    completed = subprocess.run(
        [
            sys.executable,
            str(SEARCH_VDB),
            "Install safely",
            "--db",
            str(out_dir / "docs" / "documentation_chunks.sqlite"),
            "--collection",
            "documentation",
            "--limit",
            "3",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "README.md" in completed.stdout
    assert "Project > Setup" in completed.stdout


def test_build_vdb_removes_legacy_symbol_databases_and_filters_collections(
    tmp_path: Path,
) -> None:
    project, out_dir, _ = build_tiny_tree(tmp_path)
    legacy_paths = [
        out_dir / "python_symbols.sqlite",
        out_dir / "tests" / "python_symbols.sqlite",
    ]
    for path in legacy_paths:
        with sqlite3.connect(path) as connection:
            connection.execute("create table metadata (key text primary key, value text not null)")
            connection.execute("insert into metadata(key, value) values ('format', 'plsqlwks-local-vdb-v1')")

    subprocess.run(
        [
            sys.executable,
            str(BUILD_VDB),
            "--root",
            str(project),
            "--out",
            str(out_dir),
            "--dims",
            "16",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not any(path.exists() for path in legacy_paths)
    assert len(list(out_dir.rglob("*.sqlite"))) == 3

    completed = subprocess.run(
        [
            sys.executable,
            str(SEARCH_VDB),
            "application",
            "--db",
            str(out_dir / "python_chunks.sqlite"),
            "--collection",
            "symbols",
            "--limit",
            "10",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "function  application" in completed.stdout
    assert "  file  " not in completed.stdout


def test_build_vdb_refuses_existing_non_vdb_output_directories(tmp_path: Path) -> None:
    project, safe_out, _ = build_tiny_tree(tmp_path)
    original_readme = (project / "README.md").read_text(encoding="utf-8")
    original_guide = (project / "docs" / "guide.md").read_text(encoding="utf-8")

    for unsafe_output in (project, project / "docs"):
        completed = subprocess.run(
            [
                sys.executable,
                str(BUILD_VDB),
                "--root",
                str(project),
                "--out",
                str(unsafe_output),
                "--dims",
                "16",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0
        assert "Refusing to write" in completed.stderr

    assert (project / "README.md").read_text(encoding="utf-8") == original_readme
    assert (project / "docs" / "guide.md").read_text(encoding="utf-8") == original_guide

    output_link = tmp_path / "output-link"
    external_readme = tmp_path / "external-readme.md"
    external_readme.write_text("keep\n", encoding="utf-8")
    generated_readme = safe_out / "docs" / "README.md"
    try:
        output_link.symlink_to(safe_out, target_is_directory=True)
        generated_readme.unlink()
        generated_readme.symlink_to(external_readme)
    except OSError:
        return

    for unsafe_output in (output_link, safe_out):
        completed = subprocess.run(
            [
                sys.executable,
                str(BUILD_VDB),
                "--root",
                str(project),
                "--out",
                str(unsafe_output),
                "--dims",
                "16",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0
        assert "symbolic link" in completed.stderr.lower()
    assert external_readme.read_text(encoding="utf-8") == "keep\n"


def test_build_vdb_refuses_concurrent_rebuild(tmp_path: Path) -> None:
    project, out_dir, _ = build_tiny_tree(tmp_path)
    source_database = out_dir / "python_chunks.sqlite"
    before = source_database.read_bytes()
    lock_path = out_dir / ".build.lock"
    lock_path.write_text("active\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(BUILD_VDB),
            "--root",
            str(project),
            "--out",
            str(out_dir),
            "--dims",
            "16",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "already active or left a stale lock" in completed.stderr
    assert source_database.read_bytes() == before
    assert lock_path.read_text(encoding="utf-8") == "active\n"
