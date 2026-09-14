"""Command line interface.

Three commands, in the order you would actually use them:

    trace2eval build   # log  -> case set
    trace2eval run     # case set + outputs -> metrics
    trace2eval check   # metrics vs metrics -> pass/fail

``check`` exits non-zero on regression, which is the whole point: it is meant to
be the last line of a CI job, not a report someone reads on a dashboard an hour
later.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .report import build_report, render_metrics, render_violations
from .runner import (
    OutputFormatError,
    RunMetrics,
    compare_runs,
    load_outputs,
    run_cases,
)
from .schema import TraceFormatError, load_traces
from .annotations import load_annotations, render_template
from .select import (
    DEFAULT_DEDUP_THRESHOLD,
    MatcherSpecError,
    load_matcher,
    select_cases,
)

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_BAD_INPUT = 2


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" disables platform newline translation. Without it, Windows
    # writes CRLF and Linux writes LF, and the committed case set stops being
    # byte-identical across platforms -- which is exactly what the CI job asserts.
    path.write_text(content, encoding="utf-8", newline="\n")


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows


def cmd_build(args: argparse.Namespace) -> int:
    try:
        loaded = load_traces(args.traces)
    except TraceFormatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    if not loaded.traces:
        print("error: no usable traces found in the input file", file=sys.stderr)
        if loaded.skipped:
            for line_no, reason in loaded.skipped[:5]:
                print(f"  line {line_no}: {reason}", file=sys.stderr)
        return EXIT_BAD_INPUT

    out_dir = Path(args.out)

    expectations_path = (
        Path(args.expectations) if args.expectations else out_dir / "expectations.jsonl"
    )
    expectations = load_annotations(expectations_path)
    for problem in expectations.skipped:
        print(f"warning: {expectations_path.name}: {problem}", file=sys.stderr)

    matcher = None
    if args.similarity:
        try:
            matcher = load_matcher(args.similarity)
        except MatcherSpecError as exc:
            print(f"error: --similarity {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT

    result = select_cases(
        loaded.traces,
        max_cases=args.max_cases,
        min_score=args.min_score,
        dedup_threshold=args.dedup_threshold,
        matcher=matcher,
        expectations=expectations,
    )

    _write_jsonl(out_dir / "cases.jsonl", result.cases)
    # as_posix() so the report is byte-identical on Windows and Linux, which is
    # what lets CI diff the committed case set against a fresh build.
    _write_text(out_dir / "report.md", build_report(result, Path(args.traces).as_posix()))

    # Written only when there is no expectations file yet, so a real one is never
    # clobbered by a rebuild.
    template_path: Path | None = None
    if not expectations_path.exists() and result.cases_needing_annotation:
        template_path = expectations_path.with_name("expectations.template.jsonl")
        _write_text(template_path, render_template(result.cases_needing_annotation))

    stats = result.stats()
    print(f"read {stats['total_traces']} traces from {args.traces}")
    if loaded.skipped:
        print(f"  skipped {loaded.skipped_count} unreadable line(s)")
    if matcher is not None:
        print(
            "  custom similarity matcher: the n-gram index is bypassed, "
            "so this run compares against every cluster"
        )
    print(f"generated {stats['cases']} cases -> {out_dir / 'cases.jsonl'}")
    print(f"  {stats['dropped_as_duplicate']} collapsed as near-duplicates")
    print(f"  {stats['dropped_below_min_score']} below the minimum score")
    print(f"  {stats['dropped_beyond_limit']} beyond the case limit")
    if stats["cases_whose_reference_fails"]:
        print(
            f"  {stats['cases_whose_reference_fails']} trusted reference(s) fail their "
            f"own checks -- look at these before committing the set"
        )
    if stats["cases_with_annotation"]:
        print(f"  {stats['cases_with_annotation']} case(s) use a human-written expectation")
    if stats["cases_with_weak_checks"]:
        covered = stats["cases_with_annotation"]
        print(
            f"  {stats['cases_with_weak_checks']} case(s) carry checks that cannot "
            f"detect the failure they came from"
            + (f" ({covered} covered by an annotation)" if covered else "")
        )
    if stats["cases_needing_annotation"]:
        print(
            f"  {stats['cases_needing_annotation']} of those still need a human-written "
            f"expectation -> fill in {template_path or expectations_path}"
        )
    print(f"report -> {out_dir / 'report.md'}")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    cases_path = Path(args.cases)
    if not cases_path.exists():
        print(f"error: case file not found: {cases_path}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        cases = _read_jsonl(cases_path)
        outputs = load_outputs(args.outputs)
    except (OutputFormatError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    metrics = run_cases(cases, outputs)

    if args.out:
        payload = {"version": __version__, "metrics": metrics.to_dict()}
        _write_text(Path(args.out), json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    print(render_metrics(metrics))
    if args.out:
        print(f"\nwritten -> {args.out}")

    if metrics.failures and args.show_failures:
        print("\nFailures:")
        for failure in metrics.failures[:20]:
            detail = ", ".join(failure["failed_checks"]) or failure["reason"]
            print(f"  {failure['case_id']}: {detail}")
        if len(metrics.failures) > 20:
            print(f"  ... and {len(metrics.failures) - 20} more")

    if args.min_pass_rate is not None and metrics.pass_rate < args.min_pass_rate:
        print(
            f"\nFAIL: pass rate {metrics.pass_rate:.2%} is below the required "
            f"{args.min_pass_rate:.2%}",
            file=sys.stderr,
        )
        return EXIT_GATE_FAILED

    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    try:
        baseline_payload = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        current_payload = json.loads(Path(args.current).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read metrics file: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    baseline = RunMetrics.from_dict(baseline_payload.get("metrics", baseline_payload))
    current = RunMetrics.from_dict(current_payload.get("metrics", current_payload))

    print("baseline")
    print(render_metrics(baseline))
    print("\ncurrent")
    print(render_metrics(current))

    violations = compare_runs(baseline, current, tolerance_scale=args.tolerance_scale)

    print("\nregressions")
    print(render_violations(violations))

    if violations:
        print(f"\nFAIL: {len(violations)} regression(s) detected", file=sys.stderr)
        return EXIT_GATE_FAILED

    print("\nPASS: no regressions detected")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trace2eval",
        description=(
            "Turn production LLM traces into a regression eval set. "
            "Zero dependencies, no API keys, fully offline."
        ),
    )
    parser.add_argument("--version", action="version", version=f"trace2eval {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build",
        help="turn a JSONL trace log into a regression eval set",
        description=(
            "Score every trace, collapse near-duplicates, and emit the highest-value "
            "subset as a case set."
        ),
    )
    build.add_argument("traces", help="input JSONL trace log")
    build.add_argument("-o", "--out", default="evalset", help="output directory (default: evalset)")
    build.add_argument("--max-cases", type=int, default=50, help="cap on generated cases")
    build.add_argument(
        "--min-score",
        type=float,
        default=1.0,
        help="minimum signal weight for a trace to become a case",
    )
    build.add_argument(
        "--dedup-threshold",
        type=float,
        default=DEFAULT_DEDUP_THRESHOLD,
        help=(
            "overlap similarity above which two inputs are treated as the same "
            f"question (default: {DEFAULT_DEDUP_THRESHOLD})"
        ),
    )
    build.add_argument(
        "--similarity",
        default=None,
        metavar="MODULE:FUNCTION",
        help=(
            "swap in your own matcher, e.g. 'my_embeddings:cosine'. Must take two "
            "strings and return a score in [0, 1]. Disables the n-gram index, so "
            "clustering gets slower -- see trace2eval.matchers."
        ),
    )
    build.add_argument(
        "--expectations",
        default=None,
        metavar="PATH",
        help=(
            "human-written expected answers, one JSON object per line. Defaults to "
            "expectations.jsonl beside the case set. If the file does not exist, a "
            "fill-in template is written for the cases that need one."
        ),
    )
    build.set_defaults(func=cmd_build)

    run = subparsers.add_parser(
        "run",
        help="score a batch of outputs against a case set",
        description="Apply the deterministic checks in a case set to a batch of outputs.",
    )
    run.add_argument("--cases", required=True, help="cases.jsonl produced by build")
    run.add_argument("--outputs", required=True, help="JSONL of {id, output} records to score")
    run.add_argument("-o", "--out", help="write metrics JSON to this path")
    run.add_argument(
        "--min-pass-rate",
        type=float,
        default=None,
        help="exit non-zero if the pass rate falls below this value",
    )
    run.add_argument(
        "--show-failures",
        action="store_true",
        help="list which cases failed and on which checks",
    )
    run.set_defaults(func=cmd_run)

    check = subparsers.add_parser(
        "check",
        help="compare two metrics files and fail on regression",
        description=(
            "Compare a baseline metrics file against a current one. Exits 1 if any "
            "tracked metric moved the wrong way beyond its tolerance."
        ),
    )
    check.add_argument("--baseline", required=True, help="baseline metrics JSON")
    check.add_argument("--current", required=True, help="current metrics JSON")
    check.add_argument(
        "--tolerance-scale",
        type=float,
        default=1.0,
        help="multiply every tolerance; use <1 for a stricter gate",
    )
    check.set_defaults(func=cmd_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
