from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from tools.coverage_gate import (
    CRITICAL_FILES,
    CoverageGateError,
    check_coverage,
    coverage_snapshot,
    record_coverage,
    render_markdown,
)


def _summary(covered_lines: int = 95, covered_branches: int = 18) -> dict[str, int | float]:
    return {
        "covered_lines": covered_lines,
        "num_statements": 100,
        "covered_branches": covered_branches,
        "num_branches": 20,
        "percent_covered": 94.1667,
    }


def _report() -> dict[str, object]:
    files = {
        "plsqlwks/example.py": {"summary": _summary()},
        "plsqlwks/db/transactions.py": {"summary": _summary()},
        "plsqlwks/db/sql_analysis.py": {"summary": _summary()},
        "plsqlwks/exporting.py": {"summary": _summary()},
        "tests/oracle_matrix.py": {"summary": _summary()},
    }
    return {"meta": {"version": "7.15.1"}, "files": files}


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _baseline(report: dict[str, object]) -> dict[str, object]:
    snapshot = coverage_snapshot(report)
    version = {
        "production": snapshot["production"].as_json(),
        "critical": {path: snapshot[path].as_json() for path in CRITICAL_FILES},
    }
    return {
        "schema": 1,
        "coverage_version": "7.15.1",
        "critical_floors": {"line_percent": 95.0, "branch_percent": 90.0},
        "initial": {"3.10": version},
        "minimum": {"3.10": deepcopy(version)},
    }


def test_coverage_gate_accepts_exact_baseline_and_renders_summary(tmp_path):
    report = _report()
    report_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "baseline.json"
    _write_json(report_path, report)
    _write_json(baseline_path, _baseline(report))

    snapshot = check_coverage(report_path, baseline_path, "3.10")

    summary = render_markdown(snapshot, "3.10")
    assert "Python 3.10 coverage" in summary
    assert "| `production` | 95.0000% | 90.0000% |" in summary


@pytest.mark.parametrize(
    ("file_name", "field", "value", "expected"),
    [
        ("plsqlwks/example.py", "covered_lines", 94, "Production lines coverage dropped"),
        ("plsqlwks/example.py", "covered_branches", 17, "Production branches coverage dropped"),
    ],
)
def test_coverage_gate_rejects_regressions(tmp_path, file_name, field, value, expected):
    report = _report()
    baseline = _baseline(report)
    report["files"][file_name]["summary"][field] = value
    report_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "baseline.json"
    _write_json(report_path, report)
    _write_json(baseline_path, baseline)

    with pytest.raises(CoverageGateError, match=expected):
        check_coverage(report_path, baseline_path, "3.10")


def test_coverage_gate_applies_the_critical_floor_independently(tmp_path):
    report = _report()
    report["files"]["plsqlwks/exporting.py"]["summary"]["covered_branches"] = 17
    baseline = _baseline(report)
    report_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "baseline.json"
    _write_json(report_path, report)
    _write_json(baseline_path, baseline)

    with pytest.raises(CoverageGateError, match="branch coverage 85.0000% is below"):
        check_coverage(report_path, baseline_path, "3.10")


def test_coverage_gate_fails_closed_for_unknown_version_and_missing_critical_file(tmp_path):
    report = _report()
    baseline = _baseline(report)
    report_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "baseline.json"
    _write_json(report_path, report)
    _write_json(baseline_path, baseline)

    with pytest.raises(CoverageGateError, match="no minimum for Python 3.14"):
        check_coverage(report_path, baseline_path, "3.14")

    del report["files"]["tests/oracle_matrix.py"]
    _write_json(report_path, report)
    with pytest.raises(CoverageGateError, match="missing critical file tests/oracle_matrix.py"):
        check_coverage(report_path, baseline_path, "3.10")


def test_record_coverage_updates_only_the_requested_version_and_section(tmp_path):
    report = _report()
    report_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "baseline.json"
    _write_json(report_path, report)
    _write_json(
        baseline_path,
        {
            "schema": 1,
            "coverage_version": "7.15.1",
            "critical_floors": {"line_percent": 95.0, "branch_percent": 90.0},
            "initial": {},
            "minimum": {},
        },
    )

    record_coverage(report_path, baseline_path, "3.14", "minimum")

    recorded = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert recorded["initial"] == {}
    assert recorded["minimum"]["3.14"]["production"]["lines"] == {
        "covered": 380,
        "total": 400,
    }
