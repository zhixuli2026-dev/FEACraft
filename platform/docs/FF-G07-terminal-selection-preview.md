# Web 终端切换与选区预览

在当前算例的终端中选择 Windows cmd、WSL 或隔离沙箱，再点 **新建终端**。已有会话留在标签栏并在后台运行。点会话标签可重新挂接，读取该会话的输出并继续执行。新建类型和正在挂接的会话分别显示。[终端入口](../web/src/panels/TerminalPanel.tsx)

在已连接的终端中选择实际输出，点击 **预览选区**。Markdown 模式支持标题、表格、代码块和夹在正文中的 LaTeX。LaTeX 模式接受单个表达式，既支持裸公式也支持括号或美元符号定界。公式无法解析时保留选区原文供核对。按 Esc 或点 **关闭预览** 返回终端。预览在当前页本地生成，Markdown 链接和图片以文字保留。[预览界面](../web/src/panels/TerminalSelectionPreview.tsx) · [渲染实现](../web/src/utils/terminalSelectionPreview.ts)

预览按当前账户、算例和挂接会话绑定，也可在当前账户自己的独立终端中打开。切换会话或算例使旧选区失效。单次处理开头 32,768 字符，截断有界面提示。较大的公式和超过文档渲染预算的公式以原文保留，长内容可在缩小选区后继续查看。[会话绑定](../web/src/panels/TerminalPanel.tsx)

## 2026-11-19 验收

四组隔离环境各用真实浏览器新建 Windows cmd 和非特权 WSL，确认两个原进程同时运行。切回 cmd 后原输出能够读回，新的输入仍送到原进程。切回 WSL 后原公式输出能够读回，来源切换清空旧选区。切换算例只显示所属会话，另一账户读取两条终端回执均返回 404。每组在页面分别关闭两个真实终端，API 核验进程停止。

实际鼠标选区渲染标题、表格、强调、代码、两种行内定界和根号。代码块内的公式符号原样显示。选择无效公式模式时可读取原文。按 Esc 关闭并把焦点交还预览按钮，终端的输入帧数保持一致。裸公式的分数和根号在 390 像素宽度可读。实际网页记录中，预览产生的外部请求和模型调用均为 0。前端构建与机械检测通过。[深色桌面](../../tmp/terminal-preview-w1gek5fa/markdown-preview.png) · [深色 390 像素](../../tmp/terminal-preview-w1gek5fa/formula-narrow.png) · [浅色桌面](../../tmp/terminal-preview-69gglt05/markdown-preview.png) · [构建结果](../../tmp/terminal-preview-20261119/web-build-final.log)

| 部署 | 浏览器和主题 | 实际回执 |
| --- | --- | --- |
| SQLite 与文件系统 | Edge 深色 | [5 组检查](../../tmp/terminal-preview-w1gek5fa/terminal-preview-result.json) |
| PostgreSQL 与文件系统 | Edge 浅色 | [5 组检查](../../tmp/terminal-preview-dquk9bia/terminal-preview-result.json) |
| SQLite 与 S3 | Chromium 深色 | [5 组检查](../../tmp/terminal-preview-zb7uarzo/terminal-preview-result.json) |
| PostgreSQL 与 S3 | Chromium 浅色 | [5 组检查](../../tmp/terminal-preview-69gglt05/terminal-preview-result.json) |

最终构建另实测独立 cmd 预览。源终端的 `case_id` 为空，另一账户仍收到 404，显式关闭后实际进程停止。[Edge](../../tmp/terminal-preview-rnrpu81r/terminal-preview-result.json) · [Chromium](../../tmp/terminal-preview-6o7mhxp8/terminal-preview-result.json) · [最终构建](../../tmp/terminal-preview-20261119/web-build-verified.log)

初轮 Windows 输出与启动提示位于同一文本节点，过严的浏览器整节点匹配因此失败。最终入口等待提示符再输入，并按实际回显检查。回退可重新部署此前的前端构建，会话与存储接口沿用原格式。[首次测试记录](../../tmp/terminal-preview-1uebh56j/terminal-preview-result.json) · [复跑入口](../server/terminal_selection_preview_acceptance.py)

```powershell
platform/server/.venv/Scripts/python.exe -B -X utf8 platform/server/terminal_selection_preview_acceptance.py `
  --dist tmp/terminal-preview-20261119/web-dist-verified --browser-python C:/ProgramData/anaconda3/python.exe
```

## FF-G01 剪贴板、链接、尺寸和历史

Edge 与 Chromium 的最终构建均把实际选中的 WSL 输出复制到浏览器剪贴板。用 `Ctrl+Shift+V` 粘贴测试指令，先观察到实际 WebSocket 输入，再按回车确认原 WSL 进程的输出。两个浏览器点击实际终端中的本机链接均打开独立标签。工作台变窄后，两组均收到 PTY 已采用 176 列的回执。切回 cmd 和 WSL 可读原输出，并继续接收新的终端输入。四种部署此前已验证约 3.29 MiB 实际终端连续输出、滚动历史和备用画面。[Edge 最终回执](../../tmp/terminal-preview-rnrpu81r/terminal-preview-result.json) · [Chromium 最终回执](../../tmp/terminal-preview-6o7mhxp8/terminal-preview-result.json) · [历史与大输出验收](FF-G03-G04-terminal-input-output.md)

专项复跑时加 `--clipboard-links`。首次用 `Ctrl+V` 的尝试没有取得预期 PTY 输出，保留该记录。完成验收采用浏览器实际支持的纯文本粘贴路径，并核验输入确实进入终端。[首次剪贴板试验](../../tmp/terminal-preview-cf4n0mue/terminal-preview-result.json)
