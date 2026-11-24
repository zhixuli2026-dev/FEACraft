# FF-H01 作业提交、列表、增量日志、取消与 SSE

2026-11-18，修复了 SSE 在作业刚刚结束时丢掉最后一段输出的问题。原服务先读到当时的日志末尾，随后查询终态。两次读取之间如果进程追加输出并结束，客户端可能只收到前 6 字节和成功事件。实测原日志有 85,007 字节。[修复前回执](../../tmp/live-cli-startup-2ibhnpu4/job-log-stream.json)

现在，如果本页是在日志仍可写时读取，发送完成事件前会从原字节游标再读已结束的作业。每页继续检查权限。验收在真实读取后暂停一次 SSE 返回，让实际进程写完并退出，再恢复发送，逐字对照所有 SSE 正文。

| 验收项 | 结果 |
|---|---|
| 完整日志与重连 | SQLite 加文件存储、PostgreSQL 加 S3 均收到完整 85,007 字节，随后收到对应尝试的成功事件。杀死原 API 并重启，旧式 SSE 与带字节游标的 SSE 均读回完整内容或准确的剩余部分。包含中文及四字节字符。非法区间返回 422。 |
| 共享会话的权限 | 匿名请求返回 401，外部用户返回 404。只读成员获授权后收到前 6 字节，取消作业请求被拒绝。所有者撤销共享后，已打开的流结束于 permission_denied。所有者随后取消，自己的 SSE 返回已验证清理的取消终态。 |
| 提交、列表与取消 | 既有双库验收覆盖并发同键提交、非法命令、分页日志与旧状态查询，核对详情、列表和持久行。新增 V09 通过真实几何预览覆盖启动、执行、收尾三个时点，取消后运行数和预留字节归零，原发布保持。 |
| 本地与 WSL | Windows 实测独立父子进程退出。既有普通 WSL、WSL 沙箱及 Windows 启动取消验收覆盖门进程、后续作业准入，以及普通 WSL 非特权运行。既有浏览器验收核对历史作业、尝试、阶段和日志读取错误。 |

本轮两种存储各有两次真实 DeepSeek 请求，模型调用文件读取工具核对验收摘要。运行在隔离数据库、算例和 API 进程中。[验收入口](../server/live_llm_job_log_stream_acceptance.py) · [文件存储](../../tmp/live-cli-startup-u8pvufvq/job-log-stream.json) · [S3](../../tmp/live-cli-startup-qgqs9i7c/job-log-stream.json)

同项已有证据包括[作业提交与历史列表](FF-H02A-H19-completion-review.md)、[三阶段取消](process-tree-stop.md#v09-三个阶段取消几何预览)、[日志诊断与兼容](job-log-diagnostics.md)、[WSL 及 Windows 启动取消](../../tmp/job-starting-cancel-v2vmhzcc/starting-cancel-result.json)和[历史作业浏览器验收](../../tmp/legacy-query-browser-result.json)。
