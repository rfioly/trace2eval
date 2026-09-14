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

pool: 1062 questions, 1062 distinct after folding. There are only 0 pairs that *should* merge, so almost every merge below is damage.

| threshold | clusters | singletons | largest | purity | unrelated pulled in | median diameter |
| --- | --- | --- | --- | --- | --- | --- |
| 0.40 | 8 | 7 | 1055 | 0.008 | 0.992 | 0.000 |
| 0.50 | 62 | 52 | 940 | 0.058 | 0.942 | 0.406 |
| 0.60 **<- default** | 486 | 434 | 425 | 0.458 | 0.542 | 0.613 |
| 0.70 | 931 | 874 | 50 | 0.877 | 0.123 | 0.704 |
| 0.80 | 1043 | 1027 | 4 | 0.982 | 0.018 | 0.840 |
| 0.90 | 1058 | 1054 | 2 | 0.996 | 0.004 | 0.937 |

### Worst merges at the default threshold 0.60

Two questions in one cluster whose own similarity is far below the threshold
-- i.e. they were chained together through other questions:

- cluster of 2, most distant pair scores 0.643 (different questions)
  - according to polynomial time reduction squaring can ultimately be logically reduced to what?
  - what measurement of time is used in polynomial time reduction?
- cluster of 2, most distant pair scores 1.0 (different questions)
  - by the opening of the 2008 general conference, what was the total umc membership?
  - by the opening of the 2008 general conference, what was the total umc membership in the u.s.?
- cluster of 2, most distant pair scores 0.765 (different questions)
  - how many huguenots were killed during this purge?
  - how many huguenots were killed in toulouse?
- cluster of 2, most distant pair scores 0.656 (different questions)
  - if roman numerals were used in the naming of the 50th super bowl, which one would have been used?
  - which super bowl, after the 50th one, will begin have roman numerals in the title again?
- cluster of 2, most distant pair scores 0.812 (different questions)
  - who found that a culture had developed where few commissioners had any sense of responsibility?
  - who found that there was a developed culture of commissioner's who lacked responsibility?
- cluster of 2, most distant pair scores 0.667 (different questions)
  - some elements of the brotherhood directed what action against the government?
  - the muslim brotherhood's competence compares well against what type of local governments?

