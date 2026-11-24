# Agent 回答语言、追问模板和推理流

回答和文档遵循当前用户请求的语言。明确指定语言时采用指定语言。需要补参数时，每项包含 `Parameter name`、`Purpose`、`Explanation` 和 `Recommendation`。复杂工程任务的阶段汇报采用 `Status Update`、`Todo` 和 `Next Steps`。用户指定纯 JSON 或原样输出时，该格式覆盖整轮正文。问答模式的提示与实际权限一致，直接回答或追问。

FF-F06A 使用已授权的 DeepSeek 官方接口验证两种协议的推理传输。默认设置省略显式推理字段，模型仍可以按自身设置生成推理。显式强度经 Chat Completions 传入 `reasoning_effort`，经 Messages 传入 `thinking`。逐项比较上游、平台事件及数据库的正文和推理内容，Edge 刷新后重新读回正文与可展开的推理卡。[DeepSeek 协议文档](https://api-docs.deepseek.com/guides/anthropic_api/)

## 2026-11-22 实测

- 两种协议共 8 次真实 `deepseek-flash` 调用。每种均通过中文双参数追问、纯英文阶段汇报、中文提出的西班牙语回答要求，以及纯 JSON 请求。上游、平台流与落库逐字对应，浏览器刷新不重发模型请求，跨账号读回返回 404。[通过回执](../../tmp/live-f18-response-contract-3q9jc_gx/live-result.json)
- 早期真实问答曾把新的 DSML 工具标记作为正文返回。修复问答提示并更新解析器后，取该次模型实际返回的标记进行协议回放。178 个分割位置和逐字符片段均通过，两侧正常正文保持。[原始实测](../../tmp/live-f18-response-contract-0p438bs_/live-result.json) · [协议回放](../../tmp/FF-F18-response-contract-20261122/protocol-replay.json)
- 早期回执保留语言跟随失败、过长追问遗漏字段和测试采集错误。显式开启推理的简单题也可能只返回正文，实测按上游实际返回的内容验收，分别核对请求参数和推理事件。所有模型回执使用真实服务，模拟调用 0。[验收入口](../server/live_llm_response_contract_acceptance.py)
- 正式 Plan 权限下，模型实际读取隔离算例的随机文件，整轮只返回原始核对串。问答模式的纯 JSON 复验也通过。这组单独使用 3 次真实调用。两组最终验收合计 11 次，较早的未通过运行单列。[真实工具回执](../../tmp/live-output-format-epzbjdl7/live-result.json) · [阶段汇总](../../tmp/FF-F18-response-contract-20261122/validation.json)

09:00 Docker 定时维护正常结束，Docker 内部回收 0 B，当次 Windows 可用空间相同。阶段结束时 C 盘可用约 16.3 GiB，两个现有服务健康检查均为 200。下次维护为 15:00。[磁盘和服务记录](../../tmp/FF-F18-response-contract-20261122/disk.json)
