# FF-F04 流式 Markdown

聊天按空行划分稳定区块。代码围栏结束时，需要使用相同标记，长度至少与开始标记相同，标记后的内容只能是空白。代码里的较短围栏和不同标记继续作为字面文本，围栏内空行不触发分块。旧解析器会把这些行当成结束围栏，导致包含嵌套 Markdown 示例的代码被拆开。[实现](../web/src/utils/chatBlocks.ts) · [修复前复现](../../tmp/ff-f04-fence-baseline-20261119.json) · [纯解析复验](../../tmp/ff-f04-fence-node-check-20261119.json)

已完成区块按位置保留节点，流式回答只显示一个光标。收到完成事件后，从该轮的完整源文渲染单份最终回答，移除流式光标。正式验收对照真实 DeepSeek 流、完成事件引用的历史消息和刷新后的输出。验收在两个真实正文片段之后暂缓后续读取，让浏览器观察仍在生成的代码和节点。[渲染组件](../web/src/panels/ChatMarkdown.tsx) · [显式验收入口](../server/live_llm_streaming_markdown_acceptance.py) · [FF-F03 正文 Markdown](FF-F03-chat-markdown.md)

Edge 分别在实际收到 51 和 86 个正文字符后检查 DOM。先前完成的区块和流式回答容器保持同一个节点，内层较短围栏、数学字面量和空行留在同一个代码块。两个阶段各有一个光标。完成后旧流式节点脱离，页面仅显示一份答案且光标清除。刷新后重新获取同一历史，渲染出的 HTML 完全一致。其他账户读取为 404。[流式截图](../../tmp/live-f04-streaming-markdown-ltld565r/markdown-stream-second.png) · [完成截图](../../tmp/live-f04-streaming-markdown-ltld565r/markdown-completed.png)

实际 DeepSeek `deepseek-flash` 调用一次，HTTP 200，3,405 tokens，模拟调用为零。官方流、15 个顺序 SSE 帧和 SQLite 助手事件 8 的源码一致。独立读回确认请求和运行均为已完成，会话归属为空闲。验收退出 0，隔离 API 已退出、端口关闭，临时密钥未落盘。[真实验收](../../tmp/live-f04-streaming-markdown-ltld565r/live-result.json) · [独立读回](../../tmp/live-f04-streaming-markdown-ltld565r/independent-durable-proof.json) · [审查清单](../../tmp/f04-streaming-markdown-live-review-20261119.json)
