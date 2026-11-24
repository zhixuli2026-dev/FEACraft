# 模型断点、跨页显示与回合恢复

2026-11-18，FF-F38 与 FF-F42 已验收。含开发中两轮失败在内，共运行 19 轮，17 轮通过。对授权 DeepSeek 服务实际发起 98 次聊天请求，其中 92 次返回 HTTP 200，另 6 次针对不存在的模型返回预期的 HTTP 400。模拟模型调用为零。测试使用隔离算例、API 与数据库。全功能 **20/295，剩 275 项**，主线 **50/53，剩 3 项**。[逐轮模型及清理回执](../../tmp/checkpoint-lifecycle-delivery-20261118.json)

## FF-F42

用户刷新会话后可以看到已经落库的半截正文和推理过程，二者分开显示。中断、失败或超时的片段按原回合位置出现，且没有回答分支按钮。重新打开页面或加载更早的事件时，仍只显示一次。后续已完成的回答可以继续分支。[公开 API](api.md) · [浏览器投影](../web/src/panels/ChatPanel.tsx)

**写入。** 每次真实模型请求独立累计新增正文和推理的 UTF-8 字节。事件循环同时等待模型、取消、33 毫秒显示刷新及 750 毫秒断点期限；上游停顿期间也能保存最后一小段。累计至少 16 KiB 立即保存，收尾时冲刷剩余内容。无新增内容时保持已有断点。提交被取消时等待实际事务完成；若提交确认丢失，按同一事件 ID、会话、类型及清洗后的完整内容向 SQL 核实已落库，再继续原回合。[写入入口](../server/app/routers/agent.py)

**读取。** 在中断终态的所属会话和回合中，查询其时间位置之前最近的断点、助手消息或回合状态。只投影尚未被后续消息覆盖的断点，并与事件页共用字节预算。旧会话无需迁移，原始事件和导出的 JSONL 仍可核对。OpenAI 兼容 SSE 流若提前关闭且缺少两个上游终止信号，将回合标记为失败，保留实际片段。[查询](../server/app/services/conversation_history.py) · [流结束检查](../server/app/services/llm.py)

| 隔离配置 | 距上次提交触发定时保存 | 字节触发的新增量及耗时 | 终态冲刷 | 跨页恢复的正文 / 推理 |
|---|---:|---:|---:|---:|
| SQLite / 本地文件 | 782 ms | 16,388 B，141 ms | 11,897 B | 42 B / 1,898 B |
| PostgreSQL / S3 | 765 ms | 16,389 B，141 ms | 11,690 B | 40 B / 2,333 B |

上述字节测试使用实际 DeepSeek 响应，在验收客户端缓冲同一连接的原始 SSE 行后原样读取。定时测试在另一条真实连接收到正文后暂停消费，独立数据库会话核对完整文本摘要。插入 320 条系统分页记录后终止输出进程，再通过正常 API 启动恢复。每种数据库的长回复各有 3 次不同序号的真实断点；第一笔已提交但调用方看见受控的确认丢失，最终仅有 3 条。Chromium 从首页、加载历史后和刷新后分别核对顺序、正文复制、推理展开及分支操作。数值是此机器这两轮的观察值。[SQLite 回执](../../tmp/live-cli-startup-jvdibw0d/checkpoint-window.json) · [PostgreSQL/S3 回执](../../tmp/live-cli-startup-5zurk09e/checkpoint-window.json)

**终态与隔离。** DeepSeek 配置和指向同一官方端点的自定义兼容配置都走了实际网络。服务端收到未知模型的真实 HTTP 400，成功响应头之后的 1 ms 读取期限触发真实传输超时，用户取消停止另一个正在等待的真实请求。第四个场景在已经收到至少 80 字节正文后关闭真实响应，未收到完成标记，服务端没有提交完整助手回答或执行工具。重新打开页面可以看到已失败的片段，并可在同会话继续正常完成一轮。跨用户读取返回 404。既有实际文件审批资料覆盖等待审批及显式批准，跨进程启动验收覆盖存活归属和幂等恢复。[SQLite 终态](../../tmp/live-cli-startup-1he_jlc9/checkpoint-terminals.json) · [PostgreSQL/S3 终态](../../tmp/live-cli-startup-b7e9wlip/checkpoint-terminals.json) · [真实审批](../../tmp/live-cli-startup-idirhagb/agent-approval-recovery.json) · [启动恢复](agent-startup-reconciliation.md)

**兼容复验。** 12,100 条历史、约 24 MiB 载荷的 PostgreSQL 算例仍只在首页加载 280 个事件对象。浏览器故障恢复、翻页快照、祖先边界及手动恢复均通过。初次 PostgreSQL 验收在暂停前结束，后续改为按数据库已保存的真实前缀与已送达的增量对账。另一次 SQLite 浏览器复验在连续翻页复制时未观察到成功反馈；修正旧反馈定时器可能清除新提示的问题，再逐次运行真实系统剪贴板验收。两份失败记录均保留。[历史窗口](../../tmp/live-cli-startup-utxerf45/history-window.json) · [首次 PostgreSQL](../../tmp/live-cli-startup-ft6mh434/live-result.json) · [复制反馈失败](../../tmp/live-cli-startup-a7h7lcet/live-result.json)

**复现与回退。** 构建 `platform/web` 的生产前端，将输出目录和带 Playwright 的 Python 路径分别传给 `VIBEFLOW_TEST_WEB_DIST` 和 `VIBEFLOW_TEST_BROWSER_PYTHON`。在服务端运行验收入口加 `--source-profile-id 26`，使用 PostgreSQL/S3 时另加 `--postgres --s3`。设置 `VIBEFLOW_CHECKPOINT_TERMINALS=1` 可单独执行终态组合。常规已有部署可恢复上一个前后端版本，无数据库迁移；旧前端保留其原有的页内断点显示能力。此次验收使用隔离 API 与生产前端构建。[真实模型入口](../server/live_llm_checkpoint_window_acceptance.py) · [浏览器入口](../server/checkpoint_window_browser.py)

## FF-F38

旧 JSONL 允许不同回合使用相同的请求 ID。原导入器仅按请求 ID 查找最终状态，后一个回合完成后，前一个回合可能永远显示运行中。现在按 `(run_id, request_id)` 归属请求，在一次遍历中保留各个回合自己的开始时间和最新状态。导入追加的中断记录保留请求序号、原开始时间和耗时。再次导出仍可核对来源历史。此修改适用于新导入，已存会话继续走启动恢复入口。[导入实现](../server/app/services/conversation_import.py)

SQLite 和 PostgreSQL/S3 各实际创建一个会话，由 DeepSeek 选择读文件并返回实际文件标识。每个回合发起两个不同请求，序号均为 0、1；两个请求分别保存开始、完成和耗时。公开接口导出后回导，逐一核对全部 16 条事件的类型和载荷，包括工具状态的历史化转换。第二轮在收到真实增量后直接关闭 HTTP 客户端，运行和请求均最终写为 `interrupted`。另将两轮导出的真实生命周期元数据组合为请求 ID 相同的旧格式样例，两库均只中断仍在运行的那一个请求。缺请求 ID 的输入返回 400，跨用户读取三个会话均返回 404。[SQLite 回执](../../tmp/live-cli-startup-i6yig_bp/jsonl-lifecycle.json) · [PostgreSQL/S3 回执](../../tmp/live-cli-startup-tfpxrx5a/jsonl-lifecycle.json)

终态验收补充了真实错误、读取超时、取消和同会话再次完成。早期浏览器下载、回导及旧 `message/tool/request` 格式、摘要 ID 重映射的验收可按原记录核对。运行 `live_llm_jsonl_lifecycle_acceptance.py --source-profile-id 26` 复现公开 API 场景，PostgreSQL/S3 另加 `--postgres --s3`。回退应用版本无需数据迁移，旧导入器存在上述重名限制。[导出与旧格式](conversation-export-and-legacy-cache.md) · [生命周期验收入口](../server/live_llm_jsonl_lifecycle_acceptance.py)
