"""Deduplication tests.

Every pair below came out of ``examples/sample_traces.jsonl`` and was labelled by
hand. Two of them are the reason the similarity measure is an overlap coefficient
rather than Jaccard; if someone swaps it back,
``test_jaccard_ranks_the_false_pair_higher`` fails and explains why.

Note the two different kinds of "same question" being tested here. Some phrasings
are near-identical and match directly. Others are the same intent but share too
little surface text to match on their own -- ``我想了解退款政策`` only ever joins
the refund cluster because single linkage routes it through a nearer neighbour.
That distinction is real, it is load-bearing, and it is tested separately.
"""

from __future__ import annotations

from pathlib import Path

from trace2eval.schema import load_traces
from trace2eval.select import (
    DEFAULT_DEDUP_THRESHOLD,
    jaccard,
    select_cases,
    shingles,
    similarity,
)

SAMPLE_LOG = Path(__file__).resolve().parents[1] / "examples" / "sample_traces.jsonl"

#: Same question, and close enough in surface text to match on their own.
DIRECT_MATCHES = [
    ("你们的退款政策是什么？", "退款政策"),
    ("你们的退款政策是什么？", "退款政策是什么"),
    ("退款政策？？", "退款政策"),
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


def test_jaccard_ranks_the_false_pair_higher():
    """The measurement that ruled out Jaccard.

    Jaccard scores two genuinely *different* questions above two phrasings of the
    *same* question, so no threshold on Jaccard can separate them. That is why
    ``similarity`` divides by the shorter set instead of the union.
    """
    same_intent = ("你们的退款政策是什么？", "退款政策是怎样的")
    different_intent = ("支持哪些登录方式", "支持哪些支付方式")

    jaccard_same = jaccard(shingles(same_intent[0]), shingles(same_intent[1]))
    jaccard_different = jaccard(shingles(different_intent[0]), shingles(different_intent[1]))
    assert jaccard_different > jaccard_same, (
        "if this ever flips, the Jaccard-vs-overlap reasoning in select.py "
        "needs revisiting"
    )

    # Overlap fixes the ordering, but it cannot separate these two pairs either --
    # both land on 0.571. The threshold alone does not solve deduplication; the
    # single-linkage clustering is what makes it work.
    assert similarity(shingles(same_intent[0]), shingles(same_intent[1])) == similarity(
        shingles(different_intent[0]), shingles(different_intent[1])
    )


def test_empty_input_never_matches():
    assert similarity(set(), shingles("退款政策")) == 0.0
    assert jaccard(set(), shingles("退款政策")) == 0.0


def test_one_question_becomes_exactly_one_case():
    """The end-to-end promise: seven phrasings, one case, honest occurrence count."""
    loaded = load_traces(SAMPLE_LOG)
    result = select_cases(loaded.traces)

    refund_cases = [case for case in result.cases if "退款政策" in case["input"]]
    assert len(refund_cases) == 1, (
        f"expected the refund question to collapse to one case, got "
        f"{[case['input'] for case in refund_cases]}"
    )
    assert refund_cases[0]["occurrences_in_log"] >= 5
    assert refund_cases[0]["source_trace_id"] == "req-0001"
    assert len(refund_cases[0]["duplicate_trace_ids"]) == refund_cases[0]["occurrences_in_log"] - 1


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
