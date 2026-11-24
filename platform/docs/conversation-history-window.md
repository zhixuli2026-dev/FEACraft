# 长会话读取、回合计时和显示恢复

Agent 面板打开会话时读取最新一页。此前请求 280 条事件仍会先把分支的全部历史加载到应用进程。另一处错误会在多条大消息提前耗尽字节预算时跳过更早的消息。现在服务端从可见分支的最新事件向前取数，浏览器使用服务端返回的实际偏移和快照 ID 继续翻页。

## 读取行为

| 入口 | 事件数量 | 事件载荷预算 |
|---|---:|---:|
| 默认打开会话 | 最多 280 条 | 4 MiB |
| 构建模型历史 | 最多 1,200 条 | 8 MiB |
| 浏览较早的归档窗口 | 同一快照最多回看最近 6,000 条 | 单次最多 16 MiB |

每一页在数据库读快照中核对用户、分支祖先和分支点。后续请求使用首页可见的最大事件 ID 限定历史。工具记录按当前页引用的 ID 查找，实时待审批工具和当前目标单独提供。完整 JSONL 导出仍可读取更早的历史。[API 契约](api.md) · [完整导出](conversation-export-and-legacy-cache.md)

超过单页预算的一个事件会保留身份字段和截断标志，文本按包含 JSON 转义的 UTF-8 大小截取，放不下的结构化字段整体省略。预算计量的是事件载荷字节。数据库驱动、解码单行、响应外壳和工具快照另有开销。总数汇总和较深的偏移可能扫描索引，数据库的执行计划取决于统计信息。FF-F17 继续跟踪供应商精确 token 计量和各模型的上下文限制。

## 数据库和浏览器实测

在隔离的算例中写入 12,100 条工程笔记，载荷合计 25,181,094 字节。SQLite 和 PostgreSQL 的首页都返回 280 条，应用进程分别构造 280 个事件对象。偏移 5,800 时各读取 200 条，偏移 6,000 时各读取零条。模型历史读取 1,200 条。归档读取 6,000 条。单条包含大对象和多字节文本的 36,700,219 字节记录，经模型历史入口处理后为 8,388,607 字节。三个各约 2.2 MB 的事件按 3、2、1 连续出现。分支未读到分支点以后追加到父会话的内容，其他账号访问该快照收到 404。[SQLite 读取记录](../../tmp/live-cli-startup-a3txu5e1/history-window.json) · [PostgreSQL 读取记录](../../tmp/live-cli-startup-ewcr9xtc/history-window.json)

长历史数据只使用笔记和用户消息。实际长会话通过 DeepSeek 完成问答，测试按回答的 run ID 核对开始时间、结束时间和毫秒耗时。生产前端在 Chromium 中先打开首页，在另一个请求追加消息后向前翻页，320 条原有用户消息逐条核对到位，刷新后显示新增消息和原回合耗时。浏览器故障测试对工作台渲染时的屏幕宽度查询注入异常。一次瞬时故障后工作台恢复，持续故障在 60 秒内重建三次后显示手动重载，手动重载后实际算例重新可见。观察到的消息 POST 数为零。[SQLite 浏览器](../../tmp/live-cli-startup-a3txu5e1/window-browser.json) · [PostgreSQL 浏览器](../../tmp/live-cli-startup-ewcr9xtc/window-browser.json) · [真实模型回执](../../tmp/live-cli-startup-a3txu5e1/live-result.json)

## 升级和回退

版本 `e42a60d73fb1` 在 `agent_events(conversation_id, seq, id)` 上增加排序索引。已通过 Alembic 管理且位于 `d31f98a254ca` 的数据库应在维护窗口执行 `alembic upgrade e42a60d73fb1`，待索引建立后再启动新版 API。回退到上一版服务时执行 `alembic downgrade d31f98a254ca`。在有历史事件的隔离 SQLite 库实际完成升级、降级和再升级，三次逐列核对原事件一致。新 PostgreSQL 验收库从迁移链建立，开发数据库保持原版本。[迁移记录](../../tmp/history-window-migration-k4cnu_f5/result.json) · [迁移源码](../server/migrations/versions/e42a60d73fb1_agent_history_index.py)

本次也重新运行了会话导出验收，包括生产前端下载和回导、大文件导出、分支边界及另一 API 进程的并发状态。[导出回归](../../tmp/live-cli-startup-slh2g4z6/conversation-export.json) · [模型回执](../../tmp/live-cli-startup-slh2g4z6/live-result.json)

## PostgreSQL 与 SeaweedFS S3

首次组合测试在创建隔离算例时达到客户端的 30 秒期限，重复尝试和等待 SeaweedFS 锁服务就绪均出现同一问题。诊断时让两次创建返回实际结果，分别用时 81.03 秒和 68.75 秒。调用栈显示 SDK 在发送小对象前等待 HTTP `100 Continue`，采样的 94 次 PUT 合计约 91 秒。大量短小的条件写入累计成了创建延迟。[首次失败](../../tmp/live-cli-startup-dmgog9py/live-result.json) · [锁服务就绪后的复验](../../tmp/live-cli-startup-fjtdxctv/live-result.json) · [隔离调用耗时](../../tmp/live-cli-startup-mtrrtbv5/s3-ops.jsonl)

平台 S3 客户端现在直接发送已在内存中、长度最多 64 KiB 的 PUT 正文，大正文和文件流沿用原握手。真实端点收到的请求仍带条件头。空正文、恰好 65,536 字节、65,537 字节和文件流均能逐字节回读，重复创建均返回 412。64 KiB 正文的单次观察为 219 毫秒，较大正文和文件流保留了握手。复验使用原来的 30 秒算例创建期限。[传输记录](../../tmp/s3-attempt-store-gx4mwxct/small-put.json) · [端到端读取记录](../../tmp/live-cli-startup-bohxke28/history-window.json) · [生产前端](../../tmp/live-cli-startup-bohxke28/window-browser.json)

## 验收结论

FF-F48 已完成。最终 SQLite/文件系统和 PostgreSQL/S3 分别有 3 次真实 DeepSeek HTTP 200，其中一次为长会话问答，两次为工具读取证据和模型回读。两次最终运行的 API 进程均已退出，隔离 PostgreSQL 库在测试后已删除。导出兼容回归另有 6 次 HTTP 200。两轮早期通过的历史测试另有 6 次，修复前的 S3 诊断有 1 次，合计 19 次 HTTP 200。前三次 S3 基础设施超时均发生在模型调用前，诊断运行在刷新后等待算例列表时超时，保留为失败证据。实际数据库批量写入、S3 条件请求和浏览器故障注入各自计量。[最终 SQLite 模型回执](../../tmp/live-cli-startup-a3txu5e1/live-result.json) · [最终 PostgreSQL/S3 模型回执](../../tmp/live-cli-startup-bohxke28/live-result.json) · [修复前诊断](../../tmp/live-cli-startup-mtrrtbv5/live-result.json) · [隔离数据库清理](../../tmp/job-repeat-postgres-3_jmxvs5/result.json)
