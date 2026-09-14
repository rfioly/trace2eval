#!/usr/bin/env python3
"""Fetch the benchmark data.

Uses the GitHub Git Blobs API rather than ``raw.githubusercontent.com``: in the
environment this was developed in, raw is blocked while api.github.com answers.
Blobs are served base64-encoded and are capped at 100 MB, which is far more than
these files need.

Nothing fetched here is committed. The dataset carries its own licence and is not
the author's to redistribute; only the aggregate results in ``results.md`` are.

    python benchmarks/fetch_data.py --out _data
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import urllib.request

REPO = "nusnlp/paraphrasing-squad"
FILES = (
    "datasets/dev_orig.json",
    "datasets/dev_para.json",
    "datasets/adv_orig.json",
    "datasets/adv_para.json",
)


def api(path: str) -> dict:
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"User-Agent": "trace2eval-benchmarks", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="_data", type=pathlib.Path)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    info = api(f"/repos/{REPO}")
    tree = api(f"/repos/{REPO}/git/trees/{info['default_branch']}?recursive=1")
    blobs = {entry["path"]: entry["sha"] for entry in tree["tree"] if entry["type"] == "blob"}

    for path in FILES:
        sha = blobs.get(path)
        if sha is None:
            print(f"missing in {REPO}: {path}")
            return 1
        payload = api(f"/repos/{REPO}/git/blobs/{sha}")
        raw = base64.b64decode(payload["content"])
        target = args.out / f"{REPO.split('/')[-1]}__{path.split('/')[-1]}"
        target.write_bytes(raw)
        print(f"wrote {target}  ({len(raw)} bytes)")

    print(f"\nNow run:  python benchmarks/dedup_eval.py --data-dir {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
