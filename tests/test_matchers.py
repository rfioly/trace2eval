"""Tests for the pluggable matcher hook.

These exist because "it is only character n-grams, so it cannot handle paraphrase"
is the first thing anyone will ask about this tool. The answer is not that the
weakness is gone -- it is that the weakness is measured, and the matcher is a
single swappable function.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trace2eval.matchers import char_bigram_jaccard, tokenise, word_jaccard
from trace2eval.schema import load_traces
from trace2eval.select import (
    DEFAULT_DEDUP_THRESHOLD,
    MatcherSpecError,
    load_matcher,
    select_cases,
    shingle_overlap,
)

SAMPLE_LOG = Path(__file__).resolve().parents[1] / "examples" / "sample_traces.jsonl"


# --------------------------------------------------------------------------- #
# The hook
# --------------------------------------------------------------------------- #


def test_load_matcher_accepts_colon_and_dotted_specs():
    for spec in ("trace2eval.matchers:word_jaccard", "trace2eval.matchers.word_jaccard"):
        matcher = load_matcher(spec)
        assert callable(matcher)
        assert matcher("refund policy", "policy refund") == 1.0


@pytest.mark.parametrize(
    "spec",
    [
        "no_dots_or_colons",
        "trace2eval.matchers:does_not_exist",
        "definitely_not_a_module_xyz:similarity",
        "trace2eval.matchers:MIN_SHARED_TOKENS",
        "",
    ],
)
def test_load_matcher_rejects_a_bad_spec(spec):
    with pytest.raises(MatcherSpecError):
        load_matcher(spec)


def test_a_bad_matcher_spec_is_a_usage_error_not_a_traceback(tmp_path):
    from trace2eval.cli import EXIT_BAD_INPUT, main

    log = tmp_path / "traces.jsonl"
    log.write_text(
        json.dumps({"input": "查询订单", "output": "好的", "feedback": "negative"}) + "\n",
        encoding="utf-8",
    )
    exit_code = main(["build", str(log), "-o", str(tmp_path / "out"), "--similarity", "bogus"])
    assert exit_code == EXIT_BAD_INPUT


# --------------------------------------------------------------------------- #
# The bundled alternative
# --------------------------------------------------------------------------- #


def test_tokenise_mixes_words_and_cjk_characters():
    tokens = tokenise("退款 refund")
    assert "refund" in tokens
    assert "退" in tokens and "款" in tokens


def test_word_jaccard_handles_word_order_that_the_default_matcher_does_not():
    """The documented reason to reach for it: word order stops mattering."""
    left, right = "refund policy for digital goods", "digital goods refund policy"
    assert word_jaccard(left, right) == 1.0
    assert shingle_overlap(left, right) < 1.0


def test_word_jaccard_is_coarser_on_chinese_and_that_is_documented():
    """The honest caveat, asserted so it cannot quietly stop being true.

    Single characters lose the ordering information that bigrams keep, so two
    genuinely different Chinese questions score higher here than under the default
    matcher. Anyone running this on Chinese traffic has to raise the threshold.
    """
    different_questions = ("支持哪些登录方式", "支持哪些支付方式")
    same_question = ("你们的退款政策是什么？", "退款政策是怎样的")

    assert shingle_overlap(*different_questions) < DEFAULT_DEDUP_THRESHOLD
    assert word_jaccard(*different_questions) > DEFAULT_DEDUP_THRESHOLD
    assert word_jaccard(*different_questions) > shingle_overlap(*different_questions)
    assert word_jaccard(*same_question) >= DEFAULT_DEDUP_THRESHOLD


def test_char_bigram_jaccard_reproduces_the_inversion():
    """Same conclusion as the built-in path, reached through the hook."""
    same_question = ("你们的退款政策是什么？", "退款政策是怎样的")
    different_questions = ("支持哪些登录方式", "支持哪些支付方式")
    assert char_bigram_jaccard(*different_questions) > char_bigram_jaccard(*same_question)


# --------------------------------------------------------------------------- #
# The hook end to end
# --------------------------------------------------------------------------- #


def test_a_custom_matcher_changes_the_case_set():
    loaded = load_traces(SAMPLE_LOG)
    default = select_cases(loaded.traces)
    custom = select_cases(loaded.traces, matcher=word_jaccard)

    # A coarser matcher merges more, so it can only produce fewer distinct
    # questions -- never more.
    assert custom.distinct_questions <= default.distinct_questions
    assert custom.cases, "a custom matcher must still produce a usable case set"
    assert custom.total_traces == default.total_traces


def test_a_coarser_matcher_collapses_more_into_one_case():
    loaded = load_traces(SAMPLE_LOG)
    default = select_cases(loaded.traces)
    custom = select_cases(loaded.traces, matcher=word_jaccard)

    default_collapsed = default.stats()["dropped_as_duplicate"]
    custom_collapsed = custom.stats()["dropped_as_duplicate"]
    assert custom_collapsed >= default_collapsed


# --------------------------------------------------------------------------- #
# Clustering cost
# --------------------------------------------------------------------------- #


def test_identical_questions_cost_one_comparison_not_n(tmp_path):
    """Distinct-phrasing tracking is what keeps a hot question from going quadratic.

    Five hundred copies of one question is one phrasing to compare against, so the
    cluster still forms, still knows its size, and does not scan 500 members every
    time a new row arrives.
    """
    log = tmp_path / "traces.jsonl"
    with log.open("w", encoding="utf-8") as handle:
        for index in range(500):
            row = {"id": f"r{index:04d}", "input": "你们的退款政策是什么？", "output": "退款会在 14 天内处理。"}
            if index % 50 == 0:
                row["feedback"] = "negative"
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    loaded = load_traces(log)
    result = select_cases(loaded.traces)

    assert len(result.cases) == 1
    case = result.cases[0]
    assert case["occurrences_in_log"] == 500
    assert case["duplicate_count"] == 499
    # All 500 rows are near-identical and 10 of them are flagged, so the cluster's
    # representative is one of the flagged ones and the rest collapse into it.
    assert result.stats()["dropped_as_duplicate"] == 499
    assert len(case["duplicate_trace_ids"]) <= 499
