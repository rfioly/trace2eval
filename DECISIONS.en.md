**English** | [简体中文](DECISIONS.md)

---

# Design decisions

This file records *why*, not *what*. The code says what. These are the calls that
could reasonably have gone the other way, the evidence that settled them, and the
places where the evidence is thin.

---

## 1. The similarity measure is the overlap coefficient, not Jaccard

**The conventional choice is Jaccard. On this data it ranks the pairs backwards.**

Both pairs below are real rows from `examples/sample_traces.jsonl`, labelled by hand.

| Pair | Overlap | Jaccard | Same question? |
| --- | --- | --- | --- |
| `你们的退款政策是什么？` vs `退款政策` | 1.000 | 0.333 | **yes** |
| `你们的退款政策是什么？` vs `退款政策是怎样的` | 0.571 | 0.333 | **yes** |
| `修改手机号` vs `我要改手机号` | 0.750 | 0.500 | **yes** |
| `支持哪些登录方式` vs `支持哪些支付方式` | 0.571 | 0.400 | **no** |
| `登录不上` vs `支持哪些登录方式` | 0.000 | 0.111 | no |

Jaccard assigns the *different* pair (0.400) a higher score than either *same*
pair (0.333). No threshold on Jaccard can separate them — the ordering itself is
wrong. The cause is that Jaccard divides by the union, so it punishes every length
difference, and short user queries are almost always a fragment of a longer
phrasing. Overlap divides by the shorter set, which asks the question we actually
care about: does the shorter query sit inside the longer one?

**Honest caveat.** Overlap gets the ordering right but does *not* cleanly separate
the last two rows — the true pair and the false pair both score exactly 0.571.
Surface similarity alone cannot tell them apart. That is not a threshold-tuning
problem; it is a limit of character n-grams on short Chinese text. The threshold
handles it by favouring precision, and single-linkage clustering (below) recovers
the recall that precision costs.

Pinned by `tests/test_dedup.py::test_jaccard_ranks_the_false_pair_higher`, which
fails with an explanation if anyone swaps the measure back.

---

## 2. The threshold is 0.6, chosen by sweep

Sweeping `--dedup-threshold` over the sample log and inspecting every merged group
by hand:

| Threshold | Merged groups | Lines collapsed | Verdict |
| --- | --- | --- | --- |
| 0.50 | 6 | 14 | merges `支持哪些登录方式` with `支持哪些支付方式` — different questions |
| 0.55 | 4 | 11 | still merges the same false pair |
| **0.60** | **3** | **9** | no false pairs on the sample log |
| 0.65 | 2 | 5 | starts losing real duplicates |
| 0.70 | 2 | 5 | — |
| 0.80 | 1 | 3 | most duplicates survive; dedup is nearly a no-op |

0.6 is where precision comes out clean without collapsing the groups that matter.

**This is tuned to one 42-row sample and should be treated as a starting point,
not a constant of nature.** That is precisely why `--dedup-threshold` is exposed.
A real deployment should sweep it against hand-labelled pairs from its own traffic
— which is the same job this tool does for output quality, applied to the tool
itself.

---

## 3. Clustering runs over the whole log, not just the candidates

The obvious implementation scores every trace, keeps the ones above the minimum,
and deduplicates that shortlist. It produces a dishonest number.

If a question is asked 200 times and goes wrong once, shortlist-then-deduplicate
reports "1 occurrence". Cluster-the-log reports "200 occurrences", which is the
sentence you can actually put in a report: *this case stands for 200 of the calls
we recorded.*

So the pipeline is: score everything → cluster everything → promote the clusters
that contain something interesting. The representative of each cluster is its
highest-scoring member, because the ordering guarantees the worst row claims the
cluster first.

---

## 4. Single linkage, not centroid

A cluster is matched against **every** member, not just its representative.

Without this, chains break. `我想了解退款政策` scores 0.43 against the long
representative `你们的退款政策是什么？` but 0.60 against the shorter
`请问退款政策`, which is already in the cluster. Centroid comparison loses it;
single linkage keeps it.

The cost is that similarity is not transitive, so a large loose cluster can grow
by chaining. Bounded here by `min_shared` and by the fact that failure traces sort
first and therefore anchor clusters early.

---

## 5. Character bigrams, not word tokens

One code path has to serve both English and Chinese. Chinese has no whitespace
word boundaries, so word tokenisation needs a segmenter and a dictionary — a large
dependency for a tool whose main selling point is zero dependencies.

Character bigrams rather than trigrams because user queries are short: a trigram
over a five-character question leaves almost nothing to match on.

**Cost:** English paraphrase recall is weak. `"how do I get a refund"` and
`"refund process"` share character n-grams and will match; `"getting my money
back"` will not. Documented as a known limitation rather than papered over.

---

## 6. An empty output is a signal, not a bad row

The first implementation reused one "is this value present" helper for both sides
of the call. It treated `"output": ""` as a missing field and skipped the row.

That silently deleted exactly the rows the tool exists to find. An empty *input*
is an unusable row — there was nothing to answer. An empty *output* is a perfectly
readable row and one of the most valuable in the log.

The two cases are now handled separately (`_pick(..., allow_empty=True)`), and
`test_empty_output_is_a_trace_not_an_unreadable_line` guards it.

---

## 7. A reference output is not ground truth

This is the decision that shapes the case set.

Most selected traces are selected *because* something went wrong with them. So
inferring an expected shape from the output is usually inferring a shape from a
failure.

The rule:

- **Clean trace** (`negative_feedback`, `user_retried`, `empty_output`,
  `fallback_phrase`, `expected_shape_violated`, `output_much_shorter` — none
  fired): the output is a presumed-good reference. Infer `min_chars` at 40% of its
  length, and infer a JSON schema if it parses as an object.
- **Failure seed** (any of those fired): assert `not_fallback` and nothing else.
  The case exists to make sure this specific failure never ships twice.

`min_chars` is deliberately loose at 40%. It is meant to catch *truncation*, not
to punish rewording — a check that fails on a legitimate rephrase gets disabled
within a week.

### Why no `contains` is inferred from the reference

Tempting, and wrong. Auto-generating `contains: ["14 个工作日"]` from a reference
output overfits the test to one exact phrasing. Every future answer that is
correct but differently worded fails, the suite becomes noise, and the team stops
trusting it. Explicit `contains` checks are honoured when a trace carries them —
because then a human chose those words.

---

## 8. No LLM-as-judge

Actively rejected for v0.1, on three grounds:

1. **Cost and latency in CI.** A judge model on every pull request is a bill and a
   bottleneck, and it is the first thing switched off when it slows the build.
2. **Nondeterminism.** The tool's whole value proposition is that the same log
   produces the same case set. A judge breaks that.
3. **It is already solved.** DeepEval, Ragas and promptfoo all do judged metrics
   well. Rebuilding that here is the "another eval framework" trap this project
   exists to avoid.

`trace2eval` produces cases; it does not pretend to grade open-ended quality.
Deterministic checks are the boring half of evaluation, and the boring half is the
half that actually runs on every commit.

---

## 9. Regression tolerances are absolute points or relative percentages

Pass rate and format compliance are judged in **absolute percentage points** (2pp),
because "we lost 2% of passing cases" means the same thing at any baseline.

p95 latency and average cost are judged **relatively** (20%), because "20% slower"
and "20% more expensive" scale with whatever the numbers already are.

`--tolerance-scale` multiplies all of them, so a stricter or looser gate needs no
code change.

---

## What to do next

Ranked by how much they would improve the tool, not by how interesting they are.

1. **Give failure seeds a shape derived from clean answers.** Today a
   `fallback_phrase` seed only asserts "must not fall back" — a differently-wrong
   answer passes. If the same question has clean answers elsewhere in the log,
   `min_chars` should be derived from *those*, not from the bad reference. This is
   the single biggest gap. The sample log demonstrates it: breaking a failure-seed
   case to `订单处理中。` is not caught.
2. **Blocking keys for occurrence counting.** O(cases × log size) is fine to
   ~10k rows. An inverted index over cluster shortlists would carry it to
   millions without changing semantics.
3. **A labelled dedup evaluation set.** The threshold is tuned on 42 rows. A set
   of a few hundred hand-labelled pairs, scored as precision/recall at each
   threshold, would turn a reasonable guess into a defensible default — and would
   be the tool eating its own dog food.
