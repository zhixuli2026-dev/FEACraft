# FF-F53 Codex app-server JSON-RPC 与 stdio

平台以独立读取循环处理原生 Codex app-server 的 RPC 回应和流式通知。请求按 ID 匹配。会话启动、恢复、分叉和中断使用原生协议。发送审批前校验 ID 和决定，关闭时清理所持进程范围。 [RPC 客户端](../server/app/services/codex_stdio.py) · [所属传输](../server/app/services/owned_stdio.py)

- 实际 Windows Codex 启动 5 个模型回合，官方 DeepSeek Responses 返回 6 次 HTTP 200。覆盖 PowerShell 读取、重启 API 后原生分叉和续聊、运行中取消、幂等重放、新回合恢复，以及 6 份会话停止凭据。 [原生生命周期](codex-native-responses-lifecycle.md) · [实际 HTTP 与回合](../../tmp/live-codex-native-responses-nok8975c/native-result.json) · [进程退出核对](../../tmp/live-codex-native-responses-nok8975c/independent-exit-checks.json)
- 非特权 WSL 使用安装的 Linux Codex 和实际 DeepSeek 完成读取、重启 API、原生分叉和两个续聊。SQLite、私有 CLI 历史和 thread ID 保持连续。 [WSL 原生连续性](../../tmp/live-cli-inference-5mr77nm6/live-result.json)
- Windows 和非特权 WSL 的真实模型触发复杂审批与交互问答。补充权限、拒绝、密码输入脱敏、一次性决定以及取消时失效均已在隔离算例验收。 [交互与权限](FF-F30-codex-complex-approval.md) · [审批竞争与取消](FF-F29-one-time-approval.md)
- 2026-11-24 在最新构建的正常 Edge 工作台重验 Windows 与 WSL 流式通知、回合持久化、`stream.done` 终态、刷新恢复和停止范围。两种运行环境各收到一次真实 `deepseek-flash` HTTP 200，两次均报告 `cleanup_verified`。本机 Windows Codex `0.171.0-alpha.13` 也成功导出协议 Schema。 [当日复验](../../tmp/live-f10-native-reply-sound-dmbo__r0/native-reply-sound-result.json) · [中断参数 Schema](../../tmp/FF-F53-stdio-schema-20261124/v2/TurnInterruptParams.json)

JSON-RPC 帧上限、未知响应 ID 和无效审批参数已有协议负例。此轮验收使用实际 Codex 和官方模型服务，未执行历史模拟套件。本地停止凭据证明进程范围清空。远端取消回合的 HTTP 200 仍保留在实际请求总数中。 [协议及故障历史](owned-codex-transport.md)
