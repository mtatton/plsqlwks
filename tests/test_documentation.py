from __future__ import annotations

import configparser
import re
import shlex
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import unquote

import plsqlwks
from plsqlwks.config import load_config, settings
from plsqlwks.plugins import PLUGIN_API_VERSION
from plsqlwks.plugins import html_export as html_export_plugin
from plsqlwks.plugins import xlsx_export as xlsx_export_plugin
from plsqlwks.ui import parse_args as parse_app_args
from tools import dev

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTATION_PATHS = tuple(
    ROOT / name
    for name in (
        "README.md",
        "QUICKSTART.md",
        "PLUGINS.md",
        "ARCHITECTURE.md",
    )
)
GITLAB_REPOSITORY_URL = "https://gitlab.com/unununu/plsqlwks"
MARKDOWN_LINK_RE = re.compile(r"!?\[[^]]*\]\((?P<target>[^)\s]+)(?:\s+[^)]*)?\)")
FENCED_BLOCK_RE = re.compile(
    r"^```(?P<language>[A-Za-z0-9_-]*)[^\n]*\n(?P<body>.*?)^```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
INLINE_INI_RE = re.compile(
    r"`\[(?P<section>[a-z0-9_.-]+)\]\s+(?P<option>[a-z][a-z0-9_]*)"
    r"(?:\s*=\s*[^`]*)?`"
)
DOCUMENTED_ENV_RE = re.compile(
    r"^(?:export\s+)?(?P<name>ORACLE_[A-Z0-9_]+|PLSQLWKS_(?!TEST_)[A-Z0-9_]+)=",
    re.MULTILINE,
)
CORE_ENV_NAMES = {
    "ORACLE_USER",
    "ORACLE_DSN",
    "ORACLE_PASSWORD_FILE",
    "PLSQLWKS_WORKSPACE",
    "PLSQLWKS_MAX_ROWS",
    "PLSQLWKS_ARRAYSIZE",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _toml_string_table(text: str, section: str) -> dict[str, str]:
    marker = f"[{section}]"
    if marker not in text:
        return {}
    block = text.split(marker, 1)[1].split("\n[", 1)[0]
    return {
        match.group("key"): match.group("value")
        for match in re.finditer(
            r'^\s*(?P<key>[A-Za-z0-9_-]+)\s*=\s*"(?P<value>[^"]*)"\s*$',
            block,
            re.MULTILINE,
        )
    }


def _repository_file(target: str, source: Path) -> Path | None:
    target = target.strip("<>")
    for prefix in (
        f"{GITLAB_REPOSITORY_URL}/-/blob/main/",
        f"{GITLAB_REPOSITORY_URL}/-/raw/main/",
    ):
        if target.startswith(prefix):
            relative = unquote(target.removeprefix(prefix).split("#", 1)[0].split("?", 1)[0])
            return ROOT / relative
    if "://" in target or target.startswith(("mailto:", "#")):
        return None
    relative = unquote(target.split("#", 1)[0].split("?", 1)[0])
    return source.parent / relative


def _assert_repository_file(path: Path, *, reference: str) -> None:
    resolved = path.resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        raise AssertionError(f"repository reference escapes the project: {reference}") from None
    assert resolved.is_file(), f"repository reference does not exist: {reference}"


def _fenced_blocks(text: str, language: str) -> Iterator[str]:
    for match in FENCED_BLOCK_RE.finditer(text):
        if match.group("language").casefold() == language.casefold():
            yield match.group("body")


def _ini_pairs(text: str) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for block in _fenced_blocks(text, "ini"):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(block)
        pairs.update((section, option) for section in parser.sections() for option in parser.options(section))
    pairs.update((match.group("section"), match.group("option")) for match in INLINE_INI_RE.finditer(text))
    return pairs


def _shell_commands(text: str) -> Iterator[list[str]]:
    for block in _fenced_blocks(text, "bash"):
        for raw_line in block.replace("\\\n", " ").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            tokens = shlex.split(line, comments=True)
            while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
                tokens.pop(0)
            if tokens:
                yield tokens


def _parse_documented_args(parser: Callable[[list[str]], object], arguments: list[str]) -> None:
    try:
        parser(arguments)
    except SystemExit as exc:
        assert exc.code == 0 and {"--help", "--version"}.intersection(arguments)


def _module_environment_names(module: object) -> set[str]:
    return {
        value
        for name, value in vars(module).items()
        if name.endswith("_ENV") and isinstance(value, str)
    }


def test_internal_documentation_and_package_urls_resolve_to_repository_files():
    for source in DOCUMENTATION_PATHS:
        for match in MARKDOWN_LINK_RE.finditer(_read(source)):
            target = match.group("target")
            referenced = _repository_file(target, source)
            if referenced is not None:
                _assert_repository_file(referenced, reference=f"{source.name}: {target}")

    project_urls = _toml_string_table(_read(ROOT / "pyproject.toml"), "project.urls")
    file_urls = {"Architecture", "Changelog", "Compatibility", "Quickstart"}
    assert file_urls <= project_urls.keys()
    for label in file_urls:
        referenced = _repository_file(project_urls[label], ROOT / "pyproject.toml")
        assert referenced is not None
        _assert_repository_file(referenced, reference=f"project.urls.{label}")


def test_documented_application_and_development_commands_parse_without_execution():
    app_examples: list[list[str]] = []
    development_examples: list[list[str]] = []
    for source in DOCUMENTATION_PATHS:
        for tokens in _shell_commands(_read(source)):
            if tokens[0] == "plsqlwks":
                app_examples.append(tokens)
                _parse_documented_args(parse_app_args, tokens[1:])
            elif tokens[:3] == ["python3", "-m", "plsqlwks"]:
                app_examples.append(tokens)
                _parse_documented_args(parse_app_args, tokens[3:])
            elif tokens[:2] == ["python3", "tools/dev.py"]:
                development_examples.append(tokens)
                _parse_documented_args(dev.create_parser().parse_args, tokens[2:])

    assert app_examples
    assert development_examples
    assert 'plsqlwks = "plsqlwks.ui:main"' in _read(ROOT / "pyproject.toml")


def test_documented_ini_names_match_the_configuration_that_is_actually_loaded():
    documented_pairs = set().union(*(_ini_pairs(_read(path)) for path in DOCUMENTATION_PATHS))
    example = configparser.ConfigParser(interpolation=None)
    example.read(ROOT / "workspace/config.ini.example", encoding="utf-8")
    public_example_pairs = {
        (section, option)
        for section in example.sections()
        if section != "session.tabs"
        for option in example.options(section)
    }
    assert documented_pairs == public_example_pairs

    example.set("database", "autocommit", "yes")
    example.set("database", "read_only", "yes")
    example.set("database", "remember_bind_values", "yes")
    example.set(settings.CSV_EXPORT_SECTION, "separator", ";")
    example.set(settings.CSV_EXPORT_SECTION, "null_value", "NULL")
    example.set(settings.CSV_EXPORT_SECTION, "date_format", "%Y")
    example.set(settings.CSV_EXPORT_SECTION, "protect_formulas", "yes")
    for section in (settings.CSV_EXPORT_SECTION, settings.HTML_EXPORT_SECTION, settings.XLSX_EXPORT_SECTION):
        example.set(section, "enabled", "no")

    assert settings.read_autocommit(example) is True
    assert settings.read_read_only(example) is True
    assert settings.read_remember_bind_values(example) is True
    assert settings.read_csv_export_settings(example) == (";", "NULL", "%Y")
    assert settings.read_csv_protect_formulas(example) is True
    assert all(
        settings.read_plugin_enabled(example, section) is False
        for section in (settings.CSV_EXPORT_SECTION, settings.HTML_EXPORT_SECTION, settings.XLSX_EXPORT_SECTION)
    )
    assert set(settings.read_editor_colors(example)) == set(settings.EDITOR_COLOR_KINDS)
    assert set(settings.read_explain_colors(example)) == set(settings.EXPLAIN_COLOR_KINDS)


def test_documented_environment_names_match_runtime_and_plugin_loaders(monkeypatch, tmp_path):
    html_env_names = _module_environment_names(html_export_plugin)
    xlsx_env_names = _module_environment_names(xlsx_export_plugin)
    expected_names = CORE_ENV_NAMES | html_env_names | xlsx_env_names
    documented_names = set().union(*(set(DOCUMENTED_ENV_RE.findall(_read(path))) for path in DOCUMENTATION_PATHS))
    assert documented_names == expected_names

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    password_file = tmp_path / "orapass"
    password_file.write_text("secret", encoding="utf-8")
    environment = {
        "ORACLE_USER": "documented-user",
        "ORACLE_DSN": "documented-dsn",
        "ORACLE_PASSWORD_FILE": str(password_file),
        "PLSQLWKS_WORKSPACE": str(workspace),
        "PLSQLWKS_MAX_ROWS": "321",
        "PLSQLWKS_ARRAYSIZE": "17",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    config = load_config()
    assert config.user == "documented-user"
    assert config.dsn == "documented-dsn"
    assert config.password_file == password_file
    assert config.workspace_dir == workspace
    assert config.max_rows == 321
    assert config.arraysize == 17

    monkeypatch.setenv("PLSQLWKS_HTML_EXPORT_NULL_VALUE", "NULL")
    monkeypatch.setenv("PLSQLWKS_HTML_EXPORT_THEME", "dark")
    monkeypatch.setenv("PLSQLWKS_HTML_EXPORT_DATE_FORMAT", "%Y")
    html_options = html_export_plugin._environment_options()
    assert (html_options.null_value, html_options.theme, html_options.date_format) == ("NULL", "dark", "%Y")

    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_NULL_VALUE", "NULL")
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_THEME", "dark")
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_DATE_FORMAT", "%Y")
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_AUTO_FILTER", "no")
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_AUTO_WIDTH", "no")
    monkeypatch.setenv("PLSQLWKS_XLSX_EXPORT_FREEZE_TOP_ROW", "no")
    xlsx_options = xlsx_export_plugin._environment_options()
    assert (xlsx_options.null_value, xlsx_options.theme, xlsx_options.date_format) == ("NULL", "dark", "%Y")
    assert (xlsx_options.auto_filter, xlsx_options.auto_width, xlsx_options.freeze_top_row) == (False, False, False)


def test_documented_versions_match_package_metadata_and_source_contracts():
    pyproject = _read(ROOT / "pyproject.toml")
    readme = _read(ROOT / "README.md")
    quickstart = _read(ROOT / "QUICKSTART.md")
    plugins = _read(ROOT / "PLUGINS.md")
    architecture = _read(ROOT / "ARCHITECTURE.md")

    assert 'dynamic = ["version"]' in pyproject
    assert 'version = {attr = "plsqlwks.__version__"}' in pyproject
    latest_release = re.search(r"^## (?P<version>\d+\.\d+\.\d+) \d{8}$", _read(ROOT / "CHANGELOG.md"), re.MULTILINE)
    assert latest_release is not None
    assert latest_release.group("version") == plsqlwks.__version__
    quickstart_version = re.search(r'dependencies = \["plsqlwks>=(?P<version>\d+\.\d+\.\d+)"\]', quickstart)
    assert quickstart_version is not None
    assert quickstart_version.group("version") == plsqlwks.__version__
    quickstart_html = _read(ROOT / "QUICKSTART.html")
    assert f"plsqlwks&gt;={plsqlwks.__version__}" in quickstart_html
    assert ">--version</span>" in quickstart_html

    python_requirement = re.search(r'^requires-python = "(?P<value>>=\d+\.\d+)"$', pyproject, re.MULTILINE)
    assert python_requirement is not None
    minimum_python = python_requirement.group("value").removeprefix(">=")
    assert f"Python {minimum_python} or newer" in readme
    assert f"Python {minimum_python} or newer" in quickstart
    assert f'requires-python = "{python_requirement.group("value")}"' in quickstart

    for document in (readme, quickstart, plugins, architecture):
        assert re.search(rf"\bPlugin API (?:version {PLUGIN_API_VERSION}|v{PLUGIN_API_VERSION})\b", document)

    xlsx_requirement_match = re.search(r'"(?P<requirement>openpyxl>=[^"]+)"', pyproject)
    assert xlsx_requirement_match is not None
    xlsx_requirement = xlsx_requirement_match.group("requirement")
    assert _read(ROOT / "plugin-requirements/xlsx-export/requirements.txt").splitlines()[-1] == xlsx_requirement
    assert xlsx_requirement in readme
    assert xlsx_requirement in plugins
