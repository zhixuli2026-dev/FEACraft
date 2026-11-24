# 已发布 Pandoc 报告原件的分块下载

2026-11-19，N4-05 的 RB-12 与 RB-14 补齐正式 `GET /api/v1/cases/{case_id}/report/artifact`。路由先核对访问权限，在算例事务中确认当前或指定发布及其成功作业，随后释放事务和 S3 写操作准入。发布清单的所有成员在事务外按块核验。请求原件经发布摘要校验后才开始响应，保留原 GET 的 MIME 类型、附件名和历史版本选择。下载使用既有节点额度、临时文件和响应结束后的清理。报告结果页继续单独计算当前输入的新鲜度。[下载路由](../server/app/routers/reports.py) · [发布校验](../server/app/services/report_publication.py) · [响应清理](../server/app/raw_download.py)

## 原版对照

隔离 SQLite 与文件系统用真实 Windows Pandoc 先生成嵌有三张 PNG 的报告，再生成较新的小报告。两次原版对照均通过正式历史原件 GET，返回 44,268,285 B，且各自暂停在正文读取时，独立 SQLite 连接未取得 `BEGIN IMMEDIATE`。单次原件读取的 Python 跟踪分配峰值分别为 44,285,618 B 和 44,285,533 B。原件的 SHA-256 与首轮原作业捕获记录一致。原生作业均保存成功状态和已核验的空执行范围。[首轮原始回执](../../tmp/report-artifact-fs-_ki88b5v/report-artifact-streaming.json) · [第二轮原始回执](../../tmp/report-artifact-fs-3sjesni3/report-artifact-streaming.json)

两次执行器结果均为失败。第一轮在 Windows 原进程仍可被出生探针查询时结束，第二轮在循环检查本机监听端口时超时。第二轮已取得原创建句柄的退出码 1。执行器退出后的独立系统复核分别确认 API 的原 PID 与监听端口均已消失。[首轮退出复核](../../tmp/report-artifact-fs-_ki88b5v/api-exit-independent.json) · [第二轮退出复核](../../tmp/report-artifact-fs-3sjesni3/api-exit-independent.json)

## 修复复验

历史 HTML 正文只读取一次，单块最大 65,536 B。暂停历史原件核验时，独立 SQLite 写连接取得锁，同一算例通过正式文件 API 完成保存。HTTP 200 的 MIME、附件名、正文长度与旧接口一致。小报告保持默认可读，外部账号和不公开的配套文件返回 404，同尺寸损坏原件返回 409，换回经原作业摘要核对的原件后读回成功。暂存预留覆盖 44,268,285 B 完整原件，下载结束或证据校验失败后额度归零。[验收入口](../server/report_artifact_streaming_smoke.py)

| 实际存储 | HTTP 200 正文字节 | Python 跟踪分配峰值 | 回执 |
| --- | ---: | ---: | --- |
| 文件系统与 SQLite | 44,268,285 B | 8,746,497 B | [下载](../../tmp/report-artifact-fs-q6iecloy/report-artifact-streaming.json) · [原作业摘要及终态](../../tmp/report-artifact-fs-q6iecloy/sqlite-receipt-independent.json) · [API 退出](../../tmp/report-artifact-fs-q6iecloy/api-exit-independent.json) |
| 认证 SeaweedFS 与 SQLite | 44,268,285 B | 2,194,341 B | [下载](../../tmp/report-artifact-s3-s24b5suj/report-artifact-streaming.json) · [原作业摘要及终态](../../tmp/report-artifact-s3-s24b5suj/sqlite-receipt-independent.json) · [API 退出](../../tmp/report-artifact-s3-s24b5suj/api-exit-independent.json) |

原版跟踪一次整份原件读取，修复版跟踪发布核验和响应暂存。两个隔离库分别保存两项成功的 Pandoc 准备和两项成功的正式报告作业。四个作业均有已核验的空执行范围。两次 API 各取得原创建句柄的退出码，Windows 独立复核确认原 PID 和监听端口都已退出。SeaweedFS 原进程也经 WSL `/proc` 独立核验退出。精确回归 1/1 通过，耗时 386.609 秒。真实模型调用和模拟模型调用均为 0。[执行器摘要](../../tmp/vibeflow-regression-all-20261119-161910-904753/summary.json) · [SeaweedFS 实例](../../tmp/s3-attempt-store-ap6pxjq1/store.json) · [SeaweedFS 独立退出](../../tmp/s3-attempt-store-ap6pxjq1/owner-exit-independent.json)

文件、原生临时目录和验收产物位于 D 盘。固定回归前后，C 盘可用空间分别为 22.12 GiB 和 21.82 GiB，D 盘为 387.45 GiB 和 386.35 GiB。前后均确认 11 个项目 Docker 容器处于运行状态，两组既有 API 容器健康。
