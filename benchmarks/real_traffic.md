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

## Options, none verified

1. **A length-aware threshold.** The false-positive rate climbs monotonically with
   length, so the threshold should too. Directly supported by the data above;
   it also means the single `--dedup-threshold` value stops having one meaning.
2. **Treat the asymmetric case as containment.** When one side is much shorter,
   require near-total containment (a high bar) instead of 0.60; when the lengths
   are comparable, use Jaccard. This is the one I would try first, because it
   separates the two situations the overlap coefficient currently conflates —
   but it is untested and the cut-off is a free parameter.
3. **Do nothing, but stop being silent.** Today a log full of long inputs produces
   one 93% cluster and says nothing. Whatever the threshold should be, this
   failure should not be invisible.

No code was changed for this finding. The matcher is the root of everything else
the tool claims, and swapping it for an unverified alternative would be the worst
possible trade.

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
