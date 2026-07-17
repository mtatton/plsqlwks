from __future__ import annotations

import io
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from zipfile import ZipFile

import pytest

from tools import dev


def test_test_profiles_build_deterministic_commands_and_environments():
    inherited = dict.fromkeys(dev.OPTIONAL_TEST_FLAGS, "unexpected")
    inherited["ORACLE_DSN"] = "localhost/service"

    core = dev.test_environment("core", inherited)
    all_tests = dev.test_environment("all", inherited)
    non_oracle = dev.test_environment("non-oracle", inherited)
    plugins = dev.test_environment("plugins", inherited)
    matrix = dev.test_environment("oracle-matrix", inherited)

    assert all(name not in core for name in dev.OPTIONAL_TEST_FLAGS)
    assert all(all_tests[name] == "1" for name in dev.OPTIONAL_TEST_FLAGS)
    assert non_oracle["PLSQLWKS_TEST_PTY"] == "1"
    assert non_oracle["PLSQLWKS_TEST_SLOW"] == "1"
    assert "PLSQLWKS_TEST_ORACLE" not in non_oracle
    assert plugins["PLSQLWKS_TEST_PLUGINS"] == "1"
    assert matrix["PLSQLWKS_TEST_ORACLE"] == "1"
    assert matrix["PLSQLWKS_TEST_ORACLE_MATRIX"] == "1"
    assert matrix["ORACLE_DSN"] == "localhost/service"
    assert dev.test_command("core") == [sys.executable, "-m", "pytest", "--strict-markers"]
    assert dev.test_command("all") == [sys.executable, "-m", "pytest", "--strict-markers"]
    assert dev.test_command("plugins")[-2:] == ["-m", "plugin"]


def test_test_sh_all_switch_selects_complete_test_profile(monkeypatch, tmp_path):
    capture = tmp_path / "arguments"
    fake_python = tmp_path / "python3"
    fake_python.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$PLSQLWKS_TEST_CAPTURE"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PLSQLWKS_TEST_CAPTURE", str(capture))

    subprocess.run(
        [str(dev.ROOT / "test.sh"), "--all"],
        cwd=dev.ROOT,
        check=True,
    )

    assert capture.read_text(encoding="utf-8").splitlines() == [
        "tools/dev.py",
        "test",
        "all",
    ]


def test_coverage_commands_keep_profiles_separate_and_append_plugin_data(tmp_path):
    non_oracle = dev.coverage_test_command(
        "non-oracle",
        tmp_path / "non-oracle.xml",
        append=False,
    )
    plugins = dev.coverage_test_command(
        "plugins",
        tmp_path / "plugins.xml",
        append=True,
    )

    assert non_oracle[:5] == [sys.executable, "-m", "coverage", "run", "-m"]
    assert "--append" not in non_oracle
    assert non_oracle[-3:-1] == ["-m", "not oracle"]
    assert plugins[:5] == [sys.executable, "-m", "coverage", "run", "--append"]
    assert plugins[-3:-1] == ["-m", "plugin"]
    assert plugins[-1] == f"--junitxml={tmp_path / 'plugins.xml'}"


def test_install_constructs_editable_commands(monkeypatch):
    calls: list[tuple[list[str], dict[str, str]]] = []

    def record(arguments, **kwargs):
        calls.append((list(arguments), kwargs["env"]))

    monkeypatch.setattr(dev, "run_command", record)

    dev.install(xlsx=True)

    constraint = str(dev.CI_CONSTRAINTS)
    assert [arguments for arguments, _env in calls] == [
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--constraint",
            constraint,
            "pip",
            "setuptools",
            "wheel",
        ],
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--constraint",
            constraint,
            "--no-build-isolation",
            "--editable",
            ".[dev,xlsx]",
        ],
    ]
    assert calls[0][1]["PIP_CONSTRAINT"] == constraint
    assert "PIP_BUILD_CONSTRAINT" not in calls[0][1]
    assert calls[1][1]["PIP_CONSTRAINT"] == constraint
    assert "PIP_BUILD_CONSTRAINT" not in calls[1][1]


def test_install_release_uses_only_constrained_build_tools(monkeypatch):
    calls: list[tuple[list[str], dict[str, str]]] = []

    def record(arguments, **kwargs):
        calls.append((list(arguments), kwargs["env"]))

    monkeypatch.setattr(dev, "run_command", record)

    dev.install_release()

    constraint = str(dev.CI_CONSTRAINTS)
    assert [arguments for arguments, _env in calls] == [
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--constraint",
            constraint,
            "pip",
            "setuptools",
            "wheel",
            "build",
            "twine",
        ]
    ]
    assert "--editable" not in calls[0][0]
    assert calls[0][1]["PIP_CONSTRAINT"] == constraint


def _preflight_root(tmp_path: Path) -> Path:
    for relative, kind in dev.CI_REQUIRED_PATHS:
        path = tmp_path / relative
        if kind == "file":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("required", encoding="utf-8")
        else:
            path.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _gitlab_environment(version: str) -> dict[str, str]:
    return {
        "GITLAB_CI": "true",
        "CI_RUNNER_TAGS": '["plsqlwks", "docker"]',
        "CI_JOB_IMAGE": f"python:{version}-slim",
        "CI_DISPOSABLE_ENVIRONMENT": "true",
    }


def _oracle_environment(tmp_path: Path, version: str) -> dict[str, str]:
    env = _gitlab_environment(version)
    env.update(dict.fromkeys(dev.ORACLE_MATRIX_ENV_NAMES, "configured"))
    for name in dev.ORACLE_SECRET_FILE_ENV_NAMES:
        path = tmp_path / f"{name.lower()}.secret"
        path.write_text("secret", encoding="utf-8")
        path.chmod(0o600)
        env[name] = str(path)
    env.update(
        {
            "CI_PROJECT_DIR": str(tmp_path),
            "CI_JOB_ID": "42",
            "CI_JOB_TOKEN": "configured",
        }
    )
    return env


def test_gitlab_preflight_accepts_compliant_common_and_oracle_jobs(tmp_path):
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    root = _preflight_root(tmp_path)
    marker = tmp_path / ".dockerenv"
    marker.write_text("", encoding="utf-8")

    assert dev.gitlab_preflight_errors(
        version,
        environ=_gitlab_environment(version),
        root=root,
        docker_marker=marker,
        curses_check=lambda: True,
        pty_check=lambda: True,
    ) == []
    assert dev.gitlab_preflight_errors(
        version,
        oracle=True,
        environ=_oracle_environment(tmp_path, version),
        root=root,
        docker_marker=marker,
        curses_check=lambda: True,
        pty_check=lambda: True,
    ) == []


@pytest.mark.parametrize("missing_name", dev.ORACLE_MATRIX_ENV_NAMES)
def test_oracle_preflight_rejects_each_missing_matrix_variable(tmp_path, missing_name):
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    marker = tmp_path / ".dockerenv"
    marker.write_text("", encoding="utf-8")
    env = _oracle_environment(tmp_path, version)
    env[missing_name] = "   "

    with pytest.raises(RuntimeError, match=rf"required Oracle variable {missing_name} is blank"):
        dev.preflight(
            version,
            oracle=True,
            environ=env,
            root=_preflight_root(tmp_path),
            docker_marker=marker,
            curses_check=lambda: True,
            pty_check=lambda: True,
        )


@pytest.mark.parametrize("secret_name", dev.ORACLE_SECRET_FILE_ENV_NAMES)
def test_oracle_preflight_rejects_each_invalid_secret_file(tmp_path, secret_name):
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    marker = tmp_path / ".dockerenv"
    marker.write_text("", encoding="utf-8")
    env = _oracle_environment(tmp_path, version)
    invalid_secret = tmp_path / f"invalid-{secret_name.lower()}"
    invalid_secret.write_text("secret", encoding="utf-8")
    invalid_secret.chmod(0o644)
    env[secret_name] = str(invalid_secret)

    with pytest.raises(RuntimeError, match=rf"{secret_name} does not reference"):
        dev.preflight(
            version,
            oracle=True,
            environ=env,
            root=_preflight_root(tmp_path),
            docker_marker=marker,
            curses_check=lambda: True,
            pty_check=lambda: True,
        )


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("context", "GitLab CI context is unavailable"),
        ("malformed-tags", "CI_RUNNER_TAGS is not a JSON string array"),
        ("plsqlwks-tag", "required runner tag plsqlwks is absent"),
        ("docker-tag", "required runner tag docker is absent"),
        ("image", "configured Python job image is not active"),
        ("disposable", "job is not running in a disposable executor environment"),
    ],
)
def test_gitlab_preflight_names_runner_contract_failures(tmp_path, case, expected):
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    env = _gitlab_environment(version)
    if case == "context":
        env.pop("GITLAB_CI")
    elif case == "malformed-tags":
        env["CI_RUNNER_TAGS"] = "not-json"
    elif case == "plsqlwks-tag":
        env["CI_RUNNER_TAGS"] = '["docker"]'
    elif case == "docker-tag":
        env["CI_RUNNER_TAGS"] = '["plsqlwks"]'
    elif case == "image":
        env["CI_JOB_IMAGE"] = "wrong"
    elif case == "disposable":
        env["CI_DISPOSABLE_ENVIRONMENT"] = "false"

    errors = dev.gitlab_preflight_errors(
        version,
        environ=env,
        root=_preflight_root(tmp_path),
        docker_marker=tmp_path,
        curses_check=lambda: True,
        pty_check=lambda: True,
    )

    assert expected in errors


@pytest.mark.parametrize(
    ("expected_python", "curses_ok", "pty_ok", "expected"),
    [
        ("invalid", True, True, "expected Python version is not in major.minor form"),
        ("0.0", True, True, "running interpreter does not match requested Python 0.0"),
        (None, False, True, "standard-library curses support is unavailable"),
        (None, True, False, "PTY allocation is unavailable"),
    ],
)
def test_gitlab_preflight_names_python_terminal_failures(
    tmp_path,
    expected_python,
    curses_ok,
    pty_ok,
    expected,
):
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    selected_version = version if expected_python is None else expected_python
    marker = tmp_path / ".dockerenv"
    marker.write_text("", encoding="utf-8")
    errors = dev.gitlab_preflight_errors(
        selected_version,
        environ=_gitlab_environment(selected_version),
        root=_preflight_root(tmp_path),
        docker_marker=marker,
        curses_check=lambda: curses_ok,
        pty_check=lambda: pty_ok,
    )

    assert expected in errors


def test_pty_check_closes_both_descriptors():
    closed: list[int] = []

    assert dev._pty_available(
        openpty=lambda: (10, 11),
        isatty=lambda _fd: True,
        close=closed.append,
    )
    assert closed == [10, 11]


def test_gitlab_preflight_reports_missing_checkout_and_docker_marker(tmp_path):
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    root = _preflight_root(tmp_path)
    (root / "pytest.ini").unlink()

    errors = dev.gitlab_preflight_errors(
        version,
        environ=_gitlab_environment(version),
        root=root,
        docker_marker=tmp_path / "missing-docker-marker",
        curses_check=lambda: True,
        pty_check=lambda: True,
    )

    assert "Docker container marker is unavailable" in errors
    assert "required checkout file pytest.ini is unavailable" in errors


def test_oracle_preflight_is_explicit_and_redacts_values(tmp_path):
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    root = _preflight_root(tmp_path)
    marker = tmp_path / ".dockerenv"
    marker.write_text("", encoding="utf-8")
    env = _oracle_environment(tmp_path, version)
    secret_values = (
        "sentinel-user-value",
        "sentinel-dsn-value",
        "sentinel-secret-path",
        "sentinel-secret-content",
        "\x00sentinel-os-error-path",
        "\x00sentinel-project-path",
    )
    env["ORACLE_USER"] = secret_values[0]
    env["ORACLE_DSN"] = secret_values[1]
    exposed_path = tmp_path / secret_values[2]
    exposed_path.write_text(secret_values[3], encoding="utf-8")
    exposed_path.chmod(0o644)
    env["ORACLE_PASSWORD_FILE"] = str(exposed_path)
    env["PLSQLWKS_TEST_ORACLE_DML_PASSWORD_FILE"] = secret_values[4]
    env["CI_PROJECT_DIR"] = secret_values[5]
    env["PLSQLWKS_TEST_ORACLE_DML_USER"] = "   "

    with pytest.raises(RuntimeError) as error:
        dev.preflight(
            version,
            oracle=True,
            environ=env,
            root=root,
            docker_marker=marker,
            curses_check=lambda: True,
            pty_check=lambda: True,
        )

    message = str(error.value)
    assert "required Oracle variable PLSQLWKS_TEST_ORACLE_DML_USER is blank" in message
    assert "ORACLE_PASSWORD_FILE does not reference a readable, private, nonempty regular non-symlink file" in message
    assert "CI_PROJECT_DIR does not reference an available directory" in message
    assert all(value not in message for value in secret_values)


def test_ci_constraints_are_exact_and_cover_direct_dependencies():
    lines = [
        line.strip()
        for line in dev.CI_CONSTRAINTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    requirements = [line.split(";", 1)[0].strip() for line in lines]

    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.!+-]+", requirement) for requirement in requirements)
    names = {requirement.split("==", 1)[0].lower().replace("_", "-") for requirement in requirements}
    assert {
        "build",
        "coverage",
        "mypy",
        "openpyxl",
        "oracledb",
        "pip",
        "pytest",
        "ruff",
        "setuptools",
        "twine",
        "wheel",
    } <= names


def test_local_ci_runs_canonical_workflow_and_cleans_owned_artifacts(monkeypatch, tmp_path):
    calls: list[object] = []
    monkeypatch.setattr(dev, "hygiene", lambda: calls.append("hygiene"))
    monkeypatch.setattr(dev, "install", lambda *, xlsx: calls.append(("install", xlsx)))
    monkeypatch.setattr(dev, "lint", lambda: calls.append("lint"))
    monkeypatch.setattr(dev, "run_coverage", lambda *, report_dir: calls.append(("coverage", report_dir)))
    monkeypatch.setattr(dev, "build", lambda *, smoke: calls.append(("build", smoke)))
    monkeypatch.setattr(dev, "_remove_generated_packaging_artifacts", lambda: calls.append("cleanup"))

    dev.run_ci(report_dir=tmp_path)

    assert calls == [
        "hygiene",
        ("install", True),
        "lint",
        ("coverage", tmp_path),
        ("build", True),
        "cleanup",
    ]


def test_local_ci_cleans_owned_artifacts_after_failure(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(dev, "hygiene", lambda: calls.append("hygiene"))
    monkeypatch.setattr(dev, "install", lambda *, xlsx: calls.append("install"))

    def fail_lint():
        raise RuntimeError("quality failed")

    monkeypatch.setattr(dev, "lint", fail_lint)
    monkeypatch.setattr(dev, "_remove_generated_packaging_artifacts", lambda: calls.append("cleanup"))

    with pytest.raises(RuntimeError, match="quality failed"):
        dev.run_ci(report_dir=tmp_path)

    assert calls == ["hygiene", "install", "cleanup"]


def test_run_command_propagates_child_failure(monkeypatch, tmp_path):
    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(7, ["false"])

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(subprocess.CalledProcessError) as error:
        dev.run_command(["false"], cwd=tmp_path)

    assert error.value.returncode == 7


def _write_policy_project(root: Path, python_comment: str, toml_suppression: str) -> None:
    package = root / "plsqlwks"
    package.mkdir()
    (package / "example.py").write_text(
        f"DIRECTIVE_TEXT = '# type: ignore[misc]'\ndef example():\n    return 1  {python_comment}\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[tool.ruff.lint.per-file-ignores]\n" + toml_suppression + "\n",
        encoding="utf-8",
    )


def test_suppression_policy_accepts_codes_reasons_and_ignores_strings(tmp_path):
    _write_policy_project(
        tmp_path,
        "# type: ignore[misc]  # reason: deliberate invalid-call coverage",
        '"plsqlwks/example.py" = ["F401"]  # reason: public re-export',
    )

    assert dev.suppression_policy_errors(tmp_path) == []


@pytest.mark.parametrize(
    ("python_comment", "toml_suppression", "expected"),
    [
        ("# type: ignore[misc]", '"plsqlwks/example.py" = []  # reason: staged', "# reason:"),
        ("# noqa  # reason: blanket", '"plsqlwks/example.py" = []  # reason: staged', "# noqa: CODE"),
        (
            "# noqa: F401  # reason: staged",
            '"plsqlwks/example.py" = ["F401"]',
            "configured suppression",
        ),
    ],
)
def test_suppression_policy_rejects_missing_codes_or_reasons(
    tmp_path,
    python_comment,
    toml_suppression,
    expected,
):
    _write_policy_project(tmp_path, python_comment, toml_suppression)

    errors = dev.suppression_policy_errors(tmp_path)

    assert any(expected in error for error in errors)


def _distribution_metadata() -> str:
    return "\n".join(
        (
            "Metadata-Version: 2.4",
            "Name: plsqlwks",
            "Version: 0.1.8",
            "Author: unu2000",
            "License-Expression: LicenseRef-plsqlwks-Donationware",
            "License-File: license.txt",
            "Project-URL: Repository, https://gitlab.com/unununu/plsqlwks",
            "Project-URL: Ko-fi, https://ko-fi.com/unu2000",
            'Requires-Dist: openpyxl>=3.1; extra == "xlsx"',
            "Description: https://gitlab.com/unununu/plsqlwks/-/raw/main/img/preview.png",
            "",
        )
    )


def _write_distribution_archives(tmp_path: Path, metadata: str) -> tuple[Path, Path]:
    encoded_metadata = metadata.encode("utf-8")
    wheel = tmp_path / "plsqlwks-0.1.8-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("plsqlwks-0.1.8.dist-info/METADATA", encoded_metadata)
        archive.writestr("plsqlwks-0.1.8.dist-info/licenses/license.txt", b"license")

    source_archive = tmp_path / "plsqlwks-0.1.8.tar.gz"
    with tarfile.open(source_archive, "w:gz") as archive:
        for name, payload in (("license.txt", b"license"), ("PKG-INFO", encoded_metadata)):
            info = tarfile.TarInfo(f"plsqlwks-0.1.8/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return wheel, source_archive


def test_distribution_inspection_accepts_required_metadata(tmp_path):
    wheel, source_archive = _write_distribution_archives(tmp_path, _distribution_metadata())

    dev.inspect_distributions(wheel, source_archive, expected_version="0.1.8")


def test_distribution_inspection_rejects_release_identity_mismatch(tmp_path):
    wheel, source_archive = _write_distribution_archives(
        tmp_path,
        _distribution_metadata().replace("Version: 0.1.8", "Version: 0.1.9"),
    )

    with pytest.raises(RuntimeError, match="distribution identity does not match release"):
        dev.inspect_distributions(wheel, source_archive, expected_version="0.1.8")


def _write_release_project(
    root: Path,
    *,
    version: str = "1.2.3",
    project_name: str = "plsqlwks",
    unreleased: str = "",
    release_heading: str = "1.2.3 20260717",
    notes: str = "- Release note.",
) -> None:
    package = root / "plsqlwks"
    package.mkdir()
    (package / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (root / "pyproject.toml").write_text(f'[project]\nname = "{project_name}"\n', encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## Unreleased\n\n{unreleased}\n\n## {release_heading}\n\n{notes}\n\n## 1.2.2 20260716\n\n- Older.\n",
        encoding="utf-8",
    )


def test_release_check_matches_tag_package_project_and_changelog(tmp_path):
    _write_release_project(tmp_path)
    notes_path = tmp_path / "release-notes.md"

    dev.release_check("plsqlwks-1.2.3", "plsqlwks", root=tmp_path, notes_out=notes_path)

    assert notes_path.read_text(encoding="utf-8") == "- Release note.\n"


@pytest.mark.parametrize(
    ("tag", "ci_project_name", "project_options", "expected"),
    [
        ("v1.2.3", "plsqlwks", {}, "tag must match"),
        ("plsqlwks-1.2.4", "plsqlwks", {}, "does not match plsqlwks.__version__"),
        ("plsqlwks-1.2.3", "renamed", {}, "does not match distribution name"),
        ("plsqlwks-1.2.3", "plsqlwks", {"unreleased": "- Pending."}, "Unreleased section must be empty"),
        (
            "plsqlwks-1.2.3",
            "plsqlwks",
            {"release_heading": "1.2.3 20260230"},
            "release date is invalid",
        ),
        (
            "plsqlwks-1.2.3",
            "plsqlwks",
            {"release_heading": "1.2.4 20260717"},
            "latest CHANGELOG.md version 1.2.4 does not match release version 1.2.3",
        ),
        ("plsqlwks-1.2.3", "plsqlwks", {"notes": ""}, "release notes must not be empty"),
    ],
)
def test_release_check_rejects_inconsistent_identity(tmp_path, tag, ci_project_name, project_options, expected):
    _write_release_project(tmp_path, **project_options)

    with pytest.raises(RuntimeError, match=expected):
        dev.release_check(tag, ci_project_name, root=tmp_path)


def test_release_check_requires_the_referenced_changelog(tmp_path):
    _write_release_project(tmp_path)
    (tmp_path / "CHANGELOG.md").unlink()

    with pytest.raises(RuntimeError, match="CHANGELOG.md is required for release validation"):
        dev.release_check("plsqlwks-1.2.3", "plsqlwks", root=tmp_path)


@pytest.mark.parametrize(
    "notes",
    (
        "1.2.3",
        "plsqlwks-1.2.3",
        "plsqlwks 1.2.3",
        "- plsqlwks-1.2.3",
        "# plsqlwks 1.2.3",
    ),
)
def test_release_check_rejects_tag_only_placeholder_notes(tmp_path, notes):
    _write_release_project(tmp_path, notes=notes)

    with pytest.raises(RuntimeError, match="release notes must contain descriptive content"):
        dev.release_check("plsqlwks-1.2.3", "plsqlwks", root=tmp_path)


def test_sha256_manifest_is_sorted_and_verifiable(tmp_path):
    second = tmp_path / "b.tar.gz"
    first = tmp_path / "a.whl"
    second.write_bytes(b"source")
    first.write_bytes(b"wheel")
    manifest = tmp_path / "SHA256SUMS"

    dev.write_sha256s((second, first), manifest)

    lines = manifest.read_text(encoding="ascii").splitlines()
    assert lines[0].endswith("  a.whl")
    assert lines[1].endswith("  b.tar.gz")
    assert lines[0].split()[0] == "ba59926159d2aa256eb8739b8da7e2b574b960e1202c6d624cbe981cef996c91"


def test_release_build_runs_strict_metadata_smoke_and_checksum_gates(monkeypatch, tmp_path):
    _write_release_project(tmp_path)
    wheel = tmp_path / "dist" / "plsqlwks-1.2.3-py3-none-any.whl"
    source_archive = tmp_path / "dist" / "plsqlwks-1.2.3.tar.gz"
    calls: list[object] = []
    monkeypatch.setattr(dev, "hygiene", lambda root: calls.append(("hygiene", root)))
    monkeypatch.setattr(dev, "run_command", lambda arguments, **kwargs: calls.append((list(arguments), kwargs)))
    monkeypatch.setattr(dev, "_distribution_paths", lambda **_kwargs: (wheel, source_archive))
    monkeypatch.setattr(
        dev,
        "inspect_distributions",
        lambda *args, **kwargs: calls.append(("inspect", args, kwargs)),
    )
    monkeypatch.setattr(dev, "smoke_test_wheel", lambda *args, **kwargs: calls.append(("smoke", args, kwargs)))
    monkeypatch.setattr(dev, "write_sha256s", lambda paths, output: calls.append(("sha256", paths, output)))

    dev.release_build("plsqlwks-1.2.3", "plsqlwks", root=tmp_path)

    commands = [call[0] for call in calls if isinstance(call, tuple) and isinstance(call[0], list)]
    assert commands == [
        [sys.executable, "-m", "build", "--no-isolation"],
        [sys.executable, "-m", "twine", "check", "--strict", str(wheel), str(source_archive)],
    ]
    assert calls[0] == ("hygiene", tmp_path)
    assert any(call[0] == "inspect" for call in calls)
    assert any(call[0] == "smoke" for call in calls)
    assert calls[-1] == ("sha256", (wheel, source_archive), tmp_path / "dist" / "SHA256SUMS")


def test_repository_satisfies_suppression_policy():
    assert dev.suppression_policy_errors(dev.ROOT) == []
