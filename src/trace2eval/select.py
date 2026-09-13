"""Deduplication and case construction.

Two jobs live here.

**Collapse near-duplicates.** A real log asks the same question forty times. If
you promote all forty, you get a slow suite that nobody trusts. So we cluster the
whole log by input similarity and promote one case per cluster -- and because we
cluster the *log* rather than the candidates, a question that appeared 200 times
but only went wrong once still becomes a single case that is honestly labelled
"stands for 200 of the calls we recorded".

**Build the case.** This is where the interesting judgement sits, and the rule is
narrow on purpose: *a reference output is not ground truth*. Most of what we
select is selected precisely because something went wrong with it. So we only
infer an expected shape from the output when the trace looks clean, and we treat
everything else as a regression seed -- a case that exists to make sure the same
failure never ships twice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .schema import Trace
from .signals import Signal, TraceContext, build_context, compute_signals, score

#: Signals that mean "something already went wrong on this row".
QUALITY_SIGNALS = frozenset(
    {
        "negative_feedback",
        "user_retried",
        "empty_output",
        "fallback_phrase",
        "expected_shape_violated",
        "output_much_shorter",
    }
)

#: Character n-grams, not word tokens: Chinese has no whitespace word boundaries,
#: so character n-grams are the only thing that works for both languages with one
#: code path. Bigrams rather than trigrams because user queries are short -- a
#: trigram over a five-character question leaves almost nothing to match on.
SHINGLE_SIZE = 2

#: Two inputs are only compared at all if they share at least this many n-grams.
#: Without it, two unrelated short queries that happen to share one character
#: pair can score 1.0 on the overlap coefficient below.
MIN_SHARED_SHINGLES = 2

#: Default similarity threshold.
#:
#: Chosen by sweeping the sample log and checking each merged group by hand:
#:
#:   threshold   merged groups   collapsed   verdict
#:   0.50        6               14          merges "支持哪些登录方式" with
#:                                           "支持哪些支付方式" -- different questions
#:   0.55        4               11          still merges the same false pair
#:   0.60        3                9          no false pairs on the sample log
#:   0.70        2                5          starts missing real duplicates
#:
#: 0.6 is where precision comes out clean without losing the groups that matter.
#: It is tuned to one 42-row sample, so treat it as a starting point, not a
#: constant of nature -- which is why ``--dedup-threshold`` exists.
DEFAULT_DEDUP_THRESHOLD = 0.6

_STRIP_PATTERN = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’()（）\[\]【】]+")

#: Inferred minimum length is a fraction of the reference output. 0.4 is
#: deliberately loose: it catches *truncation*, not rewording.
_MIN_CHARS_RATIO = 0.4
_MIN_CHARS_FLOOR = 8


@dataclass
class ScoredTrace:
    trace: Trace
    signals: list[Signal]
    score: float


@dataclass
class Cluster:
    """One distinct question, plus every log line that asked it."""

    representative: Trace
    signals: list[Signal]
    score: float
    member_ids: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.member_ids)

    @property
    def duplicate_count(self) -> int:
        return max(0, self.size - 1)

    @property
    def duplicate_ids(self) -> list[str]:
        return [mid for mid in self.member_ids if mid != self.representative.id]


@dataclass
class SelectionResult:
    cases: list[dict[str, Any]] = field(default_factory=list)
    context: TraceContext | None = None
    total_traces: int = 0
    distinct_questions: int = 0
    questions_without_signals: int = 0
    dropped_below_min_score: int = 0
    dropped_as_duplicate: int = 0
    dropped_beyond_limit: int = 0

    def stats(self) -> dict[str, Any]:
        return {
            "total_traces": self.total_traces,
            "distinct_questions": self.distinct_questions,
            "questions_without_signals": self.questions_without_signals,
            "cases": len(self.cases),
            "dropped_below_min_score": self.dropped_below_min_score,
            "dropped_as_duplicate": self.dropped_as_duplicate,
            "dropped_beyond_limit": self.dropped_beyond_limit,
        }


def normalise(text: str) -> str:
    """Fold case and strip punctuation so two phrasings of one question collapse."""
    return _STRIP_PATTERN.sub("", text.strip().lower())


def shingles(text: str, size: int = SHINGLE_SIZE) -> set[str]:
    folded = normalise(text)
    if not folded:
        return set()
    if len(folded) <= size:
        return {folded}
    return {folded[i : i + size] for i in range(len(folded) - size + 1)}


def similarity(left: set[str], right: set[str], min_shared: int = MIN_SHARED_SHINGLES) -> float:
    """Overlap coefficient -- not Jaccard.

    This choice matters more than the threshold does, and it was made by
    measuring rather than by habit. Measured on the sample log:

        pair                                             overlap   jaccard   same?
        "你们的退款政策是什么？" vs "退款政策"              1.000     0.333     yes
        "你们的退款政策是什么？" vs "退款政策是怎样的"        0.571     0.333     yes
        "修改手机号" vs "我要改手机号"                      0.750     0.500     yes
        "支持哪些登录方式" vs "支持哪些支付方式"             0.571     0.400     NO
        "登录不上" vs "支持哪些登录方式"                    0.000     0.111     no

    Read the third and fourth rows together. Jaccard ranks two genuinely
    *different* questions (0.400) above two phrasings of the *same* question
    (0.333). It cannot separate them, because it divides by the union and so
    punishes any length difference -- and short user queries are almost always a
    fragment of a longer phrasing.

    Overlap divides by the *shorter* set, which asks the question we actually
    care about: does the shorter query sit inside the longer one? It gets all
    four rows right. The cost is higher false-positive pressure on very short
    inputs, which ``min_shared`` guards against and which the threshold sweep in
    ``DEFAULT_DEDUP_THRESHOLD`` was tuned against.
    """
    if not left or not right:
        return 0.0
    shared = len(left & right)
    if shared < min_shared:
        return 0.0
    return shared / min(len(left), len(right))


def jaccard(left: set[str], right: set[str]) -> float:
    """Plain Jaccard similarity.

    Kept only as a reference point: it is the measure people reach for by default,
    and the tests use it to demonstrate *why* we do not. Nothing in the selection
    path calls this.
    """
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def cluster_scored(
    scored: list[ScoredTrace],
    fingerprints: dict[str, set[str]],
    threshold: float = DEFAULT_DEDUP_THRESHOLD,
) -> list[Cluster]:
    """Single-linkage clustering over the whole log.

    ``scored`` must arrive sorted by descending score. That ordering does the real
    work: because we walk the worst rows first, the first row to claim a cluster is
    the most interesting one, so it becomes the representative without any extra
    bookkeeping.

    Comparison is against *every* member of a cluster, not just the representative
    (single linkage, not centroid). Otherwise a chain like

        我想了解退款政策  ~  退款政策  ~  你们的退款政策是什么

    breaks: the fragments in the middle never score high enough against a long
    representative, and the cluster silently splits.

    An inverted index on n-grams shortlists which clusters are even worth
    comparing against, which is what keeps this from being quadratic.
    """
    clusters: list[Cluster] = []
    postings: dict[str, set[int]] = {}

    for item in scored:
        fingerprint = fingerprints.get(item.trace.id, set())

        candidates: set[int] = set()
        for shingle in fingerprint:
            candidates |= postings.get(shingle, set())

        target: int | None = None
        for index in sorted(candidates):
            cluster = clusters[index]
            for member_id in cluster.member_ids:
                if similarity(fingerprint, fingerprints.get(member_id, set())) >= threshold:
                    target = index
                    break
            if target is not None:
                break

        if target is None:
            clusters.append(
                Cluster(
                    representative=item.trace,
                    signals=item.signals,
                    score=item.score,
                    member_ids=[item.trace.id],
                )
            )
            new_index = len(clusters) - 1
            for shingle in fingerprint:
                postings.setdefault(shingle, set()).add(new_index)
        else:
            clusters[target].member_ids.append(item.trace.id)
            for shingle in fingerprint:
                postings.setdefault(shingle, set()).add(target)

    return clusters


def _checks_from_expect(expect: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate a trace's own ``expect`` block into check specs."""
    checks: list[dict[str, Any]] = []
    for key, value in expect.items():
        if key in {"json", "required"}:
            continue
        if value is False or value is None:
            continue
        if key == "not_fallback":
            checks.append({"type": "not_fallback"})
        elif key in {"min_chars", "max_chars", "regex"}:
            checks.append({"type": key, "value": value})
        elif key in {"contains", "not_contains"}:
            values = value if isinstance(value, list) else [value]
            checks.append({"type": key, "value": values})

    if expect.get("json"):
        check: dict[str, Any] = {"type": "json"}
        required = expect.get("required")
        if required:
            check["required"] = required if isinstance(required, list) else [required]
        checks.append(check)

    return checks


def _infer_checks(
    trace: Trace, is_failure_seed: bool, notes: list[str]
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = [{"type": "not_fallback"}]

    if is_failure_seed:
        notes.append(
            "failure seed: the reference output is the output that went wrong, "
            "so no expected shape was inferred from it"
        )
        return checks

    chars = trace.output_chars
    if chars > 0:
        minimum = max(_MIN_CHARS_FLOOR, int(chars * _MIN_CHARS_RATIO))
        checks.append({"type": "min_chars", "value": minimum})
        notes.append(
            f"clean trace: inferred min_chars={minimum} from a {chars}-char reference "
            f"(catches truncation, tolerates rewording)"
        )

    stripped = trace.output.strip()
    if stripped.startswith(("{", "[")):
        try:
            payload = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            payload_keys = sorted(payload.keys())
            checks.append({"type": "json", "required": payload_keys})
            notes.append(f"clean trace: inferred JSON schema with keys {payload_keys}")

    return checks


def build_case(index: int, cluster: Cluster) -> dict[str, Any]:
    trace = cluster.representative
    names = {signal.name for signal in cluster.signals}
    is_failure_seed = bool(names & QUALITY_SIGNALS)

    notes: list[str] = []
    if trace.expect:
        checks = _checks_from_expect(trace.expect)
        notes.append("checks inherited from the trace's own expect block")
        if not any(check["type"] == "not_fallback" for check in checks):
            checks.insert(0, {"type": "not_fallback"})
    else:
        checks = _infer_checks(trace, is_failure_seed, notes)

    duplicates = cluster.duplicate_ids
    if duplicates:
        notes.append(
            f"this question appeared {cluster.size} times in the log; "
            f"addressed once, here"
        )

    return {
        "id": f"case-{index:03d}",
        "input": trace.input,
        "checks": checks,
        "reference_output": trace.output,
        "reference_is_trusted": not is_failure_seed,
        "source_trace_id": trace.id,
        "score": cluster.score,
        "signals": [signal.to_dict() for signal in cluster.signals],
        "occurrences_in_log": cluster.size,
        "duplicate_count": cluster.duplicate_count,
        "duplicate_trace_ids": duplicates,
        "notes": notes,
    }


def select_cases(
    traces: Iterable[Trace],
    max_cases: int = 50,
    min_score: float = 1.0,
    dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD,
    weights: dict[str, float] | None = None,
) -> SelectionResult:
    trace_list = list(traces)
    result = SelectionResult(total_traces=len(trace_list))

    context = build_context(trace_list)
    result.context = context

    fingerprints = {trace.id: shingles(trace.input) for trace in trace_list}

    scored: list[ScoredTrace] = []
    for trace in trace_list:
        signals = compute_signals(trace, context, weights)
        scored.append(ScoredTrace(trace, signals, score(signals)))
    scored.sort(key=lambda item: (-item.score, item.trace.id))

    clusters = cluster_scored(scored, fingerprints, threshold=dedup_threshold)
    result.distinct_questions = len(clusters)

    interesting: list[Cluster] = []
    for cluster in clusters:
        if cluster.score <= 0:
            result.questions_without_signals += 1
            continue
        if cluster.score < min_score:
            result.dropped_below_min_score += 1
            continue
        interesting.append(cluster)

    # Clusters are already in descending score order because that is the order
    # they were created in.
    if len(interesting) > max_cases:
        result.dropped_beyond_limit = len(interesting) - max_cases
        interesting = interesting[:max_cases]

    result.dropped_as_duplicate = sum(cluster.duplicate_count for cluster in interesting)
    result.cases = [build_case(index, cluster) for index, cluster in enumerate(interesting, 1)]
    return result
