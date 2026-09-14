"""Tests for human-written expectations.

The property that matters most is survival: generated case ids are assigned in
descending score order, so adding one trace to a log can renumber everything. An
annotation keyed on a case id would detach itself on the next rebuild and the
case would quietly go back to being weak while still looking annotated. Several
of these tests exist to make sure that cannot happen unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trace2eval.annotations import (
    Annotation,
    AnnotationSet,
    fold_input,
    load_annotations,
    merge_checks,
    render_template,
)
from trace2eval.cli import main
from trace2eval.schema import Trace, load_traces
from trace2eval.select import select_cases

SAMPLE_LOG = Path(__file__).resolve().parents[1] / "examples" / "sample_traces.jsonl"
SAMPLE_EXPECTATIONS = Path(__file__).resolve().parents[1] / "evalset" / "expectations.jsonl"


def read_lines(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def test_loads_objects_and_ignores_comments_and_blanks(tmp_path):
    path = tmp_path / "expectations.jsonl"
    path.write_text(
        "// a comment\n"
        "\n"
        '{"input": "修改手机号", "expect": {"min_chars": 20}, "note": "why"}\n'
        "// another comment\n",
        encoding="utf-8",
    )
    loaded = load_annotations(path)

    assert not loaded.skipped
    assert len(loaded.filled) == 1
    assert loaded.filled[0].input == "修改手机号"
    assert loaded.filled[0].expect == {"min_chars": 20}


def test_a_missing_file_is_not_an_error(tmp_path):
    loaded = load_annotations(tmp_path / "nope.jsonl")
    assert loaded.annotations == []
    assert not loaded


def test_one_broken_line_does_not_lose_the_others(tmp_path):
    """An annotation set is small and hand-edited. Losing it to one bad comma is a bad trade."""
    path = tmp_path / "expectations.jsonl"
    path.write_text(
        '{"input": "a", "expect": {"min_chars": 5}}\n'
        '{"input": "b", "expect": {"min_chars": 5}\n'  # unclosed brace
        '{"input": "c", "expect": {"min_chars": 5}}\n',
        encoding="utf-8",
    )
    loaded = load_annotations(path)

    assert len(loaded.filled) == 2
    assert len(loaded.skipped) == 1
    assert "line 2" in loaded.skipped[0]


def test_an_unknown_check_type_is_rejected_with_a_useful_message(tmp_path):
    path = tmp_path / "expectations.jsonl"
    path.write_text('{"input": "a", "expect": {"is_nice": true}}\n', encoding="utf-8")
    loaded = load_annotations(path)

    assert loaded.filled == []
    assert "is_nice" in loaded.skipped[0]


def test_a_template_placeholder_is_not_applied(tmp_path):
    """'expect: {}' means 'not filled in yet', not 'assert nothing'."""
    path = tmp_path / "expectations.jsonl"
    path.write_text('{"input": "a", "expect": {}, "note": "TODO"}\n', encoding="utf-8")
    loaded = load_annotations(path)

    assert len(loaded.annotations) == 1
    assert loaded.filled == []
    assert not loaded  # bool() is False when nothing is actually filled in


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


def test_matching_folds_case_space_and_punctuation():
    assert fold_input("  修改手机号。 ") == fold_input("修改手机号")
    assert fold_input("API Key 轮换") == fold_input("apikey轮换")


def test_lookup_matches_a_rephrased_input():
    loaded = AnnotationSet(annotations=[Annotation(input="修改手机号", expect={"min_chars": 20})])
    assert loaded.lookup(fold_input(" 修改手机号， ")) is not None
    assert loaded.lookup(fold_input("退款政策")) is None


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #


def test_human_checks_override_inferred_ones_of_the_same_type():
    inferred = [{"type": "not_fallback"}, {"type": "min_chars", "value": 8}]
    human = [{"type": "min_chars", "value": 20}]

    merged = merge_checks(inferred, human)
    assert {"type": "min_chars", "value": 20} in merged
    assert {"type": "min_chars", "value": 8} not in merged


def test_inferred_checks_survive_when_the_human_did_not_mention_them():
    """Writing 'contains' is not an opinion about truncation.

    Dropping the inferred length check here would make annotating a case a
    downgrade, which would be a strange thing for a feature to do.
    """
    inferred = [{"type": "not_fallback"}, {"type": "min_chars", "value": 14}]
    human = [{"type": "contains", "value": ["客服"]}]

    types = [check["type"] for check in merge_checks(inferred, human)]
    assert types == ["not_fallback", "min_chars", "contains"]


def test_not_fallback_always_comes_first():
    merged = merge_checks([], [{"type": "contains", "value": ["x"]}, {"type": "not_fallback"}])
    assert merged[0]["type"] == "not_fallback"


# --------------------------------------------------------------------------- #
# Survival across rebuilds -- the whole point
# --------------------------------------------------------------------------- #


def _trace(trace_id: str, text: str, output: str, **kwargs) -> Trace:
    return Trace(id=trace_id, input=text, output=output, **kwargs)


def test_an_annotation_follows_the_question_when_case_ids_shift():
    """The design property. Case ids are positional; annotations must not be."""
    weak = _trace("w1", "修改手机号", "在设置里改。", retried=True)
    filler = _trace("f1", "退款政策是什么", "退款申请提交后会在 14 个工作日内原路退回。")

    before = select_cases([weak, filler], expectations=_annotation_set("修改手机号"))
    # Add a much higher-scoring trace. It takes case-001, pushing everything down.
    louder = _trace(
        "x1", "系统崩了", "抱歉，我无法回答这个问题。", feedback="negative", retried=True
    )
    after = select_cases([weak, filler, louder], expectations=_annotation_set("修改手机号"))

    def find(cases, text):
        return next(c for c in cases if c["input"] == text)

    weak_before, weak_after = find(before.cases, "修改手机号"), find(after.cases, "修改手机号")
    assert weak_before["id"] != weak_after["id"], "the fixture must actually renumber the case"
    assert weak_before["annotation"]["applied"] and weak_after["annotation"]["applied"]
    assert "contains" in [check["type"] for check in weak_after["checks"]]


def _annotation_set(text: str, expect: dict | None = None) -> AnnotationSet:
    return AnnotationSet(
        annotations=[
            Annotation(
                input=text,
                expect=expect if expect is not None else {"min_chars": 20, "contains": ["手机号"]},
                note="test",
            )
        ]
    )


def test_build_case_records_that_an_annotation_was_applied():
    weak = _trace("w1", "修改手机号", "在设置里改。", retried=True)
    result = select_cases([weak], expectations=_annotation_set("修改手机号"))
    case = result.cases[0]

    assert case["annotation"]["applied"] is True
    assert case["annotation"]["note"] == "test"
    assert case["shape_source"] == "a human-written expectation"
    assert any("human expectation applied" in note for note in case["notes"])


def test_an_annotation_does_not_pretend_to_fix_the_verdict():
    """Honesty check: the failure still is not reproducible, and the report says so.

    An annotation gives the case something worth asserting. It does not make the
    original failure detectable, and pretending otherwise would be the exact
    over-claiming this field exists to avoid.

    Note the reference here is a *good* answer, which is what a behavioural
    failure looks like -- the call went fine, the user just asked again. That is
    precisely why no check on this text can reproach the failure, and why the
    verdict has to stay ``failure_not_reproduced`` even after annotating.

    (A failure seed whose reference is genuinely bad behaves differently: the
    human's checks fail on it and the verdict becomes ``ok``. Also correct, just
    a different situation.)
    """
    weak = _trace(
        "w1",
        "修改手机号",
        "修改绑定手机号需要先通过原手机号接收验证码，验证通过后在账户设置里填写新号码。",
        retried=True,
    )
    result = select_cases([weak], expectations=_annotation_set("修改手机号"))

    case = result.cases[0]
    assert case["self_check"]["passed"] is True, "the reference is a fine answer"
    assert case["self_check"]["verdict"] == "failure_not_reproduced"
    assert result.stats()["cases_with_annotation"] == 1
    assert result.stats()["cases_needing_annotation"] == 0


def test_an_annotation_can_turn_a_leaky_seed_into_a_real_gate():
    """The other branch: when the reference really is bad, the human's checks catch it.

    This is the payoff. A failure seed whose reference is a truncated answer used
    to assert almost nothing; with an expectation attached the checks fail on it,
    so the verdict flips to ``ok`` -- meaning the case now does reproduce a
    failure. Same mechanism, opposite outcome, and both are honest.
    """
    weak = _trace("w1", "修改手机号", "在设置里改。", retried=True)
    result = select_cases([weak], expectations=_annotation_set("修改手机号"))

    case = result.cases[0]
    assert case["self_check"]["passed"] is False
    assert case["self_check"]["verdict"] == "ok"
    assert "min_chars" in case["self_check"]["failed_checks"]


def test_a_weak_case_without_an_annotation_stays_on_the_todo_list():
    weak = _trace("w1", "修改手机号", "在设置里改。", retried=True)
    result = select_cases([weak])

    assert result.stats()["cases_needing_annotation"] == 1
    assert result.stats()["cases_with_annotation"] == 0


# --------------------------------------------------------------------------- #
# The committed sample
# --------------------------------------------------------------------------- #


def test_the_sample_annotation_file_covers_every_weak_case():
    loaded = load_traces(SAMPLE_LOG)
    result = select_cases(loaded.traces, expectations=load_annotations(SAMPLE_EXPECTATIONS))

    assert not load_annotations(SAMPLE_EXPECTATIONS).skipped, "the shipped file must parse cleanly"
    assert result.stats()["cases_with_annotation"] == 3
    assert result.stats()["cases_needing_annotation"] == 0
    # The 3 behavioural cases are still honestly reported as not reproducible.
    assert result.stats()["cases_with_weak_checks"] == 3


def test_the_sample_annotation_changes_the_case_set():
    """Without it, an answer that misses the point of the question still passes."""
    loaded = load_traces(SAMPLE_LOG)
    bare = select_cases(loaded.traces)
    annotated = select_cases(
        loaded.traces, expectations=load_annotations(SAMPLE_EXPECTATIONS)
    )

    def checks_for(result, text):
        case = next(c for c in result.cases if c["input"] == text)
        return [check["type"] for check in case["checks"]]

    assert checks_for(bare, "修改手机号") == ["not_fallback", "min_chars"]
    assert checks_for(annotated, "修改手机号") == ["not_fallback", "min_chars", "contains"]


# --------------------------------------------------------------------------- #
# The template
# --------------------------------------------------------------------------- #


def test_the_template_is_parseable_and_lists_only_the_cases_handed_to_it():
    loaded = load_traces(SAMPLE_LOG)
    bare = select_cases(loaded.traces)
    text = render_template(bare.cases_needing_annotation)

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "template.jsonl"
        path.write_text(text, encoding="utf-8")
        parsed = load_annotations(path)

    assert not parsed.skipped, "every emitted line must be valid"
    assert len(parsed.annotations) == len(bare.cases_needing_annotation) == 3
    # Placeholders are deliberately empty: a pre-filled guess is exactly the
    # over-confidence this mechanism exists to avoid.
    assert parsed.filled == []


def test_build_writes_a_template_only_when_there_is_no_expectations_file(tmp_path):
    log = tmp_path / "traces.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(
                {"input": "修改手机号", "output": "在设置里改。", "retried": True},
                ensure_ascii=False,
            )
            for _ in range(1)
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "evalset"

    assert main(["build", str(log), "-o", str(out)]) == 0
    template = out / "expectations.template.jsonl"
    assert template.exists()

    # Once a real expectations file is in place, the template is not rewritten.
    template.unlink()
    (out / "expectations.jsonl").write_text(
        '{"input": "修改手机号", "expect": {"min_chars": 20}}\n', encoding="utf-8"
    )
    assert main(["build", str(log), "-o", str(out)]) == 0
    assert not template.exists()
    assert read_lines(out / "cases.jsonl")[0]["annotation"]["applied"] is True


def test_a_broken_annotation_line_is_reported_but_does_not_fail_the_build(tmp_path):
    log = tmp_path / "traces.jsonl"
    log.write_text(
        json.dumps({"input": "修改手机号", "output": "在设置里改。", "retried": True}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "evalset"
    out.mkdir()
    (out / "expectations.jsonl").write_text("{not json\n", encoding="utf-8")

    assert main(["build", str(log), "-o", str(out)]) == 0
    assert read_lines(out / "cases.jsonl")[0]["annotation"]["applied"] is False
