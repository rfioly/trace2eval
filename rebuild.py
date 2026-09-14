"""Rebuild every committed artefact at the new version, without a BOM.

`Out-File -Encoding utf8` on PowerShell 5.1 writes UTF-8 *with* a byte order
mark, which would put a stray BOM at the top of a committed markdown file. This
writes plain UTF-8 and LF.
"""

import pathlib
import subprocess
import sys

PROJECT = pathlib.Path(__file__).resolve().parent
PY = sys.executable
DATA = PROJECT.parent / "_data"


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "trace2eval", *args],
        cwd=str(PROJECT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def build(out_dir: str) -> None:
    result = run("build", "examples/sample_traces.jsonl", "-o", out_dir)
    if result.returncode != 0:
        raise SystemExit(f"build failed:\n{result.stdout}\n{result.stderr}")


print("building evalset ...")
build("evalset")

print("scoring the example runs ...")
for outputs, target in (
    ("examples/outputs_baseline.jsonl", "runs/baseline.json"),
    ("examples/outputs_regressed.jsonl", "runs/current.json"),
):
    result = run("run", "--cases", "evalset/cases.jsonl", "--outputs", outputs, "-o", target)
    print(f"  {target}: {result.stdout.strip().splitlines()[-1] if result.stdout else '?'}")

print("regenerating benchmarks/results.md ...")
bench = subprocess.run(
    [PY, "benchmarks/dedup_eval.py", "--data-dir", str(DATA)],
    cwd=str(PROJECT),
    capture_output=True,
    text=True,
    encoding="utf-8",
)
if bench.returncode != 0:
    raise SystemExit(f"benchmark failed:\n{bench.stdout}\n{bench.stderr}")
text = bench.stdout
(PROJECT / "benchmarks" / "results.md").write_text(text, encoding="utf-8", newline="\n")
print(f"  {len(text.encode('utf-8'))} bytes")

print("checking for BOMs and CRLF in committed text artefacts ...")
targets = [
    PROJECT / "benchmarks" / "results.md",
    PROJECT / "evalset" / "cases.jsonl",
    PROJECT / "evalset" / "report.md",
]
for path in targets:
    raw = path.read_bytes()
    print(f"  {path.relative_to(PROJECT)}: bom={raw[:3] == b'\xef\xbb\xbf'} crlf={b'\r\n' in raw}")
