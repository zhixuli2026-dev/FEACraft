# FF-F52 AgentHost 流式顺序与中断

AgentHost 对单个 API Agent run 的 SSE 帧使用同一 `run_id` 和递增 `seq`。相邻同类正文与推理片段约每 33 ms 合并，工具、完成和错误事件到来时先发出已累积的片段。断开实际客户端连接后，清理模型迭代器、执行归属及序列跟踪。原有真实显式取消和断线恢复证据随原会话保留。[原有真实流逐字核对](agent-stream-integrity.md) · [中断清理](agent-memory-stream-lifecycle.md)

真实 F08A 新文件批准回执显示，原批准响应的首帧 `tool.result` 缺少 `run_id` 与 `seq`。后续 `ready` 才以序号 1 开始。现在批准响应在首帧前创建序列，续跑沿用该标识及计数器。`tool.result` 仍先于 `ready`。结束或关闭外层响应时显式关闭续跑生成器，再释放序列。[原始真实批准 SSE](../../tmp/live-f08a-approval-diff-dn9gfoz8/new-approved-events.json) · [实现](../server/app/routers/agent.py)

## 复验

`platform/server/live_llm_agenthost_order_acceptance.py --source-profile-id 26` 使用现有授权 DeepSeek profile，服务端及文件存储在 D 盘独立运行。这一入口最多发起五次官方请求，真实上游正文和推理按分组摘要核对，证据记录实际工具片段的顺序。选取第一条完整上游推理片段，先原样交付其字节，再暂缓读取后续真实片段，用来观察稀疏输入的定时冲刷。断线在收到真实正文后关闭客户端 HTTP 流，随后在原会话发出新的真实请求。[显式真实验收入口](../server/live_llm_agenthost_order_acceptance.py) · [DeepSeek 推理与工具调用](https://api-docs.deepseek.com/guides/thinking_mode/)

首轮共三次官方 HTTP 200，实际用量 14,857 tokens。两次真实工具回合的上游推理摘要、平台 SSE 和持久历史一致。首条推理在 31.0 ms 时可见，后续真实片段在 219.0 ms 后获准继续读取。实际批准为 `tool.result` 序号 1、`ready` 序号 2 和完成序号 10，整段使用同一运行标识。第三次上游只提供正文。原夹具额外要求这一回合也有推理而退出 1，独立复核已确认真实正文与历史一致、回复包含只有文件中才有的验证串、批准写入生效且隔离 API 退出。首轮原回执保留。[原回执](../../tmp/live-f52-agenthost-order-e9_pdzso/live-result.json) · [独立批准核对](../../tmp/live-f52-agenthost-order-e9_pdzso/independent-approval-proof.json)

第二轮只执行 `--phase disconnect`，实际发出两次请求并退出 0。客户端在同一运行的第 114 帧收到正文后关闭连接，上游迭代结束前未读到 `[DONE]`。SQLite 的请求和运行状态均为 `interrupted`。检查点保留已交付的正文前缀和实际读到的完整推理，释放会话所有者和内存序列。同一会话随后以新运行标识完成 15 帧，严格从 1 递增，真实正文及推理与上游和持久历史一致。再次确认会话空闲、Windows 隔离 API 进程退出、端口关闭，且临时加密密钥未落盘。[断线和续聊回执](../../tmp/live-f52-agenthost-order-yagi92i2/live-result.json) · [数据库和进程独立核对](../../tmp/live-f52-agenthost-order-yagi92i2/independent-disconnect-proof.json)

两轮共五次 `api.deepseek.com` 的 `deepseek-flash` 请求，均返回 HTTP 200。收到的四份用量合计 18,141 tokens，断线那次没有返回最终用量。模拟推理调用为零。工具与正常完成前的冲刷已用真实帧核对，唯一待处理读取经源码核对。客户端断开证据覆盖平台本地迭代和会话归属释放，上游计费以服务回执为准。[冲刷与读取实现](../server/app/routers/agent.py#L1029)

错误前的冲刷从既有 FF-F42 实测回执逐帧复核，本次新增模型请求为零。历史夹具在收到真实 DeepSeek 正文后主动关闭实际 HTTP 响应，连接当时未收到 `[DONE]` 或 `finish_reason`。SQLite/FS 完整交付关断前的 80 字节正文，最后一条 `text.delta` 紧接着是第 49 帧 `stream.error`。PostgreSQL/S3 完整交付 84 字节，最后一条 `text.delta` 紧接着是第 13 帧 `stream.error`。两套回执都保持原 `run_id` 和连续序号，错误类型均为 `upstream_incomplete`，同一会话均完成后续真实请求。SQLite 的独立只读核对确认错误前的正文与推理均落库，运行和请求都记为 `failed`，归属为 `idle`。[错误边界复核回执](../../tmp/live-f52-agenthost-order-yagi92i2/inherited-upstream-error-proof.json) · [SQLite 原始 SSE](../../tmp/live-cli-startup-1he_jlc9/closed-before-finish-events.json) · [PostgreSQL/S3 原始 SSE](../../tmp/live-cli-startup-b7e9wlip/closed-before-finish-events.json) · [原历史与重启验收](checkpoint-window-and-lifecycle.md)
