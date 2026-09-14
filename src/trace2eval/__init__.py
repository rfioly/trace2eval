"""trace2eval -- turn production LLM traces into a regression eval set.

The gap this fills: tracing tools record what happened, and eval frameworks
score cases you already wrote. Nothing connects the two. The step in between --
deciding which recorded calls deserve to become permanent test cases -- is still
done by hand, by scrolling through logs.

    from trace2eval import load_traces, select_cases

    loaded = load_traces("traces.jsonl")
    result = select_cases(loaded.traces, max_cases=25)
    for case in result.cases:
        print(case["id"], case["score"], case["input"])

Everything here is deterministic and offline. No model is called, no API key is
needed, and the same log always produces the same case set.
"""

from .annotations import (
    Annotation,
    AnnotationSet,
    load_annotations,
    merge_checks,
    render_template,
)
from .checks import CheckResult, detect_fallback, run_check, run_checks
from .matchers import char_bigram_jaccard, word_jaccard
from .runner import RunMetrics, compare_runs, load_outputs, run_cases
from .schema import Trace, load_traces, parse_trace
from .select import (
    DEFAULT_DEDUP_THRESHOLD,
    Matcher,
    MatcherSpecError,
    build_case,
    load_matcher,
    overlap_coefficient,
    select_cases,
    shingle_overlap,
)
from .signals import DEFAULT_WEIGHTS, Signal, build_context, compute_signals, score

__version__ = "0.3.0"

__all__ = [
    "__version__",
    "Annotation",
    "AnnotationSet",
    "CheckResult",
    "DEFAULT_DEDUP_THRESHOLD",
    "DEFAULT_WEIGHTS",
    "Matcher",
    "MatcherSpecError",
    "RunMetrics",
    "Signal",
    "Trace",
    "build_case",
    "build_context",
    "char_bigram_jaccard",
    "compare_runs",
    "compute_signals",
    "detect_fallback",
    "load_annotations",
    "load_matcher",
    "load_outputs",
    "load_traces",
    "merge_checks",
    "overlap_coefficient",
    "parse_trace",
    "render_template",
    "run_check",
    "run_checks",
    "run_cases",
    "score",
    "select_cases",
    "shingle_overlap",
    "word_jaccard",
]
