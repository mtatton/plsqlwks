from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CRITICAL_FILES = (
    "plsqlwks/db/transactions.py",
    "plsqlwks/db/sql_analysis.py",
    "tests/oracle_matrix.py",
    "plsqlwks/exporting.py",
)


class CoverageGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class CoverageMetric:
    covered: int
    total: int

    @property
    def percent(self) -> float:
        return 100.0 if self.total == 0 else 100.0 * self.covered / self.total

    def as_json(self) -> dict[str, int]:
        return {"covered": self.covered, "total": self.total}


@dataclass(frozen=True)
class CoverageScope:
    lines: CoverageMetric
    branches: CoverageMetric

    @property
    def combined(self) -> CoverageMetric:
        return CoverageMetric(
            self.lines.covered + self.branches.covered,
            self.lines.total + self.branches.total,
        )

    def as_json(self) -> dict[str, dict[str, int]]:
        return {
            "lines": self.lines.as_json(),
            "branches": self.branches.as_json(),
            "combined": self.combined.as_json(),
        }


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CoverageGateError(f"Unable to read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CoverageGateError(f"{label.capitalize()} {path} must contain a JSON object")
    return value


def _summary_scope(summary: object, label: str) -> CoverageScope:
    if not isinstance(summary, dict):
        raise CoverageGateError(f"Coverage report is missing the summary for {label}")
    try:
        lines = CoverageMetric(int(summary["covered_lines"]), int(summary["num_statements"]))
        branches = CoverageMetric(int(summary["covered_branches"]), int(summary["num_branches"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise CoverageGateError(f"Coverage report has an invalid summary for {label}") from exc
    for name, metric in (("lines", lines), ("branches", branches)):
        if metric.covered < 0 or metric.total < 0 or metric.covered > metric.total:
            raise CoverageGateError(f"Coverage report has invalid {name} counts for {label}")
    return CoverageScope(lines, branches)


def coverage_snapshot(report: dict[str, Any]) -> dict[str, CoverageScope]:
    files = report.get("files")
    if not isinstance(files, dict):
        raise CoverageGateError("Coverage report is missing its files object")

    normalized_files = {str(name).replace("\\", "/"): value for name, value in files.items()}
    production_summaries = [
        value.get("summary")
        for name, value in normalized_files.items()
        if name.startswith("plsqlwks/") and isinstance(value, dict)
    ]
    if not production_summaries:
        raise CoverageGateError("Coverage report contains no plsqlwks production files")

    production_lines = CoverageMetric(
        sum(_summary_scope(summary, "production file").lines.covered for summary in production_summaries),
        sum(_summary_scope(summary, "production file").lines.total for summary in production_summaries),
    )
    production_branches = CoverageMetric(
        sum(_summary_scope(summary, "production file").branches.covered for summary in production_summaries),
        sum(_summary_scope(summary, "production file").branches.total for summary in production_summaries),
    )
    snapshot = {"production": CoverageScope(production_lines, production_branches)}
    for path in CRITICAL_FILES:
        file_report = normalized_files.get(path)
        if not isinstance(file_report, dict):
            raise CoverageGateError(f"Coverage report is missing critical file {path}")
        snapshot[path] = _summary_scope(file_report.get("summary"), path)
    return snapshot


def load_coverage_report(path: Path) -> tuple[dict[str, Any], dict[str, CoverageScope]]:
    report = _load_json(path, "coverage report")
    return report, coverage_snapshot(report)


def _configured_metric(value: object, label: str) -> CoverageMetric:
    if not isinstance(value, dict):
        raise CoverageGateError(f"Coverage baseline is missing {label}")
    try:
        metric = CoverageMetric(int(value["covered"]), int(value["total"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise CoverageGateError(f"Coverage baseline has invalid counts for {label}") from exc
    if metric.covered < 0 or metric.total <= 0 or metric.covered > metric.total:
        raise CoverageGateError(f"Coverage baseline has invalid counts for {label}")
    return metric


def _ratio_is_at_least(current: CoverageMetric, minimum: CoverageMetric) -> bool:
    if current.total == 0:
        return minimum.covered == minimum.total
    return current.covered * minimum.total >= minimum.covered * current.total


def _check_metric(current: CoverageMetric, minimum: CoverageMetric, label: str) -> None:
    if not _ratio_is_at_least(current, minimum):
        raise CoverageGateError(
            f"{label} coverage dropped: {current.covered}/{current.total} "
            f"({current.percent:.4f}%) is below {minimum.covered}/{minimum.total} "
            f"({minimum.percent:.4f}%)"
        )


def check_coverage(
    report_path: Path,
    baseline_path: Path,
    python_version: str,
) -> dict[str, CoverageScope]:
    report, snapshot = load_coverage_report(report_path)
    baseline = _load_json(baseline_path, "coverage baseline")
    if baseline.get("schema") != 1:
        raise CoverageGateError("Coverage baseline schema must be 1")
    expected_coverage_version = baseline.get("coverage_version")
    report_meta = report.get("meta")
    actual_coverage_version = report_meta.get("version") if isinstance(report_meta, dict) else None
    if actual_coverage_version != expected_coverage_version:
        raise CoverageGateError(
            f"Coverage report version {actual_coverage_version!r} does not match "
            f"the baseline version {expected_coverage_version!r}"
        )

    minimums = baseline.get("minimum")
    version_minimum = minimums.get(python_version) if isinstance(minimums, dict) else None
    if not isinstance(version_minimum, dict):
        raise CoverageGateError(f"Coverage baseline has no minimum for Python {python_version}")
    production_minimum = version_minimum.get("production")
    if not isinstance(production_minimum, dict):
        raise CoverageGateError(f"Coverage baseline has no production minimum for Python {python_version}")
    for metric_name in ("lines", "branches", "combined"):
        current_metric = getattr(snapshot["production"], metric_name)
        minimum_metric = _configured_metric(
            production_minimum.get(metric_name),
            f"Python {python_version} production {metric_name}",
        )
        _check_metric(current_metric, minimum_metric, f"Production {metric_name}")

    critical_minimums = version_minimum.get("critical")
    floors = baseline.get("critical_floors")
    if not isinstance(critical_minimums, dict) or not isinstance(floors, dict):
        raise CoverageGateError("Coverage baseline is missing critical-file settings")
    try:
        line_floor = float(floors["line_percent"])
        branch_floor = float(floors["branch_percent"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CoverageGateError("Coverage baseline has invalid critical floors") from exc
    for path in CRITICAL_FILES:
        configured = critical_minimums.get(path)
        if not isinstance(configured, dict):
            raise CoverageGateError(f"Coverage baseline is missing the critical minimum for {path}")
        current = snapshot[path]
        if current.lines.percent + 1e-12 < line_floor:
            raise CoverageGateError(
                f"{path} line coverage {current.lines.percent:.4f}% is below {line_floor:.4f}%"
            )
        if current.branches.percent + 1e-12 < branch_floor:
            raise CoverageGateError(
                f"{path} branch coverage {current.branches.percent:.4f}% is below {branch_floor:.4f}%"
            )
        for metric_name in ("lines", "branches"):
            _check_metric(
                getattr(current, metric_name),
                _configured_metric(configured.get(metric_name), f"{path} {metric_name}"),
                f"{path} {metric_name}",
            )
    return snapshot


def record_coverage(
    report_path: Path,
    baseline_path: Path,
    python_version: str,
    section: str,
) -> dict[str, CoverageScope]:
    if section not in {"initial", "minimum"}:
        raise CoverageGateError("Coverage baseline section must be initial or minimum")
    report, snapshot = load_coverage_report(report_path)
    baseline = _load_json(baseline_path, "coverage baseline")
    report_meta = report.get("meta")
    report_version = report_meta.get("version") if isinstance(report_meta, dict) else None
    if report_version != baseline.get("coverage_version"):
        raise CoverageGateError("Refusing to record a baseline from a different coverage.py version")
    sections = baseline.setdefault(section, {})
    if not isinstance(sections, dict):
        raise CoverageGateError(f"Coverage baseline {section} section must be an object")
    sections[python_version] = {
        "production": snapshot["production"].as_json(),
        "critical": {path: snapshot[path].as_json() for path in CRITICAL_FILES},
    }
    baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return snapshot


def render_markdown(snapshot: dict[str, CoverageScope], python_version: str) -> str:
    lines = [
        f"## Python {python_version} coverage",
        "",
        "| Scope | Line | Branch | Combined |",
        "|---|---:|---:|---:|",
    ]
    for name in ("production", *CRITICAL_FILES):
        scope = snapshot[name]
        lines.append(
            f"| `{name}` | {scope.lines.percent:.4f}% | "
            f"{scope.branches.percent:.4f}% | {scope.combined.percent:.4f}% |"
        )
    return "\n".join(lines) + "\n"
