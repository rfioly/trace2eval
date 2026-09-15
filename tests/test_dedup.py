"""Deduplication tests.

Every pair below came out of ``examples/sample_traces.jsonl`` and was labelled by
hand.

The measure changed in v0.5, from the overlap coefficient to Jaccard, and these
tests are where the cost of that change is recorded rather than hidden. Overlap
was chosen on this sample log so that a short fragment would match a longer
phrasing of the same question. That still works -- the fragment pairs are listed
below -- but it is no longer what the default does, because on 40,000 random
pairs of real user questions overlap's false-positive rate is 0.34 against
Jaccard's 0.0023, and it climbs with input length to 0.75. On a 9,236-question
corpus it fused 93% of everything into one cluster.

So: ``DIRECT_MATCHES`` still match, ``FRAGMENT_MATCHES`` no longer do, and
``test_the_default_measure_trades_fragment_recall_for_precision`` pins that
trade-off down so nobody has to rediscover it. The sample log was written to
exercise fragment chains, which is exactly why it could not reveal the failure
that killed them as a default.
"""

from __future__ import annotations

from pathlib import Path

from trace2eval.schema import load_traces
from trace2eval.select import (
    DEFAULT_DEDUP_THRESHOLD,
    jaccard,
    overlap_coefficient,
    select_cases,
    shingles,
    similarity,
)

SAMPLE_LOG = Path(__file__).resolve().parents[1] / "examples" / "sample_traces.jsonl"

#: Same question, and close enough in surface text to match on their own.
DIRECT_MATCHES = [
    ("你们的退款政策是什么？", "退款政策是什么"),
    ("退款政策？？", "退款政策"),
    ("修改手机号", "修改手机号"),
]

#: Same question, but one side is a short fragment of the other. These matched
#: under the overlap coefficient and do not match under Jaccard. Losing them
#: costs an extra case, not a wrong one -- see the module docstring.
FRAGMENT_MATCHES = [
    ("你们的退款政策是什么？", "退款政策"),
    ("修改手机号", "我要改手机号"),
    ("修改手机号", "怎么修改绑定的手机号"),
]

#: Genuinely different questions. Merging any of these would mean silently
#: dropping coverage.
DISTINCT_QUESTIONS = [
    ("支持哪些登录方式", "支持哪些支付方式"),
    ("查订单", "订单号"),
    ("登录不上", "支持哪些登录方式"),
    ("能不能退款", "你们的退款政策是什么？"),
]


def test_shingles_ignores_punctuation_and_case():
    assert shingles("Refund Policy!") == shingles("refund policy")
    assert shingles("退款政策？？") == shingles("退款政策")
    assert shingles("") == set()


def test_identical_inputs_score_one():
    assert similarity(shingles("退款政策"), shingles("退款政策")) == 1.0


def test_near_identical_variants_match_directly():
    for left, right in DIRECT_MATCHES:
        score = similarity(shingles(left), shingles(right))
        assert score >= DEFAULT_DEDUP_THRESHOLD, (
            f"expected {left!r} and {right!r} to match directly, got {score:.3f}"
        )


def test_fragment_pairs_no_longer_match_and_that_is_the_known_cost():
    """Pins the recall the v0.5 change gave up, so it cannot drift unnoticed.

    If one of these starts matching again the measure has loosened, and the
    false-positive rate on long real inputs should be re-measured before
    celebrating.
    """
    for left, right in FRAGMENT_MATCHES:
        score = similarity(shingles(left), shingles(right))
        assert score < DEFAULT_DEDUP_THRESHOLD, (
            f"{left!r} / {right!r} now match at {score:.3f}. That is more recall, "
            f"but check benchmarks/real_traffic.md before accepting it."
        )


def test_distinct_questions_stay_below_the_threshold():
    for left, right in DISTINCT_QUESTIONS:
        score = similarity(shingles(left), shingles(right))
        assert score < DEFAULT_DEDUP_THRESHOLD, (
            f"expected {left!r} and {right!r} to stay separate, got {score:.3f}"
        )


def test_short_inputs_need_more_than_one_shared_ngram():
    """Two short queries sharing only one n-gram pair must not be merged."""
    assert similarity(shingles("查订单"), shingles("订单号")) == 0.0
    assert similarity(shingles("登录不上"), shingles("支持哪些登录方式")) == 0.0


def test_the_default_measure_trades_fragment_recall_for_precision():
    """The reversal, kept as a test so it is not rediscovered by accident.

    Through v0.4 this file asserted the opposite: that Jaccard ranks two
    genuinely different questions above two phrasings of the same one, so no
    threshold could separate them. That is still true as a statement about
    ranking, and it is no longer decisive, because a threshold is always applied
    and both measures reject the different-intent pair at 0.60.

    What decided it was 40,000 pairs of real user questions, where the overlap
    coefficient's error rate is 0.34 and climbs with length. Both mistakes are
    not equally bad: a missed merge is one redundant case, a false merge is a
    case silently standing for two unrelated questions. So the fragment recall
    goes.
    """
    fragment = ("你们的退款政策是什么？", "退款政策")
    different = ("支持哪些登录方式", "支持哪些支付方式")

    assert jaccard(shingles(fragment[0]), shingles(fragment[1])) < DEFAULT_DEDUP_THRESHOLD
    assert similarity(shingles(fragment[0]), shingles(fragment[1])) < DEFAULT_DEDUP_THRESHOLD, (
        "the fragment pair must no longer match"
    )
    # Overlap still sees it. That is why it is kept, and why it is no longer the
    # default: on this pair it is right, and on long real inputs it is not.
    assert overlap_coefficient(shingles(fragment[0]), shingles(fragment[1])) >= 0.9

    # The ordering that used to rule Jaccard out is still there.
    assert (
        similarity(shingles(different[0]), shingles(different[1]))
        > similarity(shingles(fragment[0]), shingles(fragment[1]))
    )


def test_distinct_questions_stay_below_the_threshold_under_both_measures():
    """The pair that overlap also rejected, kept as a guard against loosening it."""
    for left, right in DISTINCT_QUESTIONS:
        assert overlap_coefficient(shingles(left), shingles(right)) < DEFAULT_DEDUP_THRESHOLD


def test_empty_input_never_matches():
    assert similarity(set(), shingles("退款政策")) == 0.0
    assert jaccard(set(), shingles("退款政策")) == 0.0


def test_one_question_still_collapses_but_not_all_the_way():
    """The end-to-end promise, and what v0.5 cost it.

    Under the overlap coefficient the refund question collapsed to a single case
    standing for six log lines. Under Jaccard it stands for two -- the near-equal
    phrasing joins it, the short fragments no longer do. That is the recall side
    of the trade recorded in the module docstring, and it is measured here rather
    than assumed.
    """
    loaded = load_traces(SAMPLE_LOG)
    result = select_cases(loaded.traces)

    refund_cases = [case for case in result.cases if "退款政策" in case["input"]]
    assert len(refund_cases) == 1, (
        f"expected the refund question to collapse to one case, got "
        f"{[case['input'] for case in refund_cases]}"
    )
    case = refund_cases[0]
    assert case["occurrences_in_log"] == 2, (
        f"the refund case absorbs {case['occurrences_in_log']} log lines. It was 6 "
        f"under the overlap coefficient. If that number climbs back, the measure has "
        f"loosened -- re-measure benchmarks/real_traffic.md before accepting it."
    )
    assert case["source_trace_id"] == "req-0001"
    assert len(case["duplicate_trace_ids"]) == case["occurrences_in_log"] - 1

    # The log as a whole still collapses one duplicate, so dedup has not been
    # disabled outright -- it is narrower, not gone.
    assert result.stats()["dropped_as_duplicate"] == 1
    assert result.stats()["distinct_questions"] == 39


def test_sample_log_produces_a_usable_case_set():
    loaded = load_traces(SAMPLE_LOG)
    result = select_cases(loaded.traces)
    stats = result.stats()

    assert stats["total_traces"] == 42
    assert loaded.skipped_count == 0
    assert 10 <= stats["cases"] <= 25, "the case set should be a small slice of the log"
    assert stats["dropped_as_duplicate"] > 0, "the sample log contains deliberate duplicates"
    assert stats["distinct_questions"] < stats["total_traces"]
    assert stats["questions_without_signals"] > 0, "most of a real log is boring"
