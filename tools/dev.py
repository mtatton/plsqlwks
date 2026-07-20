from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tokenize
import venv
from collections.abc import Sequence
from datetime import datetime
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile

if __package__:
    from .coverage_gate import (
        check_coverage,
        record_coverage,
        render_markdown,
    )
else:
    from coverage_gate import (  # type: ignore[no-redef]  # reason: script execution cannot use package-relative imports
        check_coverage,
        record_coverage,
        render_markdown,
    )

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_BASELINE = ROOT / "tools" / "coverage_baseline.json"
CI_CONSTRAINTS = ROOT / "constraints" / "ci.txt"
COVERAGE_REPORT_FILES = (
    "junit-non-oracle.xml",
    "junit-plugins.xml",
    "coverage.xml",
    "coverage.json",
)
OPTIONAL_TEST_FLAGS = (
    "PLSQLWKS_TEST_ORACLE",
    "PLSQLWKS_TEST_ORACLE_MATRIX",
    "PLSQLWKS_TEST_PLUGINS",
    "PLSQLWKS_TEST_PTY",
    "PLSQLWKS_TEST_SLOW",
)
GITLAB_RUNNER_TAGS = ("plsqlwks", "docker")
CI_REQUIRED_PATHS = (
    ("pyproject.toml", "file"),
    ("pytest.ini", "file"),
    ("constraints/ci.txt", "file"),
    ("plsqlwks", "directory"),
    ("tests", "directory"),
    ("plugin-requirements", "directory"),
)
ORACLE_MATRIX_ENV_NAMES = (
    "PLSQLWKS_TEST_ORACLE_TARGET",
    "ORACLE_USER",
    "ORACLE_DSN",
    "ORACLE_PASSWORD_FILE",
    "PLSQLWKS_TEST_ORACLE_DESCRIPTOR_DSN",
    "PLSQLWKS_TEST_ORACLE_DML_USER",
    "PLSQLWKS_TEST_ORACLE_DML_PASSWORD_FILE",
    "PLSQLWKS_TEST_ORACLE_READ_ONLY_USER",
    "PLSQLWKS_TEST_ORACLE_READ_ONLY_PASSWORD_FILE",
    "PLSQLWKS_TEST_ORACLE_EXPECTED_DB_UNIQUE_NAME",
    "PLSQLWKS_TEST_ORACLE_EXPECTED_CON_NAME",
    "PLSQLWKS_TEST_ORACLE_EXPECTED_SERVICE_NAME",
    "PLSQLWKS_TEST_ORACLE_GUARD_TOKEN_FILE",
)
ORACLE_SECRET_FILE_ENV_NAMES = (
    "ORACLE_PASSWORD_FILE",
    "PLSQLWKS_TEST_ORACLE_DML_PASSWORD_FILE",
    "PLSQLWKS_TEST_ORACLE_READ_ONLY_PASSWORD_FILE",
    "PLSQLWKS_TEST_ORACLE_GUARD_TOKEN_FILE",
)
POLICY_SOURCE_DIRS = ("plsqlwks", "tests", "tools")
POLICY_EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "sdist",
    "vdb",
}
REASON_SUFFIX_RE = re.compile(r"\s+#\s*reason:\s*(?P<reason>\S.*)\s*$", re.IGNORECASE)
NOQA_DIRECTIVE_RE = re.compile(
    r"#\s*(?:ruff:\s*)?noqa\s*:\s*"
    r"[A-Z]+\d{3}(?:\s*,\s*[A-Z]+\d{3})*\s*$",
    re.IGNORECASE,
)
TYPE_IGNORE_DIRECTIVE_RE = re.compile(
    r"#\s*type:\s*ignore\s*\["
    r"[a-z0-9-]+(?:\s*,\s*[a-z0-9-]+)*\]\s*$",
    re.IGNORECASE,
)
RELEASE_TAG_RE = re.compile(r"^plsqlwks-(?P<version>\d+\.\d+\.\d+)$")


class PolicyError(RuntimeError):
    pass


def python_command(*arguments: str) -> list[str]:
    return [sys.executable, *arguments]


def run_command(
    arguments: Sequence[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> None:
    print(f"+ {shlex.join(arguments)}", flush=True)
    subprocess.run(list(arguments), cwd=cwd, env=env, check=True)


def constraint_environment(
    environ: dict[str, str] | None = None,
    *,
    build: bool = False,
) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    env["PIP_CONSTRAINT"] = str(CI_CONSTRAINTS)
    if build:
        env["PIP_BUILD_CONSTRAINT"] = str(CI_CONSTRAINTS)
    else:
        env.pop("PIP_BUILD_CONSTRAINT", None)
    return env


def install(*, xlsx: bool = False) -> None:
    constraint = str(CI_CONSTRAINTS)
    run_command(
        python_command("-m", "pip", "install", "--constraint", constraint, "pip", "setuptools", "wheel"),
        env=constraint_environment(),
    )
    extras = "dev,xlsx" if xlsx else "dev"
    run_command(
        python_command(
            "-m",
            "pip",
            "install",
            "--constraint",
            constraint,
            "--no-build-isolation",
            "--editable",
            f".[{extras}]",
        ),
        env=constraint_environment(),
    )


def install_release() -> None:
    constraint = str(CI_CONSTRAINTS)
    run_command(
        python_command(
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
        ),
        env=constraint_environment(),
    )


def _curses_available() -> bool:
    try:
        import curses
    except ImportError:
        return False
    try:
        curses.setupterm(term="xterm")
    except (curses.error, OSError):
        return False
    return True


def _pty_available(
    *,
    openpty=None,
    isatty=None,
    close=None,
) -> bool:
    if openpty is None:
        try:
            import pty
        except ImportError:
            return False
        openpty = pty.openpty
    if isatty is None:
        isatty = os.isatty
    if close is None:
        close = os.close
    try:
        master_fd, slave_fd = openpty()
    except OSError:
        return False
    try:
        return bool(isatty(master_fd) and isatty(slave_fd))
    finally:
        close(master_fd)
        close(slave_fd)


def _private_secret_file_is_valid(value: str) -> bool:
    try:
        path = Path(os.path.expanduser(value))
        stat = path.stat()
        if not path.is_file() or path.is_symlink() or stat.st_size <= 0:
            return False
        if os.name == "posix" and stat.st_mode & 0o077:
            return False
        with path.open("rb") as handle:
            return bool(handle.read(1))
    except (OSError, RuntimeError, ValueError):
        return False


def gitlab_preflight_errors(
    expected_python: str,
    *,
    oracle: bool = False,
    environ: dict[str, str] | None = None,
    root: Path = ROOT,
    docker_marker: Path = Path("/.dockerenv"),
    curses_check=None,
    pty_check=None,
) -> list[str]:
    env = dict(os.environ if environ is None else environ)
    errors: list[str] = []

    if env.get("GITLAB_CI") != "true":
        errors.append("GitLab CI context is unavailable")

    raw_tags = env.get("CI_RUNNER_TAGS", "")
    try:
        parsed_tags = json.loads(raw_tags)
        tags_valid = isinstance(parsed_tags, list) and all(isinstance(tag, str) for tag in parsed_tags)
    except (json.JSONDecodeError, TypeError):
        parsed_tags = []
        tags_valid = False
    if not tags_valid:
        errors.append("CI_RUNNER_TAGS is not a JSON string array")
    else:
        for tag in GITLAB_RUNNER_TAGS:
            if tag not in parsed_tags:
                errors.append(f"required runner tag {tag} is absent")

    expected_match = re.fullmatch(r"(?P<major>\d+)\.(?P<minor>\d+)", expected_python)
    if expected_match is None:
        errors.append("expected Python version is not in major.minor form")
        expected_image = None
    else:
        expected_version = (int(expected_match.group("major")), int(expected_match.group("minor")))
        if sys.version_info[:2] != expected_version:
            errors.append(f"running interpreter does not match requested Python {expected_python}")
        expected_image = f"python:{expected_python}-slim"

    if expected_image is not None and env.get("CI_JOB_IMAGE") != expected_image:
        errors.append("configured Python job image is not active")
    if env.get("CI_DISPOSABLE_ENVIRONMENT") != "true" or env.get("CI_SHARED_ENVIRONMENT") == "true":
        errors.append("job is not running in a disposable executor environment")
    if not docker_marker.is_file():
        errors.append("Docker container marker is unavailable")

    if curses_check is None:
        curses_check = _curses_available
    if not curses_check():
        errors.append("standard-library curses support is unavailable")
    if pty_check is None:
        pty_check = _pty_available
    if not pty_check():
        errors.append("PTY allocation is unavailable")

    for relative, kind in CI_REQUIRED_PATHS:
        path = root / relative
        available = path.is_file() if kind == "file" else path.is_dir()
        if not available:
            errors.append(f"required checkout {kind} {relative} is unavailable")

    if not oracle:
        return errors

    for name in ORACLE_MATRIX_ENV_NAMES:
        if not env.get(name, "").strip():
            errors.append(f"required Oracle variable {name} is blank")
    for name in ORACLE_SECRET_FILE_ENV_NAMES:
        value = env.get(name, "")
        if value.strip() and not _private_secret_file_is_valid(value):
            errors.append(f"{name} does not reference a readable, private, nonempty regular non-symlink file")

    project_dir_value = env.get("CI_PROJECT_DIR", "")
    project_dir = None
    project_dir_valid = False
    if project_dir_value:
        try:
            project_dir = Path(project_dir_value)
            project_dir_valid = project_dir.is_dir()
        except (OSError, RuntimeError, ValueError):
            project_dir = None
    if not project_dir_valid:
        errors.append("CI_PROJECT_DIR does not reference an available directory")
    job_id = env.get("CI_JOB_ID", "")
    if not job_id.isdigit():
        errors.append("CI_JOB_ID is not numeric")
    if not env.get("CI_JOB_TOKEN", ""):
        errors.append("CI_JOB_TOKEN is blank")
    if project_dir is not None and project_dir_valid and job_id.isdigit():
        try:
            workspace = project_dir / f".ci-oracle-workspace-{job_id}"
            workspace_exists = workspace.exists() or workspace.is_symlink()
        except (OSError, RuntimeError, ValueError):
            workspace_exists = True
        if workspace_exists:
            errors.append("job-specific Oracle workspace already exists")
    return errors


def preflight(
    expected_python: str,
    *,
    oracle: bool = False,
    environ: dict[str, str] | None = None,
    root: Path = ROOT,
    docker_marker: Path = Path("/.dockerenv"),
    curses_check=None,
    pty_check=None,
) -> None:
    errors = gitlab_preflight_errors(
        expected_python,
        oracle=oracle,
        environ=environ,
        root=root,
        docker_marker=docker_marker,
        curses_check=curses_check,
        pty_check=pty_check,
    )
    if errors:
        raise RuntimeError("CI preflight failed:\n" + "\n".join(f"- {error}" for error in errors))
    print("CI preflight passed", flush=True)


def test_environment(
    profile: str,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    for name in OPTIONAL_TEST_FLAGS:
        env.pop(name, None)
    if profile == "all":
        for name in OPTIONAL_TEST_FLAGS:
            env[name] = "1"
    elif profile == "non-oracle":
        env["PLSQLWKS_TEST_PTY"] = "1"
        env["PLSQLWKS_TEST_SLOW"] = "1"
    elif profile == "plugins":
        env["PLSQLWKS_TEST_PLUGINS"] = "1"
    elif profile == "oracle":
        env["PLSQLWKS_TEST_ORACLE"] = "1"
    elif profile == "oracle-matrix":
        env["PLSQLWKS_TEST_ORACLE"] = "1"
        env["PLSQLWKS_TEST_ORACLE_MATRIX"] = "1"
    return env


def test_command(profile: str, *, junit_xml: Path | None = None) -> list[str]:
    command = python_command("-m", "pytest", "--strict-markers")
    if junit_xml is not None:
        command.append(f"--junitxml={junit_xml}")
    marker = {
        "all": None,
        "core": None,
        "non-oracle": "not oracle",
        "plugins": "plugin",
        "oracle": "oracle",
        "oracle-matrix": "oracle",
    }[profile]
    if marker is not None:
        command.extend(("-m", marker))
    return command


def run_tests(profile: str) -> None:
    run_command(test_command(profile), env=test_environment(profile))


def coverage_test_command(profile: str, junit_xml: Path, *, append: bool) -> list[str]:
    command = python_command("-m", "coverage", "run")
    if append:
        command.append("--append")
    command.extend(("-m", *test_command(profile)[2:], f"--junitxml={junit_xml}"))
    return command


def run_coverage(
    *,
    report_dir: Path,
    record: str | None = None,
) -> None:
    report_dir = report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    report_paths = {name: report_dir / name for name in COVERAGE_REPORT_FILES}
    summary_path = report_dir / "coverage-summary.md"
    data_path = report_dir / ".coverage"
    for path in (*report_paths.values(), summary_path):
        path.unlink(missing_ok=True)

    coverage_env = dict(os.environ)
    coverage_env["COVERAGE_FILE"] = str(data_path)
    run_command(python_command("-m", "coverage", "erase"), env=coverage_env)

    failures: list[str] = []
    profiles = (
        ("non-oracle", report_paths["junit-non-oracle.xml"]),
        ("plugins", report_paths["junit-plugins.xml"]),
    )
    for index, (profile, junit_path) in enumerate(profiles):
        profile_env = test_environment(profile, coverage_env)
        try:
            run_command(
                coverage_test_command(profile, junit_path, append=index > 0),
                env=profile_env,
            )
        except subprocess.CalledProcessError as exc:
            failures.append(f"{profile} tests exited with status {exc.returncode}")

    include_all = "plsqlwks/*,tests/oracle_matrix.py"
    report_commands = (
        python_command(
            "-m",
            "coverage",
            "json",
            "--include",
            include_all,
            "-o",
            str(report_paths["coverage.json"]),
        ),
        python_command(
            "-m",
            "coverage",
            "xml",
            "--include",
            "plsqlwks/*",
            "-o",
            str(report_paths["coverage.xml"]),
        ),
        python_command("-m", "coverage", "report", "--include", include_all),
    )
    for command in report_commands:
        try:
            run_command(command, env=coverage_env)
        except subprocess.CalledProcessError as exc:
            failures.append(f"coverage reporting exited with status {exc.returncode}")
            break

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if report_paths["coverage.json"].is_file():
        if record is None:
            snapshot = check_coverage(
                report_paths["coverage.json"],
                COVERAGE_BASELINE,
                python_version,
            )
        else:
            snapshot = record_coverage(
                report_paths["coverage.json"],
                COVERAGE_BASELINE,
                python_version,
                record,
            )
        summary_path.write_text(render_markdown(snapshot, python_version), encoding="utf-8")
    if failures:
        raise RuntimeError("Coverage workflow failed:\n" + "\n".join(failures))


def _policy_python_files(root: Path) -> list[Path]:
    paths = set(root.glob("*.py"))
    for directory_name in POLICY_SOURCE_DIRS:
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.py"):
            if not POLICY_EXCLUDED_DIRS.intersection(path.relative_to(root).parts):
                paths.add(path)
    return sorted(paths)


def _directive_and_reason(comment: str) -> tuple[str, str | None]:
    reason_match = REASON_SUFFIX_RE.search(comment)
    if reason_match is None:
        return comment.strip(), None
    return comment[: reason_match.start()].rstrip(), reason_match.group("reason").strip()


def _python_policy_errors(root: Path, path: Path) -> list[str]:
    errors: list[str] = []
    try:
        with tokenize.open(path) as handle:
            tokens = tokenize.generate_tokens(handle.readline)
            comments = [token for token in tokens if token.type == tokenize.COMMENT]
    except (OSError, SyntaxError, UnicodeError, tokenize.TokenError) as exc:
        return [f"{path.relative_to(root)}: unable to inspect suppression comments: {exc}"]
    for token in comments:
        comment = token.string.strip()
        lowered = comment.casefold()
        if not (re.match(r"#\s*(?:ruff:\s*)?noqa\b", lowered) or re.match(r"#\s*type:\s*ignore\b", lowered)):
            continue
        directive, reason = _directive_and_reason(comment)
        if "noqa" in lowered:
            valid_directive = NOQA_DIRECTIVE_RE.fullmatch(directive) is not None
            required = "# noqa: CODE  # reason: explanation"
        else:
            valid_directive = TYPE_IGNORE_DIRECTIVE_RE.fullmatch(directive) is not None
            required = "# type: ignore[code]  # reason: explanation"
        relative = path.relative_to(root)
        if not valid_directive:
            errors.append(f"{relative}:{token.start[0]}: suppression must use `{required}`")
        elif reason is None:
            errors.append(f"{relative}:{token.start[0]}: suppression is missing a non-empty `# reason:`")
    return errors


def _toml_policy_errors(root: Path) -> list[str]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return []
    errors: list[str] = []
    section = ""
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]")
            continue
        directive = raw_line.split("#", 1)[0].strip()
        if not directive or "=" not in directive:
            continue
        key = directive.split("=", 1)[0].strip().strip('"')
        is_ruff_ignore = section in {
            "tool.ruff.lint.per-file-ignores",
            "tool.ruff.lint.extend-per-file-ignores",
        } or (section.startswith("tool.ruff.lint") and key in {"ignore", "extend-ignore"})
        is_mypy_ignore = section.startswith("tool.mypy") and key in {
            "disable_error_code",
            "ignore_errors",
        }
        if not (is_ruff_ignore or is_mypy_ignore):
            continue
        reason_match = REASON_SUFFIX_RE.search(raw_line)
        if reason_match is None:
            errors.append(f"pyproject.toml:{line_number}: configured suppression is missing a non-empty `# reason:`")
    return errors


def suppression_policy_errors(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    for path in _policy_python_files(root):
        errors.extend(_python_policy_errors(root, path))
    errors.extend(_toml_policy_errors(root))
    return errors


def check_suppression_policy(root: Path = ROOT) -> None:
    errors = suppression_policy_errors(root)
    if errors:
        raise PolicyError("Static-check suppression policy failed:\n" + "\n".join(errors))


def lint() -> None:
    check_suppression_policy()
    run_command(python_command("-m", "ruff", "check", "plsqlwks", "tests", "tools"))
    run_command(python_command("-m", "mypy", "plsqlwks"))


def hygiene(root: Path = ROOT) -> None:
    generated = [root / "PKG-INFO", root / "build", root / "dist"]
    generated.extend(root.glob("*.egg-info"))
    present = sorted(str(path.relative_to(root)) for path in generated if path.exists())
    if present:
        raise RuntimeError("generated packaging artifacts are present: " + ", ".join(present))


def _remove_generated_packaging_artifacts(root: Path = ROOT) -> None:
    paths = [root / "PKG-INFO", root / "build", root / "dist"]
    paths.extend(root.glob("*.egg-info"))
    for path in paths:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)


def run_ci(*, report_dir: Path) -> None:
    hygiene()
    try:
        install(xlsx=True)
        lint()
        run_coverage(report_dir=report_dir)
        build(smoke=True)
    finally:
        _remove_generated_packaging_artifacts()


def _source_version(root: Path = ROOT) -> str:
    version_source = (root / "plsqlwks" / "__init__.py").read_text(encoding="utf-8")
    version_match = re.search(r'^__version__\s*=\s*["\'](?P<version>[^"\']+)["\']\s*$', version_source, re.MULTILINE)
    if version_match is None:
        raise RuntimeError("package version could not be read from plsqlwks/__init__.py")
    return version_match.group("version")


def _project_name(root: Path = ROOT) -> str:
    section = ""
    for raw_line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "project":
            continue
        match = re.fullmatch(r'name\s*=\s*["\'](?P<name>[^"\']+)["\']', line)
        if match is not None:
            return match.group("name")
    raise RuntimeError("distribution name could not be read from pyproject.toml")


def _release_notes_are_placeholder(notes: str, *, tag: str, version: str) -> bool:
    normalized_lines: list[str] = []
    for raw_line in notes.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^(?:#{1,6}|[-*+]|\d+[.)])\s+", "", line)
        normalized_lines.append(line)
    normalized = " ".join(normalized_lines).casefold()
    return normalized in {
        version.casefold(),
        tag.casefold(),
        f"plsqlwks {version}".casefold(),
    }


def release_identity(tag: str, ci_project_name: str, *, root: Path = ROOT) -> tuple[str, str, str]:
    tag_match = RELEASE_TAG_RE.fullmatch(tag)
    if tag_match is None:
        raise RuntimeError("release tag must match plsqlwks-X.Y.Z")
    version = tag_match.group("version")
    distribution_name = _project_name(root)
    if distribution_name != "plsqlwks":
        raise RuntimeError(f"distribution name must be plsqlwks, found {distribution_name!r}")
    if ci_project_name != distribution_name:
        raise RuntimeError(
            f"GitLab project name {ci_project_name!r} does not match distribution name {distribution_name!r}"
        )
    source_version = _source_version(root)
    if source_version != version:
        raise RuntimeError(f"release tag version {version} does not match plsqlwks.__version__ {source_version}")

    try:
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("CHANGELOG.md is required for release validation") from exc
    headings = list(re.finditer(r"^##[ \t]+(?P<title>.+?)[ \t]*$", changelog, re.MULTILINE))
    if not headings or headings[0].group("title") != "Unreleased":
        raise RuntimeError("CHANGELOG.md must begin with an Unreleased level-two section")
    if len(headings) < 2:
        raise RuntimeError("CHANGELOG.md does not contain a dated release section")
    unreleased = changelog[headings[0].end() : headings[1].start()].strip()
    if unreleased:
        raise RuntimeError("CHANGELOG.md Unreleased section must be empty before tagging")
    release_heading = headings[1].group("title")
    release_match = re.fullmatch(r"(?P<version>\d+\.\d+\.\d+) (?P<date>\d{8})", release_heading)
    if release_match is None:
        raise RuntimeError("latest CHANGELOG.md release heading must be 'X.Y.Z YYYYMMDD'")
    if release_match.group("version") != version:
        raise RuntimeError(
            f"latest CHANGELOG.md version {release_match.group('version')} does not match release version {version}"
        )
    try:
        datetime.strptime(release_match.group("date"), "%Y%m%d")
    except ValueError as exc:
        raise RuntimeError("latest CHANGELOG.md release date is invalid") from exc
    end = headings[2].start() if len(headings) > 2 else len(changelog)
    notes = changelog[headings[1].end() : end].strip()
    if not notes:
        raise RuntimeError("latest CHANGELOG.md release notes must not be empty")
    if _release_notes_are_placeholder(notes, tag=tag, version=version):
        raise RuntimeError("latest CHANGELOG.md release notes must contain descriptive content")
    return distribution_name, version, notes


def release_check(tag: str, ci_project_name: str, *, notes_out: Path | None = None, root: Path = ROOT) -> None:
    _distribution_name, version, notes = release_identity(tag, ci_project_name, root=root)
    if notes_out is not None:
        notes_out.write_text(notes + "\n", encoding="utf-8")
    print(f"release identity passed for plsqlwks {version}", flush=True)


def _distribution_paths(
    *,
    distribution_name: str = "plsqlwks",
    version: str | None = None,
    root: Path = ROOT,
) -> tuple[Path, Path]:
    if version is None:
        version = _source_version(root)

    normalized_version = version.replace("-", "_")
    normalized_name = distribution_name.replace("-", "_")
    wheels = sorted((root / "dist").glob(f"{normalized_name}-{normalized_version}-*.whl"))
    source_archives = sorted((root / "dist").glob(f"{distribution_name}-{version}.tar.gz"))
    if len(wheels) != 1 or len(source_archives) != 1:
        raise RuntimeError(
            f"expected one wheel and source archive for {version}; "
            f"found wheels={wheels}, source_archives={source_archives}"
        )
    return wheels[0], source_archives[0]


def _read_distribution_metadata(wheel: Path, source_archive: Path) -> tuple[str, str]:
    with ZipFile(wheel) as archive:
        corrupt_member = archive.testzip()
        if corrupt_member is not None:
            raise RuntimeError(f"wheel contains corrupt member {corrupt_member}")
        names = archive.namelist()
        if not any(name.endswith(".dist-info/licenses/license.txt") for name in names):
            raise RuntimeError("wheel does not contain license.txt")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise RuntimeError(f"wheel must contain exactly one METADATA file, found {metadata_names}")
        metadata_name = metadata_names[0]
        wheel_metadata = archive.read(metadata_name).decode("utf-8")
    with tarfile.open(source_archive) as archive:
        names = archive.getnames()
        if not any(name.endswith("/license.txt") for name in names):
            raise RuntimeError("source archive does not contain license.txt")
        pkg_info_names = [name for name in names if name.endswith("/PKG-INFO")]
        if len(pkg_info_names) != 1:
            raise RuntimeError(f"source archive must contain exactly one PKG-INFO file, found {pkg_info_names}")
        pkg_info_name = pkg_info_names[0]
        pkg_info_file = archive.extractfile(pkg_info_name)
        if pkg_info_file is None:
            raise RuntimeError("source archive PKG-INFO is not readable")
        pkg_info = pkg_info_file.read().decode("utf-8")
    return wheel_metadata, pkg_info


def inspect_distributions(
    wheel: Path,
    source_archive: Path,
    *,
    expected_name: str = "plsqlwks",
    expected_version: str | None = None,
) -> None:
    if expected_version is None:
        expected_version = _source_version()
    wheel_metadata, pkg_info = _read_distribution_metadata(wheel, source_archive)
    required_lines = {
        "Author: unu2000",
        "License-Expression: LicenseRef-plsqlwks-Donationware",
        "License-File: license.txt",
        "Project-URL: Repository, https://gitlab.com/unununu/plsqlwks",
        "Project-URL: Ko-fi, https://ko-fi.com/unu2000",
        'Requires-Dist: openpyxl>=3.1; extra == "xlsx"',
    }
    for raw_metadata in (wheel_metadata, pkg_info):
        metadata = Parser().parsestr(raw_metadata)
        if metadata["Name"] != expected_name or metadata["Version"] != expected_version:
            raise RuntimeError(
                "distribution identity does not match release: "
                f"expected {expected_name} {expected_version}, found {metadata['Name']} {metadata['Version']}"
            )
        lines = set(raw_metadata.splitlines())
        missing = sorted(required_lines - lines)
        if missing:
            raise RuntimeError(f"distribution metadata is missing: {missing}")
        if metadata["Author-email"] is not None or metadata["Maintainer"] is not None:
            raise RuntimeError("distribution exposes unauthorized author or maintainer metadata")
        if "https://gitlab.com/unununu/plsqlwks/-/raw/main/img/preview.png" not in raw_metadata:
            raise RuntimeError("distribution metadata does not contain the GitLab preview URL")


def _venv_python(environment_root: Path) -> Path:
    scripts = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return environment_root / scripts / executable


def smoke_test_wheel(
    wheel: Path,
    *,
    expected_name: str = "plsqlwks",
    expected_version: str | None = None,
) -> None:
    if expected_version is None:
        expected_version = _source_version()
    with tempfile.TemporaryDirectory(prefix="plsqlwks-wheel-smoke-") as temporary:
        smoke_root = Path(temporary)
        environment_root = smoke_root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment_root)
        smoke_python = _venv_python(environment_root)
        smoke_env = dict(os.environ)
        for name in ("ORACLE_PASSWORD_FILE", "PLSQLWKS_WORKSPACE", "PYTHONPATH"):
            smoke_env.pop(name, None)
        smoke_env["XDG_CONFIG_HOME"] = str(smoke_root / "config")
        smoke_env["XDG_DATA_HOME"] = str(smoke_root / "data")
        constraint = str(CI_CONSTRAINTS)
        run_command(
            [
                str(smoke_python),
                "-m",
                "pip",
                "install",
                "--constraint",
                constraint,
                "pip",
                "setuptools",
                "wheel",
            ],
            cwd=smoke_root,
            env=constraint_environment(smoke_env),
        )
        run_command(
            [str(smoke_python), "-m", "pip", "install", "--constraint", constraint, str(wheel)],
            cwd=smoke_root,
            env=constraint_environment(smoke_env, build=True),
        )
        run_command(
            [
                str(smoke_python),
                "-c",
                'import importlib.util; assert importlib.util.find_spec("openpyxl") is None',
            ],
            cwd=smoke_root,
            env=smoke_env,
        )
        run_command([str(smoke_python), "-m", "plsqlwks", "--help"], cwd=smoke_root, env=smoke_env)
        run_command(
            [
                str(smoke_python),
                "-c",
                (
                    "import importlib.metadata as metadata; import plsqlwks; "
                    "from plsqlwks.config import load_config; "
                    "from plsqlwks.workspace import ensure_workspace; "
                    "config=load_config(); ensure_workspace(config); "
                    f"assert metadata.metadata({expected_name!r})['Name'] == {expected_name!r}; "
                    f"assert metadata.version({expected_name!r}) == {expected_version!r}; "
                    f"assert plsqlwks.__version__ == {expected_version!r}; "
                    "assert config.autocommit is False"
                ),
            ],
            cwd=smoke_root,
            env=smoke_env,
        )
        run_command(
            [str(smoke_python), "-m", "pip", "install", "--constraint", constraint, f"{wheel}[xlsx]"],
            cwd=smoke_root,
            env=constraint_environment(smoke_env, build=True),
        )
        run_command(
            [str(smoke_python), "-c", "import openpyxl; assert openpyxl.__version__"],
            cwd=smoke_root,
            env=smoke_env,
        )


def build(*, smoke: bool = False) -> None:
    run_command(
        python_command("-m", "build", "--no-isolation"),
        env=constraint_environment(),
    )
    if not smoke:
        return
    wheel, source_archive = _distribution_paths()
    inspect_distributions(wheel, source_archive)
    smoke_test_wheel(wheel)


def write_sha256s(paths: Sequence[Path], output: Path) -> None:
    lines: list[str] = []
    for path in sorted(paths, key=lambda candidate: candidate.name):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        lines.append(f"{digest.hexdigest()}  {path.name}")
    output.write_text("\n".join(lines) + "\n", encoding="ascii")


def release_build(tag: str, ci_project_name: str, *, root: Path = ROOT) -> None:
    distribution_name, version, _notes = release_identity(tag, ci_project_name, root=root)
    hygiene(root)
    run_command(
        python_command("-m", "build", "--no-isolation"),
        cwd=root,
        env=constraint_environment(),
    )
    wheel, source_archive = _distribution_paths(
        distribution_name=distribution_name,
        version=version,
        root=root,
    )
    inspect_distributions(
        wheel,
        source_archive,
        expected_name=distribution_name,
        expected_version=version,
    )
    run_command(
        python_command("-m", "twine", "check", "--strict", str(wheel), str(source_archive)),
        cwd=root,
        env=constraint_environment(),
    )
    smoke_test_wheel(wheel, expected_name=distribution_name, expected_version=version)
    write_sha256s((wheel, source_archive), root / "dist" / "SHA256SUMS")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Canonical local and CI commands for plsqlwks")
    subparsers = parser.add_subparsers(dest="command", required=True)
    install_parser = subparsers.add_parser("install", help="install editable development dependencies")
    install_parser.add_argument("--xlsx", action="store_true", help="include optional XLSX support")
    subparsers.add_parser("install-release", help="install only constrained release build and metadata tools")
    preflight_parser = subparsers.add_parser("preflight", help="validate a GitLab runner before job work")
    preflight_parser.add_argument("platform", choices=("gitlab",))
    preflight_parser.add_argument("--expected-python", required=True, help="required Python major.minor")
    preflight_parser.add_argument("--oracle", action="store_true", help="also validate protected Oracle inputs")
    subparsers.add_parser("policy", help="validate static-check suppression reasons")
    subparsers.add_parser("lint", help="run suppression, Ruff, and mypy checks")
    test_parser = subparsers.add_parser("test", help="run a deterministic pytest profile")
    test_parser.add_argument(
        "profile",
        choices=("all", "core", "non-oracle", "plugins", "oracle", "oracle-matrix"),
    )
    coverage_parser = subparsers.add_parser(
        "coverage",
        help="run report-producing non-Oracle tests and enforce coverage gates",
    )
    coverage_parser.add_argument(
        "--report-dir",
        type=Path,
        default=ROOT / "test-reports",
        help="directory for JUnit and coverage reports",
    )
    coverage_parser.add_argument(
        "--record",
        choices=("initial", "minimum"),
        help="record this Python version's baseline instead of enforcing it",
    )
    build_parser = subparsers.add_parser("build", help="build wheel and source archive")
    build_parser.add_argument("--smoke", action="store_true", help="inspect and install built artifacts")
    release_check_parser = subparsers.add_parser("release-check", help="validate tag, package, and changelog identity")
    release_check_parser.add_argument("--tag", required=True, help="GitLab release tag")
    release_check_parser.add_argument("--project-name", required=True, help="GitLab project name")
    release_check_parser.add_argument("--notes-out", type=Path, help="write validated changelog notes here")
    release_build_parser = subparsers.add_parser("release-build", help="build and verify official release artifacts")
    release_build_parser.add_argument("--tag", required=True, help="GitLab release tag")
    release_build_parser.add_argument("--project-name", required=True, help="GitLab project name")
    subparsers.add_parser("hygiene", help="reject generated packaging artifacts in a clean checkout")
    ci_parser = subparsers.add_parser("ci", help="run the complete constrained local non-Oracle CI workflow")
    ci_parser.add_argument(
        "--report-dir",
        type=Path,
        default=ROOT / "coverage-reports",
        help="directory for JUnit and coverage reports",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    try:
        if args.command == "install":
            install(xlsx=args.xlsx)
        elif args.command == "install-release":
            install_release()
        elif args.command == "preflight":
            preflight(args.expected_python, oracle=args.oracle)
        elif args.command == "policy":
            check_suppression_policy()
        elif args.command == "lint":
            lint()
        elif args.command == "test":
            run_tests(args.profile)
        elif args.command == "coverage":
            run_coverage(report_dir=args.report_dir, record=args.record)
        elif args.command == "build":
            build(smoke=args.smoke)
        elif args.command == "release-check":
            release_check(args.tag, args.project_name, notes_out=args.notes_out)
        elif args.command == "release-build":
            release_build(args.tag, args.project_name)
        elif args.command == "hygiene":
            hygiene()
        elif args.command == "ci":
            run_ci(report_dir=args.report_dir)
    except (PolicyError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
