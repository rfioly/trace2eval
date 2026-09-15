"""End-to-end tests: loading, case building, scoring, and the regression gate."""

from __future__ import annotations

import json
from pathlib import Path

from trace2eval.checks import detect_fallback, run_check, run_checks
from trace2eval.runner import compare_runs, load_outputs, run_cases
from trace2eval.schema import Trace, load_traces, normalise_feedback, parse_trace
from trace2eval.select import build_case, select_cases, Cluster
from trace2eval.signals import build_context, compute_signals, score

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SAMPLE_LOG = EXAMPLES / "sample_traces.jsonl"
BASELINE_OUTPUTS = EXAMPLES / "outputs_baseline.jsonl"
REGRESSED_OUTPUTS = EXAMPLES / "outputs_regressed.jsonl"


def write_log(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "traces.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def test_empty_output_is_a_trace_not_an_unreadable_line(tmp_path):
    """An empty answer is the most interesting row in a log, not a parse failure.

    An earlier version of ``_pick`` treated ``""`` as a missing field and dropped
    the row, which silently deleted exactly the failures the tool exists to find.
    """
    log = write_log(
        tmp_path,
        [{"id": "a", "input": "查询订单", "output": ""}],
    )
    loaded = load_traces(log)
    assert loaded.skipped_count == 0
    assert len(loaded.traces) == 1
    assert loaded.traces[0].output == ""

    context = build_context(loaded.traces)
    signals = compute_signals(loaded.traces[0], context)
    assert [signal.name for signal in signals] == ["empty_output"]


def test_a_missing_input_is_skipped(tmp_path):
    log = write_log(
        tmp_path,
        [
            {"id": "a", "input": "查询订单", "output": "好的"},
            {"id": "b", "output": "没有输入的一行"},
            {"id": "c", "input": "", "output": "没有输入的一行"},
        ],
    )
    loaded = load_traces(log)
    assert len(loaded.traces) == 1
    assert loaded.skipped_count == 2


def test_field_aliases_and_a_broken_line(tmp_path):
    """Real logs disagree about field names and contain the occasional bad row."""
    log = tmp_path / "traces.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"prompt": "问题一", "response": "回答一"}),
                "{ this is not json",
                json.dumps({"question": "问题二", "completion": "回答二", "rating": 1}),
                "",
            ]
        ),
        encoding="utf-8",
    )
    loaded = load_traces(log)
    assert [trace.input for trace in loaded.traces] == ["问题一", "问题二"]
    assert loaded.traces[1].feedback == "negative"
    assert loaded.skipped_count == 1


def test_feedback_ratings_are_normalised():
    assert normalise_feedback("差评") == "negative"
    assert normalise_feedback("thumbs_down") == "negative"
    assert normalise_feedback(2) == "negative"
    assert normalise_feedback("好评") == "positive"
    assert normalise_feedback(5) == "positive"
    assert normalise_feedback(None) is None
    assert normalise_feedback("nonsense") is None


def test_latency_given_in_seconds_is_converted():
    trace = parse_trace({"input": "x", "output": "y", "latency_s": 1.5}, "id")
    assert trace is not None
    assert trace.latency_ms == 1500.0


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def test_fallback_detection_covers_both_languages():
    assert detect_fallback("作为一个AI，我无法提供这个信息。")
    assert detect_fallback("抱歉，我无法处理这个请求。")
    assert detect_fallback("I'm sorry, but I can't help with that.")
    assert detect_fallback("订单已发货，预计明天送达。") is None


def test_unknown_check_type_fails_loudly():
    """A test that silently does nothing is worse than no test."""
    result = run_check({"type": "vibes"}, "anything")
    assert not result.passed
    assert "unsupported" in result.detail


def test_json_check_reports_missing_keys():
    results = run_checks(
        [{"type": "json", "required": ["status", "session_id"]}],
        '{"status": "ok"}',
    )
    assert not results[0].passed
    assert "session_id" in results[0].detail


def test_regex_check_survives_a_bad_pattern():
    result = run_check({"type": "regex", "value": "([unclosed"}, "anything")
    assert not result.passed
    assert "invalid pattern" in result.detail


# --------------------------------------------------------------------------- #
# Case construction
# --------------------------------------------------------------------------- #


def test_failure_seed_without_a_clean_sibling_gets_no_shape():
    """The output that failed is never used as a shape reference.

    With nothing else in the log to learn from, all the case can honestly assert is
    that the answer is not empty and does not fall back.
    """
    trace = Trace(
        id="t1",
        input="我要投诉",
        output="请稍后再试。",
        feedback="negative",
        retried=True,
    )
    signals = compute_signals(trace, build_context([trace]))
    cluster = Cluster(trace, signals, score(signals), [trace.id])
    case = build_case(1, cluster)

    assert [check["type"] for check in case["checks"]] == ["not_fallback", "min_chars"]
    assert case["checks"][1]["value"] == 1, "only 'not empty' is knowable here"
    assert case["reference_is_trusted"] is False
    assert case["self_check"]["verdict"] == "failure_not_reproduced"
    assert any("no clean sibling" in note for note in case["notes"])


def test_failure_seed_learns_its_shape_from_clean_siblings():
    """The headline v0.2.0 change: shape comes from the answers that were fine."""
    bad = Trace(
        id="bad",
        input="你们的退款政策是什么？",
        output="退款会在 14 天内处理。",
        feedback="negative",
    )
    clean = Trace(
        id="clean",
        input="退款政策",
        output="退款申请提交后会在 14 个工作日内原路退回，具体到账时间取决于你的支付渠道。",
    )
    context = build_context([bad, clean])
    signals = compute_signals(bad, context)
    cluster = Cluster(
        bad,
        signals,
        score(signals),
        [bad.id, clean.id],
        clean_outputs=[clean.output],
    )
    case = build_case(1, cluster)

    assert [check["type"] for check in case["checks"]] == ["not_fallback", "min_chars"]
    assert case["checks"][1]["value"] == max(8, int(len(clean.output) * 0.4))
    assert case["shape_source"] == "the one clean answer to this question"

    # The failing output is short, so the checks now catch it -- on shape rather
    # than on the original feedback signal. That is the whole point: before
    # v0.2.0 this case asserted nothing but "must not fall back".
    assert case["self_check"]["verdict"] == "ok"
    assert case["self_check"]["failed_checks"] == ["min_chars"]


def test_self_check_flags_a_seed_it_cannot_detect():
    """A failure seed whose checks pass on the bad output is reported, not hidden."""
    trace = Trace(id="t1", input="修改手机号", output="修改绑定手机号需要先通过原手机号接收验证码。", retried=True)
    signals = compute_signals(trace, build_context([trace]))
    cluster = Cluster(trace, signals, score(signals), [trace.id])
    case = build_case(1, cluster)

    # The failure here was behavioural (the user asked twice); nothing in the
    # output looks wrong, so no deterministic check can reproduce it.
    assert case["self_check"]["passed"] is True
    assert case["self_check"]["verdict"] == "failure_not_reproduced"
    assert any("self-check" in note for note in case["notes"])


def test_sample_log_reports_its_own_weak_cases():
    """The residual blind spot is counted, not glossed over."""
    loaded = load_traces(SAMPLE_LOG)
    result = select_cases(loaded.traces)

    weak = result.cases_with_weak_checks
    assert weak, "the sample log contains behavioural failures no check can catch"
    assert all(case["self_check"]["verdict"] == "failure_not_reproduced" for case in weak)
    assert result.stats()["cases_with_weak_checks"] == len(weak)

    # Failure seeds whose checks DO fire must be the ones with a real, visible defect.
    reproduced = [
        case
        for case in result.cases
        if not case["reference_is_trusted"] and case["self_check"]["passed"] is False
    ]
    assert reproduced, "fallback and shape failures should be caught by their own checks"


def test_clean_json_trace_infers_a_schema():
    trace = Trace(id="t1", input="返回状态", output='{"status":"ok","count":2}')
    signals = compute_signals(trace, build_context([trace]))
    cluster = Cluster(trace, signals, score(signals), [trace.id])
    case = build_case(1, cluster)

    types = [check["type"] for check in case["checks"]]
    assert types == ["not_fallback", "min_chars", "json"]
    assert case["checks"][2]["required"] == ["count", "status"]
    assert case["reference_is_trusted"] is True


def test_explicit_expectations_win_over_inference():
    trace = Trace(
        id="t1",
        input="返回余额",
        output='{"balance": 10}',
        expect={"json": True, "required": ["balance"]},
    )
    signals = compute_signals(trace, build_context([trace]))
    cluster = Cluster(trace, signals, score(signals), [trace.id])
    case = build_case(1, cluster)

    assert [check["type"] for check in case["checks"]] == ["not_fallback", "json"]
    assert case["checks"][1]["required"] == ["balance"]
    assert any("inherited" in note for note in case["notes"])


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def build_sample_cases():
    loaded = load_traces(SAMPLE_LOG)
    return select_cases(loaded.traces).cases


def test_baseline_and_regressed_examples_are_consistent_with_the_case_set():
    """The committed example outputs must cover every generated case."""
    cases = build_sample_cases()
    case_ids = {case["id"] for case in cases}

    for path in (BASELINE_OUTPUTS, REGRESSED_OUTPUTS):
        # split("\n") not splitlines(): U+2028 is a line boundary to the latter
        # but not to json.dumps. See the note in annotations.py.
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
        assert {row["id"] for row in rows} == case_ids, f"{path.name} is out of sync"


def test_gate_passes_a_clean_run_and_fails_a_regressed_one():
    cases = build_sample_cases()

    baseline = run_cases(cases, load_outputs(BASELINE_OUTPUTS))
    regressed = run_cases(cases, load_outputs(REGRESSED_OUTPUTS))

    assert baseline.pass_rate == 1.0, "the baseline example should be clean"
    assert baseline.format_compliance_rate == 1.0

    assert regressed.pass_rate < 1.0
    assert regressed.fallback_rate > 0
    assert regressed.format_compliance_rate is not None
    assert regressed.format_compliance_rate < 1.0

    violations = compare_runs(baseline, regressed)
    metrics = {violation.metric for violation in violations}
    assert {"pass_rate", "format_compliance_rate", "fallback_rate"} <= metrics


def test_a_missing_output_counts_as_a_failure_not_a_crash():
    cases = build_sample_cases()
    outputs = load_outputs(BASELINE_OUTPUTS)
    del outputs[cases[0]["id"]]

    metrics = run_cases(cases, outputs)
    assert metrics.missing == 1
    assert metrics.failed >= 1


def test_tolerance_scale_zero_makes_the_gate_stricter():
    cases = build_sample_cases()
    baseline = run_cases(cases, load_outputs(BASELINE_OUTPUTS))
    regressed = run_cases(cases, load_outputs(REGRESSED_OUTPUTS))

    strict = compare_runs(baseline, regressed, tolerance_scale=0.0)
    loose = compare_runs(baseline, regressed, tolerance_scale=100.0)
    assert len(strict) >= len(loose)
