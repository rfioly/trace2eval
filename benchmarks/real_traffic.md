# Real user traffic: the dedup matcher breaks on long inputs

Measured 2026-09-15 against [LMSYS-Chat-1M](https://huggingface.co/datasets/lmsys/lmsys-chat-1m) —
1,000,000 real conversations from 210k unique IPs, 154 languages, 24,499 of them Chinese.

This is the test the SQuAD benchmark could not be. That one came with paraphrase
labels, which is what made precision and recall computable, but its questions are
formulaic Wikipedia-passage questions and it is entirely English. Real traffic is
neither.

**Headline: the tool is badly wrong on real inputs, and it does so silently.**

## What was measured

Independent questions only: `turn == 1`, so each row is a single user message
rather than a conversation history. A multi-turn row's input is everything up to
that point, which is not an independent question and would flatter the matcher by
handing it near-identical strings to merge.

Random-pair similarity is the measure to trust. Two accidentally chosen questions
are almost never the same question, so anything scoring above the threshold is
close to a false positive. (Not exactly — popular questions do get asked twice —
so this is an upper bound, and the bias points in the honest direction.)

| corpus | questions | random pairs ≥ 0.60 | largest cluster at 0.60 |
| --- | --- | --- | --- |
| all languages | 9,236 | **35.1%** | **8,605 = 93% of the corpus** |
| Chinese only | 3,291 | 0.10% | 180 = 5.5% |
| SQuAD, for reference | 1,062 | 0.4% | 425 = 40% |

One cluster holding 93% of the questions is not a degradation. It is total
failure, and `build` reports it as a successful run.

## The cause is input length

The overlap coefficient divides by `min(|A|, |B|)`. Long texts are mostly made of
common bigrams, so two unrelated long inputs share a large fraction of the shorter
one. Bucketing 30,000 random pairs by the length of the shorter side confirms it:

| shorter side (chars) | pairs | median overlap | FP at 0.60 | median jaccard | FP at 0.60 (jaccard) |
| --- | --- | --- | --- | --- | --- |
| 0–40 | 12,309 | 0.304 | 0.216 | 0.061 | **0.0000** |
| 40–100 | 10,492 | 0.478 | 0.356 | 0.160 | 0.0010 |
| 100–250 | 4,663 | 0.573 | 0.437 | 0.233 | 0.0004 |
| 250–600 | 1,852 | 0.679 | **0.688** | 0.361 | 0.0286 |
| 600–1500 | 660 | 0.680 | **0.752** | 0.436 | 0.0061 |

Monotonic from 22% to 75%. The Chinese corpus shows the same curve; it only looks
healthy because its questions are short (29,151 of 29,999 pairs fall in the first
bucket). The same curve is there — 32.7% in the 250–600 bucket.

The all-language corpus has a p90 of 1,241 characters. Real logs contain long
inputs: pasted documents, retrieval context, code. **This is not an edge case.**

## Jaccard is not the fix, and this was checked rather than assumed

Jaccard's false-positive rate at 0.60 is between 0 and 4% in every bucket, against
22% to 75% for the overlap coefficient. It looks like the obvious answer.

It is not. `DECISIONS.md` chooses the overlap coefficient for one specific reason:

```
我想了解退款政策  ~  退款政策  ~  你们的退款政策是什么
```

A short fragment is fully contained in a longer question, so overlap scores it
1.000 — correctly. Jaccard divides by the union, scores the same pair 0.333, and
splits it.

```
退款政策 vs 你们的退款政策是什么？   overlap=1.000 merge   jaccard=0.333 SPLIT
pairs inside the sample log's legitimate clusters:
  overlap keeps 9/11 = 82%
  jaccard keeps 4/11 = 36%
```

Jaccard shatters the exact case the current design exists to serve. The two
measures cannot both be satisfied here, so there is **no free fix**, and swapping
one for the other would trade a loud failure for a quiet one.

## Resolution: v0.5 switched the measure to Jaccard

This was first published as a documented defect with no fix, on the grounds that
the obvious alternative destroyed something the design needed. That reasoning was
right about the alternative and wrong about the conclusion.

**Jaccard is not the alternative that was feared.** Measured against labelled
paraphrase data, Jaccard beats the overlap coefficient on F1 as well:

| threshold | Jaccard F1 | overlap F1 |
| --- | --- | --- |
| 0.50 | 0.976 | 0.885 |
| 0.55 | 0.976 | 0.948 |
| 0.60 | **0.976** | 0.965 |
| 0.70 | 0.957 | 0.973 |

So the change is not precision bought with recall — it is better on both, with the
single exception of fragment matching.

The cost, stated plainly: on the sample log the measure keeps 4 of the 11 pairs
inside its legitimate clusters rather than 9, and the log goes from 33 clusters to
39. Six extra cases, out of 42 rows. The trade was accepted because **the two
mistakes are not equivalent** — a missed merge costs one redundant case, while a
false merge produces a case that silently stands for two unrelated questions.

### Approaches that were measured and rejected

| approach | outcome |
| --- | --- |
| Jaccard with a length-dependent hybrid (containment below a fragment ratio) | overall false-positive rate only falls from 0.34 to **0.19**; the sample log's legitimate clusters drop from 13/17 to **1/17**. The ratio is unstable at small sizes — 4 shingles against 19 is 0.21 — so short pairs kept landing in the containment branch. Dropped. |
| Raising the shingle size to 5, as the near-duplicate literature uses | labelled-set F1 falls monotonically: 0.976 at size 2, 0.905 at size 5, 0.867 at size 6. The sample log stops merging entirely at size 4. The literature's 5 is for *documents*; these are short questions. **Do not change this to match the convention.** |
| Raising the threshold | the sweep in `results.md` shows 0.60 is already the F1 optimum for Jaccard on labelled data, and it is flat from 0.50 to 0.65. |

## Reproducing

The analysis scripts are scratch rather than shipped. Roughly:

1. `hf download lmsys/lmsys-chat-1m --repo-type dataset` (gated; accept the terms)
2. Convert to JSONL with one record per assistant turn, where `input` is the
   cumulative history up to the user message — **including the assistant replies**,
   which is what an app actually sends.
3. Keep `turn == 1` rows, deduplicate on normalised text, sample random pairs,
   `shingles` + `overlap_coefficient` from `trace2eval.select`.

Step 2 has a trap worth naming: an early version accumulated only the user
messages. The resulting inputs were a few dozen tokens long, which is what gave
it away — an app sends the assistant replies back too.
