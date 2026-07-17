from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from plsqlwks import __version__ as PACKAGE_VERSION

ROOT = Path(__file__).resolve().parents[1]
GITLAB_REPOSITORY_URL = "https://gitlab.com/unununu/plsqlwks"
GITLAB_PREVIEW_URL = f"{GITLAB_REPOSITORY_URL}/-/raw/main/img/preview.png"
GITLAB_ARCHITECTURE_URL = f"{GITLAB_REPOSITORY_URL}/-/blob/main/ARCHITECTURE.md"
GITLAB_CHANGELOG_URL = f"{GITLAB_REPOSITORY_URL}/-/blob/main/CHANGELOG.md"
GITLAB_QUICKSTART_URL = f"{GITLAB_REPOSITORY_URL}/-/blob/main/QUICKSTART.md"
GITLAB_COMPATIBILITY_URL = f"{GITLAB_REPOSITORY_URL}/-/blob/main/COMPATIBILITY.md"
AUTHORIZED_PUBLIC_AUTHOR = "unu2000"
AUTHORIZED_KOFI_URL = "https://ko-fi.com/unu2000"
RUNTIME_PACKAGE_FILES = {
    "plsqlwks/__init__.py",
    "plsqlwks/__main__.py",
    "plsqlwks/exporting.py",
    "plsqlwks/html_exporting.py",
    "plsqlwks/sqlbinds.py",
    "plsqlwks/sqlsplit.py",
    "plsqlwks/workspace.py",
    "plsqlwks/xlsx_exporting.py",
    "plsqlwks/config/__init__.py",
    "plsqlwks/config/loader.py",
    "plsqlwks/config/models.py",
    "plsqlwks/config/paths.py",
    "plsqlwks/config/session.py",
    "plsqlwks/config/settings.py",
    "plsqlwks/db/__init__.py",
    "plsqlwks/db/editing.py",
    "plsqlwks/db/execution.py",
    "plsqlwks/db/explain.py",
    "plsqlwks/db/health.py",
    "plsqlwks/db/identifiers.py",
    "plsqlwks/db/metadata.py",
    "plsqlwks/db/models.py",
    "plsqlwks/db/session.py",
    "plsqlwks/db/sql_analysis.py",
    "plsqlwks/db/transactions.py",
    "plsqlwks/plugins/__init__.py",
    "plsqlwks/plugins/_result_export.py",
    "plsqlwks/plugins/api.py",
    "plsqlwks/plugins/csv_export.py",
    "plsqlwks/plugins/html_export.py",
    "plsqlwks/plugins/loader.py",
    "plsqlwks/plugins/xlsx_export.py",
    "plsqlwks/ui/__init__.py",
    "plsqlwks/ui/app.py",
    "plsqlwks/ui/application_controller.py",
    "plsqlwks/ui/browser.py",
    "plsqlwks/ui/buffer.py",
    "plsqlwks/ui/catalog.py",
    "plsqlwks/ui/clipboard.py",
    "plsqlwks/ui/command_dispatcher.py",
    "plsqlwks/ui/commands.py",
    "plsqlwks/ui/completion.py",
    "plsqlwks/ui/constants.py",
    "plsqlwks/ui/db_operations.py",
    "plsqlwks/ui/db_session.py",
    "plsqlwks/ui/db_worker.py",
    "plsqlwks/ui/dialogs.py",
    "plsqlwks/ui/display.py",
    "plsqlwks/ui/documents.py",
    "plsqlwks/ui/editor_controller.py",
    "plsqlwks/ui/errors.py",
    "plsqlwks/ui/help.py",
    "plsqlwks/ui/input_controller.py",
    "plsqlwks/ui/key_reader.py",
    "plsqlwks/ui/keys.py",
    "plsqlwks/ui/menu.py",
    "plsqlwks/ui/plugin_host.py",
    "plsqlwks/ui/ports.py",
    "plsqlwks/ui/query_controller.py",
    "plsqlwks/ui/renderer.py",
    "plsqlwks/ui/result_controller.py",
    "plsqlwks/ui/result_export.py",
    "plsqlwks/ui/result_presenter.py",
    "plsqlwks/ui/results.py",
    "plsqlwks/ui/sql.py",
    "plsqlwks/ui/state.py",
    "plsqlwks/ui/syntax.py",
    "plsqlwks/ui/viewport.py",
}
SDIST_ONLY_FILES = {
    "ARCHITECTURE.md",
    "COMPATIBILITY.md",
    "PLUGINS.md",
    "QUICKSTART.html",
    "QUICKSTART.md",
    "plugin-requirements/csv-export/requirements.txt",
    "plugin-requirements/html-export/requirements.txt",
    "plugin-requirements/xlsx-export/requirements.txt",
    "pytest.ini",
    "rchar.py",
    "requirements.txt",
    "tests/conftest.py",
    "tests/fixtures/config_exports.txt",
    "tests/fixtures/ui_exports.txt",
}
SDIST_PROJECT_FILES = (
    RUNTIME_PACKAGE_FILES
    | SDIST_ONLY_FILES
    | {
        "MANIFEST.in",
        "README.md",
        "license.txt",
        "pyproject.toml",
    }
)
SDIST_GENERATED_FILES = {
    "PKG-INFO",
    "setup.cfg",
    "plsqlwks.egg-info/SOURCES.txt",
}
WHEEL_DIST_INFO_FILES = {
    "METADATA",
    "RECORD",
    "WHEEL",
    "entry_points.txt",
    "licenses/license.txt",
    "top_level.txt",
}
FORBIDDEN_ARTIFACT_DIRECTORIES = {
    ".agents",
    ".codex",
    ".git",
    "pub.wks",
    "vdb",
    "workspace",
    "workspace010",
}
FORBIDDEN_EXACT_FILENAMES = {".env", "config.ini"}
FORBIDDEN_FILENAME_SUFFIXES = (".key", ".p12", ".pem", ".pfx", ".un~")
PRIVATE_SENTINEL = "PLSQLWKS-PRIVATE-ARTIFACT-SENTINEL-7F4D4A42949B"
PRIVATE_TEXT_PATTERNS = (
    (re.compile(rb"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE), "email address"),
    (re.compile(rb"/(?:home|Users)/[^/\x00\r\n]+/"), "personal home path"),
    (re.compile(rb"[A-Z]:\\Users\\[^\\\x00\r\n]+\\", re.IGNORECASE), "Windows user path"),
    (re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private key"),
    (re.compile(rb"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(rb"(?:gh[pousr]_|glpat-)[A-Za-z0-9_-]{20,}"), "hosted-service token"),
)
PERSONAL_METADATA_HEADERS = (
    "author-email:",
    "maintainer:",
    "maintainer-email:",
)


def assert_safe_artifact_paths(names: set[str]) -> None:
    for name in names:
        assert "\\" not in name, name
        path = PurePosixPath(name)
        assert not path.is_absolute(), name
        assert ".." not in path.parts, name
        lowered_parts = {part.casefold() for part in path.parts}
        assert lowered_parts.isdisjoint(FORBIDDEN_ARTIFACT_DIRECTORIES), name
        filename = path.name.casefold()
        assert filename not in FORBIDDEN_EXACT_FILENAMES, name
        assert not filename.startswith(".env."), name
        assert not filename.endswith(FORBIDDEN_FILENAME_SUFFIXES), name
        assert not any(word in filename for word in ("password", "private-key", "secret-token")), name


def assert_no_private_text(
    members: dict[str, bytes],
    *,
    source_trees: tuple[Path, ...],
) -> None:
    sensitive_paths = {*source_trees, ROOT, Path.home()}
    for variable in ("HOME", "USERPROFILE"):
        value = os.environ.get(variable)
        if value:
            sensitive_paths.add(Path(value))
    forbidden_values = {PRIVATE_SENTINEL.encode("ascii")}
    for source_tree in sensitive_paths:
        for value in (str(source_tree), source_tree.as_posix()):
            if len(value) > 3:
                forbidden_values.add(value.encode())
    for name, payload in members.items():
        assert all(value not in payload for value in forbidden_values), name
        for pattern, description in PRIVATE_TEXT_PATTERNS:
            assert pattern.search(payload) is None, f"{name}: {description}"


def assert_only_authorized_public_author_metadata(metadata: str) -> None:
    lines = metadata.splitlines()
    headers = tuple(line.casefold() for line in lines)
    assert [line for line in lines if line.casefold().startswith("author:")] == [f"Author: {AUTHORIZED_PUBLIC_AUTHOR}"]
    assert f"Project-URL: Ko-fi, {AUTHORIZED_KOFI_URL}" in lines
    for prefix in PERSONAL_METADATA_HEADERS:
        assert not any(line.startswith(prefix) for line in headers), prefix


@pytest.fixture(scope="module")
def built_distributions(tmp_path_factory):
    build_root = tmp_path_factory.mktemp("privacy-build")
    source_tree = build_root / "source"
    shutil.copytree(
        ROOT,
        source_tree,
        ignore=shutil.ignore_patterns(
            ".agents",
            ".cache",
            ".codex",
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            "*.egg-info",
            "__pycache__",
            "build",
            "dist",
            "pub.wks",
            "sdist",
            "vdb",
            "workspace",
            "workspace010",
        ),
    )
    private_paths = (
        ".agents/private.txt",
        ".codex/private.txt",
        ".env",
        ".git/config",
        "ci-secret.tmp",
        "config.ini",
        "local-artifact.bin",
        "local_private.py",
        "plsqlwks/local-private.dat",
        "pub.wks/config.ini",
        "tests/test_local_private.py",
        "temporary.un~",
        "vdb/private.sqlite",
        "workspace/config.ini",
        "workspace010/config.ini",
    )
    for relative_path in private_paths:
        path = source_tree / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PRIVATE_SENTINEL, encoding="utf-8")

    dist_dir = build_root / "dist"
    dist_dir.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-c",
            (f"from setuptools.build_meta import build_sdist; build_sdist({str(dist_dir)!r})"),
        ],
        cwd=source_tree,
        check=True,
    )
    source_archive = next(dist_dir.glob("plsqlwks-*.tar.gz"))
    unpacked_dir = build_root / "from-sdist"
    with tarfile.open(source_archive) as archive:
        members = archive.getmembers()
        assert all(member.isdir() or member.isfile() for member in members)
        assert_safe_artifact_paths({member.name for member in members})
        if hasattr(tarfile, "data_filter"):
            archive.extractall(unpacked_dir, filter="data")
        else:
            archive.extractall(unpacked_dir)
    sdist_source_tree = next(path for path in unpacked_dir.iterdir() if path.is_dir())
    subprocess.run(
        [
            sys.executable,
            "-c",
            (f"from setuptools.build_meta import build_wheel; build_wheel({str(dist_dir)!r})"),
        ],
        cwd=sdist_source_tree,
        check=True,
    )
    wheel = next(dist_dir.glob("plsqlwks-*.whl"))
    return wheel, source_archive, (source_tree, sdist_source_tree)


def assert_xlsx_extra_metadata(metadata: str) -> None:
    """Require openpyxl only through the optional XLSX dependency group."""
    lines = metadata.splitlines()
    assert "Requires-Dist: oracledb>=2.0" in lines
    assert "Provides-Extra: xlsx" in lines
    assert [line for line in lines if line.lower().startswith("requires-dist: openpyxl")] == [
        'Requires-Dist: openpyxl>=3.1; extra == "xlsx"'
    ]


def test_pyproject_declares_runtime_package_and_console_script():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = {
        line.strip().lower()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    xlsx_manifest_requirements = {
        line.strip()
        for line in (ROOT / "plugin-requirements/xlsx-export/requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert 'name = "plsqlwks"' in pyproject
    assert '{name = "unu2000"}' in pyproject
    assert 'plsqlwks = "plsqlwks.ui:main"' in pyproject
    runtime_block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert [line.strip().strip('",') for line in runtime_block.splitlines() if line.strip()] == ["oracledb>=2.0"]
    xlsx_block = pyproject.split("xlsx = [", 1)[1].split("]", 1)[0]
    xlsx_requirements = {line.strip().strip('",') for line in xlsx_block.splitlines() if line.strip()}
    assert xlsx_requirements == xlsx_manifest_requirements == {"openpyxl>=3.1"}
    assert 'requires = ["setuptools>=77", "wheel"]' in pyproject
    assert 'license = "LicenseRef-plsqlwks-Donationware"' in pyproject
    assert 'license-files = ["license.txt"]' in pyproject
    assert f'Repository = "{GITLAB_REPOSITORY_URL}"' in pyproject
    assert f'Issues = "{GITLAB_REPOSITORY_URL}/-/issues"' in pyproject
    assert f'Changelog = "{GITLAB_CHANGELOG_URL}"' in pyproject
    assert f'Architecture = "{GITLAB_ARCHITECTURE_URL}"' in pyproject
    assert f'Quickstart = "{GITLAB_QUICKSTART_URL}"' in pyproject
    assert f'Compatibility = "{GITLAB_COMPATIBILITY_URL}"' in pyproject
    assert f'Ko-fi = "{AUTHORIZED_KOFI_URL}"' in pyproject
    assert requirements == {"oracledb>=2.0"}
    for requirement in ("build>=1.2", "mypy>=1.10", "pytest>=8.0", "ruff>=0.8", "wheel>=0.43"):
        assert f'"{requirement}"' in pyproject
    setuptools_block = pyproject.split("[tool.setuptools]", 1)[1].split("[tool.setuptools.dynamic]", 1)[0]
    package_block = setuptools_block.split("packages = [", 1)[1].split("]", 1)[0]
    assert {line.strip().strip('",') for line in package_block.splitlines() if line.strip()} == {
        "plsqlwks",
        "plsqlwks.config",
        "plsqlwks.db",
        "plsqlwks.plugins",
        "plsqlwks.ui",
    }
    assert "py-modules = []" in setuptools_block
    assert "[tool.setuptools.packages.find]" not in pyproject

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert GITLAB_PREVIEW_URL in readme
    assert GITLAB_QUICKSTART_URL in readme
    assert f"[unu2000 on Ko-fi]({AUTHORIZED_KOFI_URL})" in readme
    assert "raw.githubusercontent.com" not in readme


def test_manifest_and_artifact_allowlist_cover_every_runtime_python_module():
    package_python_files = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "plsqlwks").rglob("*.py")
    }
    manifest_runtime_files = {
        line.removeprefix("include ")
        for line in (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        if line.startswith("include plsqlwks/") and line.endswith(".py")
    }
    allowlisted_runtime_python = {
        path for path in RUNTIME_PACKAGE_FILES if path.endswith(".py")
    }

    assert manifest_runtime_files == package_python_files
    assert allowlisted_runtime_python == package_python_files


@pytest.mark.integration
@pytest.mark.slow
def test_built_wheel_contains_only_allowlisted_runtime_files(built_distributions):
    wheel, _, source_trees = built_distributions
    with zipfile.ZipFile(wheel) as archive:
        names = {name for name in archive.namelist() if not name.endswith("/")}
        dist_info_roots = {
            PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts[0].endswith(".dist-info")
        }
        assert len(dist_info_roots) == 1
        dist_info_root = next(iter(dist_info_roots))
        runtime_files = {name for name in names if not name.startswith(f"{dist_info_root}/")}
        dist_info_files = {
            str(PurePosixPath(name).relative_to(dist_info_root))
            for name in names
            if name.startswith(f"{dist_info_root}/")
        }
        assert runtime_files == RUNTIME_PACKAGE_FILES
        assert dist_info_files == WHEEL_DIST_INFO_FILES
        assert_safe_artifact_paths(names)
        members = {name: archive.read(name) for name in names}
        assert_no_private_text(members, source_trees=source_trees)
        metadata = members[f"{dist_info_root}/METADATA"].decode("utf-8")

    assert_xlsx_extra_metadata(metadata)
    assert_only_authorized_public_author_metadata(metadata)
    assert "License-Expression: LicenseRef-plsqlwks-Donationware" in metadata
    assert "License-File: license.txt" in metadata
    assert f"Version: {PACKAGE_VERSION}" in metadata
    assert f"Project-URL: Repository, {GITLAB_REPOSITORY_URL}" in metadata
    assert f"Project-URL: Architecture, {GITLAB_ARCHITECTURE_URL}" in metadata
    assert f"Project-URL: Changelog, {GITLAB_CHANGELOG_URL}" in metadata
    assert f"Project-URL: Quickstart, {GITLAB_QUICKSTART_URL}" in metadata
    assert f"Project-URL: Compatibility, {GITLAB_COMPATIBILITY_URL}" in metadata
    assert GITLAB_PREVIEW_URL in metadata
    assert "raw.githubusercontent.com" not in metadata
    assert "requires-dist: platformdirs" not in metadata.lower()
    assert not SDIST_ONLY_FILES.intersection(names)


@pytest.mark.integration
@pytest.mark.slow
def test_built_sdist_contains_only_allowlisted_sources(built_distributions):
    _, source_archive, source_trees = built_distributions
    with tarfile.open(source_archive) as archive:
        members = archive.getmembers()
        assert all(member.isdir() or member.isfile() for member in members)
        archive_roots = {PurePosixPath(member.name).parts[0] for member in members}
        assert len(archive_roots) == 1
        archive_root = next(iter(archive_roots))
        file_members = [member for member in members if member.isfile()]
        archive_files = {str(PurePosixPath(member.name).relative_to(archive_root)) for member in file_members}
        assert archive_files == SDIST_PROJECT_FILES | SDIST_GENERATED_FILES
        assert_safe_artifact_paths(archive_files)
        payloads: dict[str, bytes] = {}
        for member in file_members:
            extracted = archive.extractfile(member)
            assert extracted is not None
            relative_name = str(PurePosixPath(member.name).relative_to(archive_root))
            payloads[relative_name] = extracted.read()
        assert_no_private_text(payloads, source_trees=source_trees)
        metadata = payloads["PKG-INFO"].decode("utf-8")

    assert "License-Expression: LicenseRef-plsqlwks-Donationware" in metadata
    assert "License-File: license.txt" in metadata
    assert_xlsx_extra_metadata(metadata)
    assert_only_authorized_public_author_metadata(metadata)
    assert f"Version: {PACKAGE_VERSION}" in metadata
    assert f"Project-URL: Repository, {GITLAB_REPOSITORY_URL}" in metadata
    assert f"Project-URL: Architecture, {GITLAB_ARCHITECTURE_URL}" in metadata
    assert f"Project-URL: Changelog, {GITLAB_CHANGELOG_URL}" in metadata
    assert f"Project-URL: Quickstart, {GITLAB_QUICKSTART_URL}" in metadata
    assert f"Project-URL: Compatibility, {GITLAB_COMPATIBILITY_URL}" in metadata
    assert GITLAB_PREVIEW_URL in metadata
    assert "raw.githubusercontent.com" not in metadata


@pytest.mark.integration
@pytest.mark.slow
def test_installed_wheel_uses_user_paths_outside_installation(tmp_path, built_distributions):
    wheel, _, _ = built_distributions
    install_dir = tmp_path / "installed"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(install_dir),
            str(wheel),
        ],
        check=True,
    )
    installed_workspace = install_dir / "workspace"
    (installed_workspace / "sql").mkdir(parents=True)

    xdg_config = tmp_path / "xdg-config"
    xdg_data = tmp_path / "xdg-data"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(install_dir)
    environment["XDG_CONFIG_HOME"] = str(xdg_config)
    environment["XDG_DATA_HOME"] = str(xdg_data)
    environment.pop("ORACLE_PASSWORD_FILE", None)
    environment.pop("PLSQLWKS_WORKSPACE", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
from pathlib import Path

import plsqlwks
import plsqlwks.html_exporting as html_exporting
import plsqlwks.xlsx_exporting as xlsx_exporting
from plsqlwks.config import load_config
import plsqlwks.plugins as public_plugins
from plsqlwks.plugins import PLUGIN_API_VERSION, PLUGIN_ENTRY_POINT_GROUP
from plsqlwks.plugins.html_export import create_plugin as create_html_export_plugin
from plsqlwks.plugins.xlsx_export import create_plugin as create_xlsx_export_plugin
from plsqlwks.ui import parse_args

html_plugin = create_html_export_plugin()
xlsx_plugin = create_xlsx_export_plugin()
assert parse_args([]).workspace is None

config = load_config()
print(json.dumps({
    "paths": {
        "package_file": str(Path(plsqlwks.__file__).resolve()),
        "workspace": str(config.workspace_dir.resolve()),
        "config_file": str(Path(config.config_file).resolve()),
        "password_file": str(config.password_file.resolve()),
    },
    "plugin_api": {
        "version": PLUGIN_API_VERSION,
        "entry_point_group": PLUGIN_ENTRY_POINT_GROUP,
        "exports": sorted(public_plugins.__all__),
        "package_version": plsqlwks.__version__,
    },
    "html_plugin": {
        "id": html_plugin.id,
        "name": html_plugin.name,
        "command_ids": [command.id for command in html_plugin.commands],
        "writer_file": str(Path(html_exporting.__file__).resolve()),
    },
    "xlsx_plugin": {
        "id": xlsx_plugin.id,
        "name": xlsx_plugin.name,
        "command_ids": [command.id for command in xlsx_plugin.commands],
        "writer_file": str(Path(xlsx_exporting.__file__).resolve()),
    },
}))
""",
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    paths = {key: Path(value) for key, value in payload["paths"].items()}
    assert payload["plugin_api"] == {
        "version": 1,
        "entry_point_group": "plsqlwks.plugins",
        "exports": [
            "PLUGIN_API_VERSION",
            "PLUGIN_ENTRY_POINT_GROUP",
            "Plugin",
            "PluginCommand",
            "PluginContext",
            "PluginFactory",
            "PluginHandler",
            "ResultSnapshot",
        ],
        "package_version": PACKAGE_VERSION,
    }
    assert payload["html_plugin"] == {
        "id": "html-export",
        "name": "HTML result export",
        "command_ids": ["export-loaded-rows"],
        "writer_file": payload["html_plugin"]["writer_file"],
    }
    assert payload["xlsx_plugin"] == {
        "id": "xlsx-export",
        "name": "XLSX result export",
        "command_ids": ["export-loaded-rows"],
        "writer_file": payload["xlsx_plugin"]["writer_file"],
    }
    install_root = install_dir.resolve()

    assert install_root in paths["package_file"].parents
    assert install_root in Path(payload["html_plugin"]["writer_file"]).parents
    assert install_root in Path(payload["xlsx_plugin"]["writer_file"]).parents
    assert paths["workspace"] != installed_workspace.resolve()
    for key in ("workspace", "config_file", "password_file"):
        assert paths[key] != install_root
        assert install_root not in paths[key].parents
    assert xdg_data.resolve() in paths["workspace"].parents
    assert xdg_config.resolve() in paths["config_file"].parents
