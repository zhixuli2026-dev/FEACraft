# 终端输出与截图加入 Agent

在算例内打开已就绪的终端。**输出到 Agent** 追加终端选区。没有选区时，追加到光标所在行为止的最近 20 行。**截图到 Agent** 将当前可见的终端画面生成 PNG，随现有草稿一起打开 Agent。草稿中的缩略图可以移除。点击发送后，图片通过所选 API Agent 或原生 Codex 的已有图片入口提交。[终端面板](../web/src/panels/TerminalPanel.tsx) · [Agent 图片预览](../web/src/panels/ChatImages.tsx)

PNG 由浏览器把 xterm 的可见行绘制到 Canvas，覆盖视口可见范围内的字符、背景色和文字样式。大画面按等比缩放至长边最多 2,048 px、总量最多 100 万像素。每条消息共用 3 张图片和每张 4 MiB 的上限。超出时先移除已有图片再截图。[截图实现](../web/src/panels/terminalScreenshot.ts)

草稿按算例保存在当前登录账户的页面内存中，API Agent 和原生 Codex 可切换使用。切换回原算例可取回草稿，切换账户或刷新页面会清空。截图生成过程中切换会话、算例或账户时，结果作废。发送后的文件和历史由原有算例访问权限管理。[草稿状态](../web/src/store.ts) · [原生附件提交与存储](native-cli-images.md)

API Agent 收到服务端接受回执后清除已提交的草稿内容。额度冲突或提交失败时，文字与附件仍可编辑和重试。图片提交在取得算例锁前，以已有的有限时间等待账户存储结算。[发送端](../web/src/panels/ChatPanel.tsx) · [服务端提交](../server/app/routers/agent.py)

## 验收

Edge 与真实非特权 WSL PTY 在 SQLite 与文件系统上通过 9 项浏览器检查。鼠标选区和最近 20 行追加到现有草稿，API Agent 与原生面板共用草稿，图片数量上限和异步截图跨算例隔离均通过。实际 PNG 为 1,516 × 360 像素，保留 2,010 个 ANSI 背景色像素。手动发送触发一次 DeepSeek HTTP 200，消耗 4,636 tokens。上游收到的 PNG 与浏览器原件 SHA-256 一致，模型返回仅在图片里的八位数。刷新后实际读回原始字节，另一账户访问图片和终端均返回 404，原终端关闭已验明。[浏览器回执](../../tmp/terminal-agent-m5zzflj8/terminal-agent-result.json) · [上游请求回执](../../tmp/terminal-agent-m5zzflj8/upstream.jsonl) · [草稿界面](../../tmp/terminal-agent-m5zzflj8/agent-draft.png)

SQLite 与实际 SeaweedFS S3 在 Chromium 通过同样 9 项检查，验证码只在 PNG 中送到模型。真实 DeepSeek HTTP 200 一次，4,535 tokens，存储逐字节回读、跨账户 404 和实际终端停止通过。[浏览器回执](../../tmp/terminal-agent-y83xrxli/terminal-agent-result.json) · [上游请求回执](../../tmp/terminal-agent-y83xrxli/upstream.jsonl)

PostgreSQL 与文件系统在 Chromium 通过相同检查。随机码在 WSL 命令执行时传入，夹具文件内只有输出逻辑。上游一次 HTTP 200，4,565 tokens，首次模型请求的文字段不含随机码，模型未读取工具结果。依赖的临时数据库已删除。[浏览器回执](../../tmp/terminal-agent-tte4xbhe/terminal-agent-result.json) · [上游请求回执](../../tmp/terminal-agent-tte4xbhe/upstream.jsonl)

PostgreSQL 与 SeaweedFS S3 在 Edge 通过相同检查。上游一次 HTTP 200，4,583 tokens，图片原字节读回、跨账户 404、终端停止及临时依赖清理通过。此前一轮命中了存储结算冲突，服务端在上游调用前返回 409，促成上述草稿保留修复。[首次 409 的页面](../../tmp/terminal-agent-253zyedp/agent-browser-failure.png) · [最终浏览器回执](../../tmp/terminal-agent-3jbmx6y0/terminal-agent-result.json) · [上游请求回执](../../tmp/terminal-agent-3jbmx6y0/upstream.jsonl)

专项使用独立的实际 PostgreSQL 事务占住账户结算锁。服务端等待约 0.64 秒后继续。只持久化一条用户消息和一份图片，真实 DeepSeek 只调用一次，HTTP 200。返回内容与仅在图片里的验证码一致。[锁竞争回执](../../tmp/agent-attachment-lock-hfzgm_mj/attachment-lock.json) · [锁竞争入口](../server/live_llm_agent_attachment_lock_acceptance.py)

另在 PostgreSQL 与 S3 的实际 Edge 浏览器中持锁超过等待期限，第一条真实请求在 1.547 秒后返回 409。草稿文字与同一张 PNG 逐字节保留，手动重试后仅一次真实 DeepSeek HTTP 200，4,701 tokens。最终回复准确读出图片里的验证码，历史图片原样存储，另一账户读取返回 404，终端停止有回执。复跑时加 `--postgres --s3 --owner-lock-rejection`。[浏览器拒绝与重试](../../tmp/terminal-agent-n9v1pvas/terminal-agent-result.json) · [锁等待与模型回执](../../tmp/terminal-agent-n9v1pvas/upstream.jsonl)

真实 Windows Codex app-server 通过 Responses 向已配置的 `deepseek-flash` 发送一张实际 Edge 终端截图。服务收到的 5,828 字节与原 PNG 一致。HTTP 200 返回仅出现在画面中的验证码，实际耗用 7,849 tokens。验收提示词未包含验证码，回合未执行工具，模拟调用为 0。历史保持原始 SHA-256，另一账户读图返回 404。原生回合、会话与测试 API 均已退出。[原生图片回执](../../tmp/terminal-agent-20261119/native-proof-result.json) · [原始 PNG](../../tmp/terminal-agent-20261119/terminal-vision-fixture.png) · [DeepSeek 官方视觉接口](https://api-docs.deepseek.com/guides/vision/)

一次额外 Chromium 复测中，真实模型收到原 PNG 并返回 HTTP 200，但已提交的回复把随机码的一位读错。保留该失败轮，验证码精确匹配以各通过轮的持久记录为准。[错误读图复测](../../tmp/terminal-agent-wjshrcs1/terminal-agent-result.json) · [该轮真实请求回执](../../tmp/terminal-agent-wjshrcs1/upstream.jsonl)

前端构建通过。机械检测无条目。回退可重新部署前一版前端和 API，已保存图片沿用原有历史格式。[构建结果](../../tmp/terminal-agent-20261119/web-build-acceptance.log) · [机械检测](../../tmp/terminal-agent-20261119/impeccable.json)

从仓库根目录以最终前端构建执行。`--source-profile-id` 使用本地已获授权的官方 DeepSeek 配置。`--postgres` 和 `--s3` 可独立选择临时后端。`--channel` 可选择 Edge 或 Chromium。[浏览器验收入口](../server/live_llm_terminal_agent_acceptance.py)

```powershell
platform/server/.venv/Scripts/python.exe -B -X utf8 platform/server/live_llm_terminal_agent_acceptance.py `
  --dist tmp/terminal-agent-20261119/web-dist-acceptance `
  --browser-python C:/ProgramData/anaconda3/python.exe --source-profile-id 26
```
