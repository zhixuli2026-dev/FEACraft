# FF-F34 请求边界与跨实例停止

2026-11-24 完成 T08。Agent 流按原始传输字节限制为 64 MiB，单 SSE 行和事件各 8 MiB，累计正文与推理 8 MiB，单工具调用 2 MiB，非流式响应 10 MiB。请求协商 `Accept-Encoding: identity`。上游错误预览最多 16 KiB，记录前清除凭据。纯字节边界检查覆盖超限和脱敏，模型调用 0 次。 [协议验收](../../tmp/FF-F34-wire-limits-20261124/protocol-result.json)

两个实际 Windows API 共享隔离数据库。持有算例写权限的客户端在对端提交指定运行的停止请求，执行方消费与租约绑定的记录，约 292 ms 结束实际 API Agent 模型流。跨用户请求返回 404，错误运行 ID 与过期目标被拒绝。真实翻译流断开后约 47 ms 关闭 provider 响应。同步索引扫描在断开后约 81 ms 停止，随后重新扫描成功。该组使用 3 次真实 DeepSeek Chat Completions HTTP 200。 [API 实测](../../tmp/live-f34-request-owner-rg9s82mn/isolation-result.json)

原生 Windows Codex 跨实例首次停止已清空执行范围，但错误记录为失败。修复停止原因传递后复验，在 516 ms 内记录 `cancelled` 并确认执行范围为空，跨用户停止返回 404。两个临时 API 均确认退出。首次及复验各收到 1 次真实官方 DeepSeek Responses HTTP 200。同步原生命令取消、超时、非特权 WSL 和恢复已做实际命令验收。 [原生复验](../../tmp/live-f34-native-owner-letbs0tu/native-peer-stop-result.json) · [命令验收](FF-F24-F25-agent-shell.md)

独立真实模型回归完成模型发现、翻译、Ask、Plan 读取文件及后续回复，实录另 4 次推理 HTTP 200 和 1 次模型发现 HTTP 200。本项合计 9 次真实推理 HTTP 200，模拟 0 次。跨实例结果来自单物理宿主上的独立 API 进程。 [真实模型回归](../../tmp/live-deepseek-8cq13jjk/live-result.json)

FF-J21 续租调整后用两个 Windows API 复测对端停止。首轮的读回断言直接在原始 SSE JSON 中搜索完整验证字串，隔离数据库已保存完整回答及完成回合事件。夹具改为按 `text.delta` 拼接正文并检查 `run.completed`。实际对端停止、其他账户拒绝、索引断开恢复、翻译断开和重新开始的真实回答均通过。复验停止耗时 852 ms，真实 DeepSeek 推理 HTTP 200 共 3 次，模拟 0 次。 [首轮记录](../../tmp/live-f34-request-owner-9m9ey04x/isolation-result.json) · [复验](../../tmp/live-f34-request-owner-i9mg5_19/isolation-result.json)
