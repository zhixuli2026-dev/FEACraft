# 晚期远端作业日志流式存储

2026-11-19，N4-05 的 RB-12 与 RB-14。晚期日志是原生进程退出、日志刷新后发布到 `.vibeflow/job-logs/job-{id}.log` 的不可变对象。上传前按块获取本地文件长度和 SHA-256，捕获期间检查普通文件及其身份。原作业、尝试、用户、算例、后端绑定和字节数继续写入已有 SQL 预留。[晚期日志实现](../server/app/services/job_log_storage.py) · [既有额度和恢复合同](job-log-storage-admission.md)

大日志用现有节点容量预留创建可定位的私有暂存流，复制时再核对完整 SHA-256。远端通过 `If-None-Match: *` 条件创建，原件已存在时通过对象读回校验。读回和恢复按 64 KiB 检查原 SHA-256 与原长度。暂存和节点预留随写入线程排空释放。远端 PUT 前暂存失败沿原路径结算为 released，进入远端写入后的未知响应维持 active。恢复只验证同键同内容的对象，校验成功才结算额度。空日志、旧 staging 作业与历史日志对象沿用原有记录。[原暂存写入和对象选读](../server/app/services/attempt_objects.py) · [早期收尾日志](early-finalizing-log-streaming.md)

## 实际 SQLite 与认证 SeaweedFS

两个 Windows 原生 Python 作业各输出 41,943,040 B，原内容 SHA-256 为 `d8594b3a5a4773589c171cedcd0f7440522dcd20cb65fe66c8f33752f33a2d06`。正常上传真实 HTTP 200，节点预留为 41,943,040 B。相同数据重放真实 HTTP 412，保留同一 ETag 和唯一对象版本。公开 Job 日志返回正确尾部和原字节游标。核验单次请求最多 65,536 B。正常上传和重放的 Python 跟踪分配峰值分别为 8,070,488 B 和 2,419,881 B。[实际 API 记录](../../tmp/job-log-streaming-6f0z3rj8/api.log) · [写入与核验观测](../../tmp/job-log-streaming-6f0z3rj8/late-log-trace.jsonl)

第二个原生作业的远端 PUT 实际返回 200 后，夹具丢弃该响应，SQL 继续保留 41,943,040 B。独立认证客户端仅在此夹具中换入同长度错误内容，晚期日志恢复返回 `log_unverified`。客户端换回真实原件后，恢复将剩余额度结算为 0，原失败 Job 和尝试不变。两次恢复的 Python 跟踪分配峰值为 481,772 B 和 213,125 B。独立客户端的破坏和修复只用于验证恢复的判定边界。[核验夹具](../server/job_log_streaming_smoke.py)

首轮整体执行结果为 0/1。同算例第三个原生空日志已收到 PUT 200、核验 0 B 和提交 `materialized` 记录，也已持久化 `attempt.content_drained`。其完整作业仍未在 45 秒内进入终态，夹具随后发送取消。此前故障作业在同算例的写入排空记录仍为空，与后续源验证的同算例排空要求相符。该轮缺少终态等待的异常栈，无法确认唯一阻塞点。两份大日志已完成六次真实整对象校验，节点预留已排空。[首轮证据](../../tmp/job-log-streaming-6f0z3rj8/job-log-streaming.json) · [只读状态观察](../../tmp/job-log-streaming-6f0z3rj8/first-run-storage-observation.json) · [首轮执行器摘要](../../tmp/vibeflow-regression-all-20261119-142103-cf9039/summary.json) · [对象服务退出](../../tmp/job-log-streaming-6f0z3rj8/s3-process-exited.json) · [原作业排空检查](../server/app/services/job_write_checkpoint.py)

首次兼容限定执行使用独立算例，空日志与旧 staging 都到达 `succeeded`。空日志校验和旧历史文件、staging 输出的公开读取通过。夹具误把旧 staging 的完整日志写成 14 B stdout 的断言，实际日志还包含原有 24 B 历史前缀。SQL 保存的 38 B 摘要与这两个部分的拼接完全相同，后续按该前缀追加方式校验。该轮执行器仍保存为 0/1。[阶段执行器摘要](../../tmp/vibeflow-regression-all-20261119-142856-166b04/summary.json) · [真实状态与旧前缀](../../tmp/job-log-streaming-kit92fnz/staging-prefix-observation.json) · [对象服务退出](../../tmp/job-log-streaming-kit92fnz/s3-process-exited.json)

最终兼容限定复验 1/1 通过，耗时 56.750 秒。空日志正常结束并提交真实 0 B 对象。旧 staging 公开日志和新对象均为完整 38 B，原历史路径仍能读取 24 B，staging 输出可以公开读取。两条晚期上传的 Python 跟踪分配峰值为 1,625,622 B 和 1,595,974 B。节点预留已排空，Windows API 实际出生身份已退出，隔离 SeaweedFS 启动器、Linux 出生身份和原目录成员均已核验退出。[兼容回执](../../tmp/job-log-streaming-4wojctl1/job-log-streaming.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-143211-f4831c/summary.json) · [对象服务退出](../../tmp/job-log-streaming-4wojctl1/s3-process-exited.json)

本轮使用 Windows 原生 Python 3.11.7、隔离 SQLite 和 WSL `nobody` 下认证 SeaweedFS 4.52。峰值是 `tracemalloc` 对晚期上传、重放和恢复路径的跟踪分配。原生进程和整进程 RSS 属于不同观测范围。C 盘可用空间 22.192→22.183 GiB，并行阶段一度观测到 20.595 GiB。D 盘可用空间 391.445→390.911 GiB，隔离数据库、对象数据和原生临时目录均在 D。实际和模拟模型调用均为 0 次，Docker 写操作为 0 次。本轮验证边界为 SQLite 与实际 S3 晚期日志，由较早的合同记录保留 PostgreSQL 和其他故障场景的历史验收。[回归执行和测量入口](../server/job_log_streaming_smoke.py) · [既有验收范围](job-log-storage-admission.md)

## 同算例后续作业的持久终态

基线使用 16 B 真实 Windows stdout 和独立认证 SeaweedFS。第一次条件 PUT 返回 200，夹具丢弃响应。日志对账核验原件并结算 16 B 预留。原 API 仍存活时，历史排空恢复返回 `owner_live`，原失败作业的排空记录仍为空。随后的同算例空作业已写入 0 B 对象并持久化自己的排空检查点。真实异常栈经 `SourceValidation.acquire`、`case_validation.reserve_locked` 和 `require_drained_locked` 报 `validation_job_busy`，要求历史恢复。公开 Job 状态停在 `running/finalizing`，并带 `ExecutionConflict` 收尾错误。[基线回执](../../tmp/job-log-followup-ru8fp3ia/job-log-followup.json) · [基线执行器摘要](../../tmp/vibeflow-regression-all-20261119-144130-168fbb/summary.json)

原 API 实际退出后，新 API 的生产启动恢复提交原失败作业的 v2 排空记录，沿用原 Windows 停止证据和失败结果。同算例第三个空作业成功。第二个基线作业重启后仍为 `finalizing`，启动日志将其列为需要发布恢复。[重启后的只读记录](../../tmp/job-log-followup-ru8fp3ia/baseline-terminal-observation.json) · [SeaweedFS 实际退出](../../tmp/job-log-followup-ru8fp3ia/s3-process-exited.json)

现在，源校验明确要求历史 Job 排空恢复时，原收尾调用按已有的有权终态事务持久化 `failed/recovery_required`，同时保存这个作业原生进程的停止回执。历史写入的排空门槛和可恢复的并发等待规则继续沿用原实现。该失败事务不执行成果发布或重跑。[源校验异常](../server/app/services/publication_source_validation.py:119) · [终态提交入口](../server/app/services/job_execution.py:548)

修复后的同一对照 1/1 通过，耗时 64.172 秒。第二个作业最终为 `failed/recovery_required`，公开停止回执与原检查点一致，重启后仍为失败。原 API 活跃时，先前失败作业仍返回 `owner_live`。原 API 实际退出后，启动恢复保留先前的失败状态和原生停止回执，并成功补齐 v2 排空记录，随后同算例第三个空作业成功。三个原生命令各实际启动一次。本次两轮均使用 SQLite 与真实 SeaweedFS，所有隔离 API 和 SeaweedFS 均已核验退出。C 盘可用空间 22.182→22.177 GiB，D 盘 390.911→390.632 GiB，模型调用与 Docker 写操作均为 0 次。[修复后回执](../../tmp/job-log-followup-as72v9dh/job-log-followup.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-144803-510ca0/summary.json) · [对象服务退出](../../tmp/job-log-followup-as72v9dh/s3-process-exited.json)
