# FF-F03 聊天 Markdown

聊天正文按 Markdown 的代码、链接、表格及公式标记解析。围栏代码和行内代码中的美元符号与反斜杠保持原样，正文公式由 KaTeX 排版。无法排版的公式显示原式，其后的文本继续渲染。聊天沿用 FF-E14 已验证的普通公式解析器。文件预览的 MathJax 兼容路径有自己的验收。[聊天渲染](../web/src/utils/chatMd.ts) · [公式解析](../web/src/utils/markdownMath.ts) · [FF-E14 文件预览](FF-E14-markdown-math.md)

DOMPurify 在渲染后清洗链接和 HTML。正常 HTTPS 链接和允许的静态标记可以显示。验收用惰性的自定义 URI 和标签核对清洗结果，浏览器只读取属性和可见文本。复制和会话读取仍使用模型回复的 Markdown 源文。[聊天 Markdown 组件](../web/src/panels/ChatMarkdown.tsx) · [真实验收入口](../server/live_llm_chat_markdown_acceptance.py)

正式模型回合发出一次实际 DeepSeek `deepseek-flash` 请求，HTTP 200，实际用量 3,670 tokens。Edge 实测代码字面量、表格及标题、链接消毒、两种行内和两种独立公式、无法排版公式的回退均通过。原夹具在系统剪贴板逐字比较处退出 1。独立核对确认 33 个连续 SSE 帧，完成请求及运行均已落库，官方正文摘要、浏览器接收源文和 SQLite 助手正文一致，隔离进程及端口均已关闭。[原模型回执](../../tmp/live-f03-chat-markdown-n5f5ixku/live-result.json) · [独立源文与退出核对](../../tmp/live-f03-chat-markdown-n5f5ixku/independent-live-proof.json)

正式响应的 SQLite 快照和文件复制到另一套隔离环境。使用正常登录冷启动、打开原会话和刷新，均能查看相同的 Markdown，其他账号读取得到 404。应用传给真实剪贴板的 343 字符和原文逐字一致。Windows 把 20 个 LF 转成 CRLF，系统读取的 363 字符经还原换行后与源文一致。首轮历史补验在退出后立即查询 Windows 进程身份，独立查询随后确认进程和端口已经退出。复用有界等待后，最终历史补验退出 0。模型请求累计 1 次，历史补验新增 0 次，模拟调用为 0。[最终历史回执](../../tmp/f03-retained-history-ng_svwft/retained-history-result.json) · [刷新截图](../../tmp/f03-retained-history-ng_svwft/markdown-after-reload.png) · [首轮历史退出核对](../../tmp/f03-retained-history-w06trnnl/independent-history-cleanup.json) · [最终审查清单](../../tmp/f03-chat-markdown-retained-history-review-v3-20261119.json)
