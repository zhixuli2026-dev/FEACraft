# FF-F05 长回答与大代码块

聊天正文只渲染末尾最多 512 KiB。代码围栏超过 32 KiB 时以纯文本渲染。渲染缓存最多 64 项，总预算 512 KiB，按最近使用顺序逐出。三个上限按 UTF-8 字节计算，截断保留完整 Unicode 字符。会话保存和 API 聊天的复制按钮继续使用原始全文。 [渲染预算](../web/src/utils/chatRender.ts) · [聊天面板](../web/src/panels/ChatPanel.tsx)

- 渲染器专项验证 600,018 字节的 Markdown、正好 512 KiB 时不截断、末尾中日韩字符与 emoji、非 ASCII 代码的 32 KiB 降级和缓存逐出。测试直接调用渲染器，不发送模型请求。 [边界检查](../tools/chat-performance-smoke.mjs)
- 实际 Edge 工作台收到 `deepseek-flash` 输出的 40,504 字节代码块，185 帧正文。围栏降级为纯文本并保留末尾标识。复制、刷新后复制、服务端持久化和真实 SSE 回答的 SHA-256 一致。外账号读取返回 404，隔离 API 已退出。 [真实验收入口](../server/live_llm_chat_performance_acceptance.py) · [通过回执](../../tmp/live-f05-chat-performance-j7uq5fuj/live-performance-result.json)

两次实际模型请求均返回 HTTP 200，模拟 0。首次浏览器显示和复制通过，外账号注册因验收选用保留域名返回 422。修正测试邮箱并将隔离凭据改为内存保存后完整通过，服务报告 8,341 tokens。600,018 字节样例属于渲染边界验收，模型回复的实际字节数另行记录。 [首次服务回执](../../tmp/live-f05-chat-performance-w23kkn3c/upstream.jsonl) · [首次浏览器回执](../../tmp/live-f05-chat-performance-w23kkn3c/browser-performance.json)
