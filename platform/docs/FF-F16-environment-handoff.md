# FF-F16 跨环境文字交接与原生续聊

文字交接保留选择范围内的已提交文本。旧的 `keep_chars` 入口在已有摘要或交接快照时，也会带上随后发生的消息，最后应用文本上限。跨环境交接创建独立线程。直接恢复原线程会核对凭据、环境和持久化的最后回合 ID。支持分页的 CLI 只读取最后一条原生回合的元数据。 [交接路由](../server/app/routers/agent.py) · [原生线程校验](../server/app/services/codex_sessions.py) · [分页读取](../server/app/services/codex_stdio.py)

- 实际 API 对话通过 DeepSeek 生成随机核对值。交接给 Windows Codex 后，模型取回原值并新增第二个核对值。Windows 原线程恢复前核对原生回合，继续回复同一对值，线程 ID 保持一致。 [实际交接回执](../../tmp/live-f16-environment-handoff-pac8lo4x/handoff-result.json)
- 切到非特权 WSL 后，直接恢复 Windows 线程失败且未发起模型请求。通过文字交接开启独立 WSL 线程，成功取回两个值。之后交接回 API 对话，真实模型从保存的历史读回两值。所有原生执行都核验收尾，外账号交接返回 404。 [验收入口](../server/live_llm_environment_handoff_acceptance.py)
- Windows 与 WSL 安装的 CLI 均导出了 `thread/turns/list` 参数 Schema。旧回答、中途交接、祖先边界、跨账号和 PG/SeaweedFS 组合已有实际浏览器验收。 [Windows Schema](../../tmp/FF-F53-stdio-schema-20261124/v2/ThreadTurnsListParams.json) · [WSL Schema](../../tmp/FF-F16-handoff-20261124/wsl-schema/v2/ThreadTurnsListParams.json) · [浏览器与 S3 证据](codex-branch-handoff.md)

本轮有 2 次实际 Chat Completions 和 3 次实际 Responses HTTP 200，模拟 0。WSL 的跨环境恢复拒绝属于本地准入记录。 [实际请求回执](../../tmp/live-f16-environment-handoff-pac8lo4x/official-responses.json)
