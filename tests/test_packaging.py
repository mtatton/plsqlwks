from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GITLAB_REPOSITORY_URL = "https://gitlab.com/unununu/plsqlwks"
GITLAB_PREVIEW_URL = f"{GITLAB_REPOSITORY_URL}/-/raw/main/img/preview.png"


def test_pyproject_declares_runtime_package_and_console_script():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = {
        line.strip().lower()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert 'name = "plsqlwks"' in pyproject
    assert 'plsqlwks = "plsqlwks.ui:main"' in pyproject
    runtime_block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert [line.strip().strip('",') for line in runtime_block.splitlines() if line.strip()] == ["oracledb>=2.0"]
    assert 'requires = ["setuptools>=77", "wheel"]' in pyproject
    assert 'license = "LicenseRef-plsqlwks-Donationware"' in pyproject
    assert 'license-files = ["license.txt"]' in pyproject
    assert f'Repository = "{GITLAB_REPOSITORY_URL}"' in pyproject
    assert f'Issues = "{GITLAB_REPOSITORY_URL}/-/issues"' in pyproject
    assert f'Changelog = "{GITLAB_REPOSITORY_URL}/-/blob/main/CHANGELOG.md"' in pyproject
    assert requirements == {"oracledb>=2.0"}
    for requirement in ("build>=1.2", "mypy>=1.10", "pytest>=8.0", "ruff>=0.8", "wheel>=0.43"):
        assert f'"{requirement}"' in pyproject
    assert 'include = ["plsqlwks*"]' in pyproject
    assert 'exclude = ["tests*", "tools*", "workspace*", "vdb*"]' in pyproject

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert GITLAB_PREVIEW_URL in readme
    assert "raw.githubusercontent.com" not in readme


@pytest.mark.integration
@pytest.mark.slow
def test_built_wheel_contains_runtime_package_only(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(wheelhouse),
        ],
        cwd=ROOT,
        check=True,
    )
    wheel = next(wheelhouse.glob("plsqlwks-*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")

    expected_ui_modules = {
        "__init__.py",
        "app.py",
        "app_db.py",
        "app_editor.py",
        "app_files.py",
        "app_input.py",
        "app_render.py",
        "app_results.py",
        "app_tabs_browser.py",
        "browser.py",
        "buffer.py",
        "clipboard.py",
        "commands.py",
        "completion.py",
        "constants.py",
        "display.py",
        "errors.py",
        "help.py",
        "keys.py",
        "menu.py",
        "results.py",
        "sql.py",
        "state.py",
        "syntax.py",
    }
    assert expected_ui_modules <= {
        name.removeprefix("plsqlwks/ui/")
        for name in names
        if name.startswith("plsqlwks/ui/")
    }
    assert "plsqlwks/ui.py" not in names
    assert not any(name.startswith("plsqlwks/ui_") and name.endswith(".py") for name in names)
    expected_config_modules = {
        "__init__.py",
        "loader.py",
        "models.py",
        "paths.py",
        "session.py",
        "settings.py",
    }
    assert expected_config_modules <= {
        name.removeprefix("plsqlwks/config/")
        for name in names
        if name.startswith("plsqlwks/config/")
    }
    assert "plsqlwks/config.py" not in names
    assert any(name == "plsqlwks/db/__init__.py" for name in names)
    assert "plsqlwks/db.py" not in names
    assert any(name.endswith(".dist-info/METADATA") for name in names)
    assert any(name.endswith(".dist-info/licenses/license.txt") for name in names)
    assert "Requires-Dist: oracledb>=2.0" in metadata
    assert "License-Expression: LicenseRef-plsqlwks-Donationware" in metadata
    assert "License-File: license.txt" in metadata
    assert f"Project-URL: Repository, {GITLAB_REPOSITORY_URL}" in metadata
    assert GITLAB_PREVIEW_URL in metadata
    assert "raw.githubusercontent.com" not in metadata
    assert "requires-dist: platformdirs" not in metadata.lower()
    assert not any(name.startswith("tests/") for name in names)
    assert not any(name.startswith("tools/") for name in names)
    assert not any(name.startswith("workspace/") for name in names)
    assert not any(name.startswith("vdb/") for name in names)


@pytest.mark.integration
@pytest.mark.slow
def test_built_sdist_contains_license_and_gitlab_metadata(tmp_path, monkeypatch):
    from setuptools.build_meta import build_sdist

    dist_dir = tmp_path / "dist"
    monkeypatch.chdir(ROOT)
    source_archive = dist_dir / build_sdist(str(dist_dir))

    with tarfile.open(source_archive) as archive:
        names = archive.getnames()
        assert any(name.endswith("/README.md") for name in names)
        assert any(name.endswith("/pyproject.toml") for name in names)
        assert any(name.endswith("/license.txt") for name in names)
        pkg_info_name = next(name for name in names if name.endswith("/PKG-INFO"))
        pkg_info_file = archive.extractfile(pkg_info_name)
        assert pkg_info_file is not None
        metadata = pkg_info_file.read().decode("utf-8")

    assert "License-Expression: LicenseRef-plsqlwks-Donationware" in metadata
    assert "License-File: license.txt" in metadata
    assert f"Project-URL: Repository, {GITLAB_REPOSITORY_URL}" in metadata
    assert GITLAB_PREVIEW_URL in metadata
    assert "raw.githubusercontent.com" not in metadata


@pytest.mark.integration
@pytest.mark.slow
def test_installed_wheel_uses_user_paths_outside_installation(tmp_path):
    source_tree = tmp_path / "source"
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
            "vdb",
        ),
    )
    wheelhouse = tmp_path / "wheelhouse"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(wheelhouse),
        ],
        cwd=source_tree,
        check=True,
    )
    wheel = next(wheelhouse.glob("plsqlwks-*.whl"))
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
from plsqlwks.config import load_config

config = load_config()
print(json.dumps({
    "package_file": str(Path(plsqlwks.__file__).resolve()),
    "workspace": str(config.workspace_dir.resolve()),
    "config_file": str(Path(config.config_file).resolve()),
    "password_file": str(config.password_file.resolve()),
}))
""",
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    paths = {key: Path(value) for key, value in json.loads(completed.stdout).items()}
    install_root = install_dir.resolve()

    assert install_root in paths["package_file"].parents
    assert paths["workspace"] != installed_workspace.resolve()
    for key in ("workspace", "config_file", "password_file"):
        assert paths[key] != install_root
        assert install_root not in paths[key].parents
    assert xdg_data.resolve() in paths["workspace"].parents
    assert xdg_config.resolve() in paths["config_file"].parents
