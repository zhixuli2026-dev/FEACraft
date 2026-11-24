# Agent 明确输出格式与默认进展汇报

## 2026-11-07：工具前正文也属于整个回合的格式约束

[恢复期限验收](recovery-request-deadline.md)中，PostgreSQL 的真实模型回复再次多出前言。检查[实际事件](../../tmp/sandbox-recovery-ui-retirement-pk7nd1mq/live-model-events.json)可见，第一条 assistant 消息在工具调用前输出“我来读取该文件。”，第二条消息才是正确验证字符串。已有提示虽禁止工具前说明，但这次模型没有遵守；不能只比较最后一条消息就把整轮格式改为通过。

现将格式约束放在系统提示的技能目录和目标上下文之后，明确其适用于本次请求的全部可见正文，包括工具前、工具之间和工具后。固定格式请求需要先获取数据时，先发送工具调用并保持正文为空；结果不足或工具失败仍应如实说明，不能为满足格式编造结果。用户未要求固定格式时继续允许正常说明。没有增加输出裁剪、替换、自动重试或固定答案。

新增显式真实入口 `platform/server/live_llm_whole_turn_format_acceptance.py --source-profile-id 26`，复用已审查的真实请求适配及隔离服务器。旁路记录实际上游流的 `delta.content`，原样传给生产解析器；逐例同时比较原始上游正文、全部 `text.delta` 拼接正文和持久 assistant 消息拼接正文，三者必须完全相等。

[本次结果](../../tmp/live-whole-turn-format-jjsc5uyi/live-result.json)：原恢复读回提示保持不变，在三个独立会话中分别读取新的随机验证文件，三次整轮原样输出均通过，真实工具各选择一次、源文件未修改。另验证纯 JSON 无多余正文，以及未要求固定格式的正常两句解释。五项真实模型检查均通过，共 `api.deepseek.com / deepseek-flash` 推理 8 次，HTTP 全部 200，32,093 tokens，模拟 0；流式与持久文本一致，开发数据库未修改，隔离 API 已停止。

这些是本次固定样本的真实证据，不是所有模型或任意后续生成都不会违约的保证。上次 PostgreSQL 的严格格式失败和更早 500 记录保持原样。主线仍完成 33/53、剩 20，资源剩 6，全部 295 项交付目标继续；随后推进 staging 清理及其他入口的总期限和有界 I/O。

## 之前的默认汇报冲突修复

2026-11-07。真实模型读回验收反复出现“验证串正确，但额外附带状态、待办和下一步”。检查发现 `routers/agent.py` 的系统提示无条件要求每次回答附带三部分汇报，直接与用户的原样输出请求冲突。

现将三部分汇报限定为复杂工程任务或阶段汇报的默认行为；用户明确要求原样回复、只输出指定内容或 JSON 等固定格式时，提示要求遵守该格式，不添加前言、说明、Markdown 围栏或额外汇报，工具调用前也不先输出说明。工具权限、审批与实际执行规则不变。

显式真实模型入口：`platform/server/live_llm_output_format_acceptance.py --source-profile-id 26`。使用隔离 API/SQLite/文件存储和本地已授权 DeepSeek 凭据，未修改开发数据库。

- [首次实际调用](../../tmp/live-output-format-22rruj4x/live-result.json)：上游推理 HTTP 500，未通过，没有模拟回退。
- [真实重试](../../tmp/live-output-format-eeol9zb0/live-result.json)：3 次 `api.deepseek.com / deepseek-flash` 推理全部 200，共 11,446 tokens。Plan 会话实际选择一次 `read_text_file`，读取随机文件，回复严格等于文件中的验证串；该串未放入提示，源文件未修改。Ask 会话回复严格为 `{"status":"ok"}`，无工具调用或多余正文。两例均收到 `run.completed`。
- [实际事件落库只读核对](../../tmp/output-format-persistence-20261107.json)：核对上述两个会话的持久消息正文与流式正文完全一致。此核对不产生新的模型调用，也不改写数据库。

这些结果覆盖本次 DeepSeek 的原样工具读回与 JSON 输出场景，不将之前包含多余正文的记录改为通过。前述沙箱故障验收仍分别保留其原始格式结果。全量主线和 295 项最终交付要求继续，未据两个输出算例增加整项完成数。
