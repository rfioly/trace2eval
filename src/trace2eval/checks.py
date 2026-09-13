"""Deterministic checks.

Every check in this module is a pure function of the output string. No model is
called, nothing is paid for, and the same input always produces the same verdict.

That constraint is a feature, not a shortcut: a regression gate that depends on
a paid judge model is a gate that people disable the moment it becomes noisy or
expensive. The checks here are the boring half of evaluation, and the boring
half is the half that actually runs on every pull request.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

#: Phrases that mean the assistant gave up rather than answered. These are
#: deliberately broad and match both Chinese and English refusals.
FALLBACK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"作为一个\s*(AI|人工智能|语言模型)",
        r"作为\s*(AI|人工智能|语言模型)",
        r"我(无法|不能|没有办法)(回答|提供|满足|帮助|处理)",
        r"抱歉[，,]?\s*我(无法|不能|不会)",
        r"对不起[，,]?\s*我(无法|不能|不会)",
        r"暂(时)?无法(回答|提供|处理)",
        r"没有(相关|足够)的?(信息|资料)来(回答|说明)",
        r"as an ai\b",
        r"i (can'?t|cannot|am unable to) (help|assist|answer|provide)",
        r"i'?m sorry,? but i (can'?t|cannot)",
        r"i don'?t have (enough )?(information|access)",
    )
)

SUPPORTED_CHECK_TYPES = (
    "not_fallback",
    "min_chars",
    "max_chars",
    "contains",
    "not_contains",
    "regex",
    "json",
)


@dataclass
class CheckResult:
    """Outcome of a single check against a single output."""

    type: str
    passed: bool
    detail: str = ""
    spec: dict[str, Any] | None = None


def detect_fallback(output: str) -> str | None:
    """Return the first fallback phrase found, or ``None`` if the output is clean."""
    for pattern in FALLBACK_PATTERNS:
        match = pattern.search(output)
        if match:
            return match.group(0)
    return None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def run_check(check: dict[str, Any], output: str) -> CheckResult:
    """Run one check spec against one output.

    Unknown check types fail loudly rather than silently passing -- a test that
    quietly does nothing is worse than no test at all.
    """
    check_type = str(check.get("type", "")).strip()
    spec = dict(check)

    if check_type == "not_fallback":
        hit = detect_fallback(output)
        if hit:
            return CheckResult(check_type, False, f"matched fallback phrase {hit!r}", spec)
        return CheckResult(check_type, True, "", spec)

    if check_type == "min_chars":
        limit = int(check.get("value", 0))
        actual = len(output.strip())
        if actual < limit:
            return CheckResult(check_type, False, f"{actual} chars < {limit}", spec)
        return CheckResult(check_type, True, f"{actual} chars", spec)

    if check_type == "max_chars":
        limit = int(check.get("value", 0))
        actual = len(output.strip())
        if actual > limit:
            return CheckResult(check_type, False, f"{actual} chars > {limit}", spec)
        return CheckResult(check_type, True, f"{actual} chars", spec)

    if check_type == "contains":
        needles = _as_list(check.get("value"))
        missing = [needle for needle in needles if needle not in output]
        if missing:
            return CheckResult(check_type, False, f"missing {missing}", spec)
        return CheckResult(check_type, True, "", spec)

    if check_type == "not_contains":
        needles = _as_list(check.get("value"))
        present = [needle for needle in needles if needle in output]
        if present:
            return CheckResult(check_type, False, f"found forbidden {present}", spec)
        return CheckResult(check_type, True, "", spec)

    if check_type == "regex":
        pattern = str(check.get("value", ""))
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return CheckResult(check_type, False, f"invalid pattern: {exc}", spec)
        if not compiled.search(output):
            return CheckResult(check_type, False, f"no match for {pattern!r}", spec)
        return CheckResult(check_type, True, "", spec)

    if check_type == "json":
        required = _as_list(check.get("required"))
        try:
            payload = json.loads(output)
        except (json.JSONDecodeError, TypeError) as exc:
            return CheckResult(check_type, False, f"invalid JSON: {exc}", spec)
        if required:
            if not isinstance(payload, dict):
                return CheckResult(check_type, False, "expected a JSON object", spec)
            missing = [key for key in required if key not in payload]
            if missing:
                return CheckResult(check_type, False, f"missing keys {missing}", spec)
        return CheckResult(check_type, True, "", spec)

    return CheckResult(check_type, False, f"unsupported check type {check_type!r}", spec)


def run_checks(checks: list[dict[str, Any]], output: str) -> list[CheckResult]:
    return [run_check(check, output) for check in checks]
