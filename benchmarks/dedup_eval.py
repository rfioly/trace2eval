#!/usr/bin/env python3
"""Measure the dedup threshold against labelled paraphrase data.

Why this exists
---------------
The 0.6 default was tuned on a 42-row sample log that the author of this tool
wrote himself, to exercise the features he had just built. That is circular
evidence, and it is the weakest claim the project makes.

This harness replaces it with data nobody here authored, from
``nusnlp/paraphrasing-squad``: SQuAD dev questions (crowd-written) and their
paraphrases, plus an adversarial set built so that string matching gives the
wrong answer. Labels come from the data's construction, not from judgement.

A mistake this file now guards against
--------------------------------------
The two files carry qa ``id`` fields, so the obvious thing to do is join on them.
**They are not aligned by id.** The ids were regenerated, and joining on them
produces pairs of unrelated questions -- while still producing a complete,
plausible-looking table of precision and recall numbers that mean nothing.

The giveaway is that the similarity distribution of the supposed "positive"
pairs is indistinguishable from that of randomly drawn pairs. So this harness
checks alignment before it reports anything, and refuses to run if the labelled
positives are not measurably closer to each other than chance. A silent
misalignment is far more dangerous than a crash.

What it does NOT establish
--------------------------
English, not Chinese. Paraphrases are machine-generated and mostly small edits,
so the positive set is easier than real paraphrase. SQuAD questions are
Wikipedia-passage questions, not production traffic. All three flatter the
numbers. The signal layer -- what decides which traces become cases at all -- is
not exercised at all, because no public dataset pairs real text with real
feedback. Read ``benchmarks/README.md`` before quoting anything here.

Usage
-----
    python benchmarks/dedup_eval.py --data-dir _data
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from trace2eval.schema import Trace  # noqa: E402
from trace2eval.select import (  # noqa: E402
    DEFAULT_DEDUP_THRESHOLD,
    ScoredTrace,
    cluster_scored,
    normalise,
    shingle_overlap,
    shingles,
)

THRESHOLDS = [round(0.30 + 0.05 * step, 2) for step in range(13)]  # 0.30 .. 0.90
CLUSTER_THRESHOLDS = (0.4, 0.5, 0.6, 0.7, 0.8, 0.9)


@dataclass
class Row:
    text: str
    paragraph: str   # the passage it is about; used to build hard negatives
    index: int       # position in the file -- the thing that actually aligns


def load_rows(path: pathlib.Path) -> list[Row]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[Row] = []
    for article in payload["data"]:
        for p_index, paragraph in enumerate(article["paragraphs"]):
            key = f"{article.get('title', '')}#{p_index}"
            for qa in paragraph["qas"]:
                rows.append(Row(text=qa["question"], paragraph=key, index=len(rows)))
    return rows


def align_by_id(path_a: pathlib.Path, path_b: pathlib.Path) -> float:
    """Median similarity if the two files were joined on qa id. Used as a control."""
    def index_by_id(path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        out = {}
        for article in payload["data"]:
            for paragraph in article["paragraphs"]:
                for qa in paragraph["qas"]:
                    out[qa["id"]] = qa["question"]
        return out

    left, right = index_by_id(path_a), index_by_id(path_b)
    shared = sorted(set(left) & set(right))
    if not shared:
        return 0.0
    sample = random.Random(0).sample(shared, min(400, len(shared)))
    return statistics.median(shingle_overlap(left[k], right[k]) for k in sample)


def check_alignment(positives: list[tuple[str, str]], negatives: list[tuple[str, str]]) -> str:
    """Refuse to report on data whose labels do not hold up.

    Returns a warning string, or raises when the labels are clearly broken.
    """
    pos = [shingle_overlap(a, b) for a, b in positives]
    neg = [shingle_overlap(a, b) for a, b in negatives]
    pos_median = statistics.median(pos)
    neg_median = statistics.median(neg)

    if pos_median <= neg_median:
        raise SystemExit(
            "REFUSING TO REPORT: labelled 'positive' pairs are no more similar than "
            f"random ones (median {pos_median:.3f} vs {neg_median:.3f}). The labels do "
            "not hold up, so any precision/recall computed from them would be noise. "
            "Check how the files are aligned before trusting this data."
        )
    return (
        f"alignment check: positives median {pos_median:.3f} vs negatives "
        f"{neg_median:.3f} (gap {pos_median - neg_median:+.3f})"
    )


def build_pairs(rows_orig: list[Row], rows_para: list[Row], rows_adv: list[tuple[Row, Row]],
                sample: int, seed: int):
    rng = random.Random(seed)

    positives = [(rows_orig[i].text, rows_para[i].text) for i in range(len(rows_orig))]
    rng.shuffle(positives)

    # Easy negatives: two different questions, possibly about different passages.
    easy = []
    for _ in range(sample * 2):
        i, j = rng.sample(range(len(rows_orig)), 2)
        if rows_orig[i].paragraph != rows_orig[j].paragraph:
            easy.append((rows_orig[i].text, rows_orig[j].text))
        if len(easy) >= sample:
            break

    # Hard negatives A: different questions about the *same* passage. This is the
    # shape of most real traffic -- people asking about one thing in many ways.
    by_paragraph: dict[str, list[int]] = defaultdict(list)
    for row in rows_orig:
        by_paragraph[row.paragraph].append(row.index)
    hard = []
    keys = [k for k, v in by_paragraph.items() if len(v) > 1]
    while len(hard) < sample and keys:
        members = by_paragraph[rng.choice(keys)]
        i, j = rng.sample(members, 2)
        hard.append((rows_orig[i].text, rows_orig[j].text))

    # Hard negatives B: the adversarial pairs. These are built to be lexically
    # very similar while the meaning is deliberately changed, so a matcher that
    # only sees characters is at its worst here.
    adversarial = [(a.text, b.text) for a, b in rows_adv]

    return positives, easy, hard, adversarial


def curve(positives, classes, max_pairs, seed):
    rng = random.Random(seed)

    def scores(pairs):
        pairs = list(pairs)
        if len(pairs) > max_pairs:
            pairs = rng.sample(pairs, max_pairs)
        return [shingle_overlap(a, b) for a, b in pairs]

    pos = scores(positives)
    neg = {name: scores(pairs) for name, pairs in classes.items()}

    rows = []
    for threshold in THRESHOLDS:
        tp = sum(1 for s in pos if s >= threshold)
        recall = tp / len(pos) if pos else 0.0
        row = {"threshold": threshold, "recall": recall}
        for name, values in neg.items():
            fp = sum(1 for s in values if s >= threshold)
            precision = tp / (tp + fp) if (tp + fp) else 0.0
            row[f"fp_{name}"] = fp / len(values) if values else 0.0
            row[f"f1_{name}"] = (
                2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
            )
        rows.append(row)
    return rows, pos, neg


def evaluate_clustering(texts, threshold, diameter_sample, seed):
    """Run the real clusterer over a set of questions with few genuine duplicates.

    The point is not recall here -- this pool is almost all distinct questions --
    but how much a single-linkage chain fuses things that should never have met.

    Not every merge is an error, though: the pool does contain byte-identical
    questions, and joining those is correct. So the metric is purity by exact
    text rather than a raw merge size. An earlier draft called every merge a
    false one and overstated the damage; the examples caught it.
    """
    traces = [
        Trace(id=f"q{i:05d}", input=text, output="x", feedback=None, retried=False)
        for i, text in enumerate(texts)
    ]
    scored = [ScoredTrace(trace=t, signals=[], score=0.0) for t in traces]
    fingerprints = {t.id: shingles(normalise(t.input)) for t in traces}
    clusters = cluster_scored(scored, fingerprints, threshold=threshold)

    keys = [normalise(t) for t in texts]
    distinct = len(set(keys))
    # Pairs that are genuinely the same question and therefore SHOULD be merged.
    same_text_pairs = sum(
        count * (count - 1) // 2 for count in Counter(keys).values()
    )

    multi = [c for c in clusters if c.size > 1]
    rng = random.Random(seed)
    diameters = []
    worst_examples = []
    for cluster in (rng.sample(multi, min(diameter_sample, len(multi))) if multi else []):
        members = [int(m[1:]) for m in cluster.member_ids]
        worst = None
        pair = (members[0], members[0])
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                value = shingle_overlap(texts[members[i]], texts[members[j]])
                # Seeded with None rather than 1.0: when the most distant pair
                # scores exactly 1.0 the update never fires and the placeholder
                # (i, i) survives into the report as a pair of one question,
                # which reads as a contradiction rather than as a bug.
                if worst is None or value < worst:
                    worst, pair = value, (members[i], members[j])
        if worst is None:
            continue
        diameters.append(worst)
        if len(worst_examples) < 6:
            worst_examples.append(
                {
                    "size": cluster.size,
                    "worst": round(worst, 3),
                    "left": texts[pair[0]],
                    "right": texts[pair[1]],
                    "cross_text": keys[pair[0]] != keys[pair[1]],
                }
            )

    # Purity by exact normalised text: how much of each cluster is material that
    # is not the cluster's own dominant question.
    purity = (
        sum(max(Counter(keys[int(m[1:])] for m in c.member_ids).values()) for c in clusters)
        / len(texts)
    )

    sizes = sorted((c.size for c in clusters), reverse=True)
    merged = sum(c.size for c in multi)
    return {
        "threshold": threshold,
        "clusters": len(clusters),
        "singletons": sum(1 for c in clusters if c.size == 1),
        "largest": sizes[0] if sizes else 0,
        "merged_into_clusters": merged / len(texts),
        "purity": purity,
        "unrelated_pulled_in": 1.0 - purity,
        "correct_merge_available": same_text_pairs,
        "median_diameter": statistics.median(diameters) if diameters else 1.0,
        "examples": worst_examples,
        "multi": len(multi),
        "distinct": distinct,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="_data", type=pathlib.Path)
    parser.add_argument("--sample", default=4000, type=int)
    parser.add_argument("--seed", default=20260914, type=int)
    args = parser.parse_args()

    dev_orig = load_rows(args.data_dir / "paraphrasing-squad__dev_orig.json")
    dev_para = load_rows(args.data_dir / "paraphrasing-squad__dev_para.json")
    adv_orig = load_rows(args.data_dir / "paraphrasing-squad__adv_orig.json")
    adv_para = load_rows(args.data_dir / "paraphrasing-squad__adv_para.json")

    if len(dev_orig) != len(dev_para):
        raise SystemExit(f"row counts differ: {len(dev_orig)} vs {len(dev_para)}")

    print("# Dedup threshold evaluation\n")
    print(f"source: nusnlp/paraphrasing-squad (SQuAD dev questions + paraphrases)")
    print(f"dev rows: {len(dev_orig)}   adv rows: {len(adv_orig)}\n")

    print("## Alignment\n")
    by_id = align_by_id(
        args.data_dir / "paraphrasing-squad__dev_orig.json",
        args.data_dir / "paraphrasing-squad__dev_para.json",
    )
    print(f"joining the two files on qa `id` gives median similarity {by_id:.3f}")
    print("-- which is why this harness joins on position instead:\n")
    rng = random.Random(3)
    for k in rng.sample(range(len(dev_orig)), 4):
        a, b = dev_orig[k].text, dev_para[k].text
        print(f"- sim={shingle_overlap(a, b):.3f}")
        print(f"  - {a}")
        print(f"  - {b}")
    print()

    positives, easy, hard, adversarial = build_pairs(
        dev_orig, dev_para, list(zip(adv_orig, adv_para)), args.sample, args.seed
    )
    warning = check_alignment(positives, easy)
    print(warning)
    print(f"positives {len(positives)}  easy negatives {len(easy)}  "
          f"same-passage negatives {len(hard)}  adversarial negatives {len(adversarial)}\n")

    rows, pos, neg = curve(
        positives,
        {"easy": easy, "same_passage": hard, "adversarial": adversarial},
        args.sample,
        args.seed,
    )

    print("## Pairwise similarity vs threshold\n")
    print("| threshold | recall | FP easy | FP same-passage | FP adversarial | F1 (worst) |")
    print("| --- | --- | --- | --- | --- | --- |")
    for row in rows:
        worst_f1 = min(row["f1_easy"], row["f1_same_passage"], row["f1_adversarial"])
        mark = " **<- default**" if abs(row["threshold"] - DEFAULT_DEDUP_THRESHOLD) < 1e-9 else ""
        print(
            f"| {row['threshold']:.2f}{mark} | {row['recall']:.3f} | {row['fp_easy']:.3f} | "
            f"{row['fp_same_passage']:.3f} | {row['fp_adversarial']:.3f} | {worst_f1:.3f} |"
        )

    default_row = next(r for r in rows if abs(r["threshold"] - DEFAULT_DEDUP_THRESHOLD) < 1e-9)
    best = max(rows, key=lambda r: min(r["f1_easy"], r["f1_same_passage"], r["f1_adversarial"]))
    print(
        f"\nbest threshold on the worst negative class: **{best['threshold']:.2f}**; "
        f"default {DEFAULT_DEDUP_THRESHOLD:.2f}\n"
    )
    print(
        f"At the default: recall {default_row['recall']:.3f}, "
        f"false-positive rate on adversarial pairs {default_row['fp_adversarial']:.3f}\n"
    )

    cluster_texts = [r.text for r in dev_orig]
    baseline = evaluate_clustering(cluster_texts, 0.99, 1, args.seed)
    print("## Clustering questions that are mostly distinct\n")
    print(
        f"pool: {len(cluster_texts)} questions, {baseline['distinct']} distinct after folding. "
        f"There are only {baseline['correct_merge_available']} pairs that *should* merge, so "
        f"almost every merge below is damage.\n"
    )
    print("| threshold | clusters | singletons | largest | purity | unrelated pulled in | median diameter |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for threshold in CLUSTER_THRESHOLDS:
        result = evaluate_clustering(cluster_texts, threshold, 30, args.seed)
        mark = " **<- default**" if abs(threshold - DEFAULT_DEDUP_THRESHOLD) < 1e-9 else ""
        print(
            f"| {threshold:.2f}{mark} | {result['clusters']} | {result['singletons']} | "
            f"{result['largest']} | {result['purity']:.3f} | "
            f"{result['unrelated_pulled_in']:.3f} | {result['median_diameter']:.3f} |"
        )

    at_default = evaluate_clustering(cluster_texts, DEFAULT_DEDUP_THRESHOLD, 30, args.seed)
    if at_default["examples"]:
        print(f"\n### Worst merges at the default threshold {DEFAULT_DEDUP_THRESHOLD:.2f}\n")
        print("Two questions in one cluster whose own similarity is far below the threshold")
        print("-- i.e. they were chained together through other questions:\n")
        for example in at_default["examples"]:
            kind = "different questions" if example["cross_text"] else "the SAME question"
            print(f"- cluster of {example['size']}, most distant pair scores {example['worst']} "
                  f"({kind})")
            print(f"  - {example['left']}")
            print(f"  - {example['right']}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
