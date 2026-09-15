"""Human-supplied expected answers.

The gap this closes: a case whose failure was *behavioural* -- the user asked
again, the down-vote landed, the call was just slow -- has a perfectly acceptable
answer attached to it. No deterministic check on that text can reproduce the
failure, because the failure was never in the text.

The tool can detect those cases and say so (that is what the
``failure_not_reproduced`` verdict is for). It cannot invent the missing
expectation. A person has to write down what the answer should have looked like.

The design constraint that matters is **survival**. Generated case ids are not
stable -- they are assigned in descending score order, so one new trace can
renumber everything. An annotation keyed on ``case-011`` would silently detach
itself the next time the case set is rebuilt, and a detached annotation is worse
than no annotation: the case would quietly go back to being weak while still
looking annotated.

So annotations are keyed on the *question*, not on the case id:

    {"input": "修改手机号", "expect": {"min_chars": 20, "contains": ["验证码"]}}

``input`` is matched after the same folding used for clustering (case, whitespace
and punctuation stripped), so the question can be rephrased in the file without
breaking the link. ``build`` writes a template listing every case that needs one,
so filling this in is closer to editing a form than writing a file from scratch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Check types a human annotation may set. Deliberately the same vocabulary a
#: trace's own ``expect`` block uses, so there is one thing to learn rather than
#: two -- and so ``_checks_from_expect`` can be reused rather than reimplemented.
ANNOTATABLE_CHECKS = frozenset(
    {"not_fallback", "min_chars", "max_chars", "regex", "contains", "not_contains", "json"}
)


@dataclass
class Annotation:
    """One human-written expectation."""

    input: str
    expect: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    line_number: int = 0

    def is_empty(self) -> bool:
        """True when the entry is still the unfilled template placeholder."""
        return not self.expect


@dataclass
class AnnotationSet:
    """Every annotation found in one file, plus what was skipped loading it."""

    annotations: list[Annotation] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    path: Path | None = None

    def __bool__(self) -> bool:
        return any(not a.is_empty() for a in self.annotations)

    @property
    def filled(self) -> list[Annotation]:
        return [a for a in self.annotations if not a.is_empty()]

    def lookup(self, key: str) -> Annotation | None:
        """The annotation for ``key`` (an already-folded input string), if any."""
        for annotation in self.filled:
            if _fold(annotation.input) == key:
                return annotation
        return None


def fold_input(text: str) -> str:
    """Fold an input for matching purposes.

    Kept local rather than imported from :mod:`trace2eval.select` so this module
    stays free of the selection machinery -- annotations are loaded before
    selection runs, and a cycle here would be easy to introduce and annoying to
    find.
    """
    keep = []
    for char in text.strip().lower():
        if char.isspace() or char in "，。！？、；：,.!?;:\"'“”‘’()（）[]【】":
            continue
        keep.append(char)
    return "".join(keep)


#: Same function the lookup uses on annotation inputs. Exported so callers
#: cannot accidentally match against a differently-folded key.
_fold = fold_input


def parse_annotation(payload: dict[str, Any], line_number: int) -> Annotation:
    raw_expect = payload.get("expect")
    expect = raw_expect if isinstance(raw_expect, dict) else {}
    unknown = set(expect) - ANNOTATABLE_CHECKS
    if unknown:
        raise ValueError(
            f"unknown check type(s) {sorted(unknown)}; "
            f"allowed: {sorted(ANNOTATABLE_CHECKS)}"
        )
    return Annotation(
        input=str(payload.get("input", "")).strip(),
        expect=expect,
        note=str(payload.get("note", "")).strip(),
        line_number=line_number,
    )


def load_annotations(path: Path) -> AnnotationSet:
    """Load an expectations file. Missing file is not an error -- it means none.

    A malformed line is collected rather than raised. Losing every annotation to
    one bad comma would be a bad trade: an annotation set is small, hand-edited,
    and the person editing it is the person who most needs to know which line
    they broke.
    """
    result = AnnotationSet(path=path)
    if not path.exists():
        return result

    # split("\n"), not str.splitlines(). splitlines() breaks on every Unicode
    # line boundary, including U+2028 and U+2029 -- and json.dumps with
    # ensure_ascii=False writes those through unescaped. A note containing one
    # would split a record mid-string and lose the annotation. Found on real
    # chat data, where user text carries them.
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("//"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            result.skipped.append(f"line {number}: not valid JSON ({exc.msg})")
            continue
        if not isinstance(payload, dict):
            result.skipped.append(f"line {number}: expected a JSON object")
            continue
        try:
            annotation = parse_annotation(payload, number)
        except ValueError as exc:
            result.skipped.append(f"line {number}: {exc}")
            continue
        if not annotation.input:
            result.skipped.append(f"line {number}: no 'input' field")
            continue
        result.annotations.append(annotation)

    return result


def merge_checks(
    inferred: list[dict[str, Any]], human: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Overlay human checks on inferred ones, type by type.

    Human wins wherever the two overlap -- it is evidence, the inferred value was
    a guess. Where they do not overlap the inferred check is kept, because a
    human writing ``contains`` has no opinion about truncation and silently
    dropping the length check would make the annotation a downgrade.

    ``not_fallback`` is always first, whatever either side said.
    """
    by_type: dict[str, dict[str, Any]] = {check["type"]: check for check in inferred}
    for check in human:
        by_type[check["type"]] = check
    ordered = [by_type.pop("not_fallback")] if "not_fallback" in by_type else []
    ordered.extend(by_type.values())
    return ordered


def render_template(cases: list[dict[str, Any]]) -> str:
    """A fill-in-the-blanks file for every case whose checks cannot catch it.

    Emitted only when no expectations file exists yet, so a real one is never
    overwritten. The ``expect`` object is left empty on purpose: a pre-filled
    guess would be exactly the kind of over-confidence this whole mechanism
    exists to avoid.
    """
    lines = [
        "// trace2eval expectations -- one JSON object per line, '//' comments allowed.",
        "//",
        "// Why these cases are listed: their failure happened around the call, not in",
        "// the answer, so no check on the output text can reproduce it. Write down what",
        "// a correct answer looks like and they become real gates.",
        "//",
        "// Available keys inside 'expect' (same vocabulary as a trace's own expect block):",
        "//   min_chars, max_chars, contains, not_contains, regex, json, not_fallback",
        "//",
        "// When you are done, save this file as expectations.jsonl and rebuild.",
        "// Matching is on 'input', folded for case/space/punctuation -- so edit the",
        "// wording of 'note' freely, but leave 'input' alone.",
        "",
    ]
    for case in cases:
        payload = {
            "input": case["input"],
            "expect": {},
            "note": (
                "TODO: fill in expect. "
                f"({', '.join(signal['name'] for signal in case['signals'])})"
            ),
        }
        lines.append(json.dumps(payload, ensure_ascii=False))
    lines.append("")
    return "\n".join(lines)
