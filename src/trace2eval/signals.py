"""Signals: deciding which traces are worth turning into test cases.

A production log is mostly boring. If you dump ten thousand calls into a test
suite you get a suite that is slow, redundant, and ignored. What you want is the
handful of calls that carry information -- the ones where something already went
wrong, or where the shape of the answer matters.

Each signal below is a cheap, deterministic observation about a single row plus
a little global context (percentiles across the whole log). Signals carry
weights, and weights add up into a score. The score decides what gets promoted.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

from .checks import detect_fallback
from .schema import Trace

#: Default weights. Tuned so that a single strong signal outranks several weak
#: ones: an explicit thumbs-down should always beat "this call was a bit slow".
DEFAULT_WEIGHTS: dict[str, float] = {
    "negative_feedback": 3.0,
    "user_retried": 2.5,
    "empty_output": 3.0,
    "expected_shape_violated": 3.0,
    "fallback_phrase": 2.0,
    "explicit_expectation": 2.0,
    "output_much_shorter": 1.5,
    "output_much_longer": 1.0,
    "slow_response": 1.0,
    "expensive_call": 1.0,
}

#: An output shorter than this is treated as suspicious on its own.
_MIN_PLAUSIBLE_CHARS = 12

#: A response is "much shorter/longer" past these ratios against the log median.
_SHORT_RATIO = 0.5
_LONG_RATIO = 3.0


@dataclass
class Signal:
    name: str
    weight: float
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "weight": self.weight, "detail": self.detail}


@dataclass
class TraceContext:
    """Log-wide statistics that individual signals are judged against."""

    p95_latency_ms: float | None = None
    p95_cost_usd: float | None = None
    median_output_chars: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "p95_latency_ms": self.p95_latency_ms,
            "p95_cost_usd": self.p95_cost_usd,
            "median_output_chars": round(self.median_output_chars, 1),
        }


def percentile(values: list[float], fraction: float) -> float | None:
    """Linear-interpolation percentile. Avoids depending on numpy for one number."""
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    position = fraction * (len(clean) - 1)
    lower = int(position)
    upper = min(lower + 1, len(clean) - 1)
    weight = position - lower
    return clean[lower] * (1 - weight) + clean[upper] * weight


def build_context(traces: Iterable[Trace]) -> TraceContext:
    trace_list = list(traces)
    latencies = [t.latency_ms for t in trace_list if t.latency_ms is not None]
    costs = [t.cost_usd for t in trace_list if t.cost_usd is not None]
    char_counts = [t.output_chars for t in trace_list if t.output_chars > 0]
    return TraceContext(
        p95_latency_ms=percentile(latencies, 0.95),
        p95_cost_usd=percentile(costs, 0.95),
        median_output_chars=statistics.median(char_counts) if char_counts else 0.0,
    )


def _looks_like_json(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return False
    return True


def compute_signals(
    trace: Trace,
    context: TraceContext,
    weights: dict[str, float] | None = None,
) -> list[Signal]:
    """Collect every signal that fires for this trace."""
    active = {**DEFAULT_WEIGHTS, **(weights or {})}
    signals: list[Signal] = []

    def add(name: str, detail: str) -> None:
        weight = active.get(name, 0.0)
        if weight > 0:
            signals.append(Signal(name=name, weight=weight, detail=detail))

    if trace.is_negative:
        add("negative_feedback", "user gave negative feedback")

    if trace.retried:
        add("user_retried", "the same request was retried")

    chars = trace.output_chars
    if chars == 0:
        add("empty_output", "model returned an empty response")
    else:
        fallback = detect_fallback(trace.output)
        if fallback:
            add("fallback_phrase", f"output contains {fallback!r}")

        median = context.median_output_chars
        if median > 0 and chars < median * _SHORT_RATIO and chars < _MIN_PLAUSIBLE_CHARS * 2:
            add(
                "output_much_shorter",
                f"{chars} chars vs log median {median:.0f}",
            )
        elif median > 0 and chars > median * _LONG_RATIO:
            add(
                "output_much_longer",
                f"{chars} chars vs log median {median:.0f}",
            )

    if trace.expect:
        add("explicit_expectation", "trace already carried an expectation block")
        if trace.expect.get("json") and not _looks_like_json(trace.output):
            add("expected_shape_violated", "expected JSON output, got something else")

    if (
        context.p95_latency_ms is not None
        and trace.latency_ms is not None
        and trace.latency_ms > context.p95_latency_ms
    ):
        add(
            "slow_response",
            f"{trace.latency_ms:.0f}ms above p95 {context.p95_latency_ms:.0f}ms",
        )

    if (
        context.p95_cost_usd is not None
        and trace.cost_usd is not None
        and trace.cost_usd > context.p95_cost_usd
    ):
        add(
            "expensive_call",
            f"${trace.cost_usd:.6f} above p95 ${context.p95_cost_usd:.6f}",
        )

    return signals


def score(signals: list[Signal]) -> float:
    return round(sum(signal.weight for signal in signals), 3)
