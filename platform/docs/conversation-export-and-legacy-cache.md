# 会话 JSONL 导出与旧 Codex 缓存

2026-11-18，FF-F49「会话导出」和 FF-F47「旧 Codex 缓存保留与新 Agent JSONL 边界」通过目标机隔离验收。用户可以从 Agent 面板下载当前会话的完整可见历史，并把 JSONL 导入为一个独立会话。旧版独立聊天页的缓存继续保留。原版说明见[本地 Codex 文档](../../extracted/share/doc/vibeflow-studio/docs/codex-local.md)。

## 工作区使用行为

- 导出保留 session/event.v1、来源会话、事件 ID、顺序、时间和每轮使用的目录、权限、模式及模型。会话头包含当前会话的工具记录。分叉历史在每个祖先的分支点截断。在空父会话上创建分支后，父会话后来新增的消息只在父会话可见。
- 用户消息使用随后启动的模型回合的元数据。实际计划模式的只读工具回合和问答模式的回合在同一份下载中分别保留各自的模式。服务端根据数据库中的执行状态判断能否导出，另一个 API 进程正在执行或存在等待审批的工具时返回 409。
- 浏览器下载的正文与同一会话的 API 导出逐字节一致。回导创建新会话，重映射摘要指向的历史事件 ID，使用默认写入审批权限。历史中曾经排队或运行的工具状态保留原状态字段，导入状态为已中断；工具审批、提供商配置和可执行任务均由新会话重新建立。空会话可以导出再导入。旧版 message、tool、request 记录可读；无效 JSONL 返回 400，全库中的会话集合保持一致。
- 在隔离算例中预置旧 `.vibeflow/codex-sessions/legacy.json`。两种后端在启动、重命名、新版 JSONL 下载与回导、删除两个分支及源会话后，均读回与初始字节一致的缓存。删除带子分支的源会话会先返回 409。导出的会话没有旧缓存里的专用标识，也没有平台的模型密钥或登录令牌。

## 资源和一致性

此前导出会一次加载全部事件，再为每条用户消息向后扫描寻找回合信息，最后拼接所有 JSON 行。现在导出在同一个数据库读取快照中分批扫描事件、回合开始记录及工具。每个游标按 16 行读取，正文按块写入下载暂存。超过 1 MiB 时切换到本机临时文件，按 4 MiB 递增申请节点空间。开始发送正文前释放数据库快照。下载沿用节点的大文件传输名额和 300 秒期限。名额占用时返回 429，客户端断开后释放临时文件和预留。[历史读取与序列化](../server/app/services/conversation_history.py) · [下载暂存](../server/app/services/backup_download.py) · [节点准入](../server/app/archive_admission.py)

| 验收组合 | 下载正文大小 | 服务端导出期间 Python 跟踪分配峰值 | 磁盘预留 | 浏览器实际回导 |
|---|---:|---:|---:|---:|
| SQLite 与文件系统 | 6,045,719 字节 | 1,600,570 字节 | 8 MiB | 21 条真实会话事件 |
| PostgreSQL 与隔离 S3 | 6,050,070 字节 | 1,686,143 字节 | 8 MiB | 22 条真实会话事件 |

资源测量使用单独的长历史算例。其内容是通过公开导入接口保存的 360 条参考笔记及一条摘要。在另一事务提交新笔记时，正在生成的导出仍是 361 条，下次导出为 362 条。将隔离节点额度设为 1 MiB 后，大文件返回 409，小文件可继续下载；恢复额度后大文件正常。两个 API 进程实际竞争名额返回 429，连接断开后活动磁盘预留均为零。以上峰值来自 Python 分配跟踪；进程总内存、数据库和浏览器 Blob 占用属于其他指标。[SQLite 明细](../../tmp/live-cli-startup-07k8_7et/conversation-export.json) · [PostgreSQL 明细](../../tmp/live-cli-startup-_l221r2m/conversation-export.json)

## 实际模型与回归

两组各有四次真实 DeepSeek 请求驱动导出目标中的问答、工具读取及待审批提案，另有两次工具回读验证算例中的验收凭据。共 12 次 HTTP 200。两组都使用 Chromium 147 的生产前端，下载后核对 SHA-256，再从页面导入。SQLite 与 PostgreSQL 的事件数有一条差异，真实流式输出的 checkpoint 数随运行变化。[SQLite 模型回执](../../tmp/live-cli-startup-07k8_7et/live-result.json) · [PostgreSQL 模型回执](../../tmp/live-cli-startup-_l221r2m/live-result.json) · [SQLite 浏览器回执](../../tmp/live-cli-startup-07k8_7et/conversation-browser.json) · [PostgreSQL 浏览器回执](../../tmp/live-cli-startup-_l221r2m/conversation-browser.json)

工作区备份复用同一暂存实现。独立重跑实际 ZIP 下载与恢复，覆盖进程退出、下载断连、并发、磁盘限额和 ZIP 大小限制，活动预留归零。该组另外记录两次真实模型请求。最初的会话验收曾遇到本机 S3 启动超时、浏览器脚本选中隐藏项目、连续请求时上一次下载仍在收尾，以及测试误将已中断的导入工具状态要求为原始运行状态。正式结果使用真实下载和应用既有的恢复语义。[备份检查](../../tmp/live-cli-startup-86xj04_i/backup-download.json) · [备份模型回执](../../tmp/live-cli-startup-86xj04_i/live-result.json)

复验使用服务器虚拟环境执行 `live_llm_conversation_export_acceptance.py --source-profile-id 26`，另一次加 `--postgres --s3`。`VIBEFLOW_TEST_WEB_DIST` 指向生产前端，`VIBEFLOW_TEST_BROWSER_PYTHON` 指向安装 Playwright 的解释器。此改动复用原有数据库表和 JSONL 格式，无数据库迁移。正式发布时应更新所有 API 进程，以便每个进程都检查同一持久运行状态。导入沿用 16 MiB、6000 条历史事件和 2 MiB 单行的上限。本轮使用本机隔离部署，两个现有 Docker 项目继续使用它们已部署的版本。[复验入口](../server/live_llm_conversation_export_acceptance.py)
