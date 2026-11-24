# 文件版本读取的一致性与资源预算

2026-11-18，N4-05 / RB-14。编辑器通过 `GET /cases/{id}/file:version` 取得文件长度和 SHA-256。原实现算完摘要后再次查询路径长度。在两次读取之间用 11 字节的新文件替换原来 33,554,435 字节的文件，文件存储和 S3 均返回旧摘要与新长度。旧 S3 实现还把文件完整读入内存。[文件存储复现](../../tmp/file-version-fs-h255bifo/version-result.json)、[S3 复现](../../tmp/file-version-s3-4cl8hflg/version-result.json)

现在，文件存储从同一个打开的文件计算长度与摘要，并检查读取期间的大小和修改时间。S3 对同一个 GET 响应逐块计算摘要、核对响应长度，完成或异常时关闭响应体。每块最多 1 MiB。已有文件、空文件、缺失文件沿用原 API 字段。算例复制和本地工况派生生成清单时也使用同次读取的长度与摘要。[版本接口](../server/app/routers/files.py)、[文件读取](../server/app/services/case_write.py)、[S3 读取](../server/app/services/s3_streams.py)

| 实际检查 | 结果 |
| --- | --- |
| 同一 32 MiB 加 3 字节文件，在服务算完摘要时从外部替换为 11 字节版本 | 第一次返回旧长度和旧摘要，再次查询返回新长度和新摘要。两个后端均通过。 |
| 摘要函数的 Python 峰值分配 | S3 从 33,578,829 字节降至 2,146,496 字节，约降 94%。文件存储最终为 2,110,003 字节。此项度量使用 tracemalloc，仅覆盖指定文件的摘要函数。 |
| PostgreSQL 上四个独立账户同时读取，第五个账户读取自己的文件 | 两种存储均返回相同结果。四个读取线程占用期间，新读取返回 429，健康检查可用。将隔离配置的期限设为 5 秒并让原线程继续停留，排空前新读取仍返回 429。放行后四个过期请求返回 504，新请求返回 200 和正确摘要。 |
| 公开复制与派生入口 | 新算例中读回同样的 11 字节和摘要；新工况中的 controlDict 摘要与源文件相同。另验缺失文件、中文路径和空文件。 |

[文件存储 / PostgreSQL](../../tmp/file-version-fs-i2anbyeb/version-result.json)、[S3 / PostgreSQL](../../tmp/file-version-s3-fsr3bai8/version-result.json)、[文件存储 / SQLite](../../tmp/file-version-fs-bq_ihpv8/version-result.json)、[S3 / SQLite](../../tmp/file-version-s3-gvrl95wf/version-result.json)。第一次尝试用 SQLite 同时占满四个线程时，上游写锁串行化了算例协调，未能证明线程池饱和。随后在 PostgreSQL 对独立算例执行相同负载，临时库均已删除。[SQLite 并发观察](../../tmp/file-version-fs-oyyhmncy/version-result.json)、[文件存储临时库清理](../../tmp/workflow-resume-PG-owner-n857dia9/result.json)、[S3 临时库清理](../../tmp/workflow-resume-PG-owner-87agyfvw/result.json)

审批兼容分两次使用真实 DeepSeek。第一次验证正常批准与批准前文件变更，4 次 HTTP 200。第二次验证正常批准与批准后、写事务前发生外部修改，4 次 HTTP 200。两次合计 39,474 tokens。模型产生提案并续答，内存、一致性、并发和超时由实际文件、数据库及 HTTP 请求观测。[审批前冲突](../../tmp/live-deepseek-approval-zasi70k6/live-result.json)、[批准后冲突](../../tmp/live-deepseek-approval-vkhjzi26/live-result.json)

四个名额和超时适用于单个 API 进程的版本与写前条件摘要计算。期限来自 `storage_measurement_timeout_seconds`，默认 30 秒，从进入摘要计算准入时计时。获取算例锁、其他存储操作和跨节点总额度仍由各自的资源条件跟踪。日常部署尚未更新。[专项入口](../server/file_version_capacity_acceptance.py)
