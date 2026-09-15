# trace2eval build report

Source log: `examples/sample_traces.jsonl`

## Summary

| Metric | Value |
| --- | --- |
| Traces read | 42 |
| Distinct questions | 39 |
| Questions with no signal | 23 |
| Cases generated | 16 |
| Log lines collapsed into a case | 1 |
| Questions below the minimum score | 0 |
| Questions beyond the case limit | 0 |
| Cases whose checks cannot catch their own failure | 3 |
| Trusted references failing their own checks | 0 |
| Cases using a human-written expectation | 3 |
| Cases still needing one | 0 |

## How the clustering behaved

Clustering uses single linkage, so a cluster only needs a *path* of threshold-clearing links between its members -- not for every pair to clear it. That makes a small false-positive rate compound: measured on 1,062 unrelated SQuAD questions, a 0.4% pairwise false-positive rate was enough to pull 54% of the pool into one 425-question cluster. Those numbers are in `benchmarks/`.

| Statistic | Value |
| --- | --- |
| Threshold | 0.60 |
| Clusters | 39 |
| Multi-member clusters | 2 |
| Largest cluster | 3 rows |
| Rows pulled into a cluster | 11.9% |
| Clusters holding a phrasing below the threshold | 0 of 2 sampled |
| Worst representative similarity seen | 1.000 |

**How to read this.** Single linkage holds a cluster together through a *path* of threshold-clearing links, so a cluster can contain a phrasing far from its own representative and still be one question -- `我想了解退款政策` scores 0.43 against `你们的退款政策是什么？` and is plainly the same question. That cuts both ways: the same shape appears when unrelated questions get fused. There is no automatic warning here for that reason; the numbers are for you to read.

As a reference point, running this tool over 1,062 SQuAD questions with **no duplicates among them** at the default 0.60 produced a largest cluster of 425 rows holding 54% of the pool. See `benchmarks/`.

## Log-wide baselines

| Statistic | Value |
| --- | --- |
| p95 latency | 3955.00 ms |
| p95 cost | $0.0005905 |
| Median output length | 39 chars |

## Case mix

- **15** regression seeds (the reference output is the output that went wrong — the case exists so it never ships twice)
- **1** quality anchors (clean trace, so an expected shape was inferred from it)

## Cases

| Case | Score | In log | Trusted | Shape from | Self-check | Signals | Input |
| --- | --- | --- | --- | --- | --- | --- | --- |
| case-001 | 8.0 | 1 | no | the log's short-answer line (… | ok | negative_feedback, user_retried, output_m… | 订单一直显示处理中，已经三天了 |
| case-002 | 7.0 | 1 | no | the log's short-answer line (… | ok | negative_feedback, user_retried, output_m… | 我要投诉 |
| case-003 | 6.5 | 1 | no | the log's short-answer line (… | ok | negative_feedback, fallback_phrase, outpu… | 帮我写一段 Python 代码读取 CSV 文件 |
| case-004 | 6.5 | 1 | no | the trace's own expect block | ok | output_much_shorter, explicit_expectation… | 返回订单状态的 JSON |
| case-005 | 6.5 | 1 | no | the log's short-answer line (… | ok | negative_feedback, fallback_phrase, outpu… | 能不能退款 |
| case-006 | 6.0 | 1 | no | the log's short-answer line (… | ok | user_retried, output_much_shorter, slow_r… | 重新发送验证码 |
| case-007 | 4.5 | 2 | no | the one clean answer to this … | ok | negative_feedback, output_much_shorter | 你们的退款政策是什么？ |
| case-008 | 4.5 | 1 | no | the log's short-answer line (… | ok | fallback_phrase, output_much_shorter, exp… | 帮我总结这篇文档 |
| case-009 | 3.5 | 1 | no | the log's short-answer line (… | ok | output_much_shorter, slow_response, expen… | 查询物流 |
| case-010 | 3.0 | 1 | no | none available -- only behavi… | ok | empty_output | 你能帮我查一下我的订单吗 |
| case-011 | 2.5 | 1 | no | a human-written expectation | failure_not_reproduced | user_retried | 修改手机号 |
| case-012 | 2.5 | 1 | no | a human-written expectation | failure_not_reproduced | user_retried | API key 轮换 |
| case-013 | 2.5 | 1 | no | a human-written expectation | failure_not_reproduced | user_retried | 订单还在处理中 |
| case-014 | 2.0 | 1 | yes | the trace's own expect block | ok | explicit_expectation | 返回账户余额 |
| case-015 | 1.5 | 1 | no | the log's short-answer line (… | ok | output_much_shorter | 你好 |
| case-016 | 1.5 | 1 | no | the log's short-answer line (… | ok | output_much_shorter | 现在汇率是多少 |

## Cases carrying a human-written expectation

The expectation below does not make these cases reproduce their original failure -- nothing can, the failure was never in the answer. What it does is give them something worth asserting.

- `case-011` (修改手机号) — 示例标注：用户重问了,说明第一版没解决。正确答案要说清要走验证流程,而不是一句「在设置里改」就完事。
- `case-012` (API key 轮换) — 示例标注：问的是怎么轮换,答案必须真的交代轮换方式。只要提到密钥这个词,就说明没有整段跑偏。
- `case-013` (订单还在处理中) — 示例标注：用户重问说明第一版没给出可执行的下一步。正确答案必须包含升级路径——也就是找客服。

## Why the top case was chosen

`case-001` scored **8.0**.

- `negative_feedback` (+3.0) — user gave negative feedback
- `user_retried` (+2.5) — the same request was retried
- `output_much_shorter` (+1.5) — 18 chars vs log median 39
- `slow_response` (+1.0) — 6100ms above p95 3955ms

The expected shape came from the log's short-answer line (weak reference).

Self-check: this is a failure seed, so the checks are *supposed* to fail on it — they do, on `min_chars`. The case reproduces the failure it came from.

> failure seed with no clean sibling, but the failure itself was a length failure: the response fell below the log's short-answer line, so this case asserts min_chars=20. That is a weak reference -- it comes from the log-wide median rather than from this question's own answers, so check it first when tuning

---

Generated by [trace2eval](https://github.com/rfioly/trace2eval). Review this file before committing the case set — the selection is a proposal, not a verdict.
