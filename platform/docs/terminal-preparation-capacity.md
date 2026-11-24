# 终端启动、S3 工作副本与断线

终端在短事务中读取算例存储前缀、字典版本和事件游标，然后释放算例锁与账户存储额度。S3 工作副本按已有节点预算与对象版本条件下载。下载完成后复核算例、权限和来源，放行实际 shell 前再核对一次。算例事件发生变化时，返回 `input_stale`，用户可以重新打开。单个首次文件观察事件只记录已有文件的基线，允许通过复核。[终端路由](../server/app/routers/terminal.py) · [S3 下载](../server/app/services/staging_download.py)

准备预算默认 300 秒，可通过 `VIBEFLOW_TERMINAL_PREPARE_TIMEOUT_SECONDS` 调整。每 API 进程最多有两个正在准备的 S3 终端。超过名额时返回 `429 / terminal_preparation_busy`。到期时等待实际文件线程结束，清理暂存及其节点预留，再返回 `504 / terminal_preparation_timeout`。此期限覆盖授权、选择、下载和下载后的复核。PTY 已有的宿主进程登记、就绪及停止路径继续使用各自的期限。[配置样例](../server/.env.example)

WebSocket 在发送终端就绪消息前持续观察断开，同时缓存提早发送的输入与尺寸消息。上限为 256 帧或 1 MiB。超过任一限制拒绝本次打开。就绪前断开会取消启动，已分配的进程须实际停止；只删除从未放行 shell 且没有活进程的 S3 工作副本。收到就绪后，断开保留原会话，重新连接可挂接同一个 ID。普通 WSL 实际执行用户为 `nobody`，UID 为 65534。[会话启动与停止](../server/app/services/terminal_sessions.py)

## 2026-11-19 验收

在实际 S3 下载或工作副本返回处暂停线程。两种数据库均通过同算例保存、旧副本拒绝、WebSocket 主动断开后的启动取消、两份实际过期下载占用名额直到排空及过载拒绝。清理后核对暂存目录和实际节点预留为零。四种部署均使用实际 WSL 在已登记但尚未放行 shell 时断开，取得停止回执。均通过 Windows cmd 和非特权 WSL 的早到输入与尺寸设置、同会话重连和文件读回。[验收入口](../server/live_llm_terminal_preparation_acceptance.py)

| 部署 | 基础设施、故障与实际命令 | 模型读回 |
| --- | --- | --- |
| SQLite 与文件系统 | [停止、输入与重连](../../tmp/live-cli-startup-f27vs2xk/terminal-preparation.json) | [DeepSeek](../../tmp/live-cli-startup-f27vs2xk/live-result.json) |
| PostgreSQL 与文件系统 | [停止、输入与重连](../../tmp/live-cli-startup-lsm3dtoj/terminal-preparation.json) | [DeepSeek](../../tmp/live-cli-startup-lsm3dtoj/live-result.json) |
| SQLite 与 S3 | [锁外下载、取消、期限和持久化](../../tmp/live-cli-startup-j3wv4rmy/terminal-preparation.json) | [DeepSeek](../../tmp/live-cli-startup-j3wv4rmy/live-result.json) |
| PostgreSQL 与 S3 | [锁外下载、取消、期限和持久化](../../tmp/live-cli-startup-lkc4mgc7/terminal-preparation.json) | [DeepSeek](../../tmp/live-cli-startup-lkc4mgc7/live-result.json) |
| SQLite 与 S3、WSL 隔离沙箱 | [沙箱启动取消和正常退出导出](../../tmp/live-cli-startup-qwu1p75n/terminal-preparation.json) | [DeepSeek](../../tmp/live-cli-startup-qwu1p75n/live-result.json) |

五组模型读回共 10 次真实 `deepseek-flash` HTTP 200，模拟调用 0 次。命令由实际终端执行，模型读取的是验收摘要。隔离 API 与对象存储均已退出，临时 PostgreSQL 库均已删除。

沙箱实测在门登记后断开时取得完整停止回执，随后正常启动、执行命令并在退出后读回已导出的文件。该轮取消启动的分支曾留下一份 128 MiB 私有镜像，S3 工作副本与节点预留均已清理。后续已补齐同次启动的镜像回收。[沙箱验收](../../tmp/live-cli-startup-qwu1p75n/terminal-preparation.json) · [镜像回收](sandbox-unused-image.md)

首轮沙箱补充脚本把 Windows cmd 创建输出文件当成写入已结束，读到空文件而提前退出。改为等待完整预期内容后复验通过。首轮隔离库记录仍为 `active`，独立观察已确认该次实际 cmd 根和 PTY 门均退出。[首轮断言](../../tmp/live-cli-startup-2yjxuft2/live-result.json) · [独立进程观察](../../tmp/live-cli-startup-2yjxuft2/failed-cmd-observation.json)
