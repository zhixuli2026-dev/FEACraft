# 真实模型流式丢字：复现、修复和逐字验收

2026-11-07。在验证恢复文件的真实 DeepSeek 工具回合时，工具读取到了完整随机字符串，最终 SSE 回复却遗漏字符。没有回退到模拟，也没有放宽字符串匹配；随后增加真实上游观察，定位到平台流式消费竞态。

## 已确认的缺陷与修复

`agent.py` 的 33ms 刷新循环可能在下一条 provider 事件已经完成、但还没有取出结果时，因 `next_event.done()` 为真而创建另一条 `__anext__()` 任务，覆盖原任务，丢失实际 delta。丢失发生在 SSE 发送和保存文本之前，因此数据库保存的同样是不完整回复。

新增 `live_llm_stream_integrity_acceptance.py`：只调用授权的真实 DeepSeek，观察器逐条转发真实上游 SSE 原文，记录其中正文；将其与正式消息接口的 SSE 正文、正式历史接口的 assistant 内容逐字比较。没有注入模型回复、固定文本、模型工具选择或假 CLI 输出。

修复前一次实际回复包含 **1,334 个字符 / 876 个上游正文分片**，平台 SSE 和持久化均为 **1,220 个字符**，实际丢失 114 个字符。[修复前回执](../../tmp/live-deepseek-stream-integrity-n4qa1o5s/live-result.json)、同目录 `upstream-text.jsonl`、`round-0-events.json` 和 `pre-fix-source.json` 保留了上游原文、实际下游、旧源码哈希。

修复后仅在没有未消费任务时创建下一条读取；先保存并取出原任务结果，再清空 `next_event`。在向 HTTP 消费者刷新期间完成的结果不会再被替换。

## 真实复验

| 场景 | 真实推理次数 | 结果与证据 |
| --- | ---: | --- |
| 首次恢复文件读回，发现字符串遗漏 | 2 | [失败保留](../../tmp/sandbox-recovery-ui-retirement-6mje3_qe/live-result.json)，工具结果完整，最终文本不完整 |
| 修复前上游/下游逐字对照 | 1 | [失败保留](../../tmp/live-deepseek-stream-integrity-n4qa1o5s/live-result.json)，1334 → 1220 |
| 修复后两轮实际长回复 | 2 | [通过](../../tmp/live-deepseek-stream-integrity-9apwlnxe/live-result.json)：1293 字 / 860 分片、1175 字 / 757 分片；上游 = SSE = 保存文本 |
| 原镜像不存在时恢复 S3 文件并由模型读回 | 2 | [通过](../../tmp/sandbox-recovery-ui-retirement-0m1twn76/live-result.json)，模型实际选择读文件工具并续答，随机串完整 |
| 实际取消、实际客户端断线及各自在原会话续聊 | 4 | [通过](../../tmp/live-deepseek-control-7gt1694x/live-result.json)，idle/内存注册释放、历史终态和真实续聊均核验 |

本轮共 **11 次实际 DeepSeek `deepseek-flash` 推理**，全部 HTTP 200；其中 3 次属于修复前失败场景，8 次属于通过场景。9 次返回最终 usage，已报告 **39,808 tokens**；两次取消/断开没有最终 usage，不能算零消耗。模拟 LLM 调用为 0。开发账户、原会话、开发数据库未修改；全部隔离 API 和对象存储已退出。

首次恢复失败时，外层基础设施驱动在等待模型回调成功返回后才填写调用数，因断言失败而保留了默认 0。该运行实际调用数以独立 `live-result.json` / `upstream.jsonl` 的 2 次为准；已修改驱动 finally 从实际上游回执计算次数，失败也不能漏记。原失败文件保留，未重写成通过。

## 仍需验证

此证据覆盖真实 DeepSeek 的正文流、工具续答以及取消/断线兼容，不证明所有 provider、reasoning 原文、旧非流式/JSONL、超限与跨副本矩阵已完成。模型仍在随机串后附带结构化说明，严格“只回复验证串”未通过；流式字符丢失与额外说明是两个不同问题。过去的零散漏字报告缺少原始上游对照，不能反向认定全部由本次竞态造成。

完整目标仍为 53 项主线和 295 项全功能；主线 33 项完成、剩 20 项，资源治理剩 6 块。本次修复归入 FF-F02 / FF-F52 的实施证据，不据此关闭整个功能项。
