# FF-F17 模型窗口、历史显示和完整归档

API Agent 在系统提示、工具定义和当前消息确定后，再按完整用户回合选择最近历史。当前回合优先，摘要在能容纳时保留。确定入选范围后才读取图片，因此被裁出模型窗口的历史附件无需读取。单条当前输入及其工具结果超过预算时，流返回 `context_too_large`，历史保留原始消息。完整 JSONL 仍可导出。 [输入选择](../server/app/services/model_context.py) · [Agent 与历史接口](../server/app/routers/agent.py)

官方 DeepSeek Flash 和 V4 Pro 使用 100 万 token 上下文，并为输出保留 384,000 token。预检用 UTF-8 与协议开销估算文本成本，Flash 图片按官方每张最多 1,024 token 计入，另限制请求体。没有已知窗口的模型保留请求体限制，可配置 `AGENT_CONTEXT_INPUT_TOKEN_CAP`。服务商在实际响应中返回的用量与本地预检估算独立持久化。 [供应商窗口](https://api-docs.deepseek.com/quick_start/pricing/) · [图片 token 上限](https://api-docs.deepseek.com/guides/vision/) · [服务商计量说明](https://api-docs.deepseek.com/quick_start/token_usage/)

2026-11-24 通过正常 API 导入 6,000 条用户历史，修复导入记录被当作当日新模型消息计费的问题。在测试限额 12,000 下保留最近 15 个完整回合，裁出当前加载范围内的 1,184 个早期回合。旧附件缺失未影响当前模型请求。官方 `deepseek-flash` 返回 HTTP 200 和正确的当前消息标记。服务商实报 1,643 个输入 token，与服务端持久化用量一致。预检估算为 11,933，超限当前消息在外发前被拒绝。本轮 1 次真实调用，模拟 0 次。 [最终回执](../../tmp/live-f17-model-context-w01c2vpd/context-result.json) · [首次修复前回执](../../tmp/live-f17-model-context-br11q_wv/context-result.json)

Edge 在 6,008 条历史中使用相同快照读取偏移 280 和 560，浏览器回放未触发模型 POST。首屏仍受 280 事件限制，完整导出保留首条历史和最新真实回复。 [浏览器回放](../../tmp/live-f17-model-context-w01c2vpd/browser.json) · [验收入口](../server/live_llm_context_budget_acceptance.py)
