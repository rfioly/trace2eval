"""Markdown rendering for the generated case set and for regression output."""

from __future__ import annotations

from typing import Any

from .runner import RunMetrics, Violation
from .select import SelectionResult


def _cell(value: Any, limit: int = 60) -> str:
    """Make a value safe to drop into a Markdown table cell."""
    text = str(value).replace("|", "\\|").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or "—"


def _number(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        if value == 0:
            return "0"
        if abs(value) < 0.001:
            return f"{value:.8f}".rstrip("0")
        return f"{value:.{digits}f}"
    return str(value)


def build_report(result: SelectionResult, source_name: str) -> str:
    """Human-readable account of what was selected and, more importantly, why."""
    stats = result.stats()
    context = result.context
    lines: list[str] = []

    lines.append("# trace2eval build report")
    lines.append("")
    lines.append(f"Source log: `{source_name}`")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Traces read | {stats['total_traces']} |")
    lines.append(f"| Distinct questions | {stats['distinct_questions']} |")
    lines.append(f"| Questions with no signal | {stats['questions_without_signals']} |")
    lines.append(f"| Cases generated | {stats['cases']} |")
    lines.append(f"| Log lines collapsed into a case | {stats['dropped_as_duplicate']} |")
    lines.append(f"| Questions below the minimum score | {stats['dropped_below_min_score']} |")
    lines.append(f"| Questions beyond the case limit | {stats['dropped_beyond_limit']} |")
    lines.append(
        f"| Cases whose checks cannot catch their own failure "
        f"| {stats['cases_with_weak_checks']} |"
    )
    lines.append(
        f"| Trusted references failing their own checks "
        f"| {stats['cases_whose_reference_fails']} |"
    )
    lines.append(
        f"| Cases using a human-written expectation "
        f"| {stats['cases_with_annotation']} |"
    )
    lines.append(
        f"| Cases still needing one | {stats['cases_needing_annotation']} |"
    )
    lines.append("")

    lines.append("## Log-wide baselines")
    lines.append("")
    lines.append("| Statistic | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| p95 latency | {_number(context.p95_latency_ms, 2)} ms |")
    lines.append(f"| p95 cost | ${_number(context.p95_cost_usd, 8)} |")
    lines.append(f"| Median output length | {_number(context.median_output_chars, 1)} chars |")
    lines.append("")

    failures = sum(1 for case in result.cases if not case["reference_is_trusted"])
    lines.append("## Case mix")
    lines.append("")
    lines.append(
        f"- **{failures}** regression seeds (the reference output is the output that "
        f"went wrong — the case exists so it never ships twice)"
    )
    lines.append(
        f"- **{len(result.cases) - failures}** quality anchors (clean trace, so an "
        f"expected shape was inferred from it)"
    )
    lines.append("")

    lines.append("## Cases")
    lines.append("")
    lines.append("| Case | Score | In log | Trusted | Shape from | Self-check | Signals | Input |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for case in result.cases:
        signal_names = ", ".join(signal["name"] for signal in case["signals"])
        lines.append(
            "| {id} | {score} | {occurs} | {trusted} | {shape} | {check} | {signals} | {input} |".format(
                id=_cell(case["id"], 12),
                score=_cell(case["score"], 8),
                occurs=_cell(case["occurrences_in_log"], 8),
                trusted="yes" if case["reference_is_trusted"] else "no",
                shape=_cell(case["shape_source"], 30),
                check=_cell(case["self_check"]["verdict"], 30),
                signals=_cell(signal_names, 42),
                input=_cell(case["input"], 40),
            )
        )
    lines.append("")

    flagged = result.cases_whose_reference_fails + result.cases_needing_annotation
    if flagged:
        lines.append("## Cases that need a human eye")
        lines.append("")
        lines.append(
            "Generated cases are a proposal. These are the ones where the proposal "
            "is weakest, in order of how much they should worry you."
        )
        lines.append("")
        for case in result.cases_whose_reference_fails:
            lines.append(
                f"- **{case['id']}** — a trusted reference does not satisfy its own "
                f"checks (`{', '.join(case['self_check']['failed_checks'])}`). Either "
                f"the reference is wrong or the checks are."
            )
        for case in result.cases_needing_annotation:
            if case["failure_kind"] == "behaviour":
                lines.append(
                    f"- {case['id']} — the failure here was behavioural "
                    f"(`{', '.join(signal['name'] for signal in case['signals'])}`): the "
                    f"call itself produced a perfectly acceptable answer, and what went "
                    f"wrong happened around it. No check on the output text can "
                    f"reproduce that."
                )
            else:
                lines.append(
                    f"- {case['id']} — the failure was visible in the output "
                    f"(`{', '.join(signal['name'] for signal in case['signals'])}`), yet "
                    f"the generated checks pass on it. That is a gap in the checks, not "
                    f"a limit of the approach -- worth investigating."
                )
        lines.append("")
        lines.append(
            "The way out for the behavioural ones is an expectation file: one JSON "
            "object per line, keyed on `input`, holding the checks a correct answer "
            "should satisfy. `build` writes a fill-in template next to the case set "
            "listing exactly these cases; save it as `expectations.jsonl` and rebuild. "
            "It is keyed on the question rather than the case id, so it survives "
            "regeneration."
        )
        lines.append("")

    if result.cases_with_annotation:
        lines.append("## Cases carrying a human-written expectation")
        lines.append("")
        lines.append(
            "The expectation below does not make these cases reproduce their original "
            "failure -- nothing can, the failure was never in the answer. What it does "
            "is give them something worth asserting."
        )
        lines.append("")
        for case in result.cases_with_annotation:
            lines.append(
                f"- `{case['id']}` ({case['input']}) — {case['annotation']['note'] or '(no note)'}"
            )
        lines.append("")

    if result.cases:
        lines.append("## Why the top case was chosen")
        lines.append("")
        top = result.cases[0]
        lines.append(f"`{top['id']}` scored **{top['score']}**.")
        lines.append("")
        for signal in top["signals"]:
            lines.append(f"- `{signal['name']}` (+{signal['weight']}) — {signal['detail']}")
        lines.append("")
        lines.append(f"The expected shape came from {top['shape_source']}.")
        lines.append("")
        check = top["self_check"]
        if top["reference_is_trusted"]:
            lines.append(
                "Self-check: this is a trusted reference, and it "
                + ("satisfies its own checks." if check["passed"] else "does NOT satisfy them.")
            )
        else:
            if not check["passed"]:
                detail = (
                    f"fail on it — they do, on `{', '.join(check['failed_checks'])}`. "
                    "The case reproduces the failure it came from."
                )
            else:
                detail = (
                    "fail on it — they do not. The case cannot detect the failure it "
                    "came from, which is why it appears in the list above."
                )
            lines.append(
                "Self-check: this is a failure seed, so the checks are *supposed* to "
                + detail
            )
        lines.append("")
        for note in top["notes"]:
            lines.append(f"> {note}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "Generated by [trace2eval](https://github.com/rfioly/trace2eval). "
        "Review this file before committing the case set — the selection is a "
        "proposal, not a verdict."
    )
    lines.append("")
    return "\n".join(lines)


def render_metrics(metrics: RunMetrics) -> str:
    lines: list[str] = []
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Cases | {metrics.total} |")
    lines.append(f"| Passed | {metrics.passed} |")
    lines.append(f"| Failed | {metrics.failed} |")
    if metrics.missing:
        lines.append(f"| Missing outputs | {metrics.missing} |")
    lines.append(f"| Pass rate | {metrics.pass_rate:.2%} |")
    lines.append(f"| Fallback rate | {metrics.fallback_rate:.2%} |")
    if metrics.format_compliance_rate is not None:
        lines.append(f"| Format compliance | {metrics.format_compliance_rate:.2%} |")
    lines.append(f"| Avg latency | {_number(metrics.avg_latency_ms, 2)} ms |")
    lines.append(f"| p95 latency | {_number(metrics.p95_latency_ms, 2)} ms |")
    lines.append(f"| Avg cost | ${_number(metrics.avg_cost_usd, 8)} |")
    return "\n".join(lines)


def render_violations(violations: list[Violation]) -> str:
    if not violations:
        return "No regressions detected."
    lines: list[str] = []
    lines.append("| Metric | Baseline | Current | Rule |")
    lines.append("| --- | --- | --- | --- |")
    for violation in violations:
        lines.append(
            "| {metric} | {baseline} | {current} | {rule} |".format(
                metric=_cell(violation.metric, 24),
                baseline=_cell(_number(violation.baseline), 16),
                current=_cell(_number(violation.current), 16),
                rule=_cell(violation.rule, 40),
            )
        )
    return "\n".join(lines)
