# Benchmarks

The rest of this repository measures the tool against a 42-row sample log that
the author wrote himself, in order to exercise the features he had just built.
That is circular, and it was the weakest claim the project made. This directory
replaces it with data nobody here authored.

## What is measured

**The dedup threshold.** It decides which log lines are treated as the same
question, and every other number in the tool sits on top of it. The default of
0.60 was tuned on those 42 rows.

`dedup_eval.py` measures it three ways:

1. **Pairwise precision and recall** against labelled pairs, across thresholds
   0.30 to 0.90.
2. **Clustering behaviour** — what the real clusterer does when handed questions
   that are mostly distinct.
3. **The hard ceiling** — how it behaves against pairs built specifically to
   defeat string matching.

## The data

[`nusnlp/paraphrasing-squad`](https://github.com/nusnlp/paraphrasing-squad), from
Gan & Ng (ACL 2019), *Improving the Robustness of Question Answering Systems to
Question Paraphrasing*. SQuAD dev questions (crowd-written) plus paraphrases of
them, in SQuAD v1.1 JSON format. Four files:

| File | What it is |
| --- | --- |
| `dev_orig.json` | the original SQuAD questions |
| `dev_para.json` | their paraphrases |
| `adv_orig.json` | questions from the adversarial set |
| `adv_para.json` | their paraphrases, built so that string matching gives the wrong answer |

Nothing from this dataset is committed. Only aggregate results are, in
[`results.md`](results.md). The data carries its own licence and is not mine to
redistribute.

### Fetching it

```bash
python benchmarks/fetch_data.py --out _data
```

The script uses the GitHub Git Blobs API rather than `raw.githubusercontent.com`,
because in the environment this was developed in the latter is blocked while the
former answers.

### A trap in this data, and the guard against it

The files carry qa `id` fields, so the obvious join is on `id`. **They are not
aligned that way.** The ids were regenerated; joining on them pairs unrelated
questions while still producing a complete, plausible-looking table of numbers.

That mistake was made once here, and the table it produced was entirely noise —
a "recall of 0.006 at the default threshold" that measured nothing. The giveaway
was that the similarity distribution of the supposed positive pairs was
indistinguishable from that of random pairs.

`dedup_eval.py` now checks this before reporting anything and **refuses to run**
if the labelled positives are not measurably closer to each other than chance. A
silent misalignment is far more dangerous than a crash.

The correct alignment is **positional**: both files have the same rows in the
same order.

## Results

See [`results.md`](results.md) for the SQuAD paraphrase evaluation, and
[`real_traffic.md`](real_traffic.md) for the same question asked of 1,000,000 real
user conversations. The second one is where the bad news is.

The short version:

- **0.60 is the right threshold for pairwise decisions** on short, comparable-length
  text, which is what the SQuAD benchmark measures. It is the best F1 on the hardest
  negative class, with recall 1.000 and a false-positive rate of 0.004 against
  unrelated questions.
- **Single linkage turns a small pairwise error into a large one.** On 1,062
  questions containing no duplicates at all, that 0.4% pairwise rate fuses 54% of
  the pool into clusters, the largest holding 425 rows. Any cluster that big is not
  one question.
- **Against an adversary, character n-grams lose.** 98% of the adversarial pairs
  clear the threshold. That is the expected outcome — the set exists to defeat
  lexical matching — but it is the honest ceiling on what this matcher can do.
- **On real traffic the old measure failed outright and silently, and v0.5 replaced
  it.** The overlap coefficient's false-positive rate climbs monotonically with
  input length, from 22% for inputs under 40 characters to 75% for inputs over 600.
  On 9,236 real user questions it produced a single cluster holding 93% of the
  corpus. v0.5 switched to Jaccard, which drops the overall rate from 0.3411 to
  0.0023 — and beats the old measure on labelled F1 as well, so it is not precision
  bought with recall. The cost is fragment matching, and the derivation is in
  `real_traffic.md`.
- **The shingle size stays at 2, against the convention.** The near-duplicate
  literature uses 5-grams, and measurements say that does not transfer here:
  labelled F1 falls from 0.976 at size 2 to 0.905 at size 5, and the sample log
  stops merging entirely at size 4. Those 5-grams are for documents; these are
  short questions.

## What this does NOT establish

Read this before quoting any number.

- **The language is English.** This tool is aimed at Chinese-language logs, where
  character bigrams behave differently. The English result is evidence about the
  mechanism, not about the target language.
- **The paraphrases are machine-generated and mostly small edits** (median
  similarity 0.968). The positive set is therefore much easier than real
  paraphrase, which makes recall optimistic and pushes the apparent optimum
  threshold upward. Do not read "the best threshold is 0.60" as a claim about
  human paraphrase.
- **SQuAD questions are Wikipedia-passage questions**, not production traffic.
  They are highly formulaic ("what did the ... of the ..."), which inflates the
  similarity of unrelated pairs.
- **The signal layer is not exercised at all.** Signals — feedback, retries,
  latency, cost — decide which traces become cases in the first place, and no
  public dataset pairs real text with real operational fields. Serving traces
  (BurstGPT, Azure LLM, Mooncake) have the timing but strip the text;
  conversation logs (WildChat, LMSYS-Chat-1M) have the text but no timing. So the
  half of this tool that decides *what is worth testing* still rests on the
  sample log.
