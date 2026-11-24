# S3 作业输出与补传按块归档

2026-11-19，N4-05 的 RB-12 与 RB-14 完成作业输出归档这一项。正常 S3 作业收尾与显式 `recover-outputs` 共用同一份文件写入实现。大于 64 KiB 的文件先在节点磁盘额度内逐块捕获到临时文件，逐块校验本地原文件身份，并与作业原清单的大小和 SHA-256 比对。内容核对完成后，以 `If-None-Match: *` 提交原归档对象，再逐块读取并验证远端字节。小文件沿用有界缓冲。临时文件随打开句柄关闭，并在真实 I/O 返回后释放节点额度。

显式补传沿用原共享请求期限、远端写入归属、配额、线程排空证明和原作业输出清单。已归档且通过摘要验证的对象直接复用。真实写入之后进程突然退出时，原持久记录仍为待归档。重启后，补传缺失对象和最终清单，重复提交读取同一恢复回执。补传结束时仍记录待收尾状态，原生作业实际执行一次。

| 数据库与入口 | 40 MiB 本地产物的 Python 峰值分配 | 证据 |
| --- | ---: | --- |
| SQLite 正常收尾 | 3,156,466 B | [实际回执](../../tmp/attempt-output-normal-2gxdstul/output-result.json) |
| SQLite API 突停后显式补传 | 2,900,012 B | [实际回执](../../tmp/attempt-output-recovery-vx_s9lp6/output-result.json) |
| PostgreSQL 正常收尾 | 3,298,374 B | [实际回执](../../tmp/attempt-output-normal-baxd5jy3/output-result.json) · [临时数据库清理](../../tmp/output-recovery-postgres-97l_th3h/result.json) |
| PostgreSQL API 突停后显式补传 | 3,010,447 B | [实际回执](../../tmp/attempt-output-recovery-45rb1x4j/output-result.json) · [临时数据库清理](../../tmp/output-recovery-postgres-krw5bd_0/result.json) |

隔离验收使用实际 Windows 原生 Python 作业、HTTP、实际数据库和 SeaweedFS 4.52。环境位于 D 盘。作业输出按原清单 SHA-256 与实际远端字节独立核对。有相同大小的节点临时空间预留覆盖真实 PUT，之后已释放。补传组在真实业务进程暂停于已提交一次 PUT 后终止它，复验该对象的 ETag 保持。四个作业均实际只运行一次。八个隔离 API 出生身份确认退出，四个临时对象服务停止，两个临时 PostgreSQL 数据库已删除。真实模型及模拟调用均为 0。

针对两个补传组，另一 API 在实际 I/O 排空前返回 409。故障夹具通过独立的取消控制请求直接向补传协程发送三次 `Task.cancel()`，该控制请求返回 200 并确认线程排空。被取消的原补传请求在两组中均以 HTTP 500 结束，持久归档状态当时仍为待补传。随后从两个 API 正常重试均返回 200，取得同一份恢复回执。11 月 10 日相同的直接取消夹具在两个数据库组合上也产生 HTTP 500。实际 HTTP 断开走已有请求监视分支，当前证据覆盖协程直接取消。参见 [旧 SQLite 回执](../../tmp/attempt-output-recovery-ptrl56a3/output-result.json)、[旧 PostgreSQL 回执](../../tmp/attempt-output-recovery-jneec8vf/output-result.json)、[请求生命周期](../server/app/services/request_lifecycle.py)和 [补传路由](../server/app/routers/jobs.py)。

峰值来自 Python `tracemalloc`。对象服务、操作系统页缓存和进程 RSS 单独计量。此处覆盖 attempt 私有输出；成品发布、恢复入口中的其他长文件与整体 RB-12/RB-14 在原工作表继续跟踪。

复验入口为 [attempt_output_recovery_acceptance.py](../server/attempt_output_recovery_acceptance.py)。加 `--large-output-mib 40 --normal-completion` 验收正常收尾，或加 `--large-output-mib 40 --cancel-copy --two-apis` 验收突停补传，加 `--postgres` 使用临时 PostgreSQL 数据库。每组串行运行。

## 补传和缓存恢复的中断响应

同一天的后续修复在 HTTP 请求内的工作协程已经取消并收尾时返回 HTTP 503，结构化代码为 `request_interrupted`。调用方实际关闭连接时，断线分支仍会取消操作并等到底层 I/O 排空。业务状态失效继续通过既有冲突响应反馈。[请求生命周期](../server/app/services/request_lifecycle.py)

SQLite 和真实 SeaweedFS 的两个公开入口 `:recover-outputs` 与 `:restore-output-cache` 均用 40 MiB 作业产物复验。两次独立取消中每次发出三次实际取消，等到写线程退出后才回应。补传保留待归档状态，缓存恢复保留缺失状态，另一个 API 的并发操作得到 409。重试完成原归档和缓存，已上传对象的 ETag 保持，原生作业只执行一次。全部隔离 API 和对象服务已退出。[补传及缓存回执](../../tmp/attempt-output-recovery-4slv7pp3/output-result.json)

PostgreSQL 和真实 SeaweedFS 通过同样两个入口的 40 MiB 中断、线程排空和重试。两个入口均返回 503，原作业只执行一次，真实对象服务已停止，临时数据库已删除。该组串行运行。[补传及缓存回执](../../tmp/attempt-output-recovery-9pg43w92/output-result.json) · [临时数据库清理](../../tmp/output-recovery-postgres-o_6_zugw/result.json)

首轮 SQLite 复验在副 API 启动超过原夹具 20 秒等待时退出。夹具等待调整至 45 秒后通过，业务请求期限沿用原配置。[首轮记录](../../tmp/attempt-output-recovery-a0c9amqo/output-result.json)

另用真实 HTTP socket 关闭报告收尾请求，在 SQLite 和文件存储上观测真实取消。闭合连接后原 I/O 仍阻止对等 API 接管，排空后对等 API 接续，最终读回真实 Pandoc HTML。模型调用和模拟调用均为 0。[实际 HTTP 断线回执](../../tmp/early-finalizing-9m1xu_ou/early-result.json)

真实 SeaweedFS 的报告断线恢复也通过，原 Pandoc 作业同一尝试接续并发布。真实 `deepseek-flash` 调用一个读取工具核对恢复的报告源，官方服务两次 HTTP 200，模拟调用 0，隔离 API 与对象服务均已退出。[真实报告接续和模型回读](../../tmp/early-finalizing-4uwstylo/early-result.json)

补传及缓存恢复复跑参数为 `--large-output-mib 40 --cancel-copy --two-apis --restore-cache --cancel-cache`。
