# FF-F01 API、Windows 和 WSL 统一聊天

Agent 面板按会话恢复 API、Windows 或 WSL 运行环境。运行中的原生回合可以停止。刷新和切回会话时读取持久化状态，用户可以查看已停止的回合，继续编辑任务或从已完成的历史恢复原生线程。 [统一面板](../web/src/panels/ChatPanel.tsx) · [原生面板](../web/src/panels/NativeChatPanel.tsx) · [回合接口](../server/app/routers/codex.py)

- 2026-11-24，Edge 在一个工作台中分别选择 Windows 和非特权 WSL，启动真实 Codex 回合，收到官方 DeepSeek HTTP 200 后点击停止。两条回合均变为 `cancelled`，所属执行范围为空，原会话恢复 idle。刷新后保留各自运行环境、原回合和已验证的终态，随后可以切回 API 编辑区。浏览器总共发出两次原生回合请求和两次停止请求。 [真实入口](../server/live_llm_unified_native_stop_acceptance.py) · [隔离回执](../../tmp/live-f01-unified-stop-ahdbs9h3/live-stop-result.json)
- 当日另一轮在 Windows 和 WSL 各完成一次真实回复。切到其他会话期间回合继续，切回与刷新后均能查看各自已持久化的回复。 [双环境回复与读回](../../tmp/live-f10-native-reply-sound-dmbo__r0/native-reply-sound-result.json)
- Windows 和 WSL 的实际线程在 API 重启后可以原生续聊和分叉，保留原有上下文。API 聊天此前也在收到真实推理正文前通过停止按钮取消，刷新保留原取消记录和推理内容。 [Windows/WSL 续聊](cli-native-model-continuity.md) · [API 停止与恢复](FF-F09-chat-visibility.md) · [跨环境偏好](FF-F12-environment-preferences.md)

当日两轮共收到 4 次真实 `deepseek-flash` Responses HTTP 200，模拟调用 0。停止测试让真实上游先完成，并在向 CLI 交付响应前点击停止，用于核验本机执行停止和浏览器恢复。两次取消请求的真实远端响应及用量仍保留。 [停止回执](../../tmp/live-f01-unified-stop-ahdbs9h3/official-responses.json) · [成功回复回执](../../tmp/live-f10-native-reply-sound-dmbo__r0/official-responses.json)
