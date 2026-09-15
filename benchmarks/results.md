# Dedup threshold evaluation

source: nusnlp/paraphrasing-squad (SQuAD dev questions + paraphrases)
dev rows: 1062   adv rows: 56

## Alignment

joining the two files on qa `id` gives median similarity 0.340
-- which is why this harness joins on position instead:

- sim=0.982
  - under which directive did the eu harmonize restrictions on restrictions on marketing and advertising?
  - under which directive did the eu harmonize restrictions on aligned restrictions on marketing and advertising?
- sim=1.000
  - what ideal thermodynamic cycle analyzes the process by which steam engines work?
  - what ideal thermodynamic cycle analyzes the processes by which steam engines work?
- sim=0.893
  - when did the term imperialism first come to be used by its current definition?
  - when the term imperialism first come to be is applied by its current definition?
- sim=1.000
  - what would mongol armies divert in order to cut off the resources of cities they were attacking?
  - what would metropolises be with mongol armies divert in order to cut off the resources of cities they were attacking?

alignment check: positives median 0.968 vs negatives 0.341 (gap +0.627)
positives 1062  easy negatives 4000  same-passage negatives 0  adversarial negatives 56

## Pairwise similarity vs threshold

Measure: the **overlap coefficient**, kept as the pre-v0.5 baseline so the
comparison stays visible. The clustering section below uses the measure that
actually ships. The two are not the same, and the difference is the point.

| threshold | recall | FP easy | FP same-passage | FP adversarial | F1 (worst) |
| --- | --- | --- | --- | --- | --- |
| 0.30 | 1.000 | 0.688 | 0.000 | 1.000 | 0.436 |
| 0.35 | 1.000 | 0.463 | 0.000 | 1.000 | 0.534 |
| 0.40 | 1.000 | 0.266 | 0.000 | 1.000 | 0.667 |
| 0.45 | 1.000 | 0.114 | 0.000 | 1.000 | 0.823 |
| 0.50 | 1.000 | 0.050 | 0.000 | 1.000 | 0.914 |
| 0.55 | 1.000 | 0.014 | 0.000 | 1.000 | 0.974 |
| 0.60 **<- default** | 1.000 | 0.004 | 0.000 | 0.982 | 0.975 |
| 0.65 | 1.000 | 0.002 | 0.000 | 0.982 | 0.975 |
| 0.70 | 0.998 | 0.001 | 0.000 | 0.964 | 0.974 |
| 0.75 | 0.995 | 0.000 | 0.000 | 0.946 | 0.973 |
| 0.80 | 0.985 | 0.000 | 0.000 | 0.893 | 0.969 |
| 0.85 | 0.943 | 0.000 | 0.000 | 0.732 | 0.952 |
| 0.90 | 0.836 | 0.000 | 0.000 | 0.625 | 0.895 |

best threshold on the worst negative class: **0.60**; default 0.60

At the default: recall 1.000, false-positive rate on adversarial pairs 0.982

## Clustering questions that are mostly distinct

Measure: **default_similarity** -- Jaccard since v0.5. The diameters reported
below use the same measure the clusterer did, so they can be compared against
the threshold directly.

pool: 1062 questions, 1062 distinct after folding. There are only 0 pairs that *should* merge, so almost every merge below is damage.

| threshold | clusters | singletons | largest | purity | unrelated pulled in | median diameter |
| --- | --- | --- | --- | --- | --- | --- |
| 0.40 | 964 | 891 | 5 | 0.908 | 0.092 | 0.415 |
| 0.50 | 1041 | 1023 | 3 | 0.980 | 0.020 | 0.568 |
| 0.60 **<- default** | 1055 | 1048 | 2 | 0.993 | 0.007 | 0.762 |
| 0.70 | 1058 | 1054 | 2 | 0.996 | 0.004 | 0.854 |
| 0.80 | 1060 | 1058 | 2 | 0.998 | 0.002 | 0.925 |
| 0.90 | 1060 | 1058 | 2 | 0.998 | 0.002 | 0.925 |

### Worst merges at the default threshold 0.60

Two questions in one cluster whose own similarity is far below the threshold
-- i.e. they were chained together through other questions:

- cluster of 2, most distant pair scores 0.933 (different questions)
  - by the opening of the 2008 general conference, what was the total umc membership?
  - by the opening of the 2008 general conference, what was the total umc membership in the u.s.?
- cluster of 2, most distant pair scores 0.6 (different questions)
  - in which year did the gallery devoted to chinese art open?
  - what is the name of the gallery devoted to chinese art?
- cluster of 2, most distant pair scores 0.618 (different questions)
  - what field of computer science analyzes the resource requirements of a specific algorithm isolated unto itself within a given problem?
  - what field of computer science analyzes all possible algorithms in aggregate to determine the resource requirements needed to solve to a given problem?  
- cluster of 2, most distant pair scores 0.791 (different questions)
  - what does ctenophora rely on for digestion and respiration?
  - what does ctenophora use for digestion and respiration?
- cluster of 2, most distant pair scores 0.917 (different questions)
  - why did the methodist protestant church split from the methodist episcopal church?
  - when did the methodist protestant church split from the methodist episcopal church?
- cluster of 2, most distant pair scores 0.658 (different questions)
  - who found that a culture had developed where few commissioners had any sense of responsibility?
  - who found that there was a developed culture of commissioner's who lacked responsibility?

