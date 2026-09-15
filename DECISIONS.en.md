**English** | [简体中文](DECISIONS.md)

---

# Design decisions

This file records *why*, not *what*. The code says what. These are the calls that
could reasonably have gone the other way, the evidence that settled them, and the
places where the evidence is thin.

---

## 1. The similarity measure is Jaccard

**This section used to be called "the similarity measure is the overlap coefficient, not Jaccard". The conclusion was backwards. The reasoning is kept because how it went wrong is more useful than the answer.**

### The original argument

Both pairs below are real rows from `examples/sample_traces.jsonl`, labelled by hand.

| Pair | Overlap | Jaccard | Same question? |
| --- | --- | --- | --- |
| `你们的退款政策是什么？` vs `退款政策` | 1.000 | 0.333 | **yes** |
| `你们的退款政策是什么？` vs `退款政策是怎样的` | 0.571 | 0.333 | **yes** |
| `修改手机号` vs `我要改手机号` | 0.750 | 0.500 | **yes** |
| `支持哪些登录方式` vs `支持哪些支付方式` | 0.571 | 0.400 | **no** |
| `登录不上` vs `支持哪些登录方式` | 0.000 | 0.111 | no |

Jaccard assigns the *different* pair (0.400) a higher score than either *same* pair
(0.333). Wrong ordering, so the measure was swapped.

### Where that went wrong

**First, it is an argument about ranking, and a threshold is always applied.** At
0.60 both measures reject the different pair. They disagree only on rows two and
three — the fragment pairs. So Jaccard's cost is *recall*, not confusion. The
recall loss was described as a failure to separate, which it is not.

**Second, and decisively: that table was measured on 42 rows the author wrote
himself**, deliberately built to exercise fragment chains. It contains no long
inputs, so it could not measure what the overlap coefficient does on them. On real
questions from LMSYS-Chat-1M, 40,000 random pairs:

| | overlap @0.60 | Jaccard @0.60 |
| --- | --- | --- |
| false-positive rate | **0.3411** | **0.0023** |

**148x.** The rate climbs monotonically with length (0.216 under 40 characters,
0.746 above 600) because overlap divides by `min(|A|, |B|)` and long texts are
piles of common bigrams. On 9,236 real questions the 0.60 threshold fused **93% of
the corpus into one cluster**, and `build` reported that as a successful run.

Jaccard also wins on labelled paraphrase data (F1 on the hardest negative class:
0.976 for Jaccard at 0.60, 0.965 for overlap). So this was never
precision-for-recall — it is better on both, except for fragment recall.

### The cost, and why it is acceptable

`退款政策` against `你们的退款政策是什么？`: overlap 1.000 (merge), Jaccard 0.333
(split). On the sample log that turns the refund question from one case into two,
and the log from 33 clusters into 39.

**The two mistakes are not equivalent.** A missed merge costs one redundant case. A
false merge produces a case that silently stands for two unrelated questions. The
first is the error to prefer.

Pinned by `test_the_default_measure_trades_fragment_recall_for_precision` and
`test_fragment_pairs_no_longer_match_and_that_is_the_known_cost`. If fragments start
matching again those tests fail and tell you to re-measure `benchmarks/real_traffic.md`
first.

### What the field does

Near-duplicate detection in the wild is **Jaccard over MinHash/LSH** (Broder 1997;
FineWeb, Dolma and RedPajama all follow it), with a threshold around 0.8 to 0.85. So
this change moves toward the standard rather than away from it.

But the same sources say to use 5-grams, and **that does not transfer**. Measured:

| shingle size | 2 | 3 | 4 | 5 | 6 |
| --- | --- | --- | --- | --- | --- |
| labelled-set F1 | **0.976** | 0.961 | 0.932 | 0.905 | 0.867 |
| sample-log clusters | 39 | 40 | 42 | 42 | 42 |

The 5 is for *documents* — web pages and corpora. This tool handles short questions
(median 14 to 122 characters), where a 5-gram is specific enough that paraphrases
share almost none of them, and the sample log stops merging entirely. **Character
bigrams stay. Do not "fix" this to match the convention.**

### An approach that was tried and dropped

The plan was a length-aware hybrid: containment when one side is much shorter,
Jaccard when the lengths are close. It sounds right and measured badly — the ratio
is unstable at small sizes (4 shingles against 19 is 0.21), so short pairs kept
falling into the containment branch, and the overall false-positive rate only came
down from 0.34 to **0.19** while the sample log's legitimate clusters fell from 13/17
to 1/17. Two branches for that is not a trade worth making.

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

## 7b. v0.2 closes the other half of that

The fix is to change where the shape comes from: **from the clean answers to the
same question, not from the one that failed.**

The refund question appears six ways in the log, one of them down-voted. That one is
a failure seed and cannot be a reference. The other six are clean, and their median
gives the case its minimum length. When a question has no clean answer at all, there
is one more fallback: if the failure itself was "this answer is too short", then it
fell below the log's short-answer line, and that line is used.

That fallback needs a warning attached. **It is a weak reference** — a log-wide
median, not a per-question one — so it can be too strict for a question whose answers
are legitimately short. The report labels those cases "the log's short-answer line
(weak reference)", and they are the first thing to look at when tuning.

It also removed a drift hazard. The short-answer line used to be a bare conditional
inside `signals.py`. It is now `suspiciously_short_boundary()`, called by both the
signal and the checks. Without that, changing the threshold and forgetting the checks
would make a case **quietly** stop reproducing its own failure, which is the worst
way for something to break.

**Side effect one: cases now check themselves.** Every case runs its own checks
against its own reference output, and the verdict is written into both `report.md`
and `cases.jsonl`. Three outcomes:

- a failure seed that **fails** its checks — correct, it reproduces the failure
- a failure seed that **passes** them — `failure_not_reproduced`: the checks cannot
  see this failure at all
- a clean case that fails its checks — `reference_fails_own_checks`: either the
  reference is wrong or the checks are

That second outcome is the residual weakness, **quantified**. Three of the sixteen
cases in the sample log land there, all for the same reason: `user_retried`. The user
asked again; the output was fine. No deterministic check on text detects "the user
will ask again". That is a limit of the method rather than unfinished work — gating
it properly needs a **human-labelled expected answer**, and this tool does not
produce one.

I have deliberately not tried to push that number down. There is exactly one way to
do it — add checks that fire on things that are actually fine — and that is how a
gate gets switched off.

**Side effect two: cost stops growing with repetition.** Clustering used to compare
an incoming trace against **every member** of a candidate cluster, so a question asked
5,000 times cost 5,000 comparisons for question 5,001. Clusters now keep **distinct
phrasings** instead (fingerprints are deduplicated), so 5,000 identical rows leave one
fingerprint to compare against. `MAX_DISTINCT_FINGERPRINTS = 512` is a guessed safety
valve: past it, extra rows start their own cluster, which yields **duplicate cases**
rather than **missed ones**. Duplicates over misses.

---

## 7c. The matcher is swappable, but the default does not change

Character n-grams have a fixed blind spot: two paraphrases sharing no characters score
zero. Fixing it means embeddings, and this package's pitch is that it has no
dependencies.

So it is not fixed — it is made **swappable**. `--similarity module:function` points at
your own `(str, str) -> float`. Two alternative matchers ship alongside, plus the
measured counter-example: on Chinese, `word_jaccard` is **coarser** than the default
(`支持哪些登录方式` vs `支持哪些支付方式` goes from 0.571 to 0.857). They are working
examples of the hook, not recommendations.

One cost, stated plainly: a custom matcher disables the n-gram inverted index. That
index shortlists candidates **lexically**, which is precisely the recall the custom
matcher was brought in to improve, so keeping it would defeat the point. With it
disabled, every trace is compared against every cluster, and a large log gets
noticeably slower.

---

## 7d. What finally happened to those 3 behavioural cases

v0.2 pushed weak cases from 9/16 down to 3/16. All three survivors were
`user_retried`: the user asked again, and **the answer itself was fine**.

I tried to solve this in code first. Every approach failed on the same fact:
**the failure is not in the text.** No amount of tuning a text check detects
"the user will ask again".

So the direction changed to **supplying the part only a person can supply.** The
tool does three things: recognise these cases, emit a fill-in template, merge the
result into the checks. The person does one thing: write down what a correct
answer looks like.

### Why the key is the question, not the case id

This is the only genuinely hard part of the feature; the rest is plumbing.

Case ids (`case-011`) are assigned **positionally**, in descending score order.
Add one high-scoring trace to a log and every id shifts by one. An annotation
keyed on an id detaches silently.

And **a detached annotation is worse than no annotation**: the case still looks
covered while asserting nothing. Nothing errors, nothing turns red; you find out
the day the gate waves through an obvious regression.

So the key is the **question itself** — `input`, compared after folding case,
whitespace and punctuation. `test_an_annotation_follows_the_question_when_case_ids_shift`
exists for exactly this: it adds a high-scoring trace that renumbers everything,
then asserts the annotation is still attached to the same question.

### An annotation does not change the self-check verdict

I nearly wrote this section as "annotations fix those 3 cases". They do not.

The self-check asks whether the checks fail on the output that went wrong. For a
behavioural failure that output is *good*, so the answer is necessarily no —
still no, after annotating. The verdict stays `failure_not_reproduced`.

What changes is the **goal**: from "make this case reproduce the original
failure" to "the answer must satisfy this specification". The first is
impossible; the second is achievable, and it is the one that actually catches a
regression.

The report keeps these numbers **apart**: how many cases cannot catch their own
failure, how many of those an annotation now covers, how many still need one. I
deliberately did not merge them into a "coverage" figure. That number would look
better and would hide the fact that three cases still cannot catch anything.

### A detail I got wrong first

If a human writes `contains` but not `min_chars`, the merge has to **keep the
inferred length check**.

My first instinct was that a hand-written annotation should replace the inferred
version wholesale. But then writing one `contains` would silently **remove** the
truncation check — annotating a case would make it weaker. A feature should not
do that. The rule is now per-check-type: the human's value wins where they
overlap, the inferred check survives where they do not.

I only worked this out while writing tests. I had paired a 6-character bad
reference with `min_chars: 20` and found the checks *did* fire and the verdict
flipped to `ok` — that counter-example made me realise both verdict branches are
correct, just for different situations. So I added a second test pinning the
other branch down.

### The template is deliberately empty

`build` writes `expectations.template.jsonl` when no expectations file exists,
listing each case that needs one with `"expect": {}`. It does not pre-fill a
guess. A plausible-looking generated expectation is exactly the over-confidence
this mechanism exists to avoid, and it would be far easier to accept by accident
than an empty field.

---

## 7e. The dedup threshold finally has outside evidence (v0.4)

Every number in this project used to be measured on a 42-row log the author wrote
himself, to exercise the features he had just built. That is circular. It is now
measured against SQuAD questions and their human-verified paraphrases
(`benchmarks/`), where the labels come from how the data was constructed rather
than from anyone's judgement.

The result splits in two.

**0.60 holds up.** On 1,062 labelled rows it is the optimal value on the hardest
negative class: recall 1.000, and a false-positive rate of 0.004 against unrelated
questions. The default does not need changing.

**But single linkage magnifies that 0.4% into a catastrophe.** Fed 1,062 questions
containing **no duplicates whatsoever**, the 0.6 threshold produced a 0.4%
pairwise false-positive rate — and fused **54% of the pool into clusters, the
largest holding 425 rows**. The arithmetic is simple: 1,062 rows make roughly
560,000 pairs, and 0.4% of those is more than two thousand spurious links, easily
enough to chain the whole pool. Raising to 0.8 cuts the damage to 1.8%, but costs
recall — and recall here is already flattered (below).

**The adversarial set gives a hard ceiling.** Those 56 pairs exist specifically to
make lexical matching answer wrongly. At 0.6, 98% of them clear the threshold.
That is the ceiling of the character n-gram approach, not a flaw in the
implementation.

### A mistake made along the way, and a thorough one

Both files carry qa `id` fields, so the obvious join is on `id`. **They are
aligned positionally; the ids were regenerated.**

The result was a complete, professional-looking evaluation table reporting a
recall of 0.006 at the default threshold. **All of it was noise.** Had it gone into
the README, a fabricated conclusion would have shipped as a measurement.

What exposed it: the similarity distribution of the supposed positives
(median 0.343) was indistinguishable from that of random pairs (median 0.345).
The labels were doing nothing.

That check is now baked into `dedup_eval.py`: **if the labelled positives are not
measurably closer to each other than chance, the script refuses to print a report
and exits.** Silent misalignment is far more dangerous than a crash.

### A warning that got written and then deleted

On the strength of that finding, `build` gained a clustering-health warning: a
cluster holding a phrasing far from its representative might be a false merge.

**It fired on the sample log's own legitimate clusters immediately.** The flagged
cluster contained

```
你们的退款政策是什么？ / 退款政策是什么 / 请问退款政策 / 我想了解退款政策 (0.429)
```

— four phrasings of one question, with `我想了解退款政策` scoring 0.429. That is
precisely the legitimate short-fragment chain the clustering docs cite as the
reason for using single linkage at all.

In other words: **a short fragment sitting far from its parent is the signature of
both a useful chain and a spurious one, and no threshold on that number separates
them.** A warning that fires on the good case is worse than none, so it was
removed. What remains are the measured facts — largest cluster, share pulled in —
printed for a human to read, with the reasoning recorded in `ClusterHealth`.

### What this evaluation does not cover

- **The language is English.** The tool targets Chinese logs; this is evidence
  about the mechanism, not about the target language.
- **The paraphrases are machine-generated and mostly small edits** (median
  similarity 0.968). The positive set is far easier than real paraphrase, so
  recall is optimistic and the apparent optimum sits high. Do not read "0.60 is
  the best threshold" as a claim about human paraphrase.
- **The signal layer is untouched.** Feedback, retries, latency and cost decide
  what is worth testing, and no public dataset pairs real text with real
  operational fields. That half of the tool still rests on the 42-row sample log.

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

v0.1 listed three items; the first two were done in v0.2. The first item v0.2
listed -- hand-written expectations for the behavioural cases -- was done in v0.3.

Ranked by how much they would improve the tool:

1. **The dedup threshold is still a guess.** 0.6 was tuned on 42 rows. A few
   hundred hand-labelled pairs, scored for precision and recall at each
   threshold, would turn a reasonable guess into a defensible default. This is
   still the tool eating its own dog food: the logic it uses to score other
   people's output is exactly what would score its own threshold.
2. **Run it on a log of realistic size.** Past 100k rows, two questions: is
   `MAX_DISTINCT_FINGERPRINTS = 512` enough, and how slow does a custom matcher
   (index disabled) actually get? Both numbers are still estimates. I have not
   measured them.
3. **Let annotations be proposed rather than written from scratch.** Today a
   person has to compose the whole `expect` block. If the tool drafted one first
   -- candidate keywords pulled from the clean answers to the same question -- the
   person would only be confirming or rejecting, which is much faster. I am not
   confident the drafts would be good enough to help rather than to distract.
   Unverified idea.
