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
select is selected precisely because something went wrong with it. So an expected
shape is inferred from the *clean* answers to the same question -- never from the
output that failed. When a question has no clean answer anywhere in the log,
there is nothing to infer from, and the case says so rather than inventing one.

Every case is then checked against its own reference, which is how the second job
grades itself: a failure seed that does not fail its own checks is a seed the
checks cannot detect, and that is worth knowing.
"""

from __future__ import annotations

import importlib
import json
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .annotations import AnnotationSet, fold_input, merge_checks
from .checks import run_checks
from .schema import Trace
from .signals import (
    Signal,
    TraceContext,
    build_context,
    compute_signals,
    score,
    suspiciously_short_boundary,
)

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

#: Failures that left a mark on the output text, and so are checkable at all.
SHAPE_SIGNALS = frozenset(
    {
        "empty_output",
        "fallback_phrase",
        "expected_shape_violated",
        "output_much_shorter",
        "output_much_longer",
    }
)

#: Failures about what happened *around* the call -- the user asked again, rated it
#: down, or it was slow and expensive. The output text can be perfectly fine, so no
#: deterministic check on it can reproduce these. Naming the distinction is the
#: difference between "this case is weak" and "this class of case cannot be checked
#: this way, and here is what it would take instead".
BEHAVIOUR_SIGNALS = frozenset(
    {
        "negative_feedback",
        "user_retried",
        "slow_response",
        "expensive_call",
    }
)

#: A similarity function: two raw inputs in, a score in [0, 1] out. The default is
#: :func:`shingle_overlap`; anything with this signature can be swapped in from the
#: command line with ``--similarity module:function``.
Matcher = Callable[[str, str], float]


class MatcherSpecError(ValueError):
    """Raised when a ``--similarity`` spec cannot be resolved to a callable."""


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

#: Safety valve on clustering cost. A cluster compares an incoming trace against
#: one fingerprint per *distinct* phrasing already in it (see ``Cluster``), so the
#: cost is bounded by distinct phrasings rather than by cluster size. This caps
#: that number anyway. Past the cap, a trace that only matches a phrasing beyond
#: the window starts its own cluster -- a duplicate rather than a miss, which is
#: the failure direction to prefer.
MAX_DISTINCT_FINGERPRINTS = 512

_STRIP_PATTERN = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’()（）\[\]【】]+")

#: Inferred minimum length is a fraction of the reference length. 0.4 is
#: deliberately loose: it catches *truncation*, not rewording.
_MIN_CHARS_RATIO = 0.4
_MIN_CHARS_FLOOR = 8

#: When a question has no clean answer to learn a shape from, the only thing we
#: still know for certain is that an empty answer was wrong. Asserting a
#: non-empty answer is not a guess about length, so it is safe to add.
_NON_EMPTY_MIN_CHARS = 1


@dataclass
class ClusterHealth:
    """What the clustering looks like, measured rather than assumed.

    This exists because of a number that came out of real labelled data. On 1,062
    SQuAD questions with no duplicates in them, the default threshold produced a
    pairwise false-positive rate of 0.4% -- and single linkage turned that into
    54% of the pool landing in some cluster, the largest holding 425 questions.

    A tenth of a percent of spurious links is plenty to fuse a big pool, and the
    tool used to say nothing about it. A case standing for 425 unrelated calls is
    worse than no case: it asserts one answer for hundreds of different questions,
    and the report gave no hint.

    So the numbers below get computed and shown. ``chained`` counts clusters
    holding a phrasing that scores below the threshold against the cluster's own
    representative.

    Deliberately **not** turned into an automatic warning. The first version was
    one, and it fired on the sample log's two legitimate clusters straight away:
    "我想了解退款政策" scores 0.429 against "你们的退款政策是什么？" while being
    unmistakably the same question. Short fragments are exactly what single
    linkage is meant to hold together, so a low representative similarity cannot
    distinguish a useful chain from a spurious one. A warning that fires on the
    good case is worse than none, so these stay as numbers for a person to read.
    """

    threshold: float
    clusters: int
    multi_member_clusters: int
    largest: int
    share_in_clusters: float
    chained_clusters: int
    chained_sampled: int
    worst_representative_similarity: float

    def summary_lines(self) -> list[str]:
        return [
            f"threshold {self.threshold:.2f}",
            f"{self.clusters} clusters, {self.multi_member_clusters} with more than one row",
            f"largest cluster holds {self.largest} rows",
            f"{self.share_in_clusters:.0%} of the log sits in a cluster with others",
        ]


@dataclass
class ScoredTrace:
    trace: Trace
    signals: list[Signal]
    score: float

    @property
    def is_clean(self) -> bool:
        """True when nothing about this row suggests it went wrong.

        Only clean rows may be used as a shape reference: their output is the
        closest thing to an answer that was actually fine in production.
        """
        if self.trace.output_chars == 0:
            return False
        return not ({signal.name for signal in self.signals} & QUALITY_SIGNALS)


@dataclass
class Cluster:
    """One distinct question, plus every log line that asked it."""

    representative: Trace
    signals: list[Signal]
    score: float
    member_ids: list[str] = field(default_factory=list)

    #: Outputs from members that look clean, in arrival order. This is the pool a
    #: case's expected shape is inferred from.
    clean_outputs: list[str] = field(default_factory=list)

    #: One representative text per *distinct* input phrasing seen in this cluster,
    #: with the matching fingerprints alongside. Deduplicating here is what keeps
    #: a cluster of 5,000 identical questions from costing 5,000 comparisons when
    #: question 5,001 arrives.
    fingerprints: list[frozenset[str]] = field(default_factory=list)
    sample_texts: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.member_ids)

    @property
    def duplicate_count(self) -> int:
        return max(0, self.size - 1)

    @property
    def duplicate_ids(self) -> list[str]:
        return [mid for mid in self.member_ids if mid != self.representative.id]

    @property
    def has_usable_shape_reference(self) -> bool:
        return bool(self.clean_outputs)


@dataclass
class SelectionResult:
    cases: list[dict[str, Any]] = field(default_factory=list)
    context: TraceContext | None = None

    #: How the clustering actually behaved, measured rather than assumed. See
    #: :class:`ClusterHealth` for the real-data numbers that motivated it.
    cluster_health: ClusterHealth | None = None

    total_traces: int = 0
    distinct_questions: int = 0
    questions_without_signals: int = 0
    dropped_below_min_score: int = 0
    dropped_as_duplicate: int = 0
    dropped_beyond_limit: int = 0

    @property
    def cases_with_weak_checks(self) -> list[dict[str, Any]]:
        """Cases where the checks are known not to catch the failure they came from."""
        return [
            case
            for case in self.cases
            if case["self_check"]["verdict"] == "failure_not_reproduced"
        ]

    @property
    def cases_whose_reference_fails(self) -> list[dict[str, Any]]:
        """Trusted references that do not satisfy their own checks. A real defect."""
        return [
            case
            for case in self.cases
            if case["self_check"]["verdict"] == "reference_fails_own_checks"
        ]

    @property
    def cases_with_annotation(self) -> list[dict[str, Any]]:
        return [case for case in self.cases if case["annotation"]["applied"]]

    @property
    def cases_needing_annotation(self) -> list[dict[str, Any]]:
        """The remaining TODO list.

        A weak case that already carries a human expectation is off this list:
        the expectation does not make the case reproduce its original failure --
        nothing can, the failure was not in the text -- but it does give the case
        something worth asserting, which is the actual goal.
        """
        return [
            case
            for case in self.cases_with_weak_checks
            if not case["annotation"]["applied"]
        ]

    def stats(self) -> dict[str, Any]:
        return {
            "total_traces": self.total_traces,
            "distinct_questions": self.distinct_questions,
            "questions_without_signals": self.questions_without_signals,
            "cases": len(self.cases),
            "dropped_below_min_score": self.dropped_below_min_score,
            "dropped_as_duplicate": self.dropped_as_duplicate,
            "dropped_beyond_limit": self.dropped_beyond_limit,
            "cases_with_weak_checks": len(self.cases_with_weak_checks),
            "cases_whose_reference_fails": len(self.cases_whose_reference_fails),
            "cases_with_annotation": len(self.cases_with_annotation),
            "cases_needing_annotation": len(self.cases_needing_annotation),
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


def overlap_coefficient(
    left: set[str], right: set[str], min_shared: int = MIN_SHARED_SHINGLES
) -> float:
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

    What this still cannot do is recognise a paraphrase that shares no
    characters with the original. That is a scope boundary, not a bug -- see
    ``trace2eval.matchers`` and ``--similarity`` for how to swap in something
    semantic if recall matters more than staying dependency-free.
    """
    if not left or not right:
        return 0.0
    shared = len(left & right)
    if shared < min_shared:
        return 0.0
    return shared / min(len(left), len(right))


def shingle_overlap(left: str, right: str) -> float:
    """The default matcher: overlap coefficient over character n-grams."""
    return overlap_coefficient(shingles(left), shingles(right))


def similarity(left: set[str], right: set[str], min_shared: int = MIN_SHARED_SHINGLES) -> float:
    """Alias kept for readability at call sites that already hold shingle sets."""
    return overlap_coefficient(left, right, min_shared)


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


def load_matcher(spec: str) -> Matcher:
    """Resolve ``"module:function"`` (or ``"module.function"``) into a matcher.

    The point of this hook is that paraphrase recall costs a dependency, and this
    tool's whole pitch is that it has none. So instead of bundling an embedding
    model, it lets you point at one you already have:

        trace2eval build traces.jsonl --similarity my_embeddings:cosine
    """
    if ":" in spec:
        module_name, _, attr = spec.partition(":")
    else:
        module_name, _, attr = spec.rpartition(".")
    if not module_name or not attr:
        raise MatcherSpecError(
            f"expected 'module:function', got {spec!r}"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise MatcherSpecError(f"cannot import module {module_name!r}: {exc}") from exc
    try:
        candidate = getattr(module, attr)
    except AttributeError as exc:
        raise MatcherSpecError(
            f"module {module_name!r} has no attribute {attr!r}"
        ) from exc
    if not callable(candidate):
        raise MatcherSpecError(f"{spec!r} is not callable")
    return candidate


def _record_phrasing(cluster: Cluster, fingerprint: set[str], text: str) -> None:
    """Remember a phrasing once, so later duplicates cost nothing to compare."""
    if len(cluster.fingerprints) >= MAX_DISTINCT_FINGERPRINTS:
        return
    frozen = frozenset(fingerprint)
    if frozen in cluster.fingerprints:
        return
    cluster.fingerprints.append(frozen)
    cluster.sample_texts.append(text)


def _cluster_matches(
    cluster: Cluster,
    fingerprint: set[str],
    text: str,
    threshold: float,
    matcher: Matcher | None,
) -> bool:
    if matcher is None:
        return any(
            overlap_coefficient(fingerprint, known) >= threshold
            for known in cluster.fingerprints
        )
    return any(matcher(text, known) >= threshold for known in cluster.sample_texts)


def cluster_scored(
    scored: list[ScoredTrace],
    fingerprints: dict[str, set[str]],
    threshold: float = DEFAULT_DEDUP_THRESHOLD,
    matcher: Matcher | None = None,
) -> list[Cluster]:
    """Single-linkage clustering over the whole log.

    ``scored`` must arrive sorted by descending score. That ordering does the real
    work: because we walk the worst rows first, the first row to claim a cluster is
    the most interesting one, so it becomes the representative without any extra
    bookkeeping.

    Comparison is against every *distinct phrasing* in a cluster, not just the
    representative, so that a chain like

        我想了解退款政策  ~  退款政策  ~  你们的退款政策是什么

    holds together: the fragments in the middle never score high enough against a
    long representative, and a centroid comparison would silently split the
    cluster. Comparing against distinct phrasings rather than against every member
    is what keeps that affordable -- five thousand identical questions produce one
    phrasing to compare against, not five thousand.

    With the default matcher, an inverted index on n-grams shortlists which
    clusters are worth comparing at all. A custom matcher disables that index,
    because a lexical shortlist would cap the recall the custom matcher was
    brought in to provide.
    """
    clusters: list[Cluster] = []
    postings: dict[str, set[int]] = {}

    for item in scored:
        fingerprint = fingerprints.get(item.trace.id, set())

        if matcher is None:
            candidates: set[int] = set()
            for shingle in fingerprint:
                candidates |= postings.get(shingle, set())
        else:
            candidates = set(range(len(clusters)))

        target: int | None = None
        for index in sorted(candidates):
            if _cluster_matches(
                clusters[index], fingerprint, item.trace.input, threshold, matcher
            ):
                target = index
                break

        if target is None:
            cluster = Cluster(
                representative=item.trace,
                signals=item.signals,
                score=item.score,
                member_ids=[item.trace.id],
            )
            clusters.append(cluster)
            target = len(clusters) - 1
        else:
            cluster = clusters[target]
            cluster.member_ids.append(item.trace.id)

        _record_phrasing(cluster, fingerprint, item.trace.input)
        if item.is_clean:
            cluster.clean_outputs.append(item.trace.output)

        if matcher is None:
            for shingle in fingerprint:
                postings.setdefault(shingle, set()).add(target)

    return clusters


#: How many distinct phrasings per cluster to compare when measuring spread.
#: Full pairwise across up to MAX_DISTINCT_FINGERPRINTS phrasings would be
#: quadratic per cluster, and this is a diagnostic, not a decision.
HEALTH_SAMPLE = 25


def assess_cluster_health(
    clusters: list[Cluster],
    threshold: float,
    total_traces: int,
) -> ClusterHealth:
    """Measure how far the produced clusters have drifted from their own question.

    Compares each distinct phrasing in a cluster against that cluster's
    representative. A cluster whose phrasings all score at or above the threshold
    is a genuine match. One holding a phrasing well below it was chained together
    through other questions, and is a candidate for being two questions wearing
    one case id.
    """
    multi = [cluster for cluster in clusters if cluster.size > 1]
    chained = 0
    sampled = 0
    worst = 1.0

    for cluster in multi:
        representative = cluster.representative.input
        representative_key = normalise(representative)
        others = [
            text
            for text in cluster.sample_texts[:HEALTH_SAMPLE]
            if normalise(text) != representative_key
        ]
        if not others:
            continue
        sampled += 1
        lowest = min(shingle_overlap(representative, text) for text in others)
        worst = min(worst, lowest)
        if lowest < threshold:
            chained += 1

    return ClusterHealth(
        threshold=threshold,
        clusters=len(clusters),
        multi_member_clusters=len(multi),
        largest=max((cluster.size for cluster in clusters), default=0),
        share_in_clusters=(sum(cluster.size for cluster in multi) / total_traces)
        if total_traces
        else 0.0,
        chained_clusters=chained,
        chained_sampled=sampled,
        worst_representative_similarity=worst,
    )


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


def _agreed_json_keys(outputs: list[str]) -> list[str]:
    """Keys every clean answer shares, or an empty list if they disagree.

    Intersecting rather than unioning is the conservative choice: requiring a key
    that only one answer happened to include is how you get a check that fails on
    correct output.
    """
    schemas: list[set[str]] = []
    for output in outputs:
        stripped = output.strip()
        if not stripped.startswith("{"):
            return []
        try:
            payload = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(payload, dict):
            return []
        schemas.append(set(payload.keys()))
    if not schemas:
        return []
    common = set.intersection(*schemas) if len(schemas) > 1 else schemas[0]
    return sorted(common)


def _infer_checks(
    trace: Trace,
    is_failure_seed: bool,
    references: list[str],
    notes: list[str],
    length_floor: int | None = None,
) -> list[dict[str, Any]]:
    """Infer an expected shape from clean answers, never from a failed one."""
    checks: list[dict[str, Any]] = [{"type": "not_fallback"}]

    usable = [output for output in references if output.strip()]
    if not usable:
        if is_failure_seed:
            if length_floor is not None:
                checks.append({"type": "min_chars", "value": length_floor})
                notes.append(
                    f"failure seed with no clean sibling, but the failure itself was a "
                    f"length failure: the response fell below the log's short-answer "
                    f"line, so this case asserts min_chars={length_floor}. That is a "
                    f"weak reference -- it comes from the log-wide median rather than "
                    f"from this question's own answers, so check it first when tuning"
                )
            else:
                checks.append({"type": "min_chars", "value": _NON_EMPTY_MIN_CHARS})
                notes.append(
                    "failure seed with no clean sibling: no answer to this question in "
                    "the log was usable as a shape reference, so all this case asserts is "
                    "that the answer is not empty and does not fall back"
                )
        else:
            notes.append("no shape inferred: the reference answer is empty")
        return checks

    lengths = [len(output.strip()) for output in usable]
    reference_length = int(statistics.median(lengths))
    minimum = max(_MIN_CHARS_FLOOR, int(reference_length * _MIN_CHARS_RATIO))
    checks.append({"type": "min_chars", "value": minimum})

    source = "clean answer" if len(usable) == 1 else f"{len(usable)} clean answers"
    if is_failure_seed:
        notes.append(
            f"failure seed: the failing output is not a shape reference, so "
            f"min_chars={minimum} was inferred from {source} to the same question "
            f"(median {reference_length} chars)"
        )
    else:
        notes.append(
            f"clean trace: min_chars={minimum} from {source} to this question "
            f"(median {reference_length} chars); catches truncation, tolerates rewording"
        )

    agreed_keys = _agreed_json_keys(usable)
    if agreed_keys:
        checks.append({"type": "json", "required": agreed_keys})
        notes.append(f"clean answers agree on a JSON shape with keys {agreed_keys}")
    elif len(usable) > 1 and all(output.strip().startswith("{") for output in usable):
        notes.append(
            "clean answers are all JSON but disagree on keys, so no JSON check was added"
        )

    return checks


def _self_check(checks: list[dict[str, Any]], output: str) -> dict[str, Any]:
    """Run a case's checks against its own reference output.

    This is the honest part. A failure seed whose checks *pass* on the bad output
    is a case that cannot detect the thing it was created to detect, and the
    verdict says so instead of letting the case look stronger than it is.
    """
    results = run_checks(checks, output)
    failed = [result.type for result in results if not result.passed]
    return {"passed": not failed, "failed_checks": failed}


def build_case(
    index: int,
    cluster: Cluster,
    context: TraceContext | None = None,
    expectations: AnnotationSet | None = None,
) -> dict[str, Any]:
    trace = cluster.representative
    names = {signal.name for signal in cluster.signals}
    is_failure_seed = bool(names & QUALITY_SIGNALS)

    shape_hit = names & SHAPE_SIGNALS
    behaviour_hit = names & BEHAVIOUR_SIGNALS
    if shape_hit and behaviour_hit:
        failure_kind = "shape_and_behaviour"
    elif shape_hit:
        failure_kind = "shape"
    elif behaviour_hit:
        failure_kind = "behaviour"
    else:
        failure_kind = "none"

    # When the failure was itself "this answer is too short", the line it fell
    # below is evidence, even with no clean answer to compare against. Same
    # function the signal used, so the two cannot drift apart.
    length_floor: int | None = None
    if is_failure_seed and "output_much_shorter" in names and context is not None:
        length_floor = suspiciously_short_boundary(context.median_output_chars)

    notes: list[str] = []
    if trace.expect:
        checks = _checks_from_expect(trace.expect)
        notes.append("checks inherited from the trace's own expect block")
        if not any(check["type"] == "not_fallback" for check in checks):
            checks.insert(0, {"type": "not_fallback"})
        shape_source = "the trace's own expect block"
    else:
        references = list(cluster.clean_outputs)
        if not references and not is_failure_seed:
            references = [trace.output]
        checks = _infer_checks(trace, is_failure_seed, references, notes, length_floor)
        usable = sum(1 for output in references if output.strip())
        if usable > 1:
            shape_source = f"{usable} clean answers to the same question"
        elif usable == 1:
            shape_source = "the one clean answer to this question"
        elif length_floor is not None:
            shape_source = "the log's short-answer line (weak reference)"
        else:
            shape_source = "none available -- only behaviour was asserted"

    # A human-written expectation outranks anything inferred. It does not make
    # the case reproduce its original failure -- for a behavioural failure
    # nothing on the output text can, by definition -- but it does give the case
    # a specification worth asserting, which is the actual point.
    annotation = (
        expectations.lookup(fold_input(trace.input)) if expectations is not None else None
    )
    if annotation is not None:
        checks = merge_checks(checks, _checks_from_expect(annotation.expect))
        if not any(check["type"] == "not_fallback" for check in checks):
            checks.insert(0, {"type": "not_fallback"})
        shape_source = "a human-written expectation"
        notes.append(
            "human expectation applied, overriding anything inferred — "
            + (annotation.note or "(no note given)")
        )

    duplicates = cluster.duplicate_ids
    if duplicates:
        notes.append(
            f"this question appeared {cluster.size} times in the log; "
            f"addressed once, here"
        )

    self_check = _self_check(checks, trace.output)
    if is_failure_seed:
        verdict = "ok" if not self_check["passed"] else "failure_not_reproduced"
    else:
        verdict = "ok" if self_check["passed"] else "reference_fails_own_checks"
    self_check["verdict"] = verdict

    if verdict == "failure_not_reproduced":
        notes.append(
            "self-check: these checks pass on the very output that went wrong, so a "
            "differently-wrong answer would also pass"
        )
    elif verdict == "reference_fails_own_checks":
        notes.append(
            "self-check: a trusted reference does not satisfy its own checks -- "
            "either the reference is wrong or the checks are"
        )

    return {
        "id": f"case-{index:03d}",
        "input": trace.input,
        "checks": checks,
        "reference_output": trace.output,
        "reference_is_trusted": not is_failure_seed,
        "shape_source": shape_source,
        "failure_kind": failure_kind,
        "annotation": {
            "applied": annotation is not None,
            "note": annotation.note if annotation is not None else "",
        },
        "source_trace_id": trace.id,
        "score": cluster.score,
        "signals": [signal.to_dict() for signal in cluster.signals],
        "occurrences_in_log": cluster.size,
        "duplicate_count": cluster.duplicate_count,
        "duplicate_trace_ids": duplicates,
        "self_check": self_check,
        "notes": notes,
    }


def select_cases(
    traces: Iterable[Trace],
    max_cases: int = 50,
    min_score: float = 1.0,
    dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD,
    weights: dict[str, float] | None = None,
    matcher: Matcher | None = None,
    expectations: AnnotationSet | None = None,
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

    clusters = cluster_scored(
        scored, fingerprints, threshold=dedup_threshold, matcher=matcher
    )
    result.distinct_questions = len(clusters)
    # Measured over every cluster, not just the promoted ones: chaining does not
    # care which cases made the cut, and a fusing pool is worth knowing about
    # even when the fused cluster scores too low to be promoted.
    result.cluster_health = assess_cluster_health(clusters, dedup_threshold, len(trace_list))

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
    result.cases = [
        build_case(index, cluster, context, expectations)
        for index, cluster in enumerate(interesting, 1)
    ]
    return result
