"""Trace loading and normalisation.

The reader is deliberately forgiving: real production logs are messy and field
names differ between frameworks. We accept a handful of common aliases for the
input/output pair, and we skip malformed lines instead of aborting the whole
run -- a single bad line in a 200k-line log must not cost you the batch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INPUT_ALIASES = ("input", "prompt", "question", "query", "user_message")
OUTPUT_ALIASES = ("output", "response", "completion", "answer", "assistant_message")
ID_ALIASES = ("id", "trace_id", "traceId", "request_id", "requestId")

_NEGATIVE_FEEDBACK = {
    "negative",
    "neg",
    "bad",
    "down",
    "thumbs_down",
    "thumbsdown",
    "差评",
    "不满意",
    "无帮助",
}
_POSITIVE_FEEDBACK = {
    "positive",
    "pos",
    "good",
    "up",
    "thumbs_up",
    "thumbsup",
    "好评",
    "满意",
    "有帮助",
}


class TraceFormatError(ValueError):
    """Raised when the input file cannot be read at all."""


@dataclass
class Trace:
    """One recorded call.

    Only ``input`` and ``output`` are required. Everything else is optional and
    simply widens the set of signals we can compute for this row.
    """

    id: str
    input: str
    output: str
    latency_ms: float | None = None
    cost_usd: float | None = None
    feedback: str | None = None
    retried: bool = False
    expect: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def output_chars(self) -> int:
        return len(self.output.strip())

    @property
    def is_negative(self) -> bool:
        return self.feedback == "negative"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "input": self.input,
            "output": self.output,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "feedback": self.feedback,
            "retried": self.retried,
            "expect": self.expect,
        }


@dataclass
class LoadResult:
    traces: list[Trace] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


def _pick(data: dict[str, Any], keys: tuple[str, ...], allow_empty: bool = False) -> Any:
    """Return the first alias that is present and usable.

    ``allow_empty`` exists because an empty string means different things for the
    two sides of a call. An empty *input* is an unusable row -- there was nothing
    to answer. An empty *output* is a perfectly readable row and one of the most
    valuable ones in the log: it means the model returned nothing at all.
    """
    for key in keys:
        if key in data:
            value = data[key]
            if value is None:
                continue
            if isinstance(value, str) and not value.strip() and not allow_empty:
                continue
            return value
    return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def normalise_feedback(value: Any) -> str | None:
    """Collapse the many spellings of a thumbs-up/down into one of two values."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "positive" if value else "negative"
    text = str(value).strip().lower()
    if not text or text in {"none", "null", "unknown", "-"}:
        return None
    if text in _NEGATIVE_FEEDBACK:
        return "negative"
    if text in _POSITIVE_FEEDBACK:
        return "positive"
    # Numeric ratings: 1-2 is a thumbs-down, 4-5 is a thumbs-up.
    try:
        score = float(text)
    except ValueError:
        return None
    if score <= 2:
        return "negative"
    if score >= 4:
        return "positive"
    return None


def parse_trace(data: dict[str, Any], fallback_id: str) -> Trace | None:
    """Build a :class:`Trace` from a raw log record, or ``None`` if unusable."""
    raw_input = _pick(data, INPUT_ALIASES)
    raw_output = _pick(data, OUTPUT_ALIASES, allow_empty=True)
    if raw_input is None or raw_output is None:
        return None

    trace_id = _pick(data, ID_ALIASES) or fallback_id
    retried = _as_bool(_pick(data, ("retried", "is_retry", "was_retried")))
    retry_count = _as_float(_pick(data, ("retry_count", "retries")))
    if retry_count:
        retried = True

    latency = _as_float(
        _pick(data, ("latency_ms", "latency", "duration_ms", "elapsed_ms"))
    )
    if latency is None:
        maybe_seconds = _as_float(_pick(data, ("latency_s", "duration_s")))
        if maybe_seconds is not None:
            latency = maybe_seconds * 1000.0

    cost = _as_float(_pick(data, ("cost_usd", "cost", "total_cost", "price_usd")))

    expect = data.get("expect") or data.get("assert") or data.get("checks")
    if not isinstance(expect, dict):
        expect = None

    return Trace(
        id=str(trace_id),
        input=str(raw_input),
        output=str(raw_output),
        latency_ms=latency,
        cost_usd=cost,
        feedback=normalise_feedback(
            _pick(data, ("feedback", "rating", "score", "user_feedback", "thumbs"))
        ),
        retried=retried,
        expect=expect,
        raw=data,
    )


def load_traces(path: str | Path) -> LoadResult:
    """Read a JSONL trace log.

    Blank lines are ignored. A line that is not valid JSON, or that lacks an
    input/output pair, is recorded in ``skipped`` and everything else still
    loads.
    """
    log_path = Path(path)
    if not log_path.exists():
        raise TraceFormatError(f"trace file not found: {log_path}")

    result = LoadResult()
    with log_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as exc:
                result.skipped.append((line_no, f"invalid JSON: {exc.msg}"))
                continue
            if not isinstance(data, dict):
                result.skipped.append((line_no, "not a JSON object"))
                continue
            trace = parse_trace(data, fallback_id=f"trace-{line_no:05d}")
            if trace is None:
                result.skipped.append((line_no, "missing input/output field"))
                continue
            result.traces.append(trace)

    return result
