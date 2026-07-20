from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GITLAB_RESERVED_TOP_LEVEL_KEYS = {
    "after_script",
    "before_script",
    "cache",
    "default",
    "image",
    "include",
    "services",
    "stages",
    "variables",
    "workflow",
}


def _top_level_yaml_blocks(text: str) -> dict[str, str]:
    """Return top-level mapping blocks from the repository's CI YAML subset."""
    lines = text.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if line and not line[0].isspace() and not line.startswith("#") and line.endswith(":")
    ]
    blocks: dict[str, str] = {}
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        blocks[lines[start][:-1]] = "\n".join(lines[start:end])
    return blocks


def _gitlab_job_blocks(text: str) -> dict[str, str]:
    return {
        name: block
        for name, block in _top_level_yaml_blocks(text).items()
        if name not in GITLAB_RESERVED_TOP_LEVEL_KEYS
    }


def test_ci_uses_the_versioned_runner_without_a_duplicate_root_workflow():
    github = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    gitlab = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")

    assert not (ROOT / "ci.yml").exists()
    for workflow in (github, gitlab):
        assert "python tools/dev.py install" in workflow
        assert "python tools/dev.py lint" in workflow
        assert "python tools/dev.py coverage --report-dir coverage-reports" in workflow
        assert "python tools/dev.py build --smoke" in workflow
        assert "python -m ruff" not in workflow
        assert "python -m mypy" not in workflow
        assert "python -m pytest" not in workflow
        assert "python -m build" not in workflow
        assert "MYPY_TARGETS" not in workflow


def test_github_uses_runner_installed_python_without_the_actions_tool_cache():
    github = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "actions/setup-python@" not in github
    assert 'PLSQLWKS_PYTHON_VERSION: "3.14"' in github
    assert "PLSQLWKS_PYTHON_VERSION: ${{ matrix.python-version }}" in github
    assert github.count(
        'run: /usr/local/bin/python"$PLSQLWKS_PYTHON_VERSION" -m venv --clear .venv'
    ) == 6
    assert "tools/prepare_github_python.sh" not in github
    assert not (ROOT / "tools/prepare_github_python.sh").exists()
    assert github.count(".venv/bin/python tools/dev.py") == 11


def test_ci_publishes_short_lived_machine_readable_reports_for_each_python_gate():
    github = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    gitlab = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    report_paths = (
        "coverage-reports/junit-non-oracle.xml",
        "coverage-reports/junit-plugins.xml",
        "coverage-reports/coverage.xml",
        "coverage-reports/coverage.json",
    )

    for workflow in (github, gitlab):
        assert '"3.10"' in workflow
        assert '"3.14"' in workflow
        assert "test-and-coverage:" in workflow
        assert "python tools/dev.py install --xlsx" in workflow
        assert "python tools/dev.py coverage --report-dir coverage-reports" in workflow
        assert all(path in workflow for path in report_paths)

    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1" in github
    assert "retention-days: 7" in github
    assert "if-no-files-found: error" in github
    assert "include-hidden-files: false" in github
    assert 'cat coverage-reports/coverage-summary.md >> "$GITHUB_STEP_SUMMARY"' in github
    assert "when: always" in gitlab
    assert "expire_in: 7 days" in gitlab
    assert "coverage_format: cobertura" in gitlab


def test_oracle_jobs_do_not_upload_reports_or_caches():
    github = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    gitlab = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")

    github_oracle = github.split("  oracle-integration-19c:", 1)[1]
    gitlab_oracle = gitlab.split(".oracle-integration:", 1)[1]
    for workflow in (github_oracle, gitlab_oracle):
        assert "upload-artifact" not in workflow
        assert "cache:" not in workflow
    assert "\n  artifacts:" not in gitlab_oracle


def test_gitlab_jobs_are_required_private_runner_gates():
    gitlab = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".gitlab/ci/release.yml").read_text(encoding="utf-8")
    root_blocks = _top_level_yaml_blocks(gitlab)
    root_jobs = _gitlab_job_blocks(gitlab)
    release_jobs = _gitlab_job_blocks(release)
    oracle_template = root_jobs[".oracle-integration"]

    assert "  tags:\n    - plsqlwks\n    - docker" in root_blocks["default"]
    assert {"repository-hygiene", "quality", "test-and-coverage", "build-smoke"} < root_jobs.keys()
    assert {"release-identity", "release-build", "release-publish"} < release_jobs.keys()
    for name, block in {**root_jobs, **release_jobs}.items():
        assert "\n  tags:" not in block, f"{name} must inherit the private runner tags from default"
    for workflow in (gitlab, release):
        assert "allow_failure:" not in workflow
        assert "optional: true" not in workflow
    assert 'PLSQLWKS_TEST_ORACLE: "1"' in oracle_template
    assert 'PLSQLWKS_TEST_ORACLE_MATRIX: "1"' in oracle_template
    assert "interruptible: false" in oracle_template
    assert 'AFTER_SCRIPT_IGNORE_ERRORS: "false"' in oracle_template
    assert (
        "    - if: '$CI_COMMIT_TAG =~ /^plsqlwks-/ && "
        "$CI_COMMIT_REF_PROTECTED == \"true\"'"
    ) in oracle_template
    assert (
        "    - if: '$CI_COMMIT_REF_PROTECTED == \"true\" && "
        "($CI_PIPELINE_SOURCE == \"push\" || $CI_PIPELINE_SOURCE == \"web\")'"
    ) in oracle_template
    for upstream_job in (
        "repository-hygiene",
        "quality",
        "test-and-coverage",
        "build-smoke",
    ):
        assert f"- job: {upstream_job}\n      artifacts: false" in oracle_template
    assert oracle_template.count("artifacts: false") == 4
    assert "extends: .oracle-integration" in root_jobs["oracle-integration-19c"]
    assert "extends: .oracle-integration" in root_jobs["oracle-integration-26ai"]
    assert gitlab.count("python tools/dev.py test oracle-matrix") == 1


def test_gitlab_preflights_each_selected_runner_before_job_work():
    gitlab = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    common = 'python tools/dev.py preflight gitlab --expected-python "$PYTHON_VERSION"'
    oracle = common + " --oracle"

    assert gitlab.count("CI preflight failed: Python interpreter is unavailable") == 4
    assert gitlab.count(common) == 4
    assert gitlab.count(oracle) == 1
    assert "test -n \"$ORACLE_USER\"" not in gitlab
    assert "test -f \"$ORACLE_PASSWORD_FILE\"" not in gitlab
    oracle_template = gitlab.split(".oracle-integration:", 1)[1].split("oracle-integration-19c:", 1)[0]
    assert oracle_template.index(oracle) < oracle_template.index("python tools/dev.py install")
    assert oracle_template.index(oracle) < oracle_template.index("python tools/dev.py test oracle-matrix")


def test_github_keeps_both_protected_oracle_matrix_jobs():
    github = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "oracle-integration-19c:" in github
    assert "oracle-integration-26ai:" in github
    assert github.count("python tools/dev.py test oracle-matrix") == 2
    assert github.count("environment: oracle-integration") == 2


def test_gitlab_has_a_separate_protected_release_tag_pipeline():
    gitlab = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".gitlab/ci/release.yml").read_text(encoding="utf-8")

    assert "local: .gitlab/ci/release.yml" in gitlab
    assert "- release-validate" in gitlab
    assert "- release-build" in gitlab
    assert "- release" in gitlab
    assert 'if: \'$CI_COMMIT_TAG =~ /^plsqlwks-/\'' in gitlab
    assert 'test "$CI_COMMIT_REF_PROTECTED" = "true"' in release
    assert "release-identity:" in release
    assert "release-build:" in release
    assert "release-publish:" in release
    assert "allow_failure:" not in release


def test_release_build_requires_every_gate_and_a_clean_official_build():
    release = (ROOT / ".gitlab/ci/release.yml").read_text(encoding="utf-8")
    build_job = release.split("release-build:", 1)[1].split("release-publish:", 1)[0]

    for upstream_job in (
        "release-identity",
        "repository-hygiene",
        "quality",
        "test-and-coverage",
        "build-smoke",
        "oracle-integration-19c",
        "oracle-integration-26ai",
    ):
        assert f"- job: {upstream_job}" in build_job
    assert "GIT_STRATEGY: clone" in build_job
    assert 'RUNNER_GENERATE_ARTIFACTS_METADATA: "true"' in build_job
    assert "cache: []" in build_job
    assert "python tools/dev.py hygiene" in build_job
    assert "python tools/dev.py install-release" in build_job
    assert "python tools/dev.py install\n" not in build_job
    assert "python tools/dev.py release-build" in build_job
    assert "dist/*.whl" in build_job
    assert "dist/*.tar.gz" in build_job
    assert "dist/SHA256SUMS" in build_job


def test_release_publication_uses_nonempty_notes_and_durable_registry_assets():
    release = (ROOT / ".gitlab/ci/release.yml").read_text(encoding="utf-8")
    jobs = _gitlab_job_blocks(release)
    identity_job = jobs["release-identity"]
    publish_job = jobs["release-publish"]

    assert "release-check" in identity_job
    assert "--notes-out release-notes.md" in identity_job
    assert "test -s release-notes.md" in identity_job
    assert "      - release-notes.md" in identity_job
    assert "- job: release-identity\n      artifacts: true" in publish_job
    assert "test -s release-notes.md" in publish_job
    assert "sha256sum --check dist/SHA256SUMS" in publish_job
    assert "--notes-file release-notes.md" in publish_job
    assert '--notes "$CI_COMMIT_TAG"' not in publish_job
    assert 'description: "$CI_COMMIT_TAG"' not in release
    assert "--no-update" in publish_job
    assert "--package-name plsqlwks" in publish_job
    assert "--use-package-registry" in publish_job
    assert "SLSA provenance and build evidence" in publish_job
    assert "RELEASE_ACCESS_TOKEN" in publish_job
