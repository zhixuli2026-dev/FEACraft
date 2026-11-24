# 终端快速输入与持续输出

中文标点兼容层把同一次提交的 `beforeinput` 和 `compositionend` 配成一组。相同标点快速连续输入仍保留。浏览器正常组合输入交由 xterm 处理，候选文字在提交前不会发送。普通中文、全角字母和数字走原有输入路径。快速输入支持多行，中文组合尚未结束时按 `Ctrl+Enter` 保留草稿。[输入兼容层](../web/src/utils/ime.ts) · [终端面板](../web/src/panels/TerminalPanel.tsx)

浏览器输出缓存按原始字节计量，上限 2 MiB，每次给 xterm 的输入数据最多来自 64 KiB。UTF-8 在字节切片间由同一解码器处理。达到上限时保留最新字节，并在丢弃边界结束先前的控制序列。浅色模式跨 WebSocket 帧识别 ANSI 色码，映射黑色与亮黑背景，并保留 RGB 前景和颜色索引。输出期间暂缓光标闪烁，回看和返回底部由 xterm 自身维护。应用的游标样式和备用画面控制仍由 PTY 输出驱动。[字节队列与 ANSI](../web/src/utils/terminalOutput.ts)

浏览器初测复现了 SQLite 上一次文件提交尚未结算时启动终端收到 409，以及在 S3 首次文件监听建立基线时误判旧副本。终端入口复用现有账户配额等待。本次准备范围里只有一个首次文件观察事件时，可以继续检查同一副本，真实文件写入仍触发过期拒绝。[终端入口](../server/app/routers/terminal.py)

## 2026-11-19 验收

四种部署使用最终构建、实际 Windows 宿主和 UID 65534 的 WSL PTY。Playwright 向 Chromium 或 Edge 发送浏览器组合输入事件，并在候选、提交和多行发送阶段检查实际 WebSocket。四份命令产物通过真实文件接口逐字读回，包含三个连续中文逗号、中文和 emoji、全角字母和两行独立命令。浏览器从终端关闭按钮提交停止，API 返回实际停止验证。其他账户请求该会话得到 404。

每组收到约 3.29 MiB 真实终端输出，终端末行保留中文和 emoji。实际输出继续时，键盘翻页后的历史内容保持，返回底部显示后续输出。另在两组部署验证真实滚轮回看和返回。`vi` 切入备用画面、读取前一步文件并退出后恢复历史。浅色视图核对 RGB 前景 `40, 50, 100` 以及跨帧的原黑色和亮黑背景分别显示 `241, 245, 249` 与 `226, 232, 240`。[浅色实际视图](../../tmp/live-cli-startup-u380w0kd/terminal-browser-light.png) · [浏览器入口](../server/live_llm_terminal_browser_acceptance.py)

| 部署 | 浏览器 | 实际终端证据 | 真实模型回读 |
| --- | --- | --- | --- |
| SQLite 与文件系统 | Chromium | [6 组检查](../../tmp/live-cli-startup-yr5_b341/terminal-browser-result.json) | [2 次 DeepSeek HTTP 200](../../tmp/live-cli-startup-yr5_b341/live-result.json) |
| PostgreSQL 与文件系统 | Edge | [7 组检查](../../tmp/live-cli-startup-u380w0kd/terminal-browser-result.json) | [2 次 DeepSeek HTTP 200](../../tmp/live-cli-startup-u380w0kd/live-result.json) |
| SQLite 与 S3 | Chromium | [7 组检查](../../tmp/live-cli-startup-jq70r641/terminal-browser-result.json) | [2 次 DeepSeek HTTP 200](../../tmp/live-cli-startup-jq70r641/live-result.json) |
| PostgreSQL 与 S3 | Edge | [6 组检查](../../tmp/live-cli-startup-esghhj1n/terminal-browser-result.json) | [2 次 DeepSeek HTTP 200](../../tmp/live-cli-startup-esghhj1n/live-result.json) |

PostgreSQL 与 S3 的真实准备回归另验证同算例文件变化拒绝旧副本、启动断线、过期读取回收和普通 cmd/WSL 重连。四组浏览器及一组准备回归共 10 次真实 `deepseek-flash` HTTP 200，模拟调用为 0。浏览器断言和实际模型读回分别记录。[准备回归](../../tmp/live-cli-startup-6qtq6wsn/terminal-preparation.json) · [模型回执](../../tmp/live-cli-startup-6qtq6wsn/live-result.json)

Node 检查覆盖 80 ms 配对边界、2 MiB 字节上限、64 KiB 切片、UTF-8 跨切片和 ANSI 拆帧。前端构建通过，Impeccable 检测无条目。输入法证据来自 Chromium 系列浏览器的组合事件，覆盖提交前后的应用和 PTY，Windows 中文候选窗的视觉和交互由系统提供。[输入检查](../tools/terminal-ime-smoke.mjs) · [输出检查](../tools/terminal-output-smoke.mjs) · [构建结果](../../tmp/terminal-browser-20261119/web-build-final.log) · [机械检测](../../tmp/terminal-browser-20261119/impeccable-final.json)
