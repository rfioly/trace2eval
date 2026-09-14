"""Sanity-check the pairing before trusting any number derived from it.

If dev_orig and dev_para are not actually aligned by qa id, every "positive"
pair is really a random pair, recall collapses to zero for reasons that have
nothing to do with the threshold, and the whole evaluation is worthless. Check
the pairing by eye first.
"""

import json
import pathlib
import random
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from trace2eval.select import normalise, shingle_overlap  # noqa: E402

DATA = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "_data")


def index(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for article in payload["data"]:
        for paragraph in article["paragraphs"]:
            for qa in paragraph["qas"]:
                out[qa["id"]] = qa["question"]
    return out


orig = index(DATA / "paraphrasing-squad__dev_orig.json")
para = index(DATA / "paraphrasing-squad__dev_para.json")

print(f"orig entries: {len(orig)}   para entries: {len(para)}")
print(f"shared ids  : {len(set(orig) & set(para))}")
print(f"orig only   : {len(set(orig) - set(para))}")
print(f"para only   : {len(set(para) - set(orig))}")
print()

print("=== 10 randomly sampled (orig, para) pairs ===")
rng = random.Random(7)
sample = rng.sample(sorted(set(orig) & set(para)), 10)
for qa_id in sample:
    a, b = orig[qa_id], para[qa_id]
    print(f"  sim={shingle_overlap(a, b):.3f}")
    print(f"    orig: {a}")
    print(f"    para: {b}")

print()
print("=== 10 random pairs of *different* ids (should look unrelated) ===")
ids = sorted(set(orig) & set(para))
for _ in range(10):
    i, j = rng.sample(ids, 2)
    print(f"  sim={shingle_overlap(orig[i], orig[j]):.3f}  {orig[i]}   ||   {orig[j]}")

print()
print("=== same-text check: are orig questions ever identical to each other? ===")
seen = {}
dupes = 0
for qa_id, text in orig.items():
    key = normalise(text)
    if key in seen:
        dupes += 1
    else:
        seen[key] = qa_id
print(f"  duplicate question texts among originals: {dupes} / {len(orig)}")

print()
print("=== distribution of orig->para similarity (all pairs) ===")
if set(orig) & set(para):
    limits = 4000
    ids_eval = sorted(set(orig) & set(para))
    if len(ids_eval) > limits:
        ids_eval = rng.sample(ids_eval, limits)
    scores = [shingle_overlap(orig[i], para[i]) for i in ids_eval]
    scores.sort()
    n = len(scores)
    print(f"  n={n}  min={scores[0]:.3f}  p25={scores[n//4]:.3f}  median={statistics.median(scores):.3f}"
          f"  p75={scores[3*n//4]:.3f}  max={scores[-1]:.3f}")
    for cut in (0.3, 0.4, 0.5, 0.6, 0.7):
        share = sum(1 for s in scores if s >= cut) / n
        print(f"  share of true pairs scoring >= {cut}: {share:.3f}")

print()
print("=== baseline: distribution of originals vs a random other original ===")
scores = []
for _ in range(4000):
    i, j = rng.sample(ids, 2)
    scores.append(shingle_overlap(orig[i], orig[j]))
scores.sort()
n = len(scores)
print(f"  n={n}  median={statistics.median(scores):.3f}  p75={scores[3*n//4]:.3f}  max={scores[-1]:.3f}")
for cut in (0.3, 0.4, 0.5, 0.6, 0.7):
    share = sum(1 for s in scores if s >= cut) / n
    print(f"  share of random pairs scoring >= {cut}: {share:.3f}")
