# 远端 Job 日志存储准入（RB-11）

2026-11-06：隔离验收完成：每库 13 项日志场景，共 26 项；另有 14 项 S3 attempt 和 6 项 LaTeX 兼容检查。迁移每库 209 项检查、41 张表、元数据差异为零。RB-11 完成，N4-05 仍在进行。

## 写入与额度

S3 Job 日志原先通过 `_s3_put` 写入 `.vibeflow/jobs/job-{id}.log`，会经过可修改文件索引，产生日志副本和索引版本。现在 `job_log_storage.upload` 在日志 flush 后取得固定内容，按实际字节数、SHA-256、Job、attempt、用户、算例和存储位置生成持久预留。账户额度锁串行化该预留与其他上传、输出归档、成果复制；额度不足在远端 PUT 前拒绝。

新日志写入 `.vibeflow/job-logs/job-{id}.log`，使用 `If-None-Match: *` 并读取核对内容。新目录直接对应不可变对象，避免额外的可修改文件索引写入。日志读取指针 `Job.log_s3_key` 与预留在同一数据库事务提交。历史 `.vibeflow/jobs/` 日志不改写；原有读取指针继续有效，历史对象及版本仍由实际用量扫描计费。相同内容的重复写入不增加版本；已有不同内容会被拒绝。

`job_log_reservations` 记录 active/materialized/released、完整预留量、内容摘要、写入进程出生身份和令牌。空日志允许 size=0；非空 active 记录保留整个将写入大小。迁移 `bf94b60735dc` 接续 `ae83a5f924cb`；有任何预留或结算历史时拒绝有损降级。当前开发数据库未升级。

## 失败与恢复

开始远端请求后，无论超时、取消、客户端异常或进程退出，都不凭本地结束释放未知结果的额度。恢复器只读取原存储位置并核对完整字节数和摘要；缺失或损坏时保留 active。完整不可变对象可结算为 materialized，即使原进程仍存活：晚到的相同条件 PUT 不能再增加版本。恢复不上传缺失日志、不重放原生任务、不把失败 Job 改为成功。

启动扫描包括仍有 active 日志预留的终态 Job。恢复不依赖原节点的本地缓存。额度不足、错误路径和上传失败时保留本地日志及追加诊断；已经上传的快照保持不变。日志快照发生在既有 runner 收尾位置，后续 finalizer 诊断不自动追加到该对象。

## 旧路径与接口

隔离工作目录和旧 staging Job 均调用同一日志上传服务。旧 staging 的普通同步排除该 Job 的本地日志文件，避免第二次上传；日志失败后不继续同步或清理 staging。新日志目录禁止文件编辑和断点上传，旧 staging 不下载、覆盖或删除它，算例复制也排除该目录。备份保留日志内容，恢复沿用已有 Job 日志重新编号逻辑。

Job 详情、轮询、SSE 和摘要读取继续使用数据库路径。LaTeX 错误诊断接口已改为读取 `Job.log_s3_key`，保留旧路径 fallback。

## 验收范围与后续

`job_log_storage_smoke.py` 在真实、启用版本的 SeaweedFS S3 上覆盖正常/空日志、启动失败、超额、既有不同版本、错误路径、未知响应、写入者退出、完整上传后退出、损坏日志、原进程存活时丢失完整响应，以及历史读取、目录保护和旧 staging 准入。SQLite 与 PostgreSQL 分别验证。`workflow_migration_smoke.py` 验证升级、约束、历史降级保护与元数据一致性。S3 attempt 与 LaTeX 工作流提供兼容验收。

RB-12 继续处理 staging 其他文件、克隆等操作的空间峰值；RB-14 处理长 I/O、事务锁与线程等待期限；RB-15 处理无法核验的历史及预留维护。该实现不按 TTL 释放未知请求的额度，也不声称实现操作系统级磁盘配额。

阶段证据：[验收清单](../../tmp/mainline-job-log-storage-evidence/validation.json)、[临时数据库清理](../../tmp/mainline-job-log-storage-evidence/database-cleanup.json)、[源码绑定](../../tmp/mainline-job-log-storage-evidence/source-binding.json)。此前迁移链和测试夹具失败单独记录，不计入通过项。
