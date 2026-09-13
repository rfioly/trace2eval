"""Running a case set and comparing two runs.

``run`` turns a case set plus a batch of outputs into a metrics document.
``check`` compares two metrics documents and reports regressions.

The split matters: metrics are a plain JSON file that can be committed, diffed,
and read by any CI system. A gate that only exists inside a dashboard is not a
gate.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .checks import CheckResult, run_checks
from .signals import percentile


class OutputFormatError(ValueError):
    """Raised when an outputs file cannot be read at all."""


@dataclass
class CaseOutcome:
    case_id: str
    passed: bool
    results: list[CheckResult] = field(default_factory=list)
    output_chars: int = 0
    latency_ms: float | None = None
    cost_usd: float | None = None

    def failed_check_types(self) -> list[str]:
        return [result.type for result in self.results if not result.passed]


def load_outputs(path: str | Path) -> dict[str, dict[str, Any]]:
    """Read a JSONL file of ``{"id": ..., "output": ...}`` records."""
    output_path = Path(path)
    if not output_path.exists():
        raise OutputFormatError(f"outputs file not found: {output_path}")

    records: dict[str, dict[str, Any]] = {}
    with output_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            record_id = data.get("id") or data.get("trace_id")
            if record_id is None:
                record_id = f"trace-{line_no:05d}"
            records[str(record_id)] = data
    return records


@dataclass
class RunMetrics:
    total: int = 0
    passed: int = 0
    failed: int = 0
    missing: int = 0
    pass_rate: float = 0.0
    fallback_count: int = 0
    fallback_rate: float = 0.0
    json_cases: int = 0
    json_passed: int = 0
    format_compliance_rate: float | None = None
    avg_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    avg_cost_usd: float | None = None
    total_cost_usd: float | None = None
    avg_output_chars: float | None = None
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunMetrics:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def run_cases(
    cases: Iterable[dict[str, Any]],
    outputs: dict[str, dict[str, Any]],
) -> RunMetrics:
    """Score every case against the matching output."""
    case_list = list(cases)
    metrics = RunMetrics(total=len(case_list))
    latency_values: list[float] = []
    cost_values: list[float] = []
    char_values: list[int] = []

    for case in case_list:
        case_id = str(case.get("id", ""))
        source_id = str(case.get("source_trace_id", ""))
        record = outputs.get(case_id) or outputs.get(source_id)

        if record is None:
            metrics.failed += 1
            metrics.missing += 1
            metrics.failures.append(
                {
                    "case_id": case_id,
                    "reason": "missing_output",
                    "failed_checks": [],
                }
            )
            continue

        output = record.get("output") or record.get("response") or ""
        output = str(output)
        checks = case.get("checks", [])
        results = run_checks(checks, output)
        passed = all(result.passed for result in results)

        outcome = CaseOutcome(case_id=case_id, passed=passed, results=results)
        outcome.output_chars = len(output.strip())

        latency = record.get("latency_ms")
        if isinstance(latency, (int, float)):
            outcome.latency_ms = float(latency)
            latency_values.append(float(latency))
        cost = record.get("cost_usd")
        if isinstance(cost, (int, float)):
            outcome.cost_usd = float(cost)
            cost_values.append(float(cost))
        char_values.append(outcome.output_chars)

        if passed:
            metrics.passed += 1
        else:
            metrics.failed += 1
            metrics.failures.append(
                {
                    "case_id": case_id,
                    "reason": "check_failed",
                    "failed_checks": outcome.failed_check_types(),
                }
            )

        if any(check.get("type") == "not_fallback" for check in checks):
            if not passed and "not_fallback" in outcome.failed_check_types():
                metrics.fallback_count += 1

        if any(check.get("type") == "json" for check in checks):
            metrics.json_cases += 1
            if passed:
                metrics.json_passed += 1

    denominator = metrics.total or 1
    metrics.pass_rate = _round(metrics.passed / denominator, 4) or 0.0
    metrics.fallback_rate = _round(metrics.fallback_count / denominator, 4) or 0.0

    if metrics.json_cases:
        metrics.format_compliance_rate = _round(
            metrics.json_passed / metrics.json_cases, 4
        )

    if latency_values:
        metrics.avg_latency_ms = _round(sum(latency_values) / len(latency_values), 2)
        metrics.p95_latency_ms = _round(percentile(latency_values, 0.95), 2)
    if cost_values:
        metrics.avg_cost_usd = _round(sum(cost_values) / len(cost_values), 8)
        metrics.total_cost_usd = _round(sum(cost_values), 6)
    if char_values:
        metrics.avg_output_chars = _round(sum(char_values) / len(char_values), 1)

    return metrics


@dataclass
class Violation:
    metric: str
    baseline: float | None
    current: float | None
    rule: str


#: (metric, direction, tolerance). "higher_is_better" fails when it drops,
#: "lower_is_better" fails when it rises.
REGRESSION_RULES: tuple[tuple[str, str, float], ...] = (
    ("pass_rate", "higher_is_better", 0.02),
    ("format_compliance_rate", "higher_is_better", 0.02),
    ("fallback_rate", "lower_is_better", 0.02),
    ("p95_latency_ms", "lower_is_better", 0.20),
    ("avg_cost_usd", "lower_is_better", 0.20),
)

#: Some metrics tolerate movement, so the baseline is nudged by this fraction
#: before comparing. Pass rate is judged on absolute points instead.
_RELATIVE_METRICS = {"p95_latency_ms", "avg_cost_usd"}


def compare_runs(
    baseline: RunMetrics,
    current: RunMetrics,
    tolerance_scale: float = 1.0,
) -> list[Violation]:
    """Return every metric that regressed beyond its tolerance."""
    violations: list[Violation] = []
    baseline_data = baseline.to_dict()
    current_data = current.to_dict()

    for metric, direction, tolerance in REGRESSION_RULES:
        base = baseline_data.get(metric)
        now = current_data.get(metric)
        if base is None or now is None:
            continue

        tolerance = tolerance * tolerance_scale
        if metric in _RELATIVE_METRICS:
            allowed = base * tolerance
        else:
            allowed = tolerance

        if direction == "higher_is_better":
            if now < base - allowed:
                violations.append(
                    Violation(
                        metric,
                        base,
                        now,
                        f"dropped by {base - now:.4f} (allowed {allowed:.4f})",
                    )
                )
        else:
            if now > base + allowed:
                violations.append(
                    Violation(
                        metric,
                        base,
                        now,
                        f"rose by {now - base:.4f} (allowed {allowed:.4f})",
                    )
                )

    return violations
